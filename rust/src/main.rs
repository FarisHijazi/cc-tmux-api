//! cc-tmux-api (Rust build) — local daemon that spawns, lists, kills and
//! schedules unattended Claude Code workers in tmux, and aggregates existing
//! Claude sessions across configured hosts. Port of the Python package in
//! ../src/cc_tmux_api; same env/config contract (see README.md).
//!
//! Not in this build: native Remote Control (CC_TMUX_NATIVE_TOKEN), the
//! claude.ai upstream mirror, Web Push delivery, and `--app` ASGI embedding.

mod amux;
mod fleet;
mod history;
mod proc;
mod push;
mod py;
mod routines;
mod security;
mod server;
mod workers;

use std::net::{SocketAddr, ToSocketAddrs};
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;

const USAGE: &str = "usage: cc-tmux-api [-h] [--config CONFIG] [--app APP]";

fn usage_error(msg: &str) -> ExitCode {
    eprintln!("{USAGE}\ncc-tmux-api: error: {msg}");
    ExitCode::from(2)
}

/// Load a private JSON file of CC_TMUX_* settings into the environment (existing env wins).
fn load_config(path: &str) -> Result<(), String> {
    let text = std::fs::read_to_string(path).map_err(|e| py::os_error(&e, std::path::Path::new(path)))?;
    let config: Value = serde_json::from_str(&text).map_err(|e| format!("invalid JSON in {path}: {e}"))?;
    let Value::Object(map) = config else { return Err("config must be a JSON object".into()) };
    for (key, value) in map {
        if !key.starts_with("CC_TMUX_") && key != "BCE_WORKSPACE_DIR" {
            return Err(format!("unsupported configuration key: {key}"));
        }
        let text = match &value {
            Value::String(s) => s.clone(),
            Value::Bool(b) => (if *b { "True" } else { "False" }).into(), // Python: bool is an int, str(True)
            Value::Number(_) => py::str_of(&value),
            Value::Array(_) | Value::Object(_) => py::dumps(&value, false),
            Value::Null => return Err(format!("invalid configuration value: {key}")),
        };
        if std::env::var_os(&key).is_none() {
            // SAFETY: single-threaded at this point (before the runtime starts).
            unsafe { std::env::set_var(&key, text) };
        }
    }
    Ok(())
}

/// server.bind_address: an address or host:port; bracket IPv6 when giving a port.
fn bind_address(value: &str, default_port: u16) -> Result<(String, u16), String> {
    let bad = || "Invalid CC_TMUX_BIND endpoint".to_string();
    if value.starts_with('[') || value.matches(':').count() == 1 {
        let (host, port) = if let Some(rest) = value.strip_prefix('[') {
            let (h, after) = rest.split_once(']').ok_or_else(bad)?;
            (h.to_string(), after.strip_prefix(':').map(String::from).or_else(|| (!after.is_empty()).then(|| "x".into())))
        } else {
            let (h, p) = value.split_once(':').unwrap();
            (h.to_string(), Some(p.to_string()))
        };
        if host.is_empty() || host.contains(['/', '?', '#', '@']) {
            return Err(bad());
        }
        let port = match port.as_deref() {
            None | Some("") => default_port,
            Some(p) => p.parse::<u16>().map_err(|_| "Port could not be cast to integer value".to_string())?,
        };
        return Ok((host.to_lowercase(), port));
    }
    Ok((value.to_string(), default_port))
}

