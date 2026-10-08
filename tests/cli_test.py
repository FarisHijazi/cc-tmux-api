"""Embedding preserves the wrapper's import-time worker configuration."""

import os
import subprocess
import sys
from pathlib import Path


def test_embedded_wrapper_sets_defaults_before_server_import(tmp_path):
    (tmp_path / "embedded_fixture.py").write_text('''
import os
os.environ["CC_TMUX_PREFIX"] = "hq-fixture-"
os.environ["CC_TMUX_ROUTINES_DIR"] = "/tmp/fixture-routines"
from cc_tmux_api import server, workers, routines
app = server.create_app()
def verify(spec):
    assert spec == "embedded_fixture:app"
    assert workers.PREFIX == "hq-fixture-"
    assert str(routines.DIR) == "/tmp/fixture-routines"
    print("embedding verified")
server.main = verify
''')
    source = Path(__file__).resolve().parents[1] / "src"
    env = {key: value for key, value in os.environ.items() if not key.startswith("CC_TMUX_")}
    env.update(PYTHONPATH=os.pathsep.join([str(tmp_path), str(source)]),
               CC_TMUX_STATE_DIR=str(tmp_path / "state"))
    result = subprocess.run(
        [sys.executable, "-m", "cc_tmux_api.cli", "--app", "embedded_fixture:app"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "embedding verified" in result.stdout
