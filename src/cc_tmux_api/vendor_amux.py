"""
VENDORED from amux (https://github.com/mixpeek/amux), amux-server.py
commit 206622101f65f966ec447ecb928295f835f4724b (2026-07-15), MIT + Commons Clause.
Function _detect_claude_status copied VERBATIM (lines 8602-8738) - do not edit;
re-sync from upstream if Claude Code's terminal UI changes.
This is the battle-tested Claude Code pane-state classifier: given raw
`tmux capture-pane` output it returns 'active' | 'waiting' | 'idle' | ''.
"""

import re

def _detect_claude_status(raw_output: str) -> str:
    """Detect Claude Code status from tmux output.

    Uses Claude Code's known terminal UI patterns to determine state.
    Scans bottom-up for the most recent definitive signal.

    Returns: 'active', 'waiting', 'idle', or ''.
    """
    if not raw_output:
        return ""
    clean = re.sub(
        r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\]8;[^\x1b]*\x1b\\|\x1b\][^\x07]*\x07'
        r'|\x1b\][^\x1b]*\x1b\\|\x1b[()][A-Z0-9]|\x1b[\x20-\x2f]*[\x40-\x7e]',
        '', raw_output,
    )
    lines = [l for l in clean.splitlines() if l.strip()]
    if not lines:
        return ""

    # ── 0. Active spinner (highest precedence, wide window) ──
    # An active spinner (dingbat U+2700-27BF + …) only renders while Claude is
    # generating, and the captured alt-screen IS the current viewport, so it's
    # authoritative wherever it appears. Scan a wide window because Claude can
    # push it well above the bottom: a long task list, or the "How is Claude
    # doing this session?" feedback survey below it. Checking this FIRST also
    # avoids a task-list item like "✔ … for col_x" being misread as a completed
    # spinner ("idle") by the bottom-up scan below.
    for l in reversed(lines[-30:]):
        s = l.strip()
        if s and "✀" <= s[0] <= "➿" and "…" in s:
            return "active"
        if s.startswith("Running…") or re.match(r"Reading \d+ file", s):
            return "active"

    # ── 1. Status bar (bottom 3 lines — always the very last line when visible) ──
    status_bar = ""
    for l in reversed(lines[-3:]):
        ls = l.strip()
        # Status bar indicators: ⏵⏵, bypass permissions, plan mode, auto-compact
        if "\u23f5\u23f5" in ls or "bypass permissions" in ls.lower() or "plan mode" in ls.lower():
            status_bar = ls.lower()
            break

    # "esc to interrupt" → active, BUT only when there is no standard status bar.
    # In background-tasks mode the "esc to interrupt · ctrl+t to hide tasks" line
    # REPLACES the ⏵⏵/bypass-permissions bar (status_bar is empty) even though
    # Claude is working. When a normal status bar IS present, "esc to interrupt"
    # appears there at ALL times (even at idle), so we skip this check and let
    # the spinner scan below determine the real state.
    if not status_bar:
        if "Resume from summary" in clean and "Resume full session" in clean:
            return "waiting"
        # Background-tasks mode replaces the status bar with "esc to interrupt · ctrl+t to hide tasks".
        # Match only this specific string to avoid false-positives from "esc to cancel",
        # "esc to accept all", or any other idle-state "esc to ..." text.
        for l in lines[-5:]:
            if "esc to interrupt" in l.lower():
                return "active"

    # ── 2. Scan last 12 lines bottom-up for the most recent signal ──
    for l in reversed(lines[-12:]):
        s = l.strip()
        sl = s.lower()

        # ── Active signals ──
        # Spinner: dingbat char (U+2700-27BF) + verb + … (ellipsis = still running)
        #   e.g. "✻ Beaming… (1m 3s)" "✽ Tinkering…" "✳ Germinating…" "✢ Synthesizing…"
        if s and "\u2700" <= s[0] <= "\u27bf" and "\u2026" in s:
            return "active"
        # Tool execution in progress
        if s.startswith("Running\u2026") or re.match(r"Reading \d+ file", s):
            return "active"

        # ── Completed signals (= idle, Claude finished and returned to prompt) ──
        # Completed spinner: dingbat + past tense + "for" + duration (no ellipsis)
        #   e.g. "✻ Brewed for 1m 8s"  "✻ Sautéed for 4m 19s"
        # Require an actual duration after "for" so task-list checkmark items like
        #   "\u2714 MI-834 TRUE-LOSS LIST for col_6c9e294720" aren't misread as idle.
        if (s and "\u2700" <= s[0] <= "\u27bf" and "\u2026" not in s
                and re.search(r" for \d+\s*[hms]\b", s)):
            return "idle"

        # ── Waiting signals (needs user action) ──
        # Multi-choice / AskUserQuestion prompt
        if "enter to select" in sl:
            return "waiting"
        # Manual tool approval prompt ("Do you want to proceed? ❯ 1. Yes")
        if "do you want to proceed" in sl:
            return "waiting"
        if re.match(r".*\u276f\s*\d+\.", s):  # ❯ 1. Yes / ❯ 2. No selector
            return "waiting"
        # "Interrupted · What should Claude do instead?" — printed after the USER
        # interrupts a turn. The session sits at a normal, fully usable prompt;
        # nothing is awaiting a selection, so this is idle, not "needs input"
        # (labeling it waiting made every interrupted session nag as if Claude
        # had asked a question).
        if "interrupted" in sl and "what should claude do" in sl:
            return "idle"

    # ── 3. Status bar secondary checks ──
    if status_bar:
        # Tool approval pending (only when bypass is off)
        bypass_on = "bypass permissions on" in status_bar
        if not bypass_on and re.search(r"\d+\s+(bash|tool|read|edit|write|glob|grep|notebook)", status_bar):
            return "waiting"
        if "approve" in status_bar:
            return "waiting"
        # Status bar visible but no active/waiting signal → idle at prompt
        return "idle"

    # ── 4. Codex-specific patterns ──
    # Codex active: bullet + "esc to interrupt" or working/running
    for l in reversed(lines[-12:]):
        s = l.strip()
        sl = s.lower()
        if s.startswith("•") and "esc to interrupt" in sl:
            return "active"
        if s.startswith("•") and ("working" in sl or "running" in sl):
            return "active"
    # Codex idle: › prompt or model status line (gpt-X · ~/path)
    for l in lines[-5:]:
        ls = l.strip()
        if "·" in ls and ("gpt-" in ls or "o3" in ls or "o4" in ls):
            return "idle"
        if ls.startswith("›") and ("implement" in ls.lower() or ls == "›"):
            return "idle"

    # ── 5. Fallback: check for shell prompt character ──
    # Only treat ❯ as a shell prompt when it's at the end of a line (not ❯ 1. Yes selector)
    for l in lines[-5:]:
        ls = l.strip()
        if ls.endswith("\u276f") or ls == "\u276f":
            return "idle"
        if "$ " in ls and not ls.startswith("❯"):
            return "idle"
    return ""

