"""Control loaded Codex threads through their existing local app-server owner."""

import copy
import hashlib
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path
from uuid import UUID, uuid4

from websockets.sync.client import unix_connect

RPC_TIMEOUT = 15
MAX_MESSAGES = 400
MAX_HISTORY_ITEMS = 4000
MAX_MESSAGE_BYTES = 2_000_000
APPROVALS = {
    "item/commandExecution/requestApproval": "CommandExecution",
    "item/fileChange/requestApproval": "FileChange",
    "item/permissions/requestApproval": "Permissions",
    "item/tool/requestUserInput": "AskUserQuestion",
}


def _error(message, status=503):
    from .fleet import FleetError

    return FleetError(message, status)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class AppServer:
    """One connection, no daemon launch, no reconnecting or retrying a write."""

    def __init__(self, socket_path, *, connect=unix_connect, timeout=RPC_TIMEOUT):
        self.socket_path = str(socket_path)
        self.connect = connect
        self.timeout = timeout
        self.epoch = ""
        self._lock = threading.RLock()
        self._connection_lock = threading.Lock()
        self._ws = None
        self._calls = {}
        self._requests = OrderedDict()
        self._responding = set()
        self._inputs = {}
        self._joined = set()

    def ensure(self):
        with self._connection_lock:
            with self._lock:
                if self._ws is not None:
                    return
            try:
                ws = self.connect(self.socket_path, uri="ws://localhost", open_timeout=5,
                                  close_timeout=1, max_size=16_000_000)
            except Exception as exc:
                raise _error("Existing Codex app-server is unavailable") from exc
            with self._lock:
                self._ws = ws
                self.epoch = uuid4().hex
                self._requests.clear()
                self._responding.clear()
                self._inputs.clear()
                self._joined.clear()
            threading.Thread(target=self._receive, args=(ws,), daemon=True,
                             name="codex-owner-events").start()
            try:
                self.call("initialize", {
                    "clientInfo": {"name": "cc_fleet", "version": "1.0"},
                    "capabilities": {"experimentalApi": True},
                }, connected=True)
                ws.send(json.dumps({"method": "initialized"}))
            except Exception:
                self._disconnect(ws)
                raise

    def _disconnect(self, ws):
        with self._lock:
            if self._ws is not ws:
                return
            self._ws = None
            self._requests.clear()
            self._responding.clear()
            self._joined.clear()
            for call in self._calls.values():
                call["event"].set()
        ws.close()

    def close(self):
        with self._lock:
            ws = self._ws
        if ws is not None:
            self._disconnect(ws)

    def _receive(self, ws):
        try:
            for raw in ws:
                message = json.loads(raw)
                with self._lock:
                    if self._ws is not ws:
                        return
                    method = message.get("method")
                    if not method:
                        call = self._calls.get(message.get("id"))
                        if call:
                            call["response"] = message
                            call["event"].set()
                        continue
                    params = message.get("params", {})
                    sid = params.get("threadId")
                    if "id" in message and sid:
                        self._requests[(sid, message["id"])] = message
                    elif method == "serverRequest/resolved":
                        self._requests.pop((sid, params.get("requestId")), None)
                        self._responding.discard((sid, params.get("requestId")))
                    elif method == "item/started" and params.get("item", {}).get("type") == "userMessage":
                        self._inputs[sid] = params["item"]["id"]
                    elif method in {"thread/closed", "thread/archived"}:
                        self._joined.discard(sid)
                        self._requests = OrderedDict((k, v) for k, v in self._requests.items()
                                                     if k[0] != sid)
        except Exception:
            pass
        finally:
            self._disconnect(ws)

    def call(self, method, params, *, write=False, connected=False, expected_epoch=None):
        if not connected and not write:
            self.ensure()
        rid = uuid4().hex
        call = {"event": threading.Event()}
        with self._lock:
            ws = self._ws
            if ws is None or expected_epoch is not None and self.epoch != expected_epoch:
                raise _error("Codex connection changed; refresh before acting", 409)
            self._calls[rid] = call
        try:
            try:
                ws.send(json.dumps({"id": rid, "method": method, "params": params}))
                call["event"].wait(self.timeout)
                response = call.get("response")
                if response is None:
                    raise TimeoutError()
            except Exception as exc:
                self._disconnect(ws)
                message = ("Action delivery unknown; do not retry" if write else
                           "Existing Codex app-server did not answer")
                raise _error(message) from exc
            if "error" in response:
                # Native errors can include filesystem paths or arbitrary tool output.
                raise _error("Codex rejected the request in its native owner", 422)
            return response["result"]
        finally:
            with self._lock:
                self._calls.pop(rid, None)

    def pending(self, sid):
        with self._lock:
            return copy.deepcopy([{**v, "responding": (thread_id, rid) in self._responding}
                                  for (thread_id, rid), v in self._requests.items() if thread_id == sid])

    def control_id(self, request):
        return self.epoch + ":" + _digest(request["id"])

    def respond(self, sid, control_id, result):
        with self._lock:
            pending = next((r for (thread_id, _), r in self._requests.items()
                            if thread_id == sid and self.control_id(r) == control_id), None)
            if pending is None or self._ws is None or (sid, pending["id"]) in self._responding:
                raise _error("Codex request is no longer pending", 409)
            ws = self._ws
            try:
                ws.send(json.dumps({"id": pending["id"], "result": result}))
                self._responding.add((sid, pending["id"]))
            except Exception as exc:
                self._disconnect(ws)
                raise _error("Action delivery unknown; do not retry") from exc
        # JSON-RPC replies have no ACK. Keep the native request until the owner
        # emits serverRequest/resolved, and never resend an uncertain response.


