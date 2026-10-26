//! Port of workers.py — spawn, observe, steer and revive unattended Claude Code
//! workers in tmux. The tmux server is the live registry (sessions named with
//! PREFIX, metadata in @hq_* user options); a per-session registry under
//! $BCE_WORKSPACE_DIR/workspace/<sid>/ survives reboots. See workers.py for the
//! full design notes; behaviour here is kept identical.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{LazyLock, Mutex};
use std::time::Duration;

use regex::Regex;
use serde_json::{Value, json};

use crate::amux::detect_claude_status;
use crate::proc::{self, RunError, tmux, tmux_t};
use crate::py;

pub static PREFIX: LazyLock<String> = LazyLock::new(|| std::env::var("CC_TMUX_PREFIX").unwrap_or_else(|_| "hq-".into()));
const STATUS_STYLE: &str = "bg=colour208,fg=colour232"; // orange = automation worker
const PROMPT_DIR: &str = "/tmp/cc-tmux-api-prompts";
pub static DEFAULT_WORKDIR: LazyLock<String> =
    LazyLock::new(|| std::env::var("CC_TMUX_WORKDIR").unwrap_or_else(|_| py::home().to_string_lossy().into()));
const SHELLS: [&str; 4] = ["zsh", "bash", "sh", "fish"];
static SESSIONS_DIR: LazyLock<PathBuf> = LazyLock::new(|| {
    std::env::var("BCE_WORKSPACE_DIR").map(PathBuf::from).unwrap_or_else(|_| py::home().join(".basecamp-enhancer")).join("workspace")
});
const RESUME_MAX_DAYS: f64 = 5.0;
const GC_DONE_DAYS: f64 = 14.0;
const JSONL_FRESH_S: i64 = 60;
const BLOCKED_TOOLS: &str = "AskUserQuestion";
const HEADLESS_SYSTEM_PROMPT: &str = "You are running UNATTENDED in a background tmux session. No human is watching \
in real time, so you MUST NOT ask questions, present option menus, or wait for \
approval — doing so hangs you forever with nobody to answer. NEVER end your turn \
with a question or an offer ('want me to X or Y?', 'shall I proceed?'): that IS \
asking-and-waiting. When you would normally ask, instead pick the most reasonable \
option, state the assumption you made, and DO it. Stay strictly on the task you \
were given — if you notice an unrelated problem, note it in one line in your \
output and keep going; do NOT wander off to fix or propose other work. Only if you \
are genuinely blocked on the ASSIGNED task (missing secret, or an \
irreversible/approval-gated action per the project's rules) do you stop — and then \
you write your blocker and exactly what you need as your final message and stop, \
never a question that waits. You may be steered by messages typed into the session \
later, but never block waiting for one.";
const HEADLESS_PROMPT_NOTE: &str = "[Unattended background session — no human is watching. Do NOT ask questions or \
end your turn with an offer/question; assume the best option and DO it, staying \
strictly on this task (note unrelated issues in one line, don't chase them). If \
truly blocked on THIS task, stop with a one-line blocker as your final message — \
never a question that waits. Then do the task below.]";

/// Ownership: our PREFIX, and an @hq_owner stamp (if any) equal to it — so
/// NESTED prefixes ("hq-" vs "hq-work-b-") stay disjoint.
pub fn mine(name: &str, owner: &str) -> bool {
    name.starts_with(PREFIX.as_str()) && (owner.is_empty() || owner == PREFIX.as_str())
}

pub struct Session {
    pub name: String,
    pub created: i64,
    pub attached: bool,
    pub owner: String,
    pub kind: String,
    pub sid: String,
    pub workdir: String,
    pub title: String,
}

pub fn sessions() -> Vec<Session> {
    sessions_full()
}

/// One tmux call for every session plus its @hq_* options; title last (may contain tabs).
fn sessions_full() -> Vec<Session> {
    let r = tmux(&[
        "list-sessions",
        "-F",
        "#{session_name}\t#{session_created}\t#{session_attached}\t#{@hq_kind}\t#{@hq_sid}\t#{@hq_workdir}\t#{@hq_owner}\t#{@hq_title}",
    ]);
    if !r.ok() {
        return Vec::new();
    }
    r.stdout
        .lines()
        .filter_map(|line| {
            let f: Vec<&str> = line.splitn(8, '\t').collect();
            (f.len() == 8).then(|| Session {
                name: f[0].into(),
                created: f[1].parse().unwrap_or(0),
                attached: f[2] == "1",
                kind: f[3].into(),
                sid: f[4].into(),
                workdir: f[5].into(),
                owner: f[6].into(),
                title: f[7].into(),
            })
        })
        .collect()
}

