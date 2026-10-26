#!/bin/sh
# Fake `claude` for the parity harness (copied onto the scratch PATH as `claude`).
#   claude -p PROMPT   -> headless answer; PROMPT "fail" exits 3, "slow" sleeps 5 s
#   claude [flags...]  -> interactive: writes the session jsonl (with a bridge id)
#                         for --session-id/--resume, draws an idle Claude screen,
#                         then echoes typed lines (shell builtins keep `claude`
#                         as the pane's foreground process, like the real CLI)
if [ "$1" = "-p" ]; then
  case "$2" in
    fail) echo "boom" >&2; exit 3 ;;
    slow) sleep 5; echo "late" ;;
    *) echo "answer: $2" ;;
  esac
  exit 0
fi
sid=""
while [ $# -gt 0 ]; do
  case "$1" in
    --session-id|--resume) sid="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [ -n "$sid" ]; then
  dir="$HOME/.claude/projects/$(pwd | tr '/.' '--')"
  mkdir -p "$dir"
  printf '{"type":"system","bridgeSessionId":"cse_parity%s"}\n' "$(printf %s "$sid" | cut -c1-8)" >> "$dir/$sid.jsonl"
fi
box() {  # an empty Claude prompt box on the bottom rows, as the probe's reader expects
  rows=$(stty size 2>/dev/null | cut -d' ' -f1)
  printf '\033[2J\033[H'
  i=0
  while [ "$i" -lt "$((${rows:-24} - 6))" ]; do echo; i=$((i + 1)); done
  printf '✻ Brewed for 1m 8s\n────────────\n❯ \n────────────\n⏵⏵ bypass permissions on'
}
box
while IFS= read -r line; do
  printf '%s\n' "$line" >> "$HOME/claude-input.log"
  box
done
