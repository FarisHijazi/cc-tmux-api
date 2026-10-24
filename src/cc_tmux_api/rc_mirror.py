"""Mirror HQ's native sessions to Anthropic's Remote Control, so claude.ai and the
Claude app see and steer them while HQ stays the CLI's only bridge.

NativeRemote remains the source of truth. Toward Anthropic this module is the
worker the real CLI would be: it registers the session, forwards the CLI's event
batches and state, and injects controller events through NativeRemote's own
queue. Hooks run under NativeRemote's lock and only enqueue; every network call
happens on this module's threads, which take that lock briefly through the
`upstream_*` host methods and never across a request.
"""

import json
import os
import queue
import re
import threading
import time

import httpx

UPSTREAM_URL = "https://api.anthropic.com"
SESSIONS = "/v1/code/sessions"
HEARTBEAT_SECONDS = 20
# A session whose CLI stays silent this long stops mirroring; its next call resumes it.
IDLE_STOP_SECONDS = 600
LIVE_SECONDS = 90
MAX_OUTBOX = 1000
ACCOUNT = re.compile(r"[A-Za-z0-9_.-]{1,64}")
WORKER_PATHS = {
    "events": ("POST", "/worker/events"),
    "internal-events": ("POST", "/worker/internal-events"),
    "delivery": ("POST", "/worker/events/delivery"),
    "worker": ("PUT", "/worker"),
    "heartbeat": ("POST", "/worker/heartbeat"),
}
# Client errors a retry can fix; any other 4xx rejects that one item for good.
RETRYABLE = (401, 408, 409, 429)


class Ended(Exception):
    """Anthropic archived or forgot the session; mirroring stops, HQ's session lives on."""


class Rejected(Exception):
    """Anthropic refused one item; drop it so everything queued behind it still goes."""


class Busy(Exception):
    """HQ's per-session command queue is full; replay the event later from the cursor."""


def from_env(host):
    proxy = os.environ.get("CC_TMUX_RC_UPSTREAM_PROXY", "")
    if not proxy:
        return None
    account = os.environ.get("CC_TMUX_RC_UPSTREAM_ACCOUNT", "personal")
    if not ACCOUNT.fullmatch(account):
        raise ValueError("CC_TMUX_RC_UPSTREAM_ACCOUNT must be a launcher account name")
    return Mirror(host, account, proxy=proxy, ca=os.environ.get("CC_TMUX_RC_UPSTREAM_CA") or None)


def _log(sid, text):
    print(f"rc-mirror {sid}: {text}", flush=True)


def _sse(lines):
    event, data = None, []
    for line in lines:
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))


class Mirror:
    def __init__(self, host, account="personal", *, proxy=None, ca=None, base_url=UPSTREAM_URL,
                 transport=None, backoff=1.0, max_backoff=60.0):
        self._host = host
        self._launch = {"Authorization": f"Bearer proxy-account-{account}"}
        network = {"transport": transport} if transport else {"proxy": proxy, "verify": ca or True}
        self._http = httpx.Client(
            base_url=base_url,
            # The account proxy passes requests carrying x-hq-mirror to Anthropic
            # instead of routing /v1/code/sessions back to this hub.
            headers={"x-hq-mirror": "1", "anthropic-version": "2023-06-01"},
            timeout=httpx.Timeout(30, read=60),
            **network,
        )
        self.backoff, self.max_backoff = backoff, max_backoff
        self._links = {}
        self._lock = threading.Lock()
        self._closed = False

    def post(self, sid, kind=None, body=None):
        """Non-blocking: start the session's link if needed and queue one item for it."""
        with self._lock:
            if self._closed:
                return
            link = self._links.get(sid)
            if link is None:
                link = self._links[sid] = _Link(self, sid)
                link.start()
        if kind:
            link.put(kind, body)

    def _drop(self, link):
        with self._lock:
            if self._links.get(link.sid) is link:
                self._links.pop(link.sid)

    def close(self):
        with self._lock:
            self._closed = True
            links, self._links = list(self._links.values()), {}
        for link in links:
            link.stop.set()
        self._http.close()