fn pane_cmds() -> HashMap<String, String> {
    let r = tmux(&["list-panes", "-a", "-F", "#{session_name}\t#{pane_current_command}"]);
    let mut out = HashMap::new();
    if r.ok() {
        for line in r.stdout.lines() {
            let (name, cmd) = line.split_once('\t').unwrap_or((line, ""));
            out.entry(name.to_string()).or_insert_with(|| cmd.to_string());
        }
    }
    out
}

fn capture(session: &str, lines: usize) -> Result<String, RunError> {
    let r = tmux_t(&["capture-pane", "-t", session, "-p", "-S", &format!("-{lines}")])?;
    Ok(if r.ok() { r.stdout } else { String::new() })
}

fn session_opts(session: &str) -> (String, String) {
    let get = |opt| py::strip(&tmux(&["show-option", "-t", session, "-qv", opt]).stdout).to_string();
    (get("@hq_sid"), get("@hq_workdir"))
}

pub fn jsonl_path(sid: &str, workdir: &str) -> PathBuf {
    let dir: String = workdir.chars().map(|c| if c == '/' || c == '.' { '-' } else { c }).collect();
    py::home().join(".claude").join("projects").join(dir).join(format!("{sid}.jsonl"))
}

fn jsonl_age(sid: &str, workdir: &str) -> Option<i64> {
    if sid.is_empty() || workdir.is_empty() {
        return None;
    }
    let mtime = std::fs::metadata(jsonl_path(sid, workdir)).ok()?.modified().ok()?;
    let secs = mtime.duration_since(std::time::UNIX_EPOCH).ok()?.as_secs_f64();
    Some(((py::now() - secs) as i64).max(0))
}

fn classify(cmd: &str, pane: &str, age: Option<i64>) -> &'static str {
    if SHELLS.contains(&cmd) {
        return "done"; // back at the shell -> claude exited
    }
    let status = match detect_claude_status(pane) {
        "active" => "working",
        "waiting" => "waiting",
        "idle" => "idle",
        _ => "unknown",
    };
    if matches!(status, "idle" | "unknown") && age.is_some_and(|a| a < JSONL_FRESH_S) {
        return "working";
    }
    status
}

pub fn status_of(session: &str) -> &'static str {
    let r = tmux(&["display-message", "-t", session, "-p", "#{pane_current_command}"]);
    let cmd = if r.ok() { py::strip(&r.stdout).to_lowercase() } else { String::new() };
    let (sid, wd) = session_opts(session);
    classify(&cmd, &capture(session, 40).unwrap_or_default(), jsonl_age(&sid, &wd))
}

/// Tmux-Orchestrator pattern: literal text, 0.5s, then Enter.
pub fn send_text(session: &str, text: &str) {
    tmux(&["send-keys", "-t", session, "-l", "--", text]);
    std::thread::sleep(Duration::from_millis(500));
    tmux(&["send-keys", "-t", session, "Enter"]);
}

/// `claude -p <prompt>` with a timeout. Ok((returncode, stdout, stderr)).
pub fn oneshot(prompt: &str, timeout: u64) -> Result<proc::Out, RunError> {
    let claude = py::which("claude").unwrap_or_else(|| py::home().join(".local/bin/claude"));
    proc::run(&claude.to_string_lossy(), &["-p", prompt], None, Duration::from_secs(timeout))
}

fn slugify(title: &str, max: usize) -> String {
    static NON: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());
    let slug = NON.replace_all(&title.to_lowercase(), "-").trim_matches('-').to_string();
    slug.chars().take(max).collect()
}

pub fn routine_slug(name: &str) -> String {
    slugify(name, usize::MAX)
}

fn stamp(session: &str, title: &str, sid: &str, workdir: &str, kind: &str) {
    for (opt, val) in [("@hq_title", title), ("@hq_sid", sid), ("@hq_workdir", workdir), ("@hq_kind", kind), ("@hq_owner", PREFIX.as_str())] {
        tmux(&["set-option", "-t", session, opt, val]);
    }
    tmux(&["set-option", "-t", session, "status-style", STATUS_STYLE]);
}

