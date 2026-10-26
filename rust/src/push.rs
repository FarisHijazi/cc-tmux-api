//! The storage half of notifications.py: mute preferences in the same
//! push.sqlite3 schema. Web Push delivery (VAPID signing, RFC 8291 encryption,
//! the event outbox) is NOT ported, so config() always reports unavailable and
//! the UI never offers to subscribe.

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use rusqlite::Connection;
use serde_json::{Value, json};

pub struct PushService {
    path: PathBuf,
    lock: Mutex<()>,
}

const SCHEMA: [&str; 5] = [
    "CREATE TABLE IF NOT EXISTS subscriptions(endpoint TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS muted(id TEXT PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, signature TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS seen(id TEXT, event TEXT, PRIMARY KEY(id,event))",
    "CREATE TABLE IF NOT EXISTS outbox(endpoint TEXT REFERENCES subscriptions(endpoint) ON DELETE CASCADE, event TEXT, session TEXT, payload TEXT, attempts INTEGER DEFAULT 0, due REAL, expires REAL, PRIMARY KEY(endpoint,event))",
];

impl PushService {
    pub fn new(state_dir: &Path) -> Result<Self, String> {
        std::fs::create_dir_all(state_dir).map_err(|e| e.to_string())?;
        let _ = std::fs::set_permissions(state_dir, std::fs::Permissions::from_mode(0o700));
        let path = state_dir.join("push.sqlite3");
        std::fs::OpenOptions::new().create(true).append(true).open(&path).map_err(|e| e.to_string())?;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
        let service = Self { path, lock: Mutex::new(()) };
        service.with_db(|db| {
            for statement in SCHEMA {
                db.execute(statement, [])?;
            }
            Ok(())
        })?;
        Ok(service)
    }

    fn with_db<T>(&self, f: impl FnOnce(&rusqlite::Transaction) -> rusqlite::Result<T>) -> Result<T, String> {
        let _guard = self.lock.lock().unwrap();
        let mut db = Connection::open(&self.path).map_err(|e| e.to_string())?;
        db.busy_timeout(Duration::from_secs(60)).map_err(|e| e.to_string())?;
        db.execute_batch("PRAGMA foreign_keys=ON").map_err(|e| e.to_string())?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(|e| e.to_string())?;
        let out = f(&tx).map_err(|e| e.to_string())?;
        tx.commit().map_err(|e| e.to_string())?;
        Ok(out)
    }

    pub fn config(&self) -> Value {
        json!({"available": false, "public_key": null, "reason": "Push delivery is not available in the Rust build"})
    }

    pub fn remove(&self, endpoint: &str) -> Result<(), String> {
        self.with_db(|db| db.execute("DELETE FROM subscriptions WHERE endpoint=?", [endpoint]).map(|_| ()))
    }

    pub fn preferences(&self) -> Result<Value, String> {
        self.with_db(|db| {
            let mut stmt = db.prepare("SELECT id FROM muted ORDER BY id")?;
            let ids: Vec<String> = stmt.query_map([], |r| r.get(0))?.collect::<Result<_, _>>()?;
            Ok(json!({"muted": ids}))
        })
    }

    pub fn set_muted(&self, id: &str, muted: bool) -> Result<Value, String> {
        if id.is_empty() {
            return Err("Session id and boolean muted are required".into());
        }
        self.with_db(|db| {
            if muted {
                db.execute("INSERT OR IGNORE INTO muted VALUES (?)", [id])?;
                db.execute("DELETE FROM outbox WHERE session=?", [id])?;
            } else {
                db.execute("DELETE FROM muted WHERE id=?", [id])?;
            }
            Ok(())
        })?;
        self.preferences()
    }
}
