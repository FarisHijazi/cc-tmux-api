"""Remote Control watchdog against a fake CLI that replays the screens Claude Code 2.1.322-2.1.329 showed live."""
import json
import os

import pytest

from cc_tmux_api import rc_watchdog as wd

RULE = "─" * 40
IDLE = ["● ready", RULE, "❯", RULE, "  ⏵⏵ bypass permissions on"]
DRAFT = ["● ready", RULE, "❯ half typed", RULE]
PLACEHOLDER = ["● ready", RULE, "\x1b[39m❯\xa0\x1b[2mTry \"how do I log an error?\"\x1b[0m", RULE]
QUESTION = ["● ready", " ☐ Pick one", "❯ 1. Yes", "  2. No", "Enter to select · ↑/↓ to navigate · Esc to cancel"]
DIALOG = ["   Remote Control", "     Disconnect this session", "     Show QR code   Scan",
          "   ❯ Continue", "   Enter to select · Esc to continue"]


class FakeCli:
    """Models the CLI: /remote-control toggles; when connected it opens the dialog."""

    def __init__(self, file, connected, screen=IDLE, bridges=True):
        self.file, self.connected, self.screen, self.typed, self.sent = file, connected, list(screen), "", []
        self.bridges = bridges
        self.cursor = 2
        self.next_id = iter(f"session_{n:032x}" for n in range(1, 99))
        self._write(self.connected and "session_" + "a" * 32)

    def _write(self, bridge):
        self.file.write_text(json.dumps({"pid": os.getpid(), "tmux": "s:@1.%9", "kind": "interactive",
                                         "status": "idle", "bridgeSessionId": bridge or None}))

    idle = float("inf")

    def idle_for(self, pane):
        return self.idle

    def capture(self, pane):
        if self.screen is DIALOG:
            options = ["Disconnect this session", "Show QR code   Scan", "Continue"]
            return ["   Remote Control"] + [("   ❯ " if i == self.cursor else "     ") + o
                                            for i, o in enumerate(options)]
        if self.typed:
            return ["● ready", "  ❯ /remote-control (rc)       Disconnect Remote Control",
                    RULE, f"❯ {self.typed}", RULE]
        return self.screen

    def keys(self, pane, *keys, literal=False):
        self.sent.append(keys)
        if literal:
            self.typed += keys[0]
            return
        key = keys[0]
        if self.screen == QUESTION and key == "Escape":
            self.screen = list(IDLE)
            return
        if self.screen is DIALOG:
            if key == "Up":
                self.cursor = max(0, self.cursor - 1)
            elif key == "Down":
                self.cursor = min(2, self.cursor + 1)
            elif key == "Enter":
                if self.cursor == 0:
                    self.connected = False
                    self._write(None)
                self.screen = IDLE
            return
        if key == "Escape":
            self.typed = ""
        if key == "Enter" and self.typed == wd.REASK:
            self.submitted, self.typed = self.typed, ""
        if key == "Enter" and self.typed == "/rc":
            self.typed = ""
            if self.connected:
                self.screen, self.cursor = DIALOG, 2
            else:
                self.connected = self.bridges
                self._write(self.bridges and next(self.next_id))


def session(file):
    return wd.read_session(file)


@pytest.fixture
def file(tmp_path):
    return tmp_path / "123.json"


def repairer(cli):
    return wd.Repairer(tmux=cli, sleep=lambda s: None, settle=0, wait=1)


def test_disconnected_session_gets_rc_twice_and_the_second_is_answered_with_continue(file):
    cli = FakeCli(file, connected=False)
    s = session(file)
    assert wd.diagnose(s, {}) == "connect"
    new = repairer(cli).repair(s, "connect")
    assert new.startswith("session_") and cli.connected and cli.screen == IDLE
    assert cli.sent.count(("/rc",)) == 2
    assert not any(k == ("Up",) for k in cli.sent)


