#!/bin/sh
set -eu
toy_name=cc-mobile-browser-toy-20261008
toy_uuid=c620be5c-5c58-4ad6-a039-97fe1673fa6f
toy_cwd=/tmp/cc-mobile-browser-toy-$toy_uuid
if tmux has-session -t "=$toy_name" 2>/dev/null; then
  echo 'Reserved toy already exists; refusing to reuse or overwrite' >&2
  exit 1
fi
mkdir -m 700 "$toy_cwd"
tmux new-session -d -s "$toy_name" -c "$toy_cwd" -x 120 -y 38 \
  "$HOME/.local/bin/claude" --safe-mode --strict-mcp-config \
  --tools AskUserQuestion --session-id "$toy_uuid" --permission-mode default
tmux display-message -p -t "$toy_name:0.0" \
  '#{session_name}|#{pane_id}|#{pane_current_path}|#{pane_current_command}|#{pane_pid}|#{pane_tty}'
