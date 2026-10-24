"""Native Claude Remote Control v2 transport, owned by the existing HQ process.

Wire contracts were checked against Claude Code 2.1.302. This module owns only
protocol state; fleet owns actions/receipts and SessionHistory owns lifecycle summaries.
"""

import asyncio
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import rc_mirror
from .session_history import RETENTION_SECONDS

MAX_EVENTS = 1000
MAX_MESSAGES = 500
MAX_BODY = 12 * 1024 * 1024  # Native HTTP event batches permit 10 MiB plus JSON framing.
HEARTBEAT_TIMEOUT = 90
MAX_ACTION_BYTES = 256 * 1024
MAX_QUEUE_BYTES = 1024 * 1024
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_PENDING_BYTES = 512 * 1024
COMPUTER_USE_DIALOG = "computer_use_approval"
COMPUTER_USE_FLAGS = ("clipboardRead", "clipboardWrite", "systemKeyCombos")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
MAX_TOOL_TEXT = 8000
MAX_STREAMING = 64 * 1024
COMMAND = re.compile(r"<command-name>/?([^<]+)</command-name>.*?<command-args>(.*?)</command-args>", re.S)
MODEL_NAME = re.compile(r"[A-Za-z0-9._:\[\]/-]{1,128}")
REVISION = re.compile(r"[0-9]{1,18}")
MODEL_COMMAND = re.compile(r"\s*/model\b")
RATE_WINDOWS = ("five_hour", "seven_day", "seven_day_overage_included")
UPSTREAM_INJECTABLE = ("user", "control_request", "control_response", "control_cancel_request")


def _size(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _clip(text, limit=MAX_TOOL_TEXT):
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters)"