fn background(state: &server::AppState) {
    let manage = std::env::var("CC_TMUX_MANAGE_WORKERS").unwrap_or_else(|_| "1".into()) == "1";
    let reap_hours: f64 = std::env::var("CC_TMUX_ROUTINE_REAP_HOURS").ok().and_then(|v| v.parse().ok()).unwrap_or(12.0);
    if manage {
        // After a reboot the tmux server is gone: revive registered task workers, GC tombstones.
        std::thread::spawn(|| {
            for name in workers::resume_registered() {
                println!("revived worker: {name}");
            }
            for name in workers::registry_gc() {
                println!("gc'd finished session dir: {name}");
            }
        });
        if routines::enabled() {
            std::thread::spawn(move || {
                loop {
                    for name in routines::tick() {
                        println!("routine spawned: {name}");
                    }
                    for name in workers::reap_stale("routine", reap_hours) {
                        println!("reaped stale routine worker: {name}");
                    }
                    std::thread::sleep(Duration::from_secs(60));
                }
            });
        }
    }
    // Lifecycle history must observe sessions even without push or an open UI.
    let _push = state.push.clone();
    std::thread::spawn(|| {
        loop {
            if let Err(e) = fleet::snapshot(&[]) {
                eprintln!("session monitor: FleetError: {}", e.message);
            }
            std::thread::sleep(Duration::from_secs(5));
        }
    });
}

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1);
    let (mut config, mut app) = (None, None);
    while let Some(arg) = args.next() {
        let (name, inline) = match arg.split_once('=') {
            Some((n, v)) if n.starts_with("--") => (n.to_string(), Some(v.to_string())),
            _ => (arg.clone(), None),
        };
        match name.as_str() {
            "-h" | "--help" => {
                println!("{USAGE}\n\nHQ — existing Claude sessions, anywhere\n\noptions:\n  -h, --help       show this help message and exit\n  --config CONFIG  Private JSON file of CC_TMUX_* settings\n  --app APP        Embedded ASGI application (not supported by the Rust build)");
                return ExitCode::SUCCESS;
            }
            "--config" | "--app" => {
                let Some(value) = inline.or_else(|| args.next()) else { return usage_error(&format!("argument {name}: expected one argument")) };
                if name == "--config" { config = Some(value) } else { app = Some(value) }
            }
            _ => return usage_error(&format!("unrecognized arguments: {arg}")),
        }
    }
    if app.is_some() {
        return usage_error("--app (embedded Python ASGI apps) is not supported by the Rust build");
    }
    if let Some(path) = config {
        if let Err(e) = load_config(&path) {
            return usage_error(&e);
        }
    }
    if std::env::var("CC_TMUX_NATIVE_TOKEN").is_ok_and(|t| !t.is_empty()) {
        eprintln!("cc-tmux-api: CC_TMUX_NATIVE_TOKEN is set, but native Remote Control is not in the Rust build; refusing to start without it");
        return ExitCode::from(2);
    }
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("cc-tmux-api: {e}");
            ExitCode::from(1)
        }
    }
}

fn run() -> Result<(), String> {
    let security = security::Security::new(
        std::env::var("CC_TMUX_TOKEN").unwrap_or_default(),
        &std::env::var("CC_TMUX_PUBLIC_URL").unwrap_or_default(),
    )?;
    let push = push::PushService::new(&fleet::state_dir())?;
    let state = server::AppState { security: Arc::new(security), push: Arc::new(push) };
    let port: u16 = std::env::var("CC_TMUX_PORT").unwrap_or_else(|_| "8377".into()).parse().map_err(|_| "CC_TMUX_PORT must be a port number")?;
    let mut addresses: Vec<String> = Vec::new();
    for part in std::env::var("CC_TMUX_BIND").unwrap_or_else(|_| "127.0.0.1".into()).split(',').map(str::trim).filter(|p| !p.is_empty()) {
        if !addresses.iter().any(|a| a == part) {
            addresses.push(part.into());
        }
    }
    if addresses.is_empty() {
        return Err("CC_TMUX_BIND requires at least one address".into());
    }
    let mut listeners = Vec::new();
    for endpoint in &addresses {
        let (address, bind_port) = bind_address(endpoint, port)?;
        let addr: SocketAddr = (address.as_str(), bind_port)
            .to_socket_addrs()
            .map_err(|e| format!("{endpoint}: {e}"))?
            .next()
            .ok_or_else(|| format!("{endpoint}: no address"))?;
        let listener = std::net::TcpListener::bind(addr).map_err(|e| format!("bind {addr}: {e}"))?;
        listener.set_nonblocking(true).map_err(|e| e.to_string())?;
        println!("cc-tmux-api (rust) listening on http://{addr}");
        listeners.push(listener);
    }
    let runtime = tokio::runtime::Builder::new_multi_thread().enable_all().build().map_err(|e| e.to_string())?;
    runtime.block_on(async move {
        background(&state);
        let mut tasks = Vec::new();
        for listener in listeners {
            let listener = tokio::net::TcpListener::from_std(listener).map_err(|e| e.to_string())?;
            let service = server::make_service(state.clone());
            tasks.push(tokio::spawn(async move { axum::serve(listener, service).with_graceful_shutdown(shutdown()).await }));
        }
        for t in tasks {
            let _ = t.await;
        }
        Ok::<(), String>(())
    })?;
    // Python bounded shutdown at 5 s; background blocking threads are abandoned.
    runtime.shutdown_timeout(Duration::from_secs(5));
    Ok(())
}

async fn shutdown() {
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("signal handler");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = term.recv() => {}
    }
}

#[cfg(test)]
mod tests {
    use super::bind_address;

    #[test]
    fn bind_endpoints() {
        assert_eq!(bind_address("127.0.0.1", 8377).unwrap(), ("127.0.0.1".into(), 8377));
        assert_eq!(bind_address("127.0.0.1:9000", 8377).unwrap(), ("127.0.0.1".into(), 9000));
        assert_eq!(bind_address("[::1]:9000", 8377).unwrap(), ("::1".into(), 9000));
        assert_eq!(bind_address("[::1]", 8377).unwrap(), ("::1".into(), 8377));
        assert_eq!(bind_address("::1", 8377).unwrap(), ("::1".into(), 8377));
        assert!(bind_address("host:abc", 8377).is_err());
    }
}
