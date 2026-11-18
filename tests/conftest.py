"""Never auto-discover a developer's real Codex daemon during isolated tests."""

import pytest


@pytest.fixture(autouse=True)
def disable_live_codex_discovery(monkeypatch):
    monkeypatch.setenv('CC_TMUX_CODEX', '0')
