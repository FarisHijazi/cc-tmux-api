//! Port of fleet.py — configured local/SSH/peer aggregation, cascade routing,
//! cycle checks, per-target action locks and process-local receipts.
//!
//! The host probe stays Python on purpose: fleet_probe.py is shipped verbatim to
//! `python3 -` (locally and over SSH), so the Rust coordinator embeds the same
//! source files at compile time instead of re-implementing pane inspection.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

use base64::Engine;
use base64::engine::general_purpose::{URL_SAFE, URL_SAFE_NO_PAD};
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::history::SessionHistory;
use crate::{amux, proc, py};

const PROBE_SOURCE: &str = include_str!("../../src/cc_tmux_api/fleet_probe.py");
const AMUX_SOURCE: &str = include_str!("../../src/cc_tmux_api/vendor_amux.py");
const NATIVE_HOST_ID: &str = "@native";

#[derive(Debug, Clone)]
pub struct FleetError {
    pub message: String,
    pub status: u16,
}

pub fn err<T>(message: impl Into<String>, status: u16) -> Result<T, FleetError> {
    Err(FleetError { message: message.into(), status })
}

type R<T> = Result<T, FleetError>;

static ID_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\A[A-Za-z0-9_.:-]{1,128}\z").unwrap());

pub fn node_id() -> R<String> {
    let value = std::env::var("CC_TMUX_NODE_ID").ok().filter(|v| !v.is_empty()).unwrap_or_else(hostname);
    if !ID_RE.is_match(&value) {
        return err("CC_TMUX_NODE_ID must be a stable node identifier", 500);
    }
    Ok(value)
}

fn hostname() -> String {
    proc::run("hostname", &[], None, Duration::from_secs(5)).map(|o| o.stdout.trim().to_string()).unwrap_or_default()
}

pub fn parse_path(value: &[String]) -> R<Vec<String>> {
    if value.len() > 8 || value.iter().any(|v| !ID_RE.is_match(v)) {
        return err("Invalid or excessive HQ path", 508);
    }
    Ok(value.to_vec())
}

fn context(path: &[String]) -> R<Vec<String>> {
    let mut path = parse_path(path)?;
    let own = node_id()?;
    let unique: std::collections::HashSet<&String> = path.iter().collect();
    if path.len() >= 8 || path.contains(&own) || unique.len() != path.len() {
        return err("HQ federation cycle or eight-hop limit reached", 508);
    }
    path.push(own);
    Ok(path)
}

pub fn hosts() -> R<Vec<Map<String, Value>>> {
    let raw = std::env::var("CC_TMUX_HOSTS").unwrap_or_else(|_| r#"[{"id":"local","label":"Local"}]"#.into());
    let value: Value = serde_json::from_str(&raw).map_err(|_| FleetError { message: "CC_TMUX_HOSTS must be a JSON array".into(), status: 500 })?;
    let Some(list) = value.as_array().filter(|l| !l.is_empty() && l.len() <= 32) else {
        return err("Configure 1–32 fleet hosts", 500);
    };
    static HOST_ID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\A[a-zA-Z0-9_-]{1,64}\z").unwrap());
    static ALIAS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\A[a-zA-Z0-9_][a-zA-Z0-9_.@-]{0,253}\z").unwrap());
    let mut seen = Vec::new();
    let mut result = Vec::new();
    for host in list {
        let Some(h) = host.as_object() else { return err("Invalid or duplicate fleet host configuration", 500) };
        let id = h.get("id").and_then(Value::as_str);
        let label = h.get("label").and_then(Value::as_str);
        if h.keys().any(|k| !["id", "label", "ssh", "url"].contains(&k.as_str()))
            || !id.is_some_and(|i| HOST_ID.is_match(i))
            || seen.contains(&id.unwrap().to_string())
            || !label.is_some_and(|l| !l.is_empty())
        {
            return err("Invalid or duplicate fleet host configuration", 500);
        }
        if h.contains_key("ssh") && h.contains_key("url") {
            return err("A fleet host cannot configure both ssh and url", 500);
        }
        if let Some(alias) = h.get("ssh") {
            if !alias.is_null() && !alias.as_str().is_some_and(|a| ALIAS.is_match(a)) {
                return err("ssh must be a configured SSH alias, not options or a command", 500);
            }
        }
        if let Some(url) = h.get("url") {
            if !url.as_str().is_some_and(valid_origin) {
                return err("Peer url must be an HTTP(S) origin without credentials", 500);
            }
        }
        seen.push(id.unwrap().to_string());
        result.push(h.clone());
    }
    Ok(result)
}

fn valid_origin(value: &str) -> bool {
    static ORIGIN: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\A(?i:https?)://(\[[0-9A-Fa-f:.]+\]|[^\s/@:?#\[\]]+)(:\d{0,5})?/?\z").unwrap());
    if value.chars().any(|c| c.is_whitespace() || (c as u32) < 32) {
        return false;
    }
    match ORIGIN.captures(value) {
        Some(c) => c.get(2).is_none_or(|p| p.as_str().len() == 1 || p.as_str()[1..].parse::<u32>().is_ok_and(|n| n <= 65535)),
        None => false,
    }
}

fn is_local(h: &Map<String, Value>) -> bool {
    !h.contains_key("url")
}

fn s<'a>(h: &'a Map<String, Value>, k: &str) -> &'a str {
    h.get(k).and_then(Value::as_str).unwrap_or("")
}