def _control(rpc, pending):
    if not pending:
        return None
    request = pending[0]
    method, params = request["method"], copy.deepcopy(request["params"])
    supported = method in APPROVALS and not request.get("responding")
    if method == "item/tool/requestUserInput":
        # Preserve the native question IDs; existing HQ/ACP form clients send
        # answers keyed by the displayed question, which the owner maps below.
        params["questions"] = [{**q, "options": q.get("options") or []}
                               for q in params.get("questions", [])]
    return dict(request_id=rpc.control_id(request), tool_name=APPROVALS.get(method, method),
                input=params, codex_method=method, supported=supported,
                subtype="codex" if supported else "request_user_dialog")


def _answer(request, response):
    params, method = request["params"], request["method"]
    behavior = response.get("behavior")
    if response.get("always"):
        raise _error("Codex fleet approvals are one-time decisions", 400)
    if behavior not in {"allow", "deny"}:
        raise _error("Response behavior must be allow or deny", 400)
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        decision = "accept" if behavior == "allow" else "decline"
        available = params.get("availableDecisions")
        if behavior == "deny" and available is not None and "decline" not in available and "cancel" in available:
            decision = "cancel"
        if available is not None and decision not in available:
            raise _error("This decision is not offered by the native request", 400)
        return {"decision": decision}
    if method == "item/permissions/requestApproval":
        return {"permissions": copy.deepcopy(params.get("permissions", {}))
                if behavior == "allow" else {}, "scope": "turn"}
    if method == "item/tool/requestUserInput":
        if behavior == "deny":
            return {"answers": {}}
        answers = response.get("updated_input", {}).get("answers", {})
        if not isinstance(answers, dict):
            raise _error("Question answers must be an object", 400)
        result = {}
        for question in params["questions"]:
            value = answers.get(question["id"], answers.get(question["question"]))
            values = value.get("answers") if isinstance(value, dict) else value
            values = [values] if isinstance(values, str) else values
            if not isinstance(values, list) or not values or any(
                not isinstance(v, str) or not v.strip() or len(v) > 100000 for v in values
            ):
                raise _error("Every question needs an explicit answer", 400)
            result[question["id"]] = {"answers": values}
        return {"answers": result}
    raise _error("Answer this native dialog in its original Codex client", 400)


def _message(entry):
    item = entry["item"]
    kind = item.get("type")
    if kind == "userMessage":
        text = "\n".join(block["text"] for block in item.get("content", [])
                         if block.get("type") == "text")
        role = "user"
    elif kind == "agentMessage":
        text, role = item.get("text", ""), "assistant"
    else:
        return None
    if not text:
        return None
    return dict(id=item["id"], role=role, text=text, client_id=item.get("clientId"),
                turn_id=entry.get("turnId"), phase=item.get("phase"),
                timestamp=(entry.get("startedAtMs") or item.get("startedAtMs") or 0) / 1000)