class _Link:
    def __init__(self, mirror, sid):
        self.mirror, self.sid = mirror, sid
        self.host, self.http = mirror._host, mirror._http
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.outbox = queue.Queue(MAX_OUTBOX)
        self.bridge_lock = threading.Lock()
        self.cse = self.jwt = None
        self.epoch = 0
        self.state_epoch = None
        self.cursor = 0
        self.refresh_at = 0.0
        self.interval = HEARTBEAT_SECONDS
        self.next_heartbeat = 0.0
        self.status = None
        self.streaming = False
        self.listener = None
        self.rejected = set()

    def start(self):
        threading.Thread(target=self._pump, name=f"rc-mirror-{self.sid}", daemon=True).start()

    def put(self, kind, body):
        while True:
            try:
                self.outbox.put_nowait((kind, body))
                return
            except queue.Full:
                try:
                    self.outbox.get_nowait()
                    _log(self.sid, "outbox full; dropped the oldest upstream item")
                except queue.Empty:
                    pass

    def _set(self, status, **fields):
        if self.stop.is_set():
            return
        if status != self.status:
            _log(self.sid, status)
        self.status = status
        self.host.upstream_update(self.sid, status=status, **fields)

    def _launch_call(self, method, path, body):
        reply = self.http.request(method, SESSIONS + path, json=body, headers=self.mirror._launch)
        if reply.status_code in (404, 410):
            raise Ended(f"{path} returned {reply.status_code}")
        reply.raise_for_status()
        return reply.json() if reply.content else {}

    def _register(self):
        snapshot = self.host.upstream_snapshot(self.sid)
        if snapshot is None:
            raise Ended("local session is gone")
        self.cursor = snapshot.get("cursor", 0)
        if snapshot.get("id"):
            self.cse = snapshot["id"]
            return
        data = self._launch_call("POST", "", {
            "title": snapshot["title"],
            "bridge": {},
            "tags": ["remote-control-repl"],
            "config": snapshot["config"],
        })
        session = data["session"]
        self.cse = session["id"]
        url = session.get("session_url")
        url = url if isinstance(url, str) and url.startswith("https://") else None
        self._set("connecting", id=self.cse, session_url=url, cursor=0)

    def bridge(self, stale_epoch=None):
        with self.bridge_lock:
            if stale_epoch is not None and stale_epoch != self.epoch:
                return
            self.ready.clear()
            data = self._launch_call("POST", f"/{self.cse}/bridge", {})
            self.jwt, self.epoch = data["worker_jwt"], data["worker_epoch"]
            self.refresh_at = time.time() + 0.8 * float(data.get("expires_in", 3600))
            self.ready.set()

    def _sync_state(self):
        """A new epoch starts with no state; send the session's current one first."""
        if self.state_epoch == self.epoch:
            return
        epoch, state = self.epoch, self.host.upstream_state(self.sid)
        if state is None:
            raise Ended("local session is gone")
        try:
            self._worker("worker", state)
        except Rejected as exc:
            self._reject(exc)
        self.state_epoch = epoch

    def _worker(self, kind, body):
        method, path = WORKER_PATHS[kind]
        for attempt in (0, 1):
            epoch = self.epoch
            reply = self.http.request(
                method, f"{SESSIONS}/{self.cse}{path}", json={**body, "worker_epoch": epoch},
                headers={"Authorization": f"Bearer {self.jwt}"},
            )
            status = reply.status_code
            if status in (401, 409) and attempt == 0:
                self.bridge(epoch)
                continue
            # 404 on an events path may only mean that endpoint does not exist upstream.
            if status == 410 or status == 404 and kind in ("worker", "heartbeat"):
                raise Ended(f"{path} returned {status}")
            if 400 <= status < 500 and status not in RETRYABLE:
                raise Rejected(f"{method} {path} {status}")
            reply.raise_for_status()
            return reply.json() if reply.content else {}

    def _reject(self, exc):
        if str(exc) not in self.rejected:
            self.rejected.add(str(exc))
            _log(self.sid, f"upstream rejected {exc}; dropped it")

    def _heartbeat(self):
        activity = self.host.upstream_activity(self.sid)
        if activity is None:
            raise Ended("local session is gone")
        idle = time.time() - activity
        if idle > IDLE_STOP_SECONDS and self.outbox.empty():
            self._set("disconnected")
            self.stop.set()
            return
        if time.time() < self.next_heartbeat or idle > LIVE_SECONDS:
            return
        # Set first, so a refused heartbeat waits for its slot instead of every pump tick.
        self.next_heartbeat = time.time() + self.interval
        data = self._worker("heartbeat", {
            "session_id": self.cse,
            "supports_heartbeat_probe": False,
            "current_interval_seconds": self.interval,
            "idle_seconds": int(idle),
        })
        value = data.get("heartbeat_interval_seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            self.interval = value
            self.next_heartbeat = time.time() + value

    def _send(self, item):
        kind, body = item
        if kind == "archive":
            if self.cse:
                reply = self.http.post(
                    f"{SESSIONS}/{self.cse}/archive", json={}, headers=self.mirror._launch
                )
                if reply.status_code not in (200, 404, 409, 410):
                    reply.raise_for_status()
            raise Ended("archived locally")
        if self.cse is None:
            self._register()
        if self.jwt is None or time.time() >= self.refresh_at:
            self.bridge()
            if self.status != "connected":
                self._set("connecting")
        if self.listener is None:
            self.listener = threading.Thread(target=self._listen, daemon=True,
                                             name=f"rc-mirror-stream-{self.sid}")
            self.listener.start()
        self._sync_state()
        if kind:
            self._worker(kind, body)

    def _pump(self):
        delay = self.mirror.backoff
        item = (None, None)
        try:
            while not self.stop.is_set():
                if item is None:
                    try:
                        item = self.outbox.get(timeout=min(1.0, max(self.interval / 4, 0.01)))
                    except queue.Empty:
                        item = (None, None)
                try:
                    self._send(item)
                    item = None
                    self._heartbeat()
                    delay = self.mirror.backoff
                    if self.status == "reconnecting" and self.streaming:
                        self._set("connected")
                except Rejected as exc:
                    item = None
                    self._reject(exc)
                except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    if self.stop.is_set():
                        return
                    if self.status != "reconnecting":
                        detail = type(exc).__name__
                        if isinstance(exc, httpx.HTTPStatusError):
                            detail += f" {exc.response.status_code}"
                        _log(self.sid, f"upstream error, retrying: {detail}")
                    self._set("reconnecting")
                    self.stop.wait(delay)
                    delay = min(delay * 2, self.mirror.max_backoff)
        except Ended as exc:
            _log(self.sid, f"mirror ended ({exc})")
            self.host.upstream_update(self.sid, status="ended")
            self.status = "ended"
        finally:
            self.stop.set()
            self.ready.set()
            self.mirror._drop(self)

    def _listen(self):
        delay = busy_delay = self.mirror.backoff
        while not self.stop.is_set():
            self.ready.wait()
            if self.stop.is_set():
                return
            epoch = self.epoch
            try:
                with self.http.stream(
                    "GET", f"{SESSIONS}/{self.cse}/worker/events/stream",
                    params={"from_sequence_num": self.cursor},
                    headers={"Authorization": f"Bearer {self.jwt}",
                             "Last-Event-ID": str(self.cursor),
                             "Accept": "text/event-stream"},
                ) as reply:
                    if reply.status_code in (401, 409):
                        self.bridge(epoch)
                    elif reply.status_code in (404, 410):
                        raise Ended(f"stream returned {reply.status_code}")
                    else:
                        reply.raise_for_status()
                        self.streaming = True
                        self._set("connected")
                        delay = self.mirror.backoff
                        for event, data in _sse(reply.iter_lines()):
                            if self.stop.is_set():
                                return
                            # Ephemeral deltas and session/delivery updates are for
                            # controllers; only persisted client events reach the CLI.
                            if event not in (None, "client_event"):
                                continue
                            self._inject(json.loads(data))
                            busy_delay = self.mirror.backoff
            except Ended as exc:
                _log(self.sid, f"mirror ended ({exc})")
                self.stop.set()
                self.host.upstream_update(self.sid, status="ended")
                return
            except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                if self.stop.is_set():
                    return
                _log(self.sid, f"stream dropped: {type(exc).__name__}")
            except Busy:
                _log(self.sid, "local queue full; replaying from the cursor later")
                # The replay hits the same full queue until the CLI catches up.
                delay, busy_delay = busy_delay, min(busy_delay * 2, self.mirror.max_backoff)
            self.streaming = False
            if self.stop.is_set():
                return
            self._set("reconnecting")
            self.stop.wait(delay)
            delay = min(delay * 2, self.mirror.max_backoff)

    def _inject(self, envelope):
        sequence = int(envelope["sequence_num"])
        if sequence <= self.cursor:
            return
        payload = envelope.get("payload")
        if (
            isinstance(payload, dict)
            and payload.get("type") == "control_request"
            and isinstance(payload.get("request"), dict)
            and payload["request"].get("subtype") == "end_session"
        ):
            raise Ended("controller ended the session")
        if envelope.get("source", "client") == "client":
            self.host.upstream_inject(self.sid, envelope.get("event_id"), payload, sequence)
        else:
            self.host.upstream_update(self.sid, cursor=sequence)
        self.cursor = sequence