// ---------- transport ----------

fn peer(host: &Map<String, Value>, endpoint: &str, path: &[String], payload: Option<&Value>) -> R<Map<String, Value>> {
    let url = format!("{}{endpoint}", s(host, "url").trim_end_matches('/'));
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .max_redirects(0)
        .http_status_as_error(false)
        .timeout_global(Some(Duration::from_secs(20)))
        .build()
        .into();
    let hq_path = path.join(",");
    let sent = match payload {
        Some(body) => agent
            .post(&url)
            .header("X-HQ-Path", &hq_path)
            .header("Accept", "application/json")
            .header("Content-Type", "application/json")
            .send(py::dumps(body, false)),
        None => agent
            .get(&url)
            .header("X-HQ-Path", &hq_path)
            .header("Accept", "application/json")
            .header("Content-Type", "application/json")
            .call(),
    };
    let unavailable = || FleetError { message: "Peer unavailable; action delivery may be unknown. Do not retry.".into(), status: 503 };
    let mut response = sent.map_err(|_| unavailable())?;
    let code = response.status().as_u16();
    let raw = response.body_mut().with_config().limit(8 * 1024 * 1024 + 1).read_to_vec();
    if code >= 300 {
        let mut message = format!("Peer returned HTTP {code}; redirects are not followed");
        if let Some(detail) = raw.ok().and_then(|b| serde_json::from_slice::<Value>(&b[..b.len().min(16384)]).ok()) {
            if let Some(d) = detail.get("detail").and_then(Value::as_str) {
                message = d.to_string();
            }
        }
        return err(message, if (400..=599).contains(&code) { code } else { 502 });
    }
    let raw = match raw {
        Ok(b) if b.len() > 8 * 1024 * 1024 => return err("Peer response exceeded size limit; delivery may be unknown", 502),
        Ok(b) => b,
        Err(ureq::Error::BodyExceedsLimit(_)) => return err("Peer response exceeded size limit; delivery may be unknown", 502),
        Err(_) => return Err(unavailable()),
    };
    match serde_json::from_slice::<Value>(&raw) {
        Ok(Value::Object(m)) => Ok(m),
        _ => err("Invalid peer response; action delivery may be unknown", 502),
    }
}