class CodexRemote:
    def __init__(self, socket_path, *, rpc=None):
        self.rpc = rpc or AppServer(socket_path)
        self._lock = threading.RLock()
        self._messages = {}
        self._history_truncated = {}
        self._history_turn = {}

    def _loaded(self):
        ids, cursor, seen = [], None, set()
        for _ in range(100):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self.rpc.call("thread/loaded/list", params)
            ids.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                return list(dict.fromkeys(ids))
            if cursor in seen:
                raise _error("Codex returned an invalid discovery cursor")
            seen.add(cursor)
        raise _error("Codex discovery exceeded the supported session limit")

    def _read(self, sid, *, detail=False):
        thread = self.rpc.call("thread/read", {"threadId": sid, "includeTurns": False})["thread"]
        if thread["id"] != sid or thread["status"]["type"] == "notLoaded":
            raise _error("Codex thread is not loaded", 409)
        if detail and sid not in self.rpc._joined:
            # Only rejoin an independently observed, already loaded UUID. Never
            # supply history, path, model, cwd, permissions or other overrides.
            if sid not in self._loaded():
                raise _error("Codex thread is not loaded", 409)
            self.rpc.call("thread/resume", {"threadId": sid, "excludeTurns": True})
            self.rpc._joined.add(sid)
            thread = self.rpc.call("thread/read", {"threadId": sid, "includeTurns": False})["thread"]
            if thread["status"]["type"] == "notLoaded":
                raise _error("Codex thread is not loaded", 409)
        turns = self.rpc.call("thread/turns/list", {
            "threadId": sid, "limit": 1, "sortDirection": "desc", "itemsView": "summary",
        })["data"]
        turn = turns[0] if turns else {}
        pending = self.rpc.pending(sid)
        control = _control(self.rpc, pending)
        native_status = thread["status"]["type"]
        active = native_status == "active"
        flags = thread["status"].get("activeFlags", [])
        status = "waiting" if pending or flags else "working" if active else (
            "idle" if native_status == "idle" else "unknown")
        direct = thread.get("canAcceptDirectInput") is True
        revision = _digest([self.rpc.epoch, sid, native_status, turn.get("id"),
                            turn.get("status"), self.rpc._inputs.get(sid), pending])
        row = dict(id=sid, session=sid, codex_thread_id=sid, provider="codex", transport="codex",
                   title=thread.get("name") or thread.get("preview", "")[:120] or "Codex",
                   label=thread.get("name") or "Codex", workdir=thread["cwd"],
                   model=thread.get("model"), effort=thread.get("reasoningEffort"),
                   status=status, lifecycle="alive", alive=True, online=True,
                   revision=revision, native_control=control, menu=None, control=None, tail="",
                   has_question=bool(control and control["tool_name"] == "AskUserQuestion"),
                   attention_kind=("question" if control and control["tool_name"] == "AskUserQuestion"
                                   else "permission") if pending or flags else None,
                   notification_eligible=bool(pending or flags),
                   event_id=control["request_id"] if control else None,
                   menu_signature=_digest(control) if control else None,
                   updated_at=thread.get("updatedAt"),
                   active_turn_id=turn.get("id") if active and turn.get("status") == "inProgress" else None,
                   owner_epoch=self.rpc.epoch,
                   capabilities={"send": direct and not (pending or flags),
                                 "interrupt": direct and active and turn.get("status") == "inProgress",
                                 "respond": bool(control and control["supported"]),
                                 "set_model": False, "set_permission_mode": False, "terminal": False})
        if detail:
            row["messages"], row["history_truncated"] = self._history(sid, turn.get("id"))
        return row

    def rows(self):
        with self._lock:
            rows = []
            for sid in self._loaded():
                try:
                    rows.append(self._read(sid))
                except Exception as exc:
                    from .fleet import FleetError

                    if not isinstance(exc, FleetError) or exc.status != 409:
                        raise
            return rows

    def detail(self, raw_id):
        try:
            if str(UUID(raw_id)) != raw_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise _error("Invalid Codex thread identity", 400) from exc
        with self._lock:
            if raw_id not in self._loaded():
                raise _error("Codex thread is not loaded", 409)
            return self._read(raw_id, detail=True)

    def _history(self, sid, latest_turn):
        previous = self._messages.get(sid, {})
        refreshing = {latest_turn, self._history_turn.get(sid)}
        entries, cursor, seen = [], None, set()
        anchor = None
        for _ in range(MAX_HISTORY_ITEMS // 100):
            params = {"threadId": sid, "limit": 100, "sortDirection": "desc"}
            if cursor:
                params["cursor"] = cursor
            page = self.rpc.call("thread/items/list", params)
            stop = False
            for entry in page["data"]:
                message = _message(entry)
                if message:
                    entries.append(message)
                    if message["id"] in previous:
                        anchor = message["id"]
                        if entry.get("turnId") not in refreshing:
                            stop = True
            cursor = page.get("nextCursor")
            if stop or not cursor or len(entries) >= MAX_MESSAGES:
                break
            if cursor in seen:
                raise _error("Codex returned an invalid history cursor")
            seen.add(cursor)
        messages = OrderedDict()
        retain_prefix = anchor is not None and bool(cursor)
        if retain_prefix:
            for key, message in previous.items():
                messages[key] = message
                if key == anchor:
                    break
        for message in reversed(entries):
            messages[message["id"]] = message
        truncated = (self._history_truncated.get(sid, False) if retain_prefix else bool(cursor))
        truncated = truncated or len(messages) > MAX_MESSAGES
        while len(messages) > MAX_MESSAGES:
            messages.popitem(last=False)
        size = sum(len(json.dumps(m).encode()) for m in messages.values())
        while size > MAX_MESSAGE_BYTES and messages:
            _, old = messages.popitem(last=False)
            size -= len(json.dumps(old).encode())
            truncated = True
        self._messages[sid] = messages
        self._history_truncated[sid] = truncated
        self._history_turn[sid] = latest_turn
        return copy.deepcopy(list(messages.values())), truncated

    def act(self, raw_id, action, revision, request_id, text=None, response=None):
        with self._lock:
            row = self.detail(raw_id)
            if revision != row["revision"] or revision != self._read(raw_id)["revision"]:
                raise _error("Codex session changed; refresh before acting", 409)
            if not row["capabilities"].get(action):
                raise _error("This action is not available on the current Codex session", 409)
            if action == "send":
                if not isinstance(text, str) or not text.strip() or len(text) > 100000:
                    raise _error("Message must contain 1–100000 characters", 400)
                params = {"threadId": raw_id, "input": [{"type": "text", "text": text}],
                          "clientUserMessageId": request_id}
                if row["active_turn_id"]:
                    params["expectedTurnId"] = row["active_turn_id"]
                    result = self.rpc.call("turn/steer", params, write=True, expected_epoch=row["owner_epoch"])
                else:
                    result = self.rpc.call("turn/start", params, write=True, expected_epoch=row["owner_epoch"])
                return dict(ok=True, id=raw_id, request_id=request_id, client_id=request_id,
                            delivery="accepted", turn_id=result.get("turn", {}).get("id") or
                            result.get("turnId") or row["active_turn_id"])
            if action == "interrupt":
                self.rpc.call("turn/interrupt", {"threadId": raw_id, "turnId": row["active_turn_id"]},
                              write=True, expected_epoch=row["owner_epoch"])
            elif action == "respond":
                if not isinstance(response, dict) or response.get("request_id") != row["native_control"]["request_id"]:
                    raise _error("Codex request is no longer pending", 409)
                request = next((r for r in self.rpc.pending(raw_id)
                                if self.rpc.control_id(r) == response["request_id"]), None)
                if request is None:
                    raise _error("Codex request is no longer pending", 409)
                result = _answer(request, response)
                self.rpc.respond(raw_id, response["request_id"], result)
            else:
                raise _error("Unsupported Codex action", 400)
            return dict(ok=True, id=raw_id, request_id=request_id,
                        delivery="queued" if action == "respond" else "accepted")


_backend = None
_backend_lock = threading.Lock()


def get_backend():
    global _backend
    if os.environ.get("CC_TMUX_CODEX", "1") == "0":
        return None
    path = Path(os.environ.get("CC_TMUX_CODEX_SOCKET") or
                Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) /
                "app-server-control/app-server-control.sock")
    with _backend_lock:
        if _backend is None and path.is_socket():
            _backend = CodexRemote(path)
        return _backend