pub fn spawn(title: &str, prompt: &str, workdir: Option<&str>, kind: &str) -> Result<Value, String> {
    let workdir = workdir.filter(|w| !w.is_empty()).unwrap_or(DEFAULT_WORKDIR.as_str()).to_string();
    let mut slug = slugify(title, 24);
    if slug.is_empty() {
        slug = "task".into();
    }
    let existing: Vec<String> = sessions().into_iter().map(|s| s.name).collect();
    let mut n = 1;
    while existing.contains(&format!("{}{slug}-{n}", *PREFIX)) {
        n += 1;
    }
    let session = format!("{}{slug}-{n}", *PREFIX);
    let sid = py::uuid4();

    let mut done_note = String::new();
    if kind == "task" {
        let reg = SESSIONS_DIR.join(&sid);
        std::fs::create_dir_all(&reg).map_err(|e| e.to_string())?;
        let meta = json!({
            "session": session, "sid": sid, "title": title, "workdir": workdir,
            "kind": kind, "created": py::now() as i64,
            "loop": Regex::new(r"/loop\s").unwrap().is_match(prompt),
        });
        std::fs::write(reg.join("meta.json"), pretty(&meta)).map_err(|e| e.to_string())?;
        let reg = reg.display();
        done_note = format!(
            "\n\n[Your scratch folder is {reg} — write any working files/outputs \
there. When your assigned task is FULLY complete — nothing left to \
do, watch, or loop on — run: touch {reg}/.done . That tells the \
spawner this session is finished and must not be revived after a \
reboot. If you are watching on a loop, do NOT write it; a looping \
watch is never finished until a human closes it.]"
        );
    }
    std::fs::create_dir_all(PROMPT_DIR).map_err(|e| e.to_string())?;
    let pf = format!("{PROMPT_DIR}/{session}.md");
    std::fs::write(&pf, format!("{HEADLESS_PROMPT_NOTE}\n\n{prompt}{done_note}")).map_err(|e| e.to_string())?;
    let sf = format!("{PROMPT_DIR}/_headless.txt");
    std::fs::write(&sf, HEADLESS_SYSTEM_PROMPT).map_err(|e| e.to_string())?;

    let r = tmux(&["new-session", "-d", "-s", &session, "-c", &workdir]);
    if !r.ok() {
        return Err(format!("tmux new-session failed: {}", r.stderr.trim()));
    }
    stamp(&session, title, &sid, &workdir, kind);
    // shell-first, ending in `; exit` so a finished worker closes its session (see workers.py)
    send_text(
        &session,
        &format!(
            "claude --dangerously-skip-permissions --disallowedTools {BLOCKED_TOOLS} --session-id {sid} \
--remote-control {session} --append-system-prompt \"$(cat {sf})\" \"$(cat {pf})\"; exit"
        ),
    );
    invalidate_list_cache();
    Ok(json!({"session": session, "title": title, "workdir": workdir, "claude_session_id": sid}))
}

/// json.dumps(meta, indent=2)
fn pretty(v: &Value) -> String {
    let compact = py::dumps(v, false);
    // re-render with indent=2 while keeping python escaping: serde's pretty
    // printer escapes differently, so build it from the object's entries.
    let Value::Object(map) = v else { return compact };
    let body: Vec<String> = map.iter().map(|(k, v)| format!("  {}: {}", py::string(k), py::dumps(v, false))).collect();
    format!("{{\n{}\n}}", body.join(",\n"))
}

pub fn kill(session: &str) -> Result<(), String> {
    let owner = py::strip(&tmux(&["show-option", "-t", session, "-qv", "@hq_owner"]).stdout).to_string();
    if !mine(session, &owner) {
        return Err(format!("refusing to kill a session not owned by this instance (prefix {})", *PREFIX));
    }
    // a deliberate kill tombstones the registry entry: never revived after reboot
    let (sid, _) = session_opts(session);
    if !sid.is_empty() && SESSIONS_DIR.join(&sid).is_dir() {
        let _ = std::fs::OpenOptions::new().create(true).append(true).open(SESSIONS_DIR.join(&sid).join(".done"));
    }
    tmux(&["kill-session", "-t", session]);
    let _ = std::fs::remove_file(format!("{PROMPT_DIR}/{session}.md"));
    invalidate_list_cache();
    Ok(())
}

