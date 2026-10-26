//! Port of routines.py — scheduled workers defined as markdown files with YAML
//! frontmatter in $CC_TMUX_ROUTINES_DIR (unset = dormant). The files are the
//! source of truth; the only state is last-run per routine in <dir>/.state.json.

use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use chrono::{Datelike, Duration as CDuration, Local, TimeZone};
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::{py, workers};

static DIR: LazyLock<Option<PathBuf>> =
    LazyLock::new(|| std::env::var("CC_TMUX_ROUTINES_DIR").ok().filter(|v| !v.is_empty()).map(|v| py::expanduser(&v)));
const DAYS: [&str; 7] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

pub fn enabled() -> bool {
    DIR.is_some()
}

fn dir() -> &'static Path {
    DIR.as_deref().expect("routines enabled")
}

fn state() -> Map<String, Value> {
    std::fs::read_to_string(dir().join(".state.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

fn save_state(st: &Map<String, Value>) {
    // json.dumps(st, indent=1)
    let body: Vec<String> = st
        .iter()
        .map(|(k, v)| {
            let inner = match v.as_object() {
                Some(o) if !o.is_empty() => format!(
                    "{{\n{}\n }}",
                    o.iter().map(|(ik, iv)| format!("  {}: {}", py::string(ik), py::dumps(iv, false))).collect::<Vec<_>>().join(",\n")
                ),
                _ => py::dumps(v, false),
            };
            format!(" {}: {inner}", py::string(k))
        })
        .collect();
    let text = if body.is_empty() { "{}".into() } else { format!("{{\n{}\n}}", body.join(",\n")) };
    let _ = std::fs::write(dir().join(".state.json"), text);
}

fn last_run(st: &Map<String, Value>, name: &str) -> f64 {
    st.get(name).and_then(|r| r.get("last_run")).and_then(Value::as_f64).unwrap_or(0.0)
}

pub struct Routine {
    name: String,
    title: String,
    every: String,
    days: Vec<String>,
    enabled: bool,
    prompt: String,
    workdir: Value,
}

/// PyYAML is YAML 1.1: unquoted yes/no/on/off are booleans.
fn yaml11(v: serde_yaml::Value) -> Value {
    match v {
        serde_yaml::Value::String(s) => match s.as_str() {
            "yes" | "Yes" | "YES" | "on" | "On" | "ON" => Value::Bool(true),
            "no" | "No" | "NO" | "off" | "Off" | "OFF" => Value::Bool(false),
            _ => Value::String(s),
        },
        other => serde_json::to_value(other).unwrap_or(Value::Null),
    }
}

fn parse(path: &Path) -> Result<Option<Routine>, std::io::Error> {
    static FRONT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)\A---\n(.*?)\n---\n?(.*)\z").unwrap());
    let text = std::fs::read_to_string(path)?;
    let Some(m) = FRONT.captures(&text) else { return Ok(None) };
    let meta: serde_yaml::Value = match serde_yaml::from_str(&m[1]) {
        Ok(v) => v,
        Err(_) => return Ok(None),
    };
    let meta = match meta {
        serde_yaml::Value::Mapping(map) => map
            .into_iter()
            .filter_map(|(k, v)| k.as_str().map(|k| (k.to_string(), yaml11(v))))
            .collect::<Map<String, Value>>(),
        serde_yaml::Value::Null => Map::new(),
        _ => return Ok(None), // Python would fail on .get(); treat as unparseable
    };
    let stem = path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    let get = |k: &str| meta.get(k).cloned();
    Ok(Some(Routine {
        title: get("title").map(|v| py::str_of(&v)).unwrap_or_else(|| stem.clone()),
        every: py::strip(&get("every").map(|v| py::str_of(&v)).unwrap_or_default()).to_string(),
        days: get("days")
            .map(|v| py::str_of(&v))
            .unwrap_or_default()
            .split(',')
            .filter(|d| !py::strip(d).is_empty())
            .map(|d| py::strip(d).to_lowercase().chars().take(3).collect())
            .collect(),
        enabled: get("enabled").is_some_and(|v| py::truthy(&v)),
        prompt: py::strip(&m[2]).to_string(),
        workdir: get("workdir").unwrap_or(Value::Null),
        name: stem,
    }))
}

/// Epoch seconds of the next due run, or None if unschedulable.
fn next_run(r: &Routine, last: f64) -> Option<f64> {
    static EVERY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\A(\d+)\s*([mh])\z").unwrap());
    static DAILY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\Adaily@(\d{1,2}):(\d{2})\z").unwrap());
    if let Some(m) = EVERY.captures(&r.every) {
        let delta = m[1].parse::<f64>().ok()? * if &m[2] == "m" { 60.0 } else { 3600.0 };
        return Some(if last != 0.0 { last + delta } else { 0.0 });
    }
    let m = DAILY.captures(&r.every)?;
    let (hh, mm): (u32, u32) = (m[1].parse().ok()?, m[2].parse().ok()?);
    let today = Local::now().date_naive();
    let mut cand = Local.from_local_datetime(&today.and_hms_opt(hh, mm, 0)?).earliest()?; // ValueError → None
    for _ in 0..8 {
        let ok_day = r.days.is_empty() || r.days.iter().any(|d| d == DAYS[cand.weekday().num_days_from_monday() as usize]);
        let ts = cand.timestamp() as f64;
        if ok_day && ts > last.max(0.0) {
            return Some(ts);
        }
        cand += CDuration::days(1);
    }
    None
}

fn session_of(name: &str, live: &[String]) -> Option<String> {
    let prefix = format!("{}{}-", *workers::PREFIX, workers::routine_slug(name));
    live.iter().find(|n| n.starts_with(&prefix)).cloned()
}

fn live_names() -> Vec<String> {
    workers::sessions().into_iter().map(|s| s.name).collect()
}

fn glob_md() -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = std::fs::read_dir(dir())
        .map(|it| it.flatten().map(|e| e.path()).filter(|p| p.extension().is_some_and(|e| e == "md") && p.is_file()).collect())
        .unwrap_or_default();
    files.sort();
    files
}

pub fn list_routines() -> Vec<Value> {
    if !enabled() {
        return Vec::new();
    }
    let _ = std::fs::create_dir_all(dir());
    let st = state();
    let live = live_names();
    let mut out = Vec::new();
    for f in glob_md() {
        let Ok(Some(r)) = parse(&f) else { continue };
        let last = last_run(&st, &r.name);
        let nxt = next_run(&r, last);
        // running only while claude is still IN the pane (a done worker lingers until reaped)
        let running = session_of(&r.name, &live).filter(|s| workers::status_of(s) != "done");
        out.push(json!({
            "name": r.name, "title": r.title, "every": r.every, "days": r.days,
            "enabled": r.enabled, "workdir": r.workdir,
            "last_run": if last != 0.0 { json!(last) } else { Value::Null },
            "next_run": if r.enabled { json!(nxt) } else { Value::Null },
            "running": running,
            "file": f.to_string_lossy(),
        }));
    }
    out
}

fn workdir_arg(r: &Routine) -> Option<String> {
    r.workdir.as_str().map(String::from)
}

pub fn run_now(name: &str) -> Result<Value, String> {
    if !enabled() {
        return Err("routines disabled (CC_TMUX_ROUTINES_DIR unset)".into());
    }
    let path = dir().join(format!("{name}.md"));
    let r = parse(&path).map_err(|e| py::os_error(&e, &path))?.ok_or_else(|| format!("no routine named {name}"))?;
    let info = workers::spawn(&r.name, &r.prompt, workdir_arg(&r).as_deref(), "routine")?;
    let mut st = state();
    st.entry(r.name.clone()).or_insert_with(|| json!({}))["last_run"] = json!(py::now());
    save_state(&st);
    Ok(info)
}

/// Flip the `enabled:` line — the file stays the source of truth.
pub fn set_enabled(name: &str, on: bool) -> Result<(), String> {
    let path = dir().join(format!("{name}.md"));
    let text = std::fs::read_to_string(&path).map_err(|e| py::os_error(&e, &path))?;
    static LINE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?m)^enabled:.*$").unwrap());
    let value = format!("enabled: {on}");
    let mut new = LINE.replacen(&text, 1, regex::NoExpand(&value)).into_owned();
    if new == text && !text.contains("enabled:") {
        new = text.replacen("---\n", &format!("---\n{value}\n"), 1);
    }
    std::fs::write(&path, new).map_err(|e| py::os_error(&e, &path))
}

/// Spawn every due routine; idempotent within a minute.
pub fn tick() -> Vec<String> {
    if !enabled() || !dir().exists() {
        return Vec::new();
    }
    let mut spawned = Vec::new();
    let now = py::now();
    let mut st = state();
    for f in glob_md() {
        let Ok(Some(r)) = parse(&f) else { continue };
        if !r.enabled || r.prompt.is_empty() {
            continue;
        }
        match next_run(&r, last_run(&st, &r.name)) {
            Some(n) if n <= now => {}
            _ => continue,
        }
        if let Some(prev) = session_of(&r.name, &live_names()) {
            if workers::status_of(&prev) != "done" {
                continue; // previous run still working
            }
            let _ = workers::kill(&prev); // finished: clear it so there is one session per routine
        }
        if workers::spawn(&r.name, &r.prompt, workdir_arg(&r).as_deref(), "routine").is_err() {
            continue;
        }
        st.entry(r.name.clone()).or_insert_with(|| json!({}))["last_run"] = json!(now);
        spawned.push(r.name);
    }
    if !spawned.is_empty() {
        save_state(&st);
    }
    spawned
}