def test_double_rc_on_a_connected_cli_keeps_its_bridge(file):
    cli = FakeCli(file, connected=True)
    s = session(file)
    r = repairer(cli)
    r._send_rc(s.pane)
    r._send_rc(s.pane)
    assert cli.connected and wd.read_session(file).bridge == s.bridge and cli.screen == IDLE


def test_autocomplete_is_recognised_even_truncated_in_a_narrow_pane():
    assert wd.COMPLETION.search("  ❯ /remote-control (rc)       Disconnect Remote Control")
    assert wd.COMPLETION.search("  ❯\xa0…emote-control (rc)  Control this session from")
    assert not wd.COMPLETION.search("  ❯ /pstack:architect    (pstack (Claude Code port))")


def test_dim_placeholder_counts_as_an_empty_prompt():
    assert wd.prompt_state(PLACEHOLDER) == "empty"
    assert wd.prompt_state(IDLE) == "empty"
    assert wd.prompt_state(DRAFT) == "draft"


def test_open_menus_are_skipped_untouched(file):
    for screen in (QUESTION, DIALOG, IDLE[:-1] + ["Enter to select · Esc to cancel"]):
        assert wd.prompt_state(screen) == "menu"
    cli = FakeCli(file, connected=False, screen=QUESTION)
    with pytest.raises(RuntimeError, match="menu"):
        repairer(cli).repair(session(file), "connect")
    assert cli.sent == []


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
    with pytest.raises(RuntimeError, match="draft"):
        repairer(cli)._send_rc("%9")
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
                          repairer=repairer(cli), sleep=lambda s: None, gave_up_file=tmp_path / "g.json")
    assert [(r["pid"], r["result"]) for r in results] == [(os.getpid(), "repaired")]


def test_a_failed_toggle_is_never_retyped_into_the_same_process(file, tmp_path):
    cli = FakeCli(file, connected=False, bridges=False)
    run = lambda: wd.run_once(health_fn=lambda: {}, sessions_fn=lambda: [session(file)], repairer=repairer(cli),
                              sleep=lambda s: None, gave_up_file=tmp_path / "g.json")
    assert "no new bridge" in run()[0]["result"]
    typed = len(cli.sent)
    assert "earlier repair failed" in run()[0]["result"]
    assert len(cli.sent) == typed


def test_ignores_dead_and_non_interactive_sessions(tmp_path):
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "1.json").write_text(json.dumps({"pid": 2**22 + 7, "tmux": "s:@1.%1", "kind": "interactive"}))
    (tmp_path / "sessions" / "2.json").write_text(json.dumps({"pid": os.getpid(), "tmux": "s:@1.%2", "kind": "print"}))
    assert wd.local_sessions([tmp_path]) == []


def test_direct_anthropic_bridge_is_left_alone(file):
    FakeCli(file, connected=True)
    direct = wd.Session(**{**session(file).__dict__, "bridge": "session_01LVUCRX6iq6D1BuQ5bBGqKf"})
    assert wd.diagnose(direct, {}) is None


def test_force_reports_every_session_and_repairs_without_waiting(file, tmp_path):
    cli = FakeCli(file, connected=False)
    other = tmp_path / "456.json"
    FakeCli(other, connected=True)
    ok = wd.Session(**{**session(other).__dict__, "pid": 456})
    waits = []
    rows = wd.run_once(health_fn=lambda: {"cse_" + "a" * 32: True}, sessions_fn=lambda: [session(file), ok],
                       repairer=repairer(cli), sleep=waits.append, confirm_after=0, report_all=True,
                       gave_up_file=tmp_path / "g.json")
    assert waits == []
    assert {r["pid"]: r["result"] for r in rows} == {os.getpid(): "repaired", 456: "connected"}


