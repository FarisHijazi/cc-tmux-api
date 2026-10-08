"""Durable push consumer for the existing fleet monitor (call via to_thread).

Plan: seed each session silently; atomically record normalized event identities
and enqueue eligible transitions per subscriber; drain due deliveries on observe.
Missing/unknown/stale sessions never reset baselines. Muting discards pending
session alerts. Delivery is at-least-once across a crash during provider acceptance;
stable notification tags let the browser replace a duplicate. No hooks or polling.
Uses upstream pywebpush and py_vapid for encryption/signing, not event parsing:
https://github.com/web-push-libs/pywebpush
https://github.com/web-push-libs/vapid
"""

import base64
import json
import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Lock, RLock
from urllib.parse import quote, urlsplit
from uuid import uuid4

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid
from pywebpush import WebPushException, webpush


class _NoRedirects(requests.Session):
    def request(self, *args, **kwargs):
        kwargs["allow_redirects"] = False
        return super().request(*args, **kwargs)


def _subscription(value):
    """Validate browser key material and a strict provider endpoint allowlist."""
    try:
        endpoint = value["endpoint"]
        url = urlsplit(endpoint)
        host = url.hostname or ""
        trusted = host in {"fcm.googleapis.com", "updates.push.services.mozilla.com"}
        trusted |= bool(re.fullmatch(r"(?:[a-z0-9-]+\.)+push\.apple\.com", host))
        if (not trusted or url.scheme != "https" or url.port not in (None, 443)
                or url.username is not None or url.password is not None
                or url.fragment or not url.path or len(endpoint) > 4096
                or any(c.isspace() or ord(c) < 32 for c in endpoint) or "\\" in endpoint):
            raise ValueError
        keys = {name: value["keys"][name] for name in ("auth", "p256dh")}
        decoded = {}
        for name, encoded in keys.items():
            if not isinstance(encoded, str) or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded):
                raise ValueError
            decoded[name] = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(decoded["auth"]) != 16 or len(decoded["p256dh"]) != 65:
            raise ValueError
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded["p256dh"])
        return {"endpoint": endpoint, "keys": keys}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Invalid browser push subscription or untrusted HTTPS endpoint") from exc


