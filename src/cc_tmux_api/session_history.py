"""Fleet-owned lifecycle persistence, independent of push persistence.

Contract: SessionHistory(state_dir=None).observe({hosts: [...], sessions: [...]})
returns a new snapshot. Feed only locally probed/local-or-SSH-owned hosts BEFORE
merging HTTP peers: each peer owns its own history. No polling or hook parsing.

Plan: upsert current sessions only for hosts with available=True; mark missing
sessions ended only for those successfully enumerated hosts; project other
non-ended records as unreachable. Missing sessions data is not an empty census.
Confirmed ends stay ended through outages until the same ID is observed alive.
End timestamps are sticky; ended records expire after 30 days, while alive and
unreachable records are never age-pruned. A new process generation has a new ID.

last_seen is the exact coordinator time of the last successful observation of
that session. ended_at is the first successful host census confirming its absence,
not its actual process exit time. Both use Unix seconds, not activity timestamps.
An offline host never changes a confirmed ended record to unreachable; only
records not already confirmed ended become unreachable. Preserve source
updated_at and opaque status_emoji. Lifecycle is separate from activity: non-live
results have activity=None and preserve last_activity. Their status/status_label
and last_status/last_status_label are historical, not evidence of current work;
consumers must gate working indicators on lifecycle. Non-live results clear
menu/control/revision plus notification eligibility. Live results retain
source controls. Persist summaries only, never transcripts or actionable controls.
The caller must also enforce lifecycle checks when resolving action/detail routes.
"""

import copy
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

RETENTION_SECONDS = 30 * 24 * 60 * 60
_TRANSIENT = {"tail", "messages", "menu", "control", "revision", "native_control"}


class SessionHistory:
    def __init__(self, state_dir: Path | None = None):
        directory = Path(state_dir) if state_dir is not None else Path(
            os.environ.get("CC_TMUX_STATE_DIR", str(Path.home() / ".local/state/cc-tmux-api"))
        )
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self._path = directory / "session-history.sqlite3"
        os.close(os.open(self._path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._path.chmod(0o600)
        with closing(sqlite3.connect(self._path, timeout=10)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS session_history ("
                       "id TEXT PRIMARY KEY, host TEXT NOT NULL, summary TEXT NOT NULL, "
                       "last_seen REAL NOT NULL, ended_at REAL)")

    def observe(self, snapshot: dict) -> dict:
        """Persist a fleet census and merge retained sessions without mutating input."""
        result = copy.deepcopy(snapshot)
        hosts = snapshot.get("hosts", [])
        sessions = snapshot.get("sessions")
        if not isinstance(hosts, list) or (sessions is not None and not isinstance(sessions, list)):
            raise ValueError("Fleet hosts and sessions must be lists")
        successful = {host["id"] for host in hosts if host.get("available") is True}
        if sessions is None or snapshot.get("stale") or snapshot.get("online") is False:
            successful = set()
        current = {}
        for session in sessions or []:
            if session.get("host") not in successful:
                continue
            sid = session.get("id")
            if not isinstance(sid, str) or not sid or sid in current:
                raise ValueError("Fleet session IDs must be nonempty and unique")
            current[sid] = copy.deepcopy(session)
        now = time.time()
        with closing(sqlite3.connect(self._path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for sid, session in current.items():
                if session.get('transport') == 'native' and session.get('lifecycle') != 'alive':
                    # A socket/heartbeat outage never proves the process ended.
                    # Only the native protocol's explicit archive confirms an end.
                    previous = db.execute(
                        "SELECT last_seen,ended_at FROM session_history WHERE id=?", (sid,)
                    ).fetchone()
                    last_seen, ended_at = previous or (now, None)
                    if session.get('lifecycle') == 'ended' and ended_at is None:
                        ended_at = now
                    session.update(
                        alive=False if ended_at is not None else None,
                        lifecycle='ended' if ended_at is not None else 'unreachable',
                        last_seen=last_seen, ended_at=ended_at, activity=None,
                        last_activity=session.get('activity') or session.get('status'),
                        menu=None, control=None, native_control=None, revision=None,
                        has_question=False, notification_eligible=False,
                    )
                    summary = {k: v for k, v in session.items() if k not in _TRANSIENT}
                    db.execute(
                        "INSERT INTO session_history VALUES (?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET summary=excluded.summary, "
                        "ended_at=excluded.ended_at",
                        (sid, session['host'], json.dumps(summary), last_seen, ended_at),
                    )
                    continue
                session.update(alive=True, lifecycle="alive", last_seen=now, ended_at=None,
                               last_status=session.get("status", "unknown"),
                               last_status_label=session.get("status_label"))
                session["activity"] = session.get("activity") or session.get("status", "unknown")
                session["last_activity"] = session["activity"]
                summary = {key: value for key, value in session.items() if key not in _TRANSIENT}
                db.execute("INSERT INTO session_history VALUES (?,?,?,?,NULL) "
                           "ON CONFLICT(id) DO UPDATE SET host=excluded.host, "
                           "summary=excluded.summary,last_seen=excluded.last_seen,ended_at=NULL",
                           (sid, session["host"], json.dumps(summary), now))
            stored = db.execute("SELECT id,host,summary,last_seen,ended_at FROM session_history ORDER BY id").fetchall()
            retained = []
            for sid, host, summary, last_seen, ended_at in stored:
                if sid in current:
                    continue
                session = json.loads(summary)
                if ended_at is None and host in successful and session.get('transport') != 'native':
                    ended_at = now
                    db.execute("UPDATE session_history SET ended_at=? WHERE id=?", (ended_at, sid))
                if ended_at is not None and ended_at <= now - RETENTION_SECONDS:
                    db.execute("DELETE FROM session_history WHERE id=?", (sid,))
                    continue
                lifecycle = "ended" if ended_at is not None else "unreachable"
                # Older persisted summaries have activity but no last_activity.
                last_activity = session.get("last_activity", session.get("activity", session.get("last_status")))
                session.update(alive=False if ended_at is not None else None,
                               lifecycle=lifecycle, activity=None, last_activity=last_activity,
                               last_seen=last_seen, ended_at=ended_at, menu=None, control=None,
                               revision=None, notification_eligible=False)
                if session.get('transport') == 'native':
                    session.update(native_control=None, has_question=False)
                retained.append(session)
            result["sessions"] = list(current.values()) + retained
        return result
