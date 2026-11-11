"""Remote Control watchdog against a fake CLI that replays the screens Claude Code 2.1.322 showed live."""
import json
import os

import pytest

from cc_tmux_api import rc_watchdog as wd

RULE = "─" * 40
IDLE = ["● ready", RULE, "❯", RULE, "  ⏵⏵ bypass permissions on"]
DRAFT = ["● ready", RULE, "❯ half typed", RULE]
DIALOG = ["   Remote Control", "     Disconnect this session", "     Show QR code   Scan",
          "   ❯ Continue", "   Enter to select · Esc to continue"]


class FakeCli:
    """Models the CLI: /remote-control toggles; when connected it opens the dialog."""

    def __init__(self, file, connected, screen=IDLE):
        self.file, self.connected, self.screen, self.typed, self.sent = file, connected, list(screen), "", []
        self.cursor = 2
        self.next_id = iter(f"session_{n:032x}" for n in range(1, 99))
        self._write(self.connected and "session_" + "a" * 32)

    def _write(self, bridge):
        self.file.write_text(json.dumps({"pid": os.getpid(), "tmux": "s:@1.%9", "kind": "interactive",
                                         "status": "idle", "bridgeSessionId": bridge or None}))

    def capture(self, pane):
        if self.screen is DIALOG:
            options = ["Disconnect this session", "Show QR code   Scan", "Continue"]
            return ["   Remote Control"] + [("   ❯ " if i == self.cursor else "     ") + o
                                            for i, o in enumerate(options)]
        if self.typed:
            return ["● ready", f"  ❯ {self.typed}                 Disconnect Remote Control",
                    RULE, f"❯ {self.typed}", RULE]
        return self.screen

    def keys(self, pane, *keys, literal=False):
        self.sent.append(keys)
        if literal:
            self.typed += keys[0]
            return
        key = keys[0]
        if self.screen is DIALOG:
            if key == "Up":
                self.cursor = max(0, self.cursor - 1)
            elif key == "Enter":
                if self.cursor == 0:
                    self.connected = False
                    self._write(None)
                self.screen = IDLE
            return
        if key == "Enter" and self.typed == "/remote-control":
            self.typed = ""
            if self.connected:
                self.screen, self.cursor = DIALOG, 2
            else:
                self.connected = True
                self._write(next(self.next_id))


def session(file):
    return wd.read_session(file)


@pytest.fixture
def file(tmp_path):
    return tmp_path / "123.json"


def repairer(cli):
    return wd.Repairer(tmux=cli, sleep=lambda s: None, settle=0, wait=1)


def test_disconnected_session_is_connected_with_one_toggle(file):
    cli = FakeCli(file, connected=False)
    s = session(file)
    assert wd.diagnose(s, {}) == "connect"
    new = repairer(cli).repair(s, "connect")
    assert new.startswith("session_") and cli.connected
    assert not any(k == ("Up",) for k in cli.sent)


def test_stale_bridge_disconnects_from_dialog_then_reconnects_with_new_id(file):
    cli = FakeCli(file, connected=True)
    s = session(file)
    assert wd.diagnose(s, {"cse_" + "a" * 32: False}) == "reconnect"
    new = repairer(cli).repair(s, "reconnect")
    assert new != s.bridge and cli.connected
    assert cli.sent.count(("Up",)) == 2


def test_healthy_bridge_needs_nothing(file):
    FakeCli(file, connected=True)
    assert wd.diagnose(session(file), {"cse_" + "a" * 32: True}) is None


def test_never_types_over_a_draft_or_busy_session(file):
    cli = FakeCli(file, connected=False, screen=DRAFT)
    with pytest.raises(RuntimeError, match="draft"):
        repairer(cli).repair(session(file), "connect")
    assert cli.sent == []
    busy = wd.Session(**{**session(file).__dict__, "status": "busy"})
    with pytest.raises(RuntimeError, match="busy"):
        repairer(FakeCli(file, connected=False)).repair(busy, "connect")


def test_bridge_health_reads_native_rows_and_upstream():
    snap = {"sessions": [
        {"session": "cse_1", "transport": "native", "alive": True, "online": True, "upstream": {"status": "connected"}},
        {"session": "cse_2", "transport": "native", "alive": True, "online": True, "upstream": {"status": "ended"}},
        {"session": "cse_3", "transport": "native", "alive": False, "online": False},
        {"session": "cse_3", "transport": "native", "alive": True, "online": True, "upstream": None},
        {"session": "tmux-pane", "alive": True},
    ]}
    assert wd.bridge_health(snap) == {"cse_1": True, "cse_2": False, "cse_3": True}


def test_run_once_repairs_only_sessions_unhealthy_on_both_checks(file, tmp_path):
    flaky = tmp_path / "456.json"
    cli = FakeCli(file, connected=False)
    FakeCli(flaky, connected=False)
    sessions = [session(file), wd.Session(**{**session(flaky).__dict__, "pid": 456})]
    calls = iter([sessions, sessions[:1]])
    results = wd.run_once(health_fn=lambda: {}, sessions_fn=lambda: next(calls),
                          repairer=repairer(cli), sleep=lambda s: None)
    assert [(r["pid"], r["result"]) for r in results] == [(os.getpid(), "repaired")]


def test_ignores_dead_and_non_interactive_sessions(tmp_path):
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "1.json").write_text(json.dumps({"pid": 2**22 + 7, "tmux": "s:@1.%1", "kind": "interactive"}))
    (tmp_path / "sessions" / "2.json").write_text(json.dumps({"pid": os.getpid(), "tmux": "s:@1.%2", "kind": "print"}))
    assert wd.local_sessions([tmp_path]) == []


def test_direct_anthropic_bridge_is_left_alone(file):
    FakeCli(file, connected=True)
    direct = wd.Session(**{**session(file).__dict__, "bridge": "session_01LVUCRX6iq6D1BuQ5bBGqKf"})
    assert wd.diagnose(direct, {}) is None