def test_unreachable_hq_still_connects_bridgeless_sessions_but_never_reconnects(file, tmp_path):
    FakeCli(file, connected=True)
    stale = session(file)
    FakeCli(tmp_path / "456.json", connected=False)
    bare = wd.Session(**{**session(tmp_path / "456.json").__dict__, "pid": 456})

    def down():
        raise OSError("connection refused")

    assert {pid: kind for pid, (_, kind) in wd.unhealthy(down, lambda: [stale, bare]).items()} == {456: "connect"}


def test_peer_runs_the_same_forced_command_from_its_env_file():
    seen = {}

    def run(cmd, **_):
        seen["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": '{"pid": 1, "result": "connected"}\nnoise\n', "stderr": ""})

    assert wd.run_peer("peer", ["--dry-run"], run=run) == [{"host": "peer", "pid": 1, "result": "connected"}]
    assert seen["cmd"][:2] == ["ssh", "-o"] and seen["cmd"][-2] == "peer"
    assert "cc-rc-watchdog --now --peers= --dry-run" in seen["cmd"][-1] and wd.ENV_FILE in seen["cmd"][-1]


def test_close_menus_escapes_repairs_then_asks_for_the_menu_again(file):
    cli = FakeCli(file, connected=False, screen=QUESTION)
    waiting = wd.Session(**{**session(file).__dict__, "status": "waiting"})
    r = wd.Repairer(tmux=cli, sleep=lambda s: None, settle=0, wait=1, close_menus=True)
    assert r.repair(waiting, "connect").startswith("session_")
    assert cli.sent[0] == ("Escape",) and cli.sent.count(("/rc",)) == 2
    assert cli.submitted == wd.REASK


def test_close_menus_never_touches_a_busy_session_or_a_draft(file):
    r = lambda cli: wd.Repairer(tmux=cli, sleep=lambda s: None, settle=0, wait=1, close_menus=True)
    cli = FakeCli(file, connected=False, screen=QUESTION)
    busy = wd.Session(**{**session(file).__dict__, "status": "busy"})
    with pytest.raises(RuntimeError, match="busy"):
        r(cli).repair(busy, "connect")
    cli = FakeCli(file, connected=False, screen=DRAFT)
    with pytest.raises(RuntimeError, match="draft"):
        r(cli).repair(session(file), "connect")
    assert cli.sent == []


def test_dry_run_reports_what_it_would_skip(file, tmp_path):
    FakeCli(file, connected=False, screen=DRAFT)
    cli = FakeCli(file, connected=False, screen=DRAFT)
    rows = wd.run_once(health_fn=lambda: {}, sessions_fn=lambda: [session(file)], repairer=repairer(cli),
                       sleep=lambda s: None, dry_run=True, gave_up_file=tmp_path / "g.json")
    assert rows[0]["result"] == "dry-run: would skip: prompt has a draft" and cli.sent == []


def test_a_pane_the_owner_is_typing_in_is_skipped_unless_forced(file):
    cli = FakeCli(file, connected=False, screen=QUESTION)
    cli.idle = 5
    waiting = wd.Session(**{**session(file).__dict__, "status": "waiting"})
    for close in (False, True):
        with pytest.raises(RuntimeError, match="in use"):
            wd.Repairer(tmux=cli, sleep=lambda s: None, settle=0, wait=1, close_menus=close).repair(waiting, "connect")
    assert cli.sent == []
    forced = wd.Repairer(tmux=cli, sleep=lambda s: None, settle=0, wait=1, close_menus=True, active_window=0)
    assert forced.repair(waiting, "connect").startswith("session_")


def test_renew_names_reconnects_a_healthy_bridge_so_it_registers_its_current_title(file, tmp_path):
    cli = FakeCli(file, connected=True)
    s = session(file)
    rows = wd.run_once(health_fn=lambda: {"cse_" + "a" * 32: True}, sessions_fn=lambda: [s], repairer=repairer(cli),
                       sleep=lambda _: None, confirm_after=0, renew=True, gave_up_file=tmp_path / "g.json")
    assert rows[0]["kind"] == "reconnect" and rows[0]["result"] == "repaired" and rows[0]["bridge"] != s.bridge
