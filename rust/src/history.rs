//! Port of session_history.py — the sole persistent lifecycle-summary owner for
//! directly probed hosts. Same SQLite file and schema, so a cutover keeps history.
//! See the Python module docstring for the full lifecycle contract.

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Map, Value, json};

use crate::py;

const RETENTION_SECONDS: f64 = 30.0 * 24.0 * 60.0 * 60.0;
const TRANSIENT: [&str; 6] = ["tail", "messages", "menu", "control", "revision", "native_control"];

pub struct SessionHistory {
    path: PathBuf,
}

fn connect(path: &Path) -> rusqlite::Result<Connection> {
    let db = Connection::open(path)?;
    db.busy_timeout(Duration::from_secs(10))?;
    Ok(db)
}

fn summary(session: &Map<String, Value>) -> String {
    let kept: Map<String, Value> = session.iter().filter(|(k, _)| !TRANSIENT.contains(&k.as_str())).map(|(k, v)| (k.clone(), v.clone())).collect();
    py::dumps(&Value::Object(kept), false)
}

impl SessionHistory {
    pub fn new(directory: &Path) -> Result<Self, String> {
        std::fs::create_dir_all(directory).map_err(|e| e.to_string())?;
        let _ = std::fs::set_permissions(directory, std::fs::Permissions::from_mode(0o700));
        let path = directory.join("session-history.sqlite3");
        std::fs::OpenOptions::new().create(true).append(true).open(&path).map_err(|e| e.to_string())?;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
        let db = connect(&path).map_err(|e| e.to_string())?;
        db.execute(
            "CREATE TABLE IF NOT EXISTS session_history (id TEXT PRIMARY KEY, host TEXT NOT NULL, summary TEXT NOT NULL, \
             last_seen REAL NOT NULL, ended_at REAL)",
            [],
        )
        .map_err(|e| e.to_string())?;
        Ok(Self { path })
    }