static WEB_URL_CACHE: LazyLock<Mutex<HashMap<String, String>>> = LazyLock::new(Default::default);

/// claude.ai/code URL once the worker's remote-control bridge id lands in its jsonl.
fn web_url(session: &str, sid: &str, wd: &str) -> Option<String> {
    if let Some(u) = WEB_URL_CACHE.lock().unwrap().get(session) {
        return Some(u.clone());
    }
    if sid.is_empty() || wd.is_empty() {
        return None;
    }
    static BRIDGE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#""bridgeSessionId"\s*:\s*"cse_([^"]+)""#).unwrap());
    let bytes = std::fs::read(jsonl_path(sid, wd)).ok()?;
    let text = String::from_utf8_lossy(&bytes);
    let token = text
        .split('\n')
        .filter(|l| l.contains("\"bridgeSessionId\""))
        .filter_map(|l| BRIDGE.captures(l).map(|c| c[1].to_string()))
        .last()?;
    let url = format!("https://claude.ai/code/session_{token}");
    WEB_URL_CACHE.lock().unwrap().insert(session.into(), url.clone());
    Some(url)
}

// list_workers is the polling hot path: one capture-pane per worker plus a 2 s
// TTL cache so concurrent pollers coalesce; a tmux stall serves the last snapshot.
const LIST_TTL_S: f64 = 2.0;
struct ListCache {
    at: f64,
    tail_lines: usize,
    workers: Option<Vec<Value>>,
}
static LIST_CACHE: LazyLock<Mutex<ListCache>> =
    LazyLock::new(|| Mutex::new(ListCache { at: 0.0, tail_lines: 0, workers: None }));

fn list_workers_uncached(tail_lines: usize) -> Result<Vec<Value>, RunError> {
    let cmds = pane_cmds();
    let mut ss: Vec<Session> = sessions_full().into_iter().filter(|s| mine(&s.name, &s.owner)).collect();
    ss.sort_by(|a, b| b.created.cmp(&a.created)); // stable, like Python's sorted(reverse=True)
    let mut out = Vec::new();
    for s in ss {
        let age = jsonl_age(&s.sid, &s.workdir);
        let (status, tail) = match capture(&s.name, tail_lines.max(40)) {
            Ok(pane) => {
                let cmd = py::strip(cmds.get(&s.name).map(String::as_str).unwrap_or("")).to_lowercase();
                let lines = py::splitlines(&pane);
                let tail = lines[lines.len().saturating_sub(tail_lines)..].join("\n");
                (classify(&cmd, &pane, age), py::rstrip(&tail).to_string())
            }
            Err(_) => ("unknown", String::new()), // one stuck pane must not 500 the list
        };
        let none_if_empty = |v: &str| if v.is_empty() { Value::Null } else { Value::from(v) };
        out.push(json!({
            "session": s.name,
            "title": if s.title.is_empty() { &s.name } else { &s.title },
            "kind": if s.kind.is_empty() { "task" } else { &s.kind },
            "status": status,
            "attached": s.attached,
            "age_s": py::now() as i64 - s.created,
            "tail": tail,
            "web_url": web_url(&s.name, &s.sid, &s.workdir),
            "jsonl_age_s": age,
            "claude_session_id": none_if_empty(&s.sid),
            "workdir": none_if_empty(&s.workdir),
        }));
    }
    Ok(out)
}

pub fn list_workers() -> Result<Vec<Value>, String> {
    let tail_lines = 14;
    let mut c = LIST_CACHE.lock().unwrap();
    let fresh = py::now() - c.at < LIST_TTL_S && c.tail_lines == tail_lines;
    if !fresh {
        match list_workers_uncached(tail_lines) {
            Ok(w) => {
                c.workers = Some(w);
                c.at = py::now();
                c.tail_lines = tail_lines;
            }
            Err(_) if c.workers.is_some() && c.tail_lines == tail_lines => {} // serve stale
            Err(_) => return Err("tmux timed out".into()),
        }
    }
    Ok(c.workers.clone().unwrap_or_default())
}

