"""Load private runtime configuration before the application's import-time settings."""

import argparse
import importlib
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="HQ — existing Claude sessions, anywhere")
    parser.add_argument("--config", type=Path, help="Private JSON file of CC_TMUX_* settings")
    parser.add_argument("--app", help="Embedded ASGI application, for example main:app")
    args = parser.parse_args()
    if args.config:
        config = json.loads(args.config.read_text())
        if not isinstance(config, dict):
            parser.error("config must be a JSON object")
        for key, value in config.items():
            if not key.startswith("CC_TMUX_") and key != "BCE_WORKSPACE_DIR":
                parser.error(f"unsupported configuration key: {key}")
            if not isinstance(value, (str, int, float, list, dict)):
                parser.error(f"invalid configuration value: {key}")
            os.environ.setdefault(
                key, json.dumps(value) if isinstance(value, (list, dict)) else str(value)
            )
    # Embedded apps set worker/routine defaults before importing the package.
    # Preserve that order: these modules read their configuration at import.
    if args.app:
        importlib.import_module(args.app.split(":", 1)[0])
    from .server import main as serve

    serve(args.app)


if __name__ == "__main__":
    main()