def _block_text(content):
    """Text of a message or tool_result content: a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b["text"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    )


def _frame(event):
    return (
        f"event: client_event\nid: {event['sequence_num']}\n"
        f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
    )


class NativeRemote:
    def __init__(self, state_dir, token):
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("CC_TMUX_NATIVE_TOKEN must contain at least 32 characters")
        self._token = token
        self._lock = threading.RLock()
        self._streams = {}
        self._computer_use_dialogs = False
        self._path = Path(state_dir) / "native-remote.json"
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._sessions = {}
        self.upstream = None  # rc_mirror.Mirror when this hub mirrors to Anthropic
        if self._path.exists():
            data = json.loads(self._path.read_text())
            if data.get("version") != 1 or not isinstance(data.get("sessions"), dict):
                raise ValueError("Invalid native Remote Control state")
            self._sessions = data["sessions"]
        # No mirror link survives a restart; one starts again when the CLI next calls.
        for session in self._sessions.values():
            upstream = session.get("upstream")
            if upstream and upstream.get("status") not in (None, "ended"):
                upstream["status"] = "disconnected"

    def enable_computer_use_dialogs(self):
        """Call only from a host whose UI can render and answer this consent dialog."""
        with self._lock:
            self._computer_use_dialogs = True
            for session in self._sessions.values():
                if self._row(session, detail=False)["alive"]:
                    self._declare_dialogs(session)

    def _declare_dialogs(self, session):
        if (
            not self._computer_use_dialogs
            or session.get("dialog_declaration_epoch") == session["epoch"]
        ):
            return
        request_id = str(uuid4())
        self._queue(
            session,
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {
                    "subtype": "initialize",
                    "supportedDialogKinds": [COMPUTER_USE_DIALOG],
                },
            },
        )
        session["dialog_declaration_epoch"] = session["epoch"]
        session["dialog_declaration_request_id"] = request_id
        self._save()

    def _mirror(self, session, kind=None, body=None):
        """Hand work to the upstream mirror; it only enqueues, so this is safe under the lock."""
        if (
            self.upstream is None
            or session.get("private")
            or session["archived"] and kind != "archive"
            or session.get("upstream", {}).get("status") == "ended"
        ):
            return
        self.upstream.post(session["id"], kind, body)

    def upstream_snapshot(self, sid):
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            upstream = session.get("upstream", {})
            config = session["config"]
            return dict(
                id=upstream.get("id"),
                cursor=upstream.get("cursor", 0),
                title=session["title"],
                config={key: config[key] for key in ("cwd", "model") if key in config},
            )

    def upstream_state(self, sid):
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            return dict(
                worker_status=session["worker_status"],
                external_metadata=copy.deepcopy(session["external_metadata"]),
            )

    def upstream_activity(self, sid):
        with self._lock:
            session = self._sessions.get(sid)
            return None if session is None else session["last_seen"]

    def upstream_update(self, sid, **fields):
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return
            upstream = session.setdefault("upstream", {})
            if any(upstream.get(key) != value for key, value in fields.items()):
                upstream.update(fields)
                self._save()

    def upstream_inject(self, sid, upstream_event_id, payload, sequence):
        """Queue a claude.ai/app event for the CLI exactly as if HQ's UI had sent it."""
        from .fleet import FleetError

        with self._lock:
            session = self._sessions.get(sid)
            if session is None or session["archived"]:
                return
            upstream = session.setdefault("upstream", {})
            if (
                isinstance(payload, dict)
                and payload.get("type") in UPSTREAM_INJECTABLE
                and self._valid_payload(payload)
            ):
                try:
                    event = self._queue(session, payload)
                except FleetError as exc:
                    if exc.status == 503:
                        raise rc_mirror.Busy() from exc
                    print(f"rc-mirror {sid}: skipped upstream event: {exc}", flush=True)
                else:
                    if isinstance(upstream_event_id, str):
                        upstream.setdefault("delivery", {})[event["event_id"]] = upstream_event_id
                    if payload["type"] == "user":
                        self._message(session, payload)
                    elif payload["type"] == "control_response":
                        self._pending(session).pop(payload["response"]["request_id"], None)
                    session["input_revision"] = session["revision"]
            upstream["cursor"] = sequence
            self._save()

    def _save(self):
        temporary = self._path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump({"version": 1, "sessions": self._sessions}, out)
        os.replace(temporary, self._path)

    def _get(self, sid):
        if sid not in self._sessions:
            raise HTTPException(404, "Unknown native session")
        return self._sessions[sid]

    def _launch_auth(self, request):
        provided = request.headers.get("x-hq-native-key", "")
        if not hmac.compare_digest(provided.encode(), self._token.encode()):
            raise HTTPException(401, "Native launch authentication required")

    def _worker_auth(self, request, session):
        auth = request.headers.get("authorization", "")
        expected = session.get("worker_hash", "")
        if (
            not auth.startswith("Bearer ")
            or not expected
            or not hmac.compare_digest(_digest(auth[7:]), expected)
        ):
            raise HTTPException(401, "Invalid native worker token")
        if time.time() >= session.get("token_expires_at", 0):
            raise HTTPException(401, "Native worker token expired")
        if session["archived"]:
            raise HTTPException(410, "Session is archived")

    @staticmethod
    def _epoch(body, session):
        if type(body.get("worker_epoch")) is not int or body["worker_epoch"] != session["epoch"]:
            raise HTTPException(409, {"type": "epoch_conflict", "actual_epoch": session["epoch"]})

    async def _body(self, request):
        chunks = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BODY:
                raise HTTPException(413, "Native request too large")
            chunks.append(chunk)
        try:
            body = json.loads(b"".join(chunks) or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "Expected JSON object") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected JSON object")
        return body

    def _touch(self, session):
        session["last_seen"] = time.time()

    def _pending(self, session):
        pending = session.setdefault("pending_requests", {})
        legacy = session.pop("pending", None)
        if legacy:
            pending.setdefault(legacy["request_id"], legacy)
        return pending

    def _row(self, session, *, detail=True):
        sid = session["id"]
        alive = (
            not session["archived"]
            and self._streams.get(sid, {}).get("epoch") == session["epoch"]
            and time.time() - session["last_seen"] < HEARTBEAT_TIMEOUT
        )
        lifecycle = "ended" if session["archived"] else "alive" if alive else "unreachable"
        pending = (
            copy.deepcopy(next(iter(self._pending(session).values()), None)) if alive else None
        )
        status = "waiting" if pending else session["worker_status"]
        status = {"running": "working", "requires_action": "waiting"}.get(status, status)
        meta = session.get("meta", {})
        worker = session.get("external_metadata", {})
        usage = worker.get("context_usage") if isinstance(worker.get("context_usage"), dict) else {}
        return dict(
            model=meta.get("model") or worker.get("model"),
            permission_mode=meta.get("permission_mode") or worker.get("permission_mode"),
            effort=worker.get("effort_level"),
            task_summary=worker.get("task_summary"),
            context_tokens=usage.get("used_tokens") or meta.get("context_tokens"),
            context_window=usage.get("max_tokens"),
            rate_limits=copy.deepcopy(meta.get("rate_limits")),
            control_error=meta.get("control_error"),
            private=bool(session.get("private")),
            upstream={
                "session_url": session["upstream"].get("session_url"),
                "status": session["upstream"].get("status"),
            }
            if session.get("upstream")
            else None,
            **(
                {
                    "slash_commands": list(meta.get("slash_commands", [])),
                    "streaming": session.get("streaming") if alive else None,
                }
                if detail
                else {}
            ),
            id=sid,
            session=sid,
            label=session["title"],
            title=session["title"],
            workdir=session["config"].get("cwd", ""),
            transport="native",
            claude_session_id=session.get("claude_session_id", sid),
            **({"messages": copy.deepcopy(session["messages"])} if detail else {}),
            status=status,
            revision=str(session["revision"]) if alive else None,
            native_control=pending,
            has_question=bool(pending and pending["tool_name"] == "AskUserQuestion"),
            attention_kind=(
                "question" if pending["tool_name"] == "AskUserQuestion" else "permission"
            )
            if pending
            else None,
            notification_eligible=bool(pending) and alive,
            event_id=pending["request_id"] if pending else None,
            menu_signature=_digest(json.dumps(pending, sort_keys=True)) if pending else None,
            lifecycle=lifecycle,
            alive=alive,
            online=alive,
            updated_at=session["last_seen"],
            capabilities={
                "send": alive,
                "interrupt": alive,
                "respond": alive and pending is not None,
                "set_model": alive,
                "set_permission_mode": alive,
                "terminal": False,
            },
            menu=None,
            tail="",
            control=None,
        )

    def _prune(self):
        expired = [
            sid
            for sid, session in self._sessions.items()
            if session["archived"]
            and time.time() - session.get("archived_at", session["last_seen"]) > RETENTION_SECONDS
        ]
        for sid in expired:
            self._sessions.pop(sid)
            self._streams.pop(sid, None)
        if expired:
            self._save()

    def rows(self):
        with self._lock:
            self._prune()
            return [self._row(s, detail=False) for s in self._sessions.values()]

    def detail(self, raw_id):
        from .fleet import FleetError

        with self._lock:
            if raw_id not in self._sessions:
                raise FleetError("Unknown native session", 404)
            return self._row(self._sessions[raw_id])

    def _queue(self, session, payload):
        from .fleet import FleetError

        if _size(payload) > MAX_ACTION_BYTES:
            raise FleetError("Native action exceeds 256 KiB", 413)
        outstanding = [
            e
            for e in session["inbound"]
            if e["sequence_num"] > session.get("delivered_sequence", 0)
        ]
        if len(outstanding) >= MAX_EVENTS or _size(outstanding) + _size(payload) > MAX_QUEUE_BYTES:
            raise FleetError("Native command queue is full; wait for delivery", 503)
        session["inbound"] = outstanding
        known_ids = {e["event_id"] for e in outstanding}
        session["acknowledged"] = {
            k: v for k, v in session.get("acknowledged", {}).items() if k in known_ids
        }
        if "delivery" in session.get("upstream", {}):
            delivery = session["upstream"]["delivery"]
            session["upstream"]["delivery"] = {k: v for k, v in delivery.items() if k in known_ids}
        session["sequence"] += 1
        event = dict(
            sequence_num=session["sequence"],
            event_id=str(uuid4()),
            event_type="client_event",
            payload=payload,
            source="client",
        )
        session["inbound"].append(event)
        session["inbound"] = session["inbound"][-MAX_EVENTS:]
        session["revision"] += 1
        return event

    def act(self, raw_id, action, revision, request_id, text=None, response=None):
        from .fleet import FleetError

        with self._lock:
            row = self.detail(raw_id)
            if row["lifecycle"] != "alive":
                raise FleetError("Native session is not connected", 409)
            session = self._sessions[raw_id]
            # Every streamed token bumps the revision, so commands are only stale
            # when they predate the last human input or bridge reconnect. Answers
            # still need the exact screen they were given for.
            floor = max(session.get("input_revision", 0), session.get("bridge_revision", 0))
            stale = (
                revision != row["revision"]
                if action == "respond"
                else not REVISION.fullmatch(str(revision)) or int(revision) < floor
            )
            if stale:
                raise FleetError("Native session changed; refresh before acting", 409)
            if action == "send":
                if not isinstance(text, str) or not text.strip() or len(text) > 100000:
                    raise FleetError("Message must contain 1–100000 characters", 400)
                if self._pending(session):
                    raise FleetError("Answer the pending native request first", 409)
                if MODEL_COMMAND.match(text):
                    raise FleetError("Switch models with the model picker; /model also changes the saved default", 400)
                payload = dict(
                    type="user",
                    client_platform="web_claude_ai",
                    server_received_wall_ms=int(time.time() * 1000),
                    uuid=str(uuid4()),
                    session_id=raw_id,
                    message={"role": "user", "content": [{"type": "text", "text": text}]},
                )
            elif action == "interrupt":
                payload = dict(
                    type="control_request", request_id=request_id, request={"subtype": "interrupt"}
                )
            elif action == "set_model":
                # A control request changes this session only; a typed /model
                # would also overwrite the user's saved default model.
                if not isinstance(text, str) or not MODEL_NAME.fullmatch(text):
                    raise FleetError("Invalid model name", 400)
                payload = dict(
                    type="control_request",
                    request_id=request_id,
                    request={"subtype": "set_model", "model": text},
                )
            elif action == "set_permission_mode":
                if text not in PERMISSION_MODES:
                    raise FleetError("Unknown permission mode", 400)
                payload = dict(
                    type="control_request",
                    request_id=request_id,
                    request={"subtype": "set_permission_mode", "mode": text},
                )
            elif action == "respond":
                pending = next(iter(self._pending(session).values()), None)
                if (
                    not isinstance(response, dict)
                    or not pending
                    or response.get("request_id") != pending["request_id"]
                ):
                    raise FleetError("Native permission request is stale", 409)
                behavior = response.get("behavior")
                if pending.get("subtype") == "request_user_dialog":
                    answer = self._dialog_answer(pending, response)
                elif behavior == "allow":
                    updated = response.get("updated_input", pending.get("input", {}))
                    if not isinstance(updated, dict):
                        raise FleetError("updatedInput must be an object", 400)
                    # Same shape claude.ai sends (captured from 2.1.304).
                    answer = {"behavior": "allow", "updatedInput": updated, "tool_name": pending["tool_name"]}
                    if pending.get("tool_use_id"):
                        answer["toolUseID"] = pending["tool_use_id"]
                    # "Always allow" applies the rules Claude itself proposed, nothing broader.
                    if response.get("always") is True and pending.get("suggestions"):
                        answer["updatedPermissions"] = pending["suggestions"]
                elif behavior == "deny":
                    answer = {"behavior": "deny", "message": str(response.get("message", "Denied"))}
                else:
                    raise FleetError("Native response behavior must be allow or deny", 400)
                payload = dict(
                    type="control_response",
                    response={
                        "request_id": pending["request_id"],
                        "subtype": "success",
                        "response": answer,
                    },
                )
            else:
                raise FleetError("Unsupported native action", 400)
            event = self._queue(session, payload)
            if action == "send":
                self._message(session, payload)
                # claude.ai shows terminal-typed messages because the CLI posts them as
                # worker events; HQ-typed ones never pass through the CLI's outbound path.
                worker_payload = {
                    k: v
                    for k, v in payload.items()
                    if k not in ("client_platform", "server_received_wall_ms", "session_id")
                }
                self._mirror(session, "events", {"events": [{"payload": worker_payload}]})
            elif action == "respond":
                self._pending(session).pop(pending["request_id"])
                # What the CLI posts after a terminal-side answer, so claude.ai and the
                # app drop their copy of the prompt.
                cancel = {"type": "control_cancel_request", "request_id": pending["request_id"]}
                self._mirror(session, "events", {"events": [{"payload": cancel}]})
            elif action in ("set_model", "set_permission_mode"):
                # Shown only once Claude confirms; see _ingest.
                key = "model" if action == "set_model" else "permission_mode"
                controls = session.setdefault("controls", {})
                controls[request_id] = [key, text]
                session["controls"] = dict(list(controls.items())[-64:])
            session["input_revision"] = session["revision"]
            self._save()
            return dict(
                ok=True,
                id=raw_id,
                request_id=request_id,
                delivery="queued",
                sequence_num=event["sequence_num"],
                revision=str(session["revision"]),
            )

    @staticmethod
    def _dialog_answer(pending, response):
        from .fleet import FleetError

        if pending.get("dialog_kind") != COMPUTER_USE_DIALOG:
            raise FleetError("Unsupported native dialog", 400)
        if response.get("behavior") == "cancelled":
            return {"behavior": "cancelled"}
        if response.get("behavior") != "completed":
            raise FleetError("Native dialog behavior must be completed or cancelled", 400)
        request = pending["input"]
        selected = response.get("granted_app_ids", [])
        flags = response.get("flags", {})
        if (
            not isinstance(selected, list)
            or any(not isinstance(value, str) for value in selected)
            or len(set(selected)) != len(selected)
            or not isinstance(flags, dict)
            or any(
                key not in COMPUTER_USE_FLAGS or type(value) is not bool
                for key, value in flags.items()
            )
        ):
            raise FleetError("Invalid computer-use selections", 400)
        apps = request.get("apps", [])
        eligible = {
            app["resolved"]["bundleId"]: app
            for app in apps
            if app.get("resolved") and not app.get("alreadyGranted")
        }
        if set(selected) - eligible.keys() or any(
            value and request.get("requestedFlags", {}).get(key) is not True
            for key, value in flags.items()
        ):
            raise FleetError("Cannot grant an unrequested app or permission", 400)
        if request.get("tccState") is not None and (selected or any(flags.values())):
            raise FleetError("macOS permissions require approval on the host", 400)
        granted = []
        for bundle_id in selected:
            app = eligible[bundle_id]
            grant = dict(
                bundleId=bundle_id,
                displayName=app["resolved"]["displayName"],
                grantedAt=int(time.time() * 1000),
            )
            if app.get("proposedTier") is not None:
                grant["tier"] = app["proposedTier"]
            granted.append(grant)
        denied = []
        for app in apps:
            resolved = app.get("resolved")
            if not resolved:
                denied.append({"bundleId": app["requestedName"], "reason": "not_installed"})
            elif not app.get("alreadyGranted") and resolved["bundleId"] not in selected:
                denied.append({"bundleId": resolved["bundleId"], "reason": "user_denied"})
        return {
            "behavior": "completed",
            "result": {
                "granted": granted,
                "denied": denied,
                "flags": {key: flags.get(key, False) for key in COMPUTER_USE_FLAGS},
            },
        }

    def _message(self, session, payload):
        role = payload.get("type")
        if role not in ("user", "assistant"):
            return
        if payload.get("isMeta") or payload.get("isSynthetic") or payload.get("parent_tool_use_id"):
            return
        origin = payload.get("origin")
        if role == "user" and (
            payload.get("promptSource") == "system"
            or payload.get("queueTranscriptOnly") is True
            or isinstance(origin, dict)
            and origin.get("kind") == "task-notification"
        ):
            return
        message = payload.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, (str, list)):
            return
        uid = payload.get("uuid") or message.get("id") or str(uuid4())
        entries = []
        if role == "user":
            # Tool output belongs to the tool call that produced it, not to a new message.
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._tool_result(session, block)
            text = _block_text(content)
            command = COMMAND.search(text)
            if command:
                entries.append(
                    dict(role="command", text=f"/{command[1].strip()} {command[2].strip()}".rstrip())
                )
            elif text.startswith("[Request interrupted by user"):
                entries.append(dict(role="system", text=text.strip("[]")))
            elif text:
                entries.append(dict(role="user", text=text))
        elif payload.get("local_command_source") or message.get("model") == "<synthetic>":
            entries.append(dict(role="system", text=_block_text(content)))
        else:
            for block in content if isinstance(content, list) else []:
                kind = block.get("type") if isinstance(block, dict) else None
                if kind == "text" and isinstance(block.get("text"), str):
                    entries.append(dict(role="assistant", text=block["text"]))
                elif kind == "thinking" and isinstance(block.get("thinking"), str):
                    entries.append(dict(role="thinking", text=block["thinking"]))
                elif kind == "tool_use" and isinstance(block.get("id"), str):
                    entries.append(
                        dict(
                            role="tool",
                            id=block["id"],
                            name=str(block.get("name", "Tool")),
                            input=_clip(json.dumps(block.get("input", {}), ensure_ascii=False)),
                            text="",
                        )
                    )
            if isinstance(content, str):
                entries.append(dict(role="assistant", text=content))
            # A thinking record can land after the next text block's first delta.
            if any(e["role"] == "assistant" for e in entries):
                session["streaming"] = None
        known = {m.get("id") for m in session["messages"]}
        for index, entry in enumerate(entries):
            entry.setdefault("id", uid if index == 0 else f"{uid}:{index}")
            if entry["id"] in known or (entry["role"] != "tool" and not entry["text"]):
                continue
            session["messages"].append(entry)
        session["messages"] = session["messages"][-MAX_MESSAGES:]
        while session["messages"] and _size(session["messages"]) > MAX_MESSAGE_BYTES:
            session["messages"].pop(0)

    @staticmethod
    def _tool_result(session, block):
        call = next(
            (
                m
                for m in reversed(session["messages"])
                if m.get("role") == "tool" and m.get("id") == block.get("tool_use_id")
            ),
            None,
        )
        if call is not None:
            call["text"] = _clip(_block_text(block.get("content")))
            call["is_error"] = block.get("is_error") is True
            call["done"] = True

    @staticmethod
    def _stream(session, payload):
        """Live text of the reply being generated; the final assistant record replaces it."""
        event = payload.get("event")
        if not isinstance(event, dict) or payload.get("parent_tool_use_id"):
            return
        kind = event.get("type")
        if kind == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            tokens = sum(
                usage.get(key) or 0
                for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            if tokens:
                session.setdefault("meta", {})["context_tokens"] = tokens
            session["streaming"] = None
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                session["streaming"] = ((session.get("streaming") or "") + delta["text"])[
                    -MAX_STREAMING:
                ]

    def _metadata(self, session, payload):
        meta = session.setdefault("meta", {})
        text = lambda value, limit=128: value[:limit] if isinstance(value, str) else None
        number = lambda value: value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        kind = payload.get("type")
        if kind == "system" and payload.get("subtype") in ("init", "status"):
            for key, field in (("model", "model"), ("permission_mode", "permissionMode")):
                if text(payload.get(field)):
                    meta[key] = text(payload[field])
            commands = payload.get("slash_commands")
            if isinstance(commands, list):
                meta["slash_commands"] = [c[:128] for c in commands if isinstance(c, str)][:500]
        elif kind == "rate_limit_event":
            windows = (payload.get("rate_limit_info") or {}).get("unifiedWindows")
            if isinstance(windows, dict):
                meta["rate_limits"] = {
                    name: {
                        "utilization": number(windows[name].get("utilization")),
                        "resets_at": number(windows[name].get("resetsAt")),
                    }
                    for name in RATE_WINDOWS
                    if isinstance(windows.get(name), dict)
                }
        elif kind == "control_response":
            response = payload["response"]
            control = session.get("controls", {}).pop(response["request_id"], None)
            reply = response.get("response") if isinstance(response.get("response"), dict) else {}
            if control and response.get("subtype") == "success":
                meta[control[0]] = control[1]
                meta.pop("control_error", None)
            elif control:
                meta["control_error"] = text(str(response.get("error") or "Claude rejected the change"), 500)
            for key, field in (("model", "current_model"), ("permission_mode", "current_permission_mode")):
                if text(reply.get(field)):
                    meta[key] = text(reply[field])

    @staticmethod
    def _valid_payload(payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            return False
        kind = payload["type"]
        if kind in ("user", "assistant") and not isinstance(payload.get("message"), dict):
            return False
        if kind == "control_request":
            if not isinstance(payload.get("request"), dict) or not isinstance(
                payload.get("request_id"), str
            ):
                return False
            request = payload["request"]
            if request.get("subtype") == "can_use_tool" and (
                not isinstance(request.get("input", {}), dict)
                or not isinstance(request.get("tool_name"), str)
            ):
                return False
            if request.get("subtype") == "request_user_dialog":
                if not isinstance(request.get("dialog_kind"), str) or not isinstance(
                    request.get("payload"), dict
                ):
                    return False
                if request[
                    "dialog_kind"
                ] == COMPUTER_USE_DIALOG and not NativeRemote._valid_computer_use(
                    request["payload"]
                ):
                    return False
        if kind == "control_response":
            response = payload.get("response")
            if not isinstance(response, dict) or not isinstance(response.get("request_id"), str):
                return False
            for key in ("pending_permission_requests", "pending_user_dialog_requests"):
                if key not in response:
                    continue
                requests = response[key]
                if (
                    not isinstance(requests, list)
                    or len(requests) > 64
                    or any(
                        not isinstance(item, dict)
                        or item.get("type") != "control_request"
                        or not NativeRemote._valid_payload(item)
                        for item in requests
                    )
                ):
                    return False
        if kind == "control_cancel_request" and not isinstance(payload.get("request_id"), str):
            return False
        return True

    @staticmethod
    def _valid_computer_use(value):
        apps, flags = value.get("apps"), value.get("requestedFlags")
        if not isinstance(apps, list) or not isinstance(flags, dict):
            return False
        if any(type(flag) is not bool for flag in flags.values()):
            return False
        tcc = value.get("tccState")
        if tcc is not None and (
            not isinstance(tcc, dict)
            or any(type(tcc.get(key)) is not bool for key in ("accessibility", "screenRecording"))
        ):
            return False
        seen = set()
        for app in apps:
            if not isinstance(app, dict) or not isinstance(app.get("requestedName"), str):
                return False
            if "alreadyGranted" in app and type(app["alreadyGranted"]) is not bool:
                return False
            if app.get("proposedTier") not in (None, "read", "click", "full"):
                return False
            resolved = app.get("resolved")
            if resolved is not None:
                if not isinstance(resolved, dict) or any(
                    not isinstance(resolved.get(key), str) or not resolved[key]
                    for key in ("bundleId", "displayName")
                ):
                    return False
                if resolved["bundleId"] in seen:
                    return False
                seen.add(resolved["bundleId"])
        return True

    @staticmethod
    def _supported_request(request):
        return request.get("subtype") == "can_use_tool" or (
            request.get("subtype") == "request_user_dialog"
            and request.get("dialog_kind") == COMPUTER_USE_DIALOG
        )

    def _incoming_requests(self, session, payload):
        requests = [payload] if payload.get("type") == "control_request" else []
        if payload.get("type") == "control_response":
            response = payload["response"]
            if response["request_id"] == session.get("dialog_declaration_request_id"):
                requests = response.get("pending_permission_requests", []) + response.get(
                    "pending_user_dialog_requests", []
                )
        return [item for item in requests if self._supported_request(item["request"])]

    @staticmethod
    def _event_identity(item):
        payload = item["payload"]
        kind = payload.get("type")
        if kind in ("control_request", "control_cancel_request"):
            return f"{kind}:{payload['request_id']}"
        if kind == "control_response":
            return f"{kind}:{payload['response']['request_id']}"
        value = item.get("event_id") or payload.get("uuid")
        return value if isinstance(value, str) else None

    def _ingest(self, session, payload):
        if not isinstance(payload, dict):
            return
        self._message(session, payload)
        self._metadata(session, payload)
        kind = payload.get("type")
        if kind == "stream_event":
            self._stream(session, payload)
        for incoming in self._incoming_requests(session, payload):
            request = incoming["request"]
            self._pending(session)[incoming["request_id"]] = dict(
                request_id=incoming["request_id"],
                tool_name=request.get("tool_name", "Computer use"),
                tool_use_id=request.get("tool_use_id") if isinstance(request.get("tool_use_id"), str) else None,
                suggestions=[
                    item for item in request.get("permission_suggestions") or [] if isinstance(item, dict)
                ][:8],
                input=request.get("payload", {})
                if request.get("subtype") == "request_user_dialog"
                else request.get("input", {}),
                subtype=request["subtype"],
                **(
                    {"dialog_kind": request["dialog_kind"]}
                    if request.get("subtype") == "request_user_dialog"
                    else {}
                ),
            )
        if kind == "control_response":
            self._pending(session).pop(payload["response"]["request_id"], None)
        elif kind == "control_cancel_request":
            self._pending(session).pop(payload.get("request_id"), None)
        elif kind == "system" and payload.get("subtype") == "init":
            session["claude_session_id"] = payload.get("session_id", session["id"])
        elif kind == "result":
            session["worker_status"] = "idle"
            session["streaming"] = None
        session["revision"] += 1

    def replay(self, sid, cursor):
        """Snapshot only this session's replay lane; retained gaps fail explicitly."""
        with self._lock:
            session = self._get(sid)
            events = session["inbound"]
            cursor = max(cursor, session.get("delivered_sequence", 0))
            acknowledged = session.get("acknowledged", {})
            if events and cursor < events[0]["sequence_num"] - 1:
                raise HTTPException(409, "Native event cursor expired; session restart required")
            return copy.deepcopy(
                [
                    e
                    for e in events
                    if e["sequence_num"] > cursor and acknowledged.get(e["event_id"]) != "processed"
                ]
            )

    def router(self):
        router = APIRouter(prefix="/v1/code/sessions")

        @router.post("")
        async def create(request: Request):
            self._launch_auth(request)
            if request.headers.get("x-hq-mirror"):
                # An account proxy without the mirror passthrough routed this hub's own
                # upstream registration back here; accepting it would mirror it again.
                raise HTTPException(508, "HQ mirror request looped back; update the account proxy")
            body = await self._body(request)
            config = body.get("config", {})
            if not isinstance(config, dict) or not isinstance(body.get("title", ""), str):
                raise HTTPException(400, "Invalid native session configuration")
            with self._lock:
                self._prune()
                if _size(config) > MAX_METADATA_BYTES:
                    raise HTTPException(413, "Native configuration exceeds storage limit")
                if len(self._sessions) >= 1000:
                    raise HTTPException(503, "Native session capacity reached")
                sid = "cse_" + uuid4().hex
                session = dict(
                    id=sid,
                    title=body.get("title") or "Claude session",
                    config=config,
                    epoch=0,
                    worker_hash="",
                    worker_status="idle",
                    archived=False,
                    messages=[],
                    inbound=[],
                    sequence=0,
                    revision=1,
                    pending=None,
                    last_seen=time.time(),
                    seen_events=[],
                    external_metadata={},
                    private=request.headers.get("x-hq-private") == "1",
                )
                self._sessions[sid] = session
                self._save()
                self._mirror(session)
            return {"session": {"id": sid}}

        @router.post("/{sid}/bridge")
        async def bridge(sid: str, request: Request):
            self._launch_auth(request)
            body = await self._body(request)
            with self._lock:
                session = self._get(sid)
                if session["archived"]:
                    raise HTTPException(410, "Session is archived")
                if "expected_epoch" in body and body["expected_epoch"] != session["epoch"]:
                    raise HTTPException(
                        409,
                        {
                            "expected_epoch": body["expected_epoch"],
                            "actual_epoch": session["epoch"],
                        },
                    )
                session["epoch"] += 1
                session["revision"] += 1
                session["bridge_revision"] = session["revision"]
                self._streams.pop(sid, None)
                token = secrets.token_urlsafe(48)
                session["worker_hash"] = _digest(token)
                session["token_expires_at"] = time.time() + 86400
                self._touch(session)
                self._save()
                return dict(
                    worker_jwt=token,
                    worker_epoch=session["epoch"],
                    expires_in=86400,
                    api_base_url="https://api.anthropic.com",
                )

        @router.get("/{sid}/worker/events/stream")
        async def stream(sid: str, request: Request):
            with self._lock:
                session = self._get(sid)
                self._worker_auth(request, session)
                epoch = session["epoch"]
            try:
                cursor = int(
                    request.query_params.get(
                        "from_sequence_num", request.headers.get("last-event-id", "0")
                    )
                )
                if cursor < 0:
                    raise ValueError
            except ValueError as exc:
                raise HTTPException(400, "Invalid native event cursor") from exc
            self.replay(sid, cursor)
            lease = {"epoch": epoch, "id": str(uuid4())}
            with self._lock:
                self._declare_dialogs(session)
                self._streams[sid] = lease
                self._touch(session)
                self._mirror(session)

            async def generate():
                nonlocal cursor
                try:
                    yield ": connected\n\n"
                    last_keepalive = time.monotonic()
                    while True:
                        with self._lock:
                            if session["archived"] or self._streams.get(sid) != lease:
                                return
                        if await request.is_disconnected():
                            return
                        for event in self.replay(sid, cursor):
                            with self._lock:
                                if self._streams.get(sid) != lease:
                                    return
                            yield _frame(event)
                            cursor = event["sequence_num"]
                        if time.monotonic() - last_keepalive >= 10:
                            yield ": keep-alive\n\n"
                            last_keepalive = time.monotonic()
                        await asyncio.sleep(0.1)
                finally:
                    with self._lock:
                        if self._streams.get(sid) == lease:
                            self._streams.pop(sid, None)

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @router.api_route("/{sid}/worker", methods=["GET", "PUT"])
        async def worker(sid: str, request: Request):
            body = await self._body(request) if request.method == "PUT" else {}
            with self._lock:
                session = self._get(sid)
                self._worker_auth(request, session)
                if request.method == "PUT":
                    self._epoch(body, session)
                    if "worker_status" in body:
                        if body["worker_status"] not in (
                            "idle",
                            "running",
                            "requires_action",
                            "done",
                        ):
                            raise HTTPException(400, "Unknown native worker status")
                        session["worker_status"] = body["worker_status"]
                    metadata = body.get("external_metadata", {})
                    if not isinstance(metadata, dict):
                        raise HTTPException(400, "external_metadata must be an object")
                    merged = {**session["external_metadata"], **metadata}
                    if _size(merged) > MAX_METADATA_BYTES:
                        raise HTTPException(413, "Native metadata exceeds storage limit")
                    session["external_metadata"] = merged
                    session["revision"] += 1
                    self._mirror(
                        session,
                        "worker",
                        {k: body[k] for k in ("worker_status", "external_metadata") if k in body},
                    )
                self._touch(session)
                if request.method == "PUT":
                    self._save()
                return dict(
                    worker_epoch=session["epoch"],
                    worker_status=session["worker_status"],
                    external_metadata=session["external_metadata"],
                )

        @router.post("/{sid}/worker/{operation:path}")
        async def worker_post(sid: str, operation: str, request: Request):
            body = await self._body(request)
            with self._lock:
                session = self._get(sid)
                self._worker_auth(request, session)
                self._epoch(body, session)
                if operation in ("events", "internal-events"):
                    events = body.get("events", [])
                    if (
                        not isinstance(events, list)
                        or len(events) > 1000
                        or any(
                            not isinstance(e, dict) or not self._valid_payload(e.get("payload"))
                            for e in events
                        )
                    ):
                        raise HTTPException(400, "Invalid native event batch")
                    seen = OrderedDict((key, True) for key in session["seen_events"])
                    pending = copy.deepcopy(self._pending(session))
                    for item in events:
                        if self._event_identity(item) in seen:
                            continue
                        payload = item["payload"]
                        for incoming in self._incoming_requests(session, payload):
                            pending[incoming["request_id"]] = incoming
                        if payload.get("type") == "control_cancel_request":
                            pending.pop(payload["request_id"], None)
                        elif payload.get("type") == "control_response":
                            pending.pop(payload["response"]["request_id"], None)
                    if len(pending) > 64 or _size(pending) > MAX_PENDING_BYTES:
                        raise HTTPException(413, "Too many pending native permissions")
                    results = []
                    for item in events:
                        payload = item["payload"]
                        eid = self._event_identity(item)
                        duplicate = eid in seen if isinstance(eid, str) else False
                        if not duplicate:
                            self._ingest(session, payload)
                            if isinstance(eid, str):
                                seen[eid] = True
                        results.append({"event_id": eid or str(uuid4()), "duplicate": duplicate})
                    session["seen_events"] = list(seen)[-MAX_EVENTS:]
                    self._touch(session)
                    self._save()
                    fresh = [item for item, r in zip(events, results) if not r["duplicate"]]
                    if fresh:
                        self._mirror(session, operation, {"events": fresh})
                    return {"results": results}
                if operation == "events/delivery":
                    updates = body.get("updates")
                    if not isinstance(updates, list) or any(
                        not isinstance(u, dict)
                        or not isinstance(u.get("event_id"), str)
                        or u.get("status") not in ("received", "processing", "processed")
                        for u in updates
                    ):
                        raise HTTPException(400, "Invalid native delivery updates")
                    known = {event["event_id"] for event in session["inbound"]}
                    acknowledged = session.setdefault("acknowledged", {})
                    for update in updates:
                        if update["event_id"] in known:
                            rank = {"received": 1, "processing": 2, "processed": 3}
                            prior = acknowledged.get(update["event_id"])
                            if rank[update["status"]] > rank.get(prior, 0):
                                acknowledged[update["event_id"]] = update["status"]
                    watermark = session.get("delivered_sequence", 0)
                    for event in session["inbound"]:
                        if event["sequence_num"] <= watermark:
                            continue
                        if acknowledged.get(event["event_id"]) != "processed":
                            break
                        watermark = event["sequence_num"]
                    session["delivered_sequence"] = watermark
                    session["acknowledged"] = {k: v for k, v in acknowledged.items() if k in known}
                    origin = session.get("upstream", {}).get("delivery", {})
                    mapped = [
                        {"event_id": origin[u["event_id"]], "status": u["status"]}
                        for u in updates
                        if u["event_id"] in origin
                    ]
                    if mapped:
                        self._mirror(session, "delivery", {"updates": mapped})
                if operation in ("heartbeat", "events/delivery", "diagnostics"):
                    self._touch(session)
                    self._mirror(session)
                    if operation != "heartbeat":
                        self._save()
                    return {"heartbeat_interval_seconds": 20} if operation == "heartbeat" else {}
                raise HTTPException(404, "Unsupported native worker operation")

        @router.get("/{sid}")
        async def get_session(sid: str, request: Request):
            self._launch_auth(request)
            with self._lock:
                session = self._get(sid)
                return {
                    "session": {
                        "id": sid,
                        "title": session["title"],
                        "status": "archived" if session["archived"] else "active",
                    }
                }

        @router.patch("/{sid}")
        async def rename(sid: str, request: Request):
            self._launch_auth(request)
            body = await self._body(request)
            with self._lock:
                session = self._get(sid)
                if not isinstance(body.get("title"), str):
                    raise HTTPException(400, "title must be a string")
                session["title"] = body["title"]
                session["revision"] += 1
                self._save()
            return {}

        @router.post("/{sid}/archive")
        async def archive(sid: str, request: Request):
            self._launch_auth(request)
            with self._lock:
                session = self._get(sid)
                session["archived"] = True
                session["archived_at"] = time.time()
                session["revision"] += 1
                self._save()
                self._mirror(session, "archive")
            return {}

        return router


_backends = {}
_backend_lock = threading.Lock()


def get_backend():
    token = os.environ.get("CC_TMUX_NATIVE_TOKEN", "")
    if not token:
        return None
    directory = Path(
        os.environ.get("CC_TMUX_STATE_DIR", str(Path.home() / ".local/state/cc-tmux-api"))
    )
    key = (str(directory.resolve()), _digest(token))
    with _backend_lock:
        if key not in _backends:
            backend = NativeRemote(directory / "native", token)
            backend.upstream = rc_mirror.from_env(backend)
            _backends[key] = backend
        return _backends[key]