class PushService:
    """One shared service instance; synchronous methods serialize SQLite access.

    observe expects {sessions: [...]}; normalized event/notification_eligible take
    precedence over status fallbacks. updated_at is deliberately not a dedup key.
    subscribe/remove return {ok: True}; test returns sent/failed/pending counts.
    Retries: 30s exponential backoff, at most five attempts, one-hour expiry;
    at most 32 due deliveries per call. The parent monitor supplies retry ticks.
    A separate nonblocking delivery lock serializes sends; SQLite transactions
    never span network I/O. Mute/remove cancel queued, not in-flight, delivery.
    """

    def __init__(self, state_dir: Path, subject: str):
        state_dir = Path(state_dir)
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        self._path = state_dir / "push.sqlite3"
        self._key = state_dir / "vapid.pem"
        self._lock = RLock()
        self._delivery_lock = Lock()
        self._subject = subject
        os.close(os.open(self._path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._path.chmod(0o600)
        with self._db() as db:
            for statement in (
                "CREATE TABLE IF NOT EXISTS subscriptions(endpoint TEXT PRIMARY KEY, data TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS muted(id TEXT PRIMARY KEY)",
                "CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, signature TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS seen(id TEXT, event TEXT, PRIMARY KEY(id,event))",
                "CREATE TABLE IF NOT EXISTS outbox(endpoint TEXT REFERENCES subscriptions(endpoint) ON DELETE CASCADE, event TEXT, session TEXT, payload TEXT, attempts INTEGER DEFAULT 0, due REAL, expires REAL, PRIMARY KEY(endpoint,event))",
            ):
                db.execute(statement)
            if not self._key.exists():
                vapid = Vapid()
                vapid.generate_keys()
                temporary = state_dir / "vapid.tmp"
                os.close(os.open(temporary, os.O_CREAT | os.O_WRONLY, 0o600))
                temporary.chmod(0o600)
                vapid.save_key(str(temporary))
                temporary.replace(self._key)
            self._key.chmod(0o600)
            vapid = Vapid.from_file(str(self._key))
            raw = vapid.public_key.public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
            self._public = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @contextmanager
    def _db(self):
        with self._lock, closing(sqlite3.connect(self._path, timeout=60)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    def config(self) -> dict:
        valid = bool(re.fullmatch(r"mailto:[^\s@]+@[^\s@]+|https://[^\s/]+(?:/[^\s]*)?", self._subject or ""))
        result = {"available": valid, "public_key": self._public}
        if not valid:
            result["reason"] = "Configure a mailto: or HTTPS VAPID subject"
        return result

    def subscribe(self, subscription: dict) -> dict:
        subscription = _subscription(subscription)
        with self._db() as db:
            db.execute("INSERT INTO subscriptions VALUES (?,?) ON CONFLICT(endpoint) DO UPDATE SET data=excluded.data",
                       (subscription["endpoint"], json.dumps(subscription)))
        return {"ok": True}

    def remove(self, endpoint: str) -> dict:
        with self._db() as db:
            db.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint,))
        return {"ok": True}

    def preferences(self) -> dict:
        with self._db() as db:
            return {"muted": [row[0] for row in db.execute("SELECT id FROM muted ORDER BY id")]}

    def set_muted(self, id: str, muted: bool) -> dict:
        if not isinstance(id, str) or not id or not isinstance(muted, bool):
            raise ValueError("Session id and boolean muted are required")
        with self._db() as db:
            if muted:
                db.execute("INSERT OR IGNORE INTO muted VALUES (?)", (id,))
                db.execute("DELETE FROM outbox WHERE session=?", (id,))
            else:
                db.execute("DELETE FROM muted WHERE id=?", (id,))
        return self.preferences()

    def _enqueue(self, db, session, payload):
        now = time.time()
        db.execute("INSERT INTO outbox(endpoint,event,session,payload,due,expires) SELECT endpoint,?,?,?,?,? FROM subscriptions",
                   (payload["tag"], session, json.dumps(payload), now, now + 3600))

    def observe(self, snapshot: dict, *, deliver: bool = True) -> None:
        with self._db() as db:
            sessions = snapshot.get("sessions") or []
            unavailable = {h.get("id") for h in snapshot.get("hosts", [])
                           if h.get("available") is False}
            if snapshot.get("stale") or snapshot.get("online") is False:
                sessions = []
            for session in sessions:
                sid, status = session.get("id"), session.get("status")
                if (not sid or status not in {"working", "waiting", "idle", "done"}
                        or session.get("host") in unavailable
                        or session.get("stale") or session.get("online") is False):
                    continue
                event = session.get("event") or ""
                signature = json.dumps([status, event, session.get("status_emoji")], sort_keys=True)
                previous = db.execute("SELECT signature FROM sessions WHERE id=?", (sid,)).fetchone()
                db.execute("INSERT INTO sessions VALUES (?,?) ON CONFLICT(id) DO UPDATE SET signature=excluded.signature",
                           (sid, signature))
                event_id = session.get("event_id")
                fresh = previous is not None and previous[0] != signature
                if event_id:
                    fresh = db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (sid, str(event_id))).rowcount == 1
                    # Identical menus can reuse metadata IDs. Only a confirmed
                    # exit from waiting permits a new occurrence; missing/offline
                    # snapshots above never change the persisted baseline.
                    fresh |= bool(previous and status == "waiting"
                                  and json.loads(previous[0])[0] != "waiting")
                eligible = session.get("notification_eligible")
                if eligible is None:
                    eligible = status in {"waiting", "done"} or event in {"menu", "notification", "notify", "outcome", "stop"}
                if (previous is None or not fresh or not eligible
                        or session.get("status_emoji") == "🥱"
                        or db.execute("SELECT 1 FROM muted WHERE id=?", (sid,)).fetchone()):
                    continue
                title = " ".join(str(session.get("title") or "Session").split())[:80]
                label = session.get("status_label")
                body = " ".join(label.split())[:160] if isinstance(label, str) else ""
                body = body or {"waiting": "Needs your attention", "done": "Finished"}.get(status, "Session update")
                self._enqueue(db, sid, {"title": title, "body": body,
                                       "url": "/#session=" + quote(sid, safe=""),
                                       "tag": str(uuid4())})
        if deliver:
            self.deliver_pending()

    def deliver_pending(self) -> dict:
        """Drain the owned outbox independently of session observation."""
        return self._drain()

    def _drain(self, event=None) -> dict:
        result = {"sent": 0, "failed": 0, "pending": 0}
        draining = self._delivery_lock.acquire(blocking=False)
        try:
            if draining:
                with self._db() as db:
                    now = time.time()
                    db.execute("DELETE FROM outbox WHERE expires<=? OR attempts>=5", (now,))
                    rows = db.execute("SELECT endpoint,event FROM outbox WHERE due<=? AND (? IS NULL OR event=?) ORDER BY due LIMIT 32",
                                      (now, event, event)).fetchall() if self.config()["available"] else []
                with _NoRedirects() as transport:
                    for endpoint, tag in rows:
                        # Recheck after each send: mute/remove may cancel queued rows.
                        with self._db() as db:
                            now = time.time()
                            row = db.execute("SELECT o.payload,o.attempts,o.expires,s.data FROM outbox o JOIN subscriptions s USING(endpoint) WHERE endpoint=? AND event=? AND due<=? AND expires>? AND attempts<5 AND NOT EXISTS(SELECT 1 FROM muted WHERE id=o.session)",
                                             (endpoint, tag, now, now)).fetchone()
                        if row is None:
                            continue
                        payload, attempts, expires, data = row
                        # No database/state lock during provider I/O. An in-flight
                        # push cannot be recalled, but cancellation stays responsive.
                        try:
                            response = webpush(subscription_info=_subscription(json.loads(data)), data=payload,
                                               vapid_private_key=str(self._key), vapid_claims={"sub": self._subject},
                                               ttl=max(1, int(expires - now)), timeout=10, requests_session=transport)
                            status = response.status_code
                        except (WebPushException, requests.RequestException, OSError, ValueError) as exc:
                            status = getattr(getattr(exc, "response", None), "status_code", None)
                        with self._db() as db:
                            if status in (404, 410):
                                # Do not delete replacement keys enrolled during I/O.
                                db.execute("DELETE FROM subscriptions WHERE endpoint=? AND data=?", (endpoint, data))
                                result["failed"] += 1
                            elif status is not None and 200 <= status < 300:
                                db.execute("DELETE FROM outbox WHERE endpoint=? AND event=?", (endpoint, tag))
                                result["sent"] += 1
                            else:
                                # UPDATE cannot resurrect a muted/removed delivery.
                                db.execute("UPDATE outbox SET attempts=attempts+1,due=? WHERE endpoint=? AND event=?",
                                           (time.time() + 30 * 2 ** attempts, endpoint, tag))
                                result["failed"] += 1
        finally:
            if draining:
                self._delivery_lock.release()
        with self._db() as db:
            result["pending"] = db.execute("SELECT count(*) FROM outbox WHERE (? IS NULL OR event=?)", (event, event)).fetchone()[0]
        return result

    def test(self) -> dict:
        """Explicit enrollment verification, independent of baseline and mute."""
        tag = str(uuid4())
        with self._db() as db:
            self._enqueue(db, None, {"title": "Notifications enabled", "body": "Test notification",
                                     "url": "/", "tag": tag})
        return self._drain(tag)