pub fn invalidate_list_cache() {
    LIST_CACHE.lock().unwrap().at = 0.0;
}

/// Revive registered task workers whose tmux session is gone (after a reboot).
pub fn resume_registered() -> Vec<String> {
    let Ok(entries) = std::fs::read_dir(&*SESSIONS_DIR) else { return Vec::new() };
    let mut metas: Vec<PathBuf> = entries.flatten().map(|e| e.path().join("meta.json")).filter(|p| p.is_file()).collect();
    metas.sort();
    let live: Vec<String> = sessions().into_iter().map(|s| s.name).collect();
    let cutoff = py::now() - RESUME_MAX_DAYS * 86400.0;
    let (mut revived, mut loops) = (Vec::new(), Vec::new());
    for meta_f in metas {
        let reg = meta_f.parent().unwrap();
        if reg.join(".done").exists() {
            continue;
        }
        let Some(meta) = std::fs::read_to_string(&meta_f).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()) else {
            continue;
        };
        let (Some(session), Some(sid)) = (meta["session"].as_str(), meta["sid"].as_str()) else { continue };
        if session.is_empty() || sid.is_empty() || live.iter().any(|l| l == session) {
            continue;
        }
        let wd = meta["workdir"].as_str().filter(|w| !w.is_empty()).unwrap_or(DEFAULT_WORKDIR.as_str());
        if meta["created"].as_f64().unwrap_or(0.0) < cutoff || !jsonl_path(sid, wd).exists() {
            continue;
        }
        if !tmux(&["new-session", "-d", "-s", session, "-c", wd]).ok() {
            continue;
        }
        stamp(session, meta["title"].as_str().unwrap_or(session), sid, wd, meta["kind"].as_str().unwrap_or("task"));
        let _ = std::fs::create_dir_all(PROMPT_DIR); // /tmp is wiped on reboot
        let sf = format!("{PROMPT_DIR}/_headless.txt");
        let _ = std::fs::write(&sf, HEADLESS_SYSTEM_PROMPT);
        send_text(
            session,
            &format!(
                "claude --dangerously-skip-permissions --disallowedTools {BLOCKED_TOOLS} --resume {sid} \
--remote-control {session} --append-system-prompt \"$(cat {sf})\""
            ),
        );
        revived.push(session.to_string());
        if py::truthy(&meta["loop"]) {
            loops.push(session.to_string());
        }
        std::thread::sleep(Duration::from_secs(2));
    }
    if !loops.is_empty() {
        std::thread::sleep(Duration::from_secs(10));
        for s in &loops {
            send_text(s, "continue");
        }
    }
    revived
}

/// Kill our workers of `kind` older than max_age_hours.
pub fn reap_stale(kind: &str, max_age_hours: f64) -> Vec<String> {
    let cutoff = py::now() - max_age_hours * 3600.0;
    let mut reaped = Vec::new();
    for s in sessions() {
        if !mine(&s.name, &s.owner) || s.created as f64 >= cutoff {
            continue;
        }
        let k = if s.kind.is_empty() { "task" } else { s.kind.as_str() };
        if k == kind && kill(&s.name).is_ok() {
            reaped.push(s.name);
        }
    }
    reaped
}

/// Delete tombstoned session dirs older than GC_DONE_DAYS.
pub fn registry_gc() -> Vec<String> {
    let Ok(entries) = std::fs::read_dir(&*SESSIONS_DIR) else { return Vec::new() };
    let cutoff = py::now() - GC_DONE_DAYS * 86400.0;
    let mut removed = Vec::new();
    for dir in entries.flatten().map(|e| e.path()) {
        let done = dir.join(".done");
        let Ok(m) = std::fs::metadata(&done).and_then(|m| m.modified()) else { continue };
        let mtime = m.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
        if mtime < cutoff {
            let _ = std::fs::remove_dir_all(&dir);
            removed.push(dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default());
        }
    }
    removed
}

/// Read-only view of sessions this instance does not own.
pub fn list_others() -> Vec<Value> {
    let cmds = pane_cmds();
    sessions()
        .into_iter()
        .filter(|s| !mine(&s.name, &s.owner))
        .map(|s| json!({"session": s.name, "attached": s.attached, "command": cmds.get(&s.name).cloned().unwrap_or_default()}))
        .collect()
}