    /// Persist a fleet census and merge retained sessions (input is not mutated).
    pub fn observe(&self, snapshot: &Value) -> Result<Value, String> {
        let mut result = snapshot.clone();
        let hosts = snapshot.get("hosts").cloned().unwrap_or(json!([]));
        let sessions = snapshot.get("sessions");
        let Some(hosts) = hosts.as_array() else { return Err("Fleet hosts and sessions must be lists".into()) };
        if sessions.is_some_and(|s| !s.is_array() && !s.is_null()) {
            return Err("Fleet hosts and sessions must be lists".into());
        }
        let sessions = sessions.and_then(Value::as_array);
        let mut successful: Vec<String> =
            hosts.iter().filter(|h| h.get("available") == Some(&json!(true))).filter_map(|h| h.get("id").map(py::str_of)).collect();
        if sessions.is_none() || snapshot.get("stale").is_some_and(py::truthy) || snapshot.get("online") == Some(&json!(false)) {
            successful.clear();
        }
        let mut current: Vec<(String, Map<String, Value>)> = Vec::new();
        for session in sessions.into_iter().flatten() {
            let host = session.get("host").map(py::str_of).unwrap_or_default();
            if session.get("host").is_none() || !successful.contains(&host) {
                continue;
            }
            let sid = session.get("id").and_then(Value::as_str).unwrap_or("");
            if sid.is_empty() || current.iter().any(|(id, _)| id == sid) {
                return Err("Fleet session IDs must be nonempty and unique".into());
            }
            current.push((sid.to_string(), session.as_object().cloned().unwrap_or_default()));
        }
        let now = py::now();
        let mut db = connect(&self.path).map_err(|e| e.to_string())?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(|e| e.to_string())?;
        let e = |e: rusqlite::Error| e.to_string();
        for (sid, session) in current.iter_mut() {
            let host = session.get("host").map(py::str_of).unwrap_or_default();
            let native = session.get("transport").and_then(Value::as_str) == Some("native");
            if native && session.get("lifecycle").and_then(Value::as_str) != Some("alive") {
                // A socket/heartbeat outage never proves the process ended.
                let previous: Option<(f64, Option<f64>)> = tx
                    .query_row("SELECT last_seen,ended_at FROM session_history WHERE id=?", [&*sid], |r| Ok((r.get(0)?, r.get(1)?)))
                    .optional()
                    .map_err(e)?;
                let (last_seen, mut ended_at) = previous.unwrap_or((now, None));
                if session.get("lifecycle").and_then(Value::as_str) == Some("ended") && ended_at.is_none() {
                    ended_at = Some(now);
                }
                let last_activity = session.get("activity").filter(|v| py::truthy(v)).or_else(|| session.get("status")).cloned().unwrap_or(Value::Null);
                for (k, v) in [
                    ("alive", if ended_at.is_some() { json!(false) } else { Value::Null }),
                    ("lifecycle", json!(if ended_at.is_some() { "ended" } else { "unreachable" })),
                    ("last_seen", json!(last_seen)),
                    ("ended_at", json!(ended_at)),
                    ("activity", Value::Null),
                    ("last_activity", last_activity),
                    ("menu", Value::Null),
                    ("control", Value::Null),
                    ("native_control", Value::Null),
                    ("revision", Value::Null),
                    ("has_question", json!(false)),
                    ("notification_eligible", json!(false)),
                ] {
                    session.insert(k.into(), v);
                }
                tx.execute(
                    "INSERT INTO session_history VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET summary=excluded.summary, ended_at=excluded.ended_at",
                    params![&*sid, host, summary(session), last_seen, ended_at],
                )
                .map_err(e)?;
                continue;
            }
            let status = session.get("status").cloned().unwrap_or(json!("unknown"));
            let status_label = session.get("status_label").cloned().unwrap_or(Value::Null);
            for (k, v) in [
                ("alive", json!(true)),
                ("lifecycle", json!("alive")),
                ("last_seen", json!(now)),
                ("ended_at", Value::Null),
                ("last_status", status.clone()),
                ("last_status_label", status_label),
            ] {
                session.insert(k.into(), v);
            }
            let activity = session.get("activity").filter(|v| py::truthy(v)).cloned().unwrap_or(status);
            session.insert("activity".into(), activity.clone());
            session.insert("last_activity".into(), activity);
            tx.execute(
                "INSERT INTO session_history VALUES (?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET host=excluded.host, \
                 summary=excluded.summary,last_seen=excluded.last_seen,ended_at=NULL",
                params![&*sid, host, summary(session), now],
            )
            .map_err(e)?;
        }
        let stored: Vec<(String, String, String, f64, Option<f64>)> = {
            let mut stmt = tx.prepare("SELECT id,host,summary,last_seen,ended_at FROM session_history ORDER BY id").map_err(e)?;
            stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))).map_err(e)?.collect::<Result<_, _>>().map_err(e)?
        };
        let mut retained = Vec::new();
        for (sid, host, summary_text, last_seen, mut ended_at) in stored {
            if current.iter().any(|(id, _)| *id == sid) {
                continue;
            }
            let mut session: Map<String, Value> = serde_json::from_str(&summary_text).map_err(|e| e.to_string())?;
            let native = session.get("transport").and_then(Value::as_str) == Some("native");
            if ended_at.is_none() && successful.contains(&host) && !native {
                ended_at = Some(now);
                tx.execute("UPDATE session_history SET ended_at=? WHERE id=?", params![now, sid]).map_err(e)?;
            }
            if ended_at.is_some_and(|t| t <= now - RETENTION_SECONDS) {
                tx.execute("DELETE FROM session_history WHERE id=?", [&sid]).map_err(e)?;
                continue;
            }
            let last_activity = session
                .get("last_activity")
                .or_else(|| session.get("activity"))
                .or_else(|| session.get("last_status"))
                .cloned()
                .unwrap_or(Value::Null);
            for (k, v) in [
                ("alive", if ended_at.is_some() { json!(false) } else { Value::Null }),
                ("lifecycle", json!(if ended_at.is_some() { "ended" } else { "unreachable" })),
                ("activity", Value::Null),
                ("last_activity", last_activity),
                ("last_seen", json!(last_seen)),
                ("ended_at", json!(ended_at)),
                ("menu", Value::Null),
                ("control", Value::Null),
                ("revision", Value::Null),
                ("notification_eligible", json!(false)),
            ] {
                session.insert(k.into(), v);
            }
            if native {
                session.insert("native_control".into(), Value::Null);
                session.insert("has_question".into(), json!(false));
            }
            retained.push(Value::Object(session));
        }
        tx.commit().map_err(e)?;
        let mut all: Vec<Value> = current.into_iter().map(|(_, s)| Value::Object(s)).collect();
        all.extend(retained);
        result["sessions"] = Value::Array(all);
        Ok(result)
    }
}
