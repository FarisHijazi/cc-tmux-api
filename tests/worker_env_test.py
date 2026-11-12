"""A worker's own env is given to tmux at spawn, kept in its registry entry, and given again on revive."""
import json

import pytest

from cc_tmux_api import workers


class FakeTmux:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def new_sessions(self):
        return [c for c in self.calls if c[0] == "new-session"]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    tmux = FakeTmux()
    monkeypatch.setattr(workers, "_tmux", tmux)
    monkeypatch.setattr(workers, "_sessions", lambda: [])
    monkeypatch.setattr(workers, "send_text", lambda session, text: None)
    monkeypatch.setattr(workers, "SESSIONS_DIR", tmp_path / "reg")
    monkeypatch.setattr(workers, "PROMPT_DIR", tmp_path / "prompts")
    monkeypatch.setattr(workers.time, "sleep", lambda s: None)
    return tmux, tmp_path


def test_spawn_passes_env_to_tmux_and_keeps_it(fake):
    tmux, tmp = fake
    info = workers.spawn("oncall", "/loop watch", "/work", env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"})
    (call,) = tmux.new_sessions()
    assert call[call.index("-e") + 1] == "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=30"
    meta = json.loads(next((tmp / "reg").glob("*/meta.json")).read_text())
    assert meta["env"] == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"} and meta["session"] == info["session"]


def test_spawn_without_env_adds_no_flag(fake):
    tmux, _ = fake
    workers.spawn("plain", "do it", "/work")
    assert "-e" not in tmux.new_sessions()[0]


def test_spawn_rejects_bad_env_name_before_creating_anything(fake):
    tmux, tmp = fake
    with pytest.raises(ValueError):
        workers.spawn("bad", "x", "/work", env={"A B; rm": "1"})
    assert tmux.calls == [] and not (tmp / "reg").exists()


def test_revive_reapplies_the_saved_env(fake, monkeypatch):
    tmux, tmp = fake
    workers.spawn("oncall", "/loop watch", "/work", env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"})
    tmux.calls.clear()
    monkeypatch.setattr(workers, "_jsonl_path", lambda sid, wd: tmp / "reg")  # exists: there is a jsonl to resume
    assert len(workers.resume_registered()) == 1
    (call,) = tmux.new_sessions()
    assert call[call.index("-e") + 1] == "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=30"
