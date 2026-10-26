//! Port of vendor_amux._detect_claude_status (vendored from amux, MIT +
//! Commons Clause; see src/cc_tmux_api/vendor_amux.py for provenance). Keep
//! this line-for-line with the Python; re-sync both when Claude Code's UI changes.
//! Returns "active" | "waiting" | "idle" | "".

use std::sync::LazyLock;

use regex::Regex;

use crate::py::{splitlines, strip};

static ANSI: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\]8;[^\x1b]*\x1b\\|\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\|\x1b[()][A-Z0-9]|\x1b[\x20-\x2f]*[\x40-\x7e]",
    )
    .unwrap()
});
static READING: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^Reading \d+ file").unwrap());
static DONE_FOR: LazyLock<Regex> = LazyLock::new(|| Regex::new(r" for \d+\s*[hms]\b").unwrap());
static SELECTOR: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^.*\x{276f}\s*\d+\.").unwrap());
static STATUS_TOOL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\d+\s+(bash|tool|read|edit|write|glob|grep|notebook)").unwrap());

fn dingbat(s: &str) -> bool {
    s.chars().next().is_some_and(|c| ('\u{2700}'..='\u{27bf}').contains(&c))
}

fn tail<'a>(lines: &'a [&'a str], n: usize) -> &'a [&'a str] {
    &lines[lines.len().saturating_sub(n)..]
}

pub fn detect_claude_status(raw: &str) -> &'static str {
    if raw.is_empty() {
        return "";
    }
    let clean = ANSI.replace_all(raw, "");
    let lines: Vec<&str> = splitlines(&clean).into_iter().filter(|l| !strip(l).is_empty()).collect();
    if lines.is_empty() {
        return "";
    }

    // 0. Active spinner (highest precedence, wide window)
    for l in tail(&lines, 30).iter().rev() {
        let s = strip(l);
        if !s.is_empty() && dingbat(s) && s.contains('…') {
            return "active";
        }
        if s.starts_with("Running…") || READING.is_match(s) {
            return "active";
        }
    }

    // 1. Status bar (bottom 3 lines)
    let mut status_bar = String::new();
    for l in tail(&lines, 3).iter().rev() {
        let ls = strip(l);
        let lower = ls.to_lowercase();
        if ls.contains("\u{23f5}\u{23f5}") || lower.contains("bypass permissions") || lower.contains("plan mode") {
            status_bar = lower;
            break;
        }
    }
    if status_bar.is_empty() {
        if clean.contains("Resume from summary") && clean.contains("Resume full session") {
            return "waiting";
        }
        for l in tail(&lines, 5) {
            if l.to_lowercase().contains("esc to interrupt") {
                return "active";
            }
        }
    }

    // 2. Last 12 lines bottom-up
    for l in tail(&lines, 12).iter().rev() {
        let s = strip(l);
        let sl = s.to_lowercase();
        if !s.is_empty() && dingbat(s) && s.contains('\u{2026}') {
            return "active";
        }
        if s.starts_with("Running\u{2026}") || READING.is_match(s) {
            return "active";
        }
        if !s.is_empty() && dingbat(s) && !s.contains('\u{2026}') && DONE_FOR.is_match(s) {
            return "idle";
        }
        if sl.contains("enter to select") || sl.contains("do you want to proceed") || SELECTOR.is_match(s) {
            return "waiting";
        }
        if sl.contains("interrupted") && sl.contains("what should claude do") {
            return "idle";
        }
    }

    // 3. Status bar secondary checks
    if !status_bar.is_empty() {
        let bypass_on = status_bar.contains("bypass permissions on");
        if !bypass_on && STATUS_TOOL.is_match(&status_bar) {
            return "waiting";
        }
        if status_bar.contains("approve") {
            return "waiting";
        }
        return "idle";
    }

    // 4. Codex-specific patterns
    for l in tail(&lines, 12).iter().rev() {
        let s = strip(l);
        let sl = s.to_lowercase();
        if s.starts_with('•') && sl.contains("esc to interrupt") {
            return "active";
        }
        if s.starts_with('•') && (sl.contains("working") || sl.contains("running")) {
            return "active";
        }
    }
    for l in tail(&lines, 5) {
        let ls = strip(l);
        if ls.contains('·') && (ls.contains("gpt-") || ls.contains("o3") || ls.contains("o4")) {
            return "idle";
        }
        if ls.starts_with('›') && (ls.to_lowercase().contains("implement") || ls == "›") {
            return "idle";
        }
    }

    // 5. Fallback: shell prompt character
    for l in tail(&lines, 5) {
        let ls = strip(l);
        if ls.ends_with('\u{276f}') || ls == "\u{276f}" {
            return "idle";
        }
        if ls.contains("$ ") && !ls.starts_with('❯') {
            return "idle";
        }
    }
    ""
}

#[cfg(test)]
mod tests {
    use super::detect_claude_status as d;

    #[test]
    fn classifies_known_screens() {
        assert_eq!(d(""), "");
        assert_eq!(d("✻ Beaming… (1m 3s)\n"), "active");
        assert_eq!(d("✻ Brewed for 1m 8s\n> \n⏵⏵ bypass permissions on\n"), "idle");
        assert_eq!(d("Do you want to proceed?\n❯ 1. Yes\n"), "waiting");
        assert_eq!(d("user@box:~$ "), ""); // strip() eats the space, as in Python
        assert_eq!(d("user@box:~$ ls\n"), "idle");
    }
}