/// cc-notify's cc-lib.sh (located like fleet_probe.notify_library).
fn notify_library() -> Option<PathBuf> {
    if let Some(helper) = py::which("cc-prompt-state") {
        if let Ok(real) = std::fs::canonicalize(helper) {
            let candidate = real.parent()?.parent()?.join("hooks/cc-lib.sh");
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    let registry = py::home().join(".claude/plugins/installed_plugins.json");
    if let Some(data) = std::fs::read_to_string(registry).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()) {
        if let Some(plugins) = data.get("plugins").and_then(Value::as_object) {
            for (name, installs) in plugins {
                if name.split('@').next() == Some("cc-notify") {
                    for install in installs.as_array().into_iter().flatten() {
                        if let Some(p) = install.get("installPath").and_then(Value::as_str) {
                            let candidate = PathBuf::from(p).join("hooks/cc-lib.sh");
                            if candidate.is_file() {
                                return Some(candidate);
                            }
                        }
                    }
                }
            }
        }
    }
    let candidate = py::home().join(".claude/hooks/cc-lib.sh");
    candidate.is_file().then_some(candidate)
}

fn probe(host: &Map<String, Value>, request: &Value) -> R<Value> {
    let payload = URL_SAFE.encode(py::dumps(request, false));
    // Reuse the installed cc-notify reader verbatim on hosts without its CLI.
    let prompt_source = notify_library()
        .and_then(|lib| lib.parent()?.parent().map(|p| p.join("bin/cc-prompt-state")))
        .filter(|h| h.is_file())
        .and_then(|h| std::fs::read_to_string(h).ok())
        .unwrap_or_default();
    let stdin = format!(
        "{AMUX_SOURCE}\nPROMPT_STATE_SOURCE = {}\n{PROBE_SOURCE}",
        serde_json::to_string(&prompt_source).unwrap() // a JSON string is a valid Python literal
    );
    let remote = format!("python3 - {payload}"); // shlex.join: payload is base64url, never needs quoting
    let output = match host.get("ssh").and_then(Value::as_str) {
        Some(alias) => proc::run(
            "ssh",
            &["-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1", "--", alias, &remote],
            Some(&stdin),
            Duration::from_secs(20),
        ),
        None => proc::run(&python(), &["-", &payload], Some(&stdin), Duration::from_secs(20)),
    };
    let Ok(output) = output else { return err("Host unavailable or timed out; action delivery may be unknown", 503) };
    if output.code != 0 {
        let e: String = output.stderr.trim().chars().take(300).collect();
        return err(format!("Host probe failed: {e}"), 503);
    }
    let invalid = || FleetError { message: "Host returned an invalid probe response; delivery may be unknown".into(), status: 503 };
    let response: Value = serde_json::from_str(&output.stdout).map_err(|_| invalid())?;
    let obj = response.as_object().ok_or_else(invalid)?;
    if let Some(e) = obj.get("error") {
        let status = obj.get("status").and_then(Value::as_u64).unwrap_or(503) as u16;
        return err(e.as_str().map(String::from).unwrap_or_else(|| e.to_string()), status);
    }
    obj.get("result").cloned().ok_or_else(invalid)
}

/// Interpreter for the local probe (Python used sys.executable).
fn python() -> String {
    std::env::var("CC_TMUX_PYTHON").unwrap_or_else(|_| "python3".into())
}

// ---------- opaque IDs ----------

fn encode(host_id: &str, identity: &Value) -> String {
    URL_SAFE_NO_PAD.encode(py::dumps(&json!([host_id, identity]), true))
}

enum Target {
    Peer(Map<String, Value>, String),
    Local(Map<String, Value>, Value),
}

fn resolve(identifier: &str) -> R<Target> {
    let invalid = || FleetError { message: "Invalid session ID".into(), status: 400 };
    if identifier.len() > 32768 {
        return Err(invalid());
    }
    let padded = format!("{identifier}{}", "=".repeat((4 - identifier.len() % 4) % 4));
    let bytes = URL_SAFE.decode(padded).map_err(|_| invalid())?;
    let decoded: Value = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
    let (host_id, identity) = match decoded.as_array().map(Vec::as_slice) {
        Some([Value::String(h), identity]) => (h.clone(), identity.clone()),
        _ => return Err(invalid()),
    };
    if host_id == NATIVE_HOST_ID {
        static CSE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\Acse_[A-Za-z0-9_-]{1,128}\z").unwrap());
        let ok = identity.as_object().is_some_and(|o| o.len() == 1 && o.get("native").and_then(Value::as_str).is_some_and(|n| CSE.is_match(n)));
        if !ok {
            return err("Invalid native session route", 400);
        }
        return err("Native Remote Control is unavailable", 503); // the Rust port has no native backend
    }
    let Some(host) = hosts()?.into_iter().find(|h| s(h, "id") == host_id) else { return err("Host is not configured", 404) };
    if !is_local(&host) {
        let peer = identity.as_object().filter(|o| o.len() == 1).and_then(|o| o.get("peer")).and_then(Value::as_str);
        return match peer {
            Some(p) if (1..=24576).contains(&p.chars().count()) => Ok(Target::Peer(host, p.to_string())),
            _ => err("Invalid peer session route", 400),
        };
    }
    let ok = identity.as_array().is_some_and(|a| {
        a.len() == 6
            && [0, 1, 2, 3, 5].iter().all(|&i| a[i].is_string())
            && a[4].is_i64()
            && Regex::new(r"\A%\d+\z").unwrap().is_match(a[1].as_str().unwrap())
    });
    if !ok {
        return Err(invalid());
    }
    Ok(Target::Local(host, identity))
}

fn decorate(host: &Map<String, Value>, item: &Value) -> R<Value> {
    let mut result = item.as_object().cloned().unwrap_or_default();
    let identity = result.remove("identity").unwrap_or(Value::Null);
    let identifier = encode(s(host, "id"), &identity);
    let node = node_id()?;
    for (k, v) in [
        ("id", json!(identifier)),
        ("host", json!(s(host, "id"))),
        ("host_label", json!(s(host, "label"))),
        ("host_path", json!([s(host, "id")])),
        ("owner_node_id", json!(node)),
        ("owner_session_id", json!(identifier)),
        ("origin_id", json!(format!("{node}:{identifier}"))),
        ("federated", json!(false)),
        ("lifecycle", json!("alive")),
        ("alive", json!(true)),
    ] {
        result.insert(k.into(), v);
    }
    if result.get("status").and_then(Value::as_str) == Some("unknown") {
        if let Some(tail) = result.get("tail") {
            let status = match amux::detect_claude_status(tail.as_str().unwrap_or("")) {
                "active" => "working",
                "waiting" => "waiting",
                "idle" => "idle",
                _ => "unknown",
            };
            result.insert("status".into(), json!(status));
        }
    }
    let status = result.get("status").cloned().unwrap_or(json!("unknown"));
    let activity = if status == "working" { json!("running") } else { status };
    result.insert("activity".into(), activity);
    Ok(Value::Object(result))
}

fn forward_row(host: &Map<String, Value>, item: &Value, peer_node: Option<&Value>) -> Value {
    let mut result = item.as_object().cloned().unwrap_or_default();
    let original = result.get("id").cloned().unwrap_or(Value::Null);
    let original_s = original.as_str().unwrap_or("").to_string();
    let item_host = item.get("host").filter(|v| py::truthy(v)).cloned().unwrap_or(json!("local"));
    result.insert("id".into(), json!(encode(s(host, "id"), &json!({"peer": original}))));
    result.insert("host".into(), json!(format!("{}/{}", s(host, "id"), py::str_of(&item_host))));
    let mut host_path = vec![json!(s(host, "id"))];
    match result.get("host_path").and_then(Value::as_array) {
        Some(p) => host_path.extend(p.iter().cloned()),
        None => host_path.push(item_host.clone()),
    }
    result.insert("host_path".into(), json!(host_path));
    let label = result
        .get("host_label")
        .filter(|v| py::truthy(v))
        .or_else(|| item.get("host").filter(|v| py::truthy(v)))
        .cloned()
        .unwrap_or(json!("Local"));
    result.insert("host_label".into(), json!(format!("{} / {}", s(host, "label"), py::str_of(&label))));
    result.insert("federated".into(), json!(true));
    let peer_node = peer_node.cloned().unwrap_or(Value::Null);
    result.entry("owner_node_id").or_insert(peer_node.clone());
    result.entry("owner_session_id").or_insert(original.clone());
    let origin_prefix = if py::truthy(&peer_node) { py::str_of(&peer_node) } else { s(host, "url").to_string() };
    result.entry("origin_id").or_insert(json!(format!("{origin_prefix}:{original_s}")));
    Value::Object(result)
}

// ---------- snapshots ----------

fn host_snapshot(host: &Map<String, Value>) -> (Value, Vec<Value>) {
    let mut status = json!({"id": s(host, "id"), "label": s(host, "label"), "available": false, "updated_at": py::now(),
                            "lifecycle": "unreachable", "host_path": [s(host, "id")], "federated": false});
    let result = probe(host, &json!({"op": "snapshot"})).and_then(|r| {
        r.get("sessions")
            .and_then(Value::as_array)
            .ok_or(FleetError { message: "Host returned an invalid probe response; delivery may be unknown".into(), status: 503 })?
            .iter()
            .map(|item| decorate(host, item))
            .collect::<R<Vec<_>>>()
    });
    match result {
        Ok(sessions) => {
            status["available"] = json!(true);
            status["lifecycle"] = json!("alive");
            (status, sessions)
        }
        Err(e) => {
            status["error"] = json!(e.message);
            status["error_status"] = json!(e.status);
            (status, Vec::new())
        }
    }
}

struct Cache {
    history: Value,
    peers: HashMap<(String, String, String), (Vec<Value>, Vec<Value>)>,
    entries: Vec<(String, Instant, Value)>,
}

static CACHE: LazyLock<Mutex<Cache>> =
    LazyLock::new(|| Mutex::new(Cache { history: json!({}), peers: HashMap::new(), entries: Vec::new() }));
static LOCAL_OBSERVATION: Mutex<()> = Mutex::new(());
static HISTORIES: LazyLock<Mutex<HashMap<(String, PathBuf), Arc<SessionHistory>>>> = LazyLock::new(Default::default);

pub fn state_dir() -> PathBuf {
    std::env::var("CC_TMUX_STATE_DIR").map(PathBuf::from).unwrap_or_else(|_| py::home().join(".local/state/cc-tmux-api"))
}

fn observe_local(value: Value) -> R<Value> {
    let key = (node_id()?, state_dir());
    let history = {
        let mut map = HISTORIES.lock().unwrap();
        match map.get(&key) {
            Some(h) => h.clone(),
            None => {
                let h = Arc::new(SessionHistory::new(&key.1).map_err(|e| FleetError { message: e, status: 500 })?);
                map.insert(key, h.clone());
                h
            }
        }
    };
    history.observe(&value).map_err(|e| FleetError { message: e, status: 500 })
}

fn local_snapshot(config: &[Map<String, Value>]) -> R<Value> {
    // Serialize the whole census so an older enumeration never commits after a newer one.
    let _guard = LOCAL_OBSERVATION.lock().unwrap();
    let local: Vec<&Map<String, Value>> = config.iter().filter(|h| is_local(h)).collect();
    let results: Vec<(Value, Vec<Value>)> = std::thread::scope(|scope| {
        let handles: Vec<_> = local.iter().map(|h| scope.spawn(move || host_snapshot(h))).collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    let hosts: Vec<Value> = results.iter().map(|r| r.0.clone()).collect();
    let sessions: Vec<Value> = results.into_iter().flat_map(|r| r.1).collect();
    let result = observe_local(json!({"hosts": hosts, "sessions": sessions, "updated_at": py::now()}))?;
    CACHE.lock().unwrap().history = result.clone();
    Ok(result)
}

fn peer_snapshot(host: &Map<String, Value>, ctx: &[String]) -> R<(Vec<Value>, Vec<Value>)> {
    let mut root = json!({"id": s(host, "id"), "label": s(host, "label"), "host_path": [s(host, "id")],
                          "available": false, "lifecycle": "unreachable", "updated_at": py::now(), "federated": true});
    let key = (node_id()?, s(host, "id").to_string(), s(host, "url").to_string());
    let attempt = || -> R<(Vec<Value>, Vec<Value>)> {
        let response = peer(host, "/api/sessions", ctx, None)?;
        let (Some(child_hosts), Some(sessions)) =
            (response.get("hosts").and_then(Value::as_array), response.get("sessions").and_then(Value::as_array))
        else {
            return err("Invalid peer snapshot", 502);
        };
        let bad = || FleetError { message: "Invalid peer snapshot".into(), status: 502 };
        let mut children = Vec::new();
        for child in child_hosts {
            let mut row = child.as_object().cloned().ok_or_else(bad)?;
            let cid = py::str_of(child.get("id").ok_or_else(bad)?);
            let clabel = py::str_of(child.get("label").ok_or_else(bad)?);
            let mut hp = vec![json!(s(host, "id"))];
            match child.get("host_path").and_then(Value::as_array) {
                Some(p) => hp.extend(p.iter().cloned()),
                None => hp.push(json!(cid)),
            }
            row.insert("id".into(), json!(format!("{}/{cid}", s(host, "id"))));
            row.insert("label".into(), json!(format!("{} / {clabel}", s(host, "label"))));
            row.insert("host_path".into(), json!(hp));
            row.insert("federated".into(), json!(true));
            children.push(Value::Object(row));
        }
        let node = response.get("node_id");
        let rows: Vec<Value> = sessions.iter().map(|sess| forward_row(host, sess, node)).collect();
        let mut root = root.clone();
        root["available"] = json!(true);
        root["lifecycle"] = json!("alive");
        root["node_id"] = node.cloned().unwrap_or(Value::Null);
        let mut all = vec![root];
        all.extend(children);
        Ok((all, rows))
    };
    match attempt() {
        Ok(value) => {
            CACHE.lock().unwrap().peers.insert(key, value.clone());
            Ok(value)
        }
        Err(e) => {
            root["error"] = json!(e.message);
            root["error_status"] = json!(e.status);
            // A reachability cache, not a second history store.
            let old = CACHE.lock().unwrap().peers.get(&key).cloned();
            let Some((old_hosts, mut old_sessions)) = old else { return Ok((vec![root], Vec::new())) };
            let mut all = vec![root.clone()];
            for mut child in old_hosts.into_iter().skip(1) {
                child["available"] = json!(false);
                child["lifecycle"] = json!("unreachable");
                child["error"] = root["error"].clone();
                all.push(child);
            }
            for sess in &mut old_sessions {
                if sess.get("lifecycle").and_then(Value::as_str) != Some("ended") {
                    sess["lifecycle"] = json!("unreachable");
                    sess["alive"] = Value::Null;
                }
                sess["stale"] = json!(true);
                sess["online"] = json!(false);
            }
            Ok((all, old_sessions))
        }
    }
}

pub fn snapshot(path: &[String]) -> R<Value> {
    let ctx = context(path)?; // before any cache/target lock or network IO
    let config = if std::env::var("CC_TMUX_NATIVE_ONLY").as_deref() == Ok("1") { Vec::new() } else { hosts()? };
    let cache_key = format!("{}|{}", ctx.join(","), serde_json::to_string(&config).unwrap());
    {
        let cache = CACHE.lock().unwrap();
        if let Some((_, at, value)) = cache.entries.iter().find(|(k, _, _)| *k == cache_key) {
            if at.elapsed() < Duration::from_secs(3) {
                return Ok(value.clone());
            }
        }
    }
    // Never hold a shared cache lock across a network request.
    let peers: Vec<&Map<String, Value>> = config.iter().filter(|h| !is_local(h)).collect();
    let (local, results) = std::thread::scope(|scope| {
        let local = scope.spawn(|| local_snapshot(&config));
        let handles: Vec<_> = peers.iter().map(|h| scope.spawn(|| peer_snapshot(h, &ctx))).collect();
        let results: Vec<R<(Vec<Value>, Vec<Value>)>> = handles.into_iter().map(|h| h.join().unwrap()).collect();
        (local.join().unwrap(), results)
    });
    let local = local?;
    let results = results.into_iter().collect::<R<Vec<_>>>()?;
    let mut sessions: Vec<Value> = local["sessions"].as_array().cloned().unwrap_or_default();
    sessions.extend(results.iter().flat_map(|r| r.1.iter().cloned()));
    let mut order: Vec<String> = Vec::new();
    let mut unique: HashMap<String, Value> = HashMap::new();
    for sess in sessions {
        let key = sess.get("origin_id").filter(|v| py::truthy(v)).or_else(|| sess.get("id")).map(py::str_of).unwrap_or_default();
        let replace = match unique.get(&key) {
            None => {
                order.push(key.clone());
                true
            }
            Some(prev) => {
                prev.get("lifecycle").and_then(Value::as_str) == Some("unreachable")
                    && sess.get("lifecycle").and_then(Value::as_str) != Some("unreachable")
            }
        };
        if replace {
            unique.insert(key, sess);
        }
    }
    let mut all_hosts: Vec<Value> = local["hosts"].as_array().cloned().unwrap_or_default();
    all_hosts.extend(results.iter().flat_map(|r| r.0.iter().cloned()));
    let result = json!({
        "hosts": all_hosts,
        "sessions": order.into_iter().map(|k| unique.remove(&k).unwrap()).collect::<Vec<_>>(),
        "updated_at": py::now(),
        "node_id": node_id()?,
    });
    let mut cache = CACHE.lock().unwrap();
    cache.entries.retain(|(k, _, _)| *k != cache_key);
    if cache.entries.len() >= 128 {
        cache.entries.remove(0);
    }
    cache.entries.push((cache_key, Instant::now(), result.clone()));
    Ok(result)
}

fn ended_summary(identifier: &str, refresh: bool) -> R<Option<Value>> {
    let value = if refresh { local_snapshot(&hosts()?)? } else { CACHE.lock().unwrap().history.clone() };
    let found = value.get("sessions").and_then(Value::as_array).and_then(|list| {
        list.iter()
            .find(|sess| sess.get("id").and_then(Value::as_str) == Some(identifier) && sess.get("lifecycle").and_then(Value::as_str) == Some("ended"))
            .cloned()
    });
    Ok(found.map(|mut sess| {
        let tail = sess.get("tail").filter(|v| py::truthy(v)).cloned().unwrap_or(json!(""));
        let messages = sess.get("messages").filter(|v| py::truthy(v)).cloned().unwrap_or(json!([]));
        sess["read_only"] = json!(true);
        sess["tail"] = tail;
        sess["messages"] = messages;
        sess["menu"] = Value::Null;
        sess
    }))
}

pub fn detail(id: &str, path: &[String]) -> R<Value> {
    let ctx = context(path)?;
    match resolve(id)? {
        Target::Peer(host, peer_id) => {
            let result = peer(&host, &format!("/api/sessions/{}", quote(&peer_id)), &ctx, None)?;
            if result.get("id").and_then(Value::as_str) != Some(peer_id.as_str()) {
                return err("Peer returned a different session identity", 502);
            }
            let node = result.get("owner_node_id").cloned();
            Ok(forward_row(&host, &Value::Object(result), node.as_ref()))
        }
        Target::Local(host, identity) => {
            if let Some(ended) = ended_summary(id, false)? {
                return Ok(ended);
            }
            match probe(&host, &json!({"op": "detail", "identity": identity})) {
                Ok(item) => decorate(&host, &item),
                Err(e) if e.status == 409 => match ended_summary(id, true)? {
                    Some(ended) => Ok(ended),
                    None => Err(e),
                },
                Err(e) => Err(e),
            }
        }
    }
}

/// urllib.parse.quote(value, safe="")
fn quote(value: &str) -> String {
    value
        .bytes()
        .map(|b| if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) { (b as char).to_string() } else { format!("%{b:02X}") })
        .collect()
}

struct Receipt {
    payload: String,
    result: Option<Value>,
    error: Option<FleetError>,
}

static LOCKS: LazyLock<Mutex<HashMap<String, Arc<Mutex<()>>>>> = LazyLock::new(Default::default);
static RECEIPTS: LazyLock<Mutex<HashMap<(String, String), Receipt>>> = LazyLock::new(Default::default);

#[derive(Default)]
pub struct ActArgs {
    pub action: String,
    pub revision: Value,
    pub request_id: Value,
    pub text: Option<String>,
    pub key: Option<String>,
    pub index: Option<i64>,
    pub response: Option<Value>,
}

pub fn act(id: &str, a: ActArgs, path: &[String]) -> R<Value> {
    let ctx = context(path)?;
    let target = resolve(id)?;
    if matches!(target, Target::Local(..)) && ended_summary(id, false)?.is_some() {
        return err("Session has ended and is read-only", 409);
    }
    let request_id = a.request_id.as_str().filter(|r| (1..=128).contains(&r.chars().count()));
    let Some(request_id) = request_id else { return err("request_id must contain 1–128 characters", 400) };
    if !a.revision.as_str().is_some_and(|r| !r.is_empty()) {
        return err("revision is required", 400);
    }
    let (host, lock_key, identity) = match &target {
        Target::Peer(h, p) => (h, format!("{}\u{0}{p}", s(h, "url")), json!({"peer": p})),
        Target::Local(h, ident) => (
            h,
            format!("{}\u{0}{}\u{0}{}", h.get("ssh").and_then(Value::as_str).unwrap_or("local"), py::str_of(&ident[0]), py::str_of(&ident[1])),
            ident.clone(),
        ),
    };
    let mut request = json!({"op": "act", "identity": identity, "action": a.action, "revision": a.revision,
                             "request_id": request_id, "text": a.text, "key": a.key, "index": a.index});
    if let Some(r) = &a.response {
        request["response"] = r.clone();
    }
    let fingerprint = py::dumps(&request, true);
    let receipt_key = (s(host, "id").to_string(), request_id.to_string());
    let lock = LOCKS.lock().unwrap().entry(lock_key).or_default().clone();
    let _held = lock.lock().unwrap();
    {
        let mut receipts = RECEIPTS.lock().unwrap();
        if let Some(prev) = receipts.get(&receipt_key) {
            if prev.payload != fingerprint {
                return err("request_id was already used with a different payload", 409);
            }
            if let Some(e) = &prev.error {
                return Err(e.clone());
            }
            if let Some(r) = &prev.result {
                return Ok(r.clone());
            }
            return err("Request is already in progress; do not resend with a new ID", 409);
        }
        if receipts.len() >= 10000 {
            return err("Control receipt capacity reached; restart required", 503);
        }
        receipts.insert(receipt_key.clone(), Receipt { payload: fingerprint, result: None, error: None });
    }
    let outcome = match &target {
        Target::Peer(h, p) => {
            let mut payload = Map::new();
            for k in ["action", "revision", "request_id", "text", "key", "index"] {
                payload.insert(k.into(), request[k].clone());
            }
            if let Some(r) = &a.response {
                payload.insert("response".into(), r.clone());
            }
            peer(h, &format!("/api/sessions/{}/action", quote(p)), &ctx, Some(&Value::Object(payload))).map(Value::Object)
        }
        Target::Local(h, _) => probe(h, &request),
    };
    CACHE.lock().unwrap().entries.clear();
    let mut receipts = RECEIPTS.lock().unwrap();
    let receipt = receipts.get_mut(&receipt_key).unwrap();
    match outcome {
        Ok(result) => {
            let mut result = match result {
                Value::Object(m) => m,
                _ => {
                    let e = FleetError { message: "Action delivery unknown; do not retry".into(), status: 503 };
                    receipt.error = Some(e.clone());
                    return Err(e);
                }
            };
            result.insert("id".into(), json!(id));
            let result = Value::Object(result);
            receipt.result = Some(result.clone());
            Ok(result)
        }
        Err(e) => {
            receipt.error = Some(e.clone());
            Err(e)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_match_python_encoding() {
        // base64.urlsafe_b64encode(json.dumps(["local", ["a", "%1", "b", "c", 7, "d"]], separators=(",", ":")).encode()).decode().rstrip("=")
        let id = encode("local", &json!(["a", "%1", "b", "c", 7, "d"]));
        assert_eq!(id, "WyJsb2NhbCIsWyJhIiwiJTEiLCJiIiwiYyIsNywiZCJdXQ");
        assert!(matches!(resolve(&id), Ok(Target::Local(..))));
    }

    #[test]
    fn origins() {
        assert!(valid_origin("http://10.0.0.2:8377"));
        assert!(valid_origin("https://hq.example.test/"));
        assert!(!valid_origin("https://u:p@hq.example.test"));
        assert!(!valid_origin("https://hq.example.test/x"));
    }
}
