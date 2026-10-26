//! Port of server.py — the HTTP contract (see that module's docstring and
//! README.md). Request bodies are validated like the pydantic models: missing
//! or mistyped fields answer 422.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{Path, Request, State};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Router};
use include_dir::{Dir, include_dir};
use serde_json::{Map, Value, json};

use crate::fleet::{self, ActArgs, FleetError};
use crate::push::PushService;
use crate::security::{self, COOKIE, MAX_AGE, Origin, Security};
use crate::{routines, workers};

static STATIC: Dir = include_dir!("$CARGO_MANIFEST_DIR/../src/cc_tmux_api/static");

#[derive(Clone)]
pub struct AppState {
    pub security: Arc<Security>,
    pub push: Arc<PushService>,
}

// ---------- responses ----------

pub fn detail(status: u16, message: &str) -> Response {
    (StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR), axum::Json(json!({"detail": message}))).into_response()
}

fn ok(v: Value) -> Response {
    axum::Json(v).into_response()
}

fn internal() -> Response {
    (StatusCode::INTERNAL_SERVER_ERROR, [(header::CONTENT_TYPE, "text/plain; charset=utf-8")], "Internal Server Error").into_response()
}

impl IntoResponse for FleetError {
    fn into_response(self) -> Response {
        detail(self.status, &self.message)
    }
}

/// Blocking work (tmux, ssh, sqlite) runs on the blocking pool, like FastAPI's threadpool.
async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    tokio::task::spawn_blocking(f).await.expect("handler panicked")
}

// ---------- pydantic-like body validation ----------

struct Body(Map<String, Value>);
type V<T> = Result<T, Response>;

fn invalid(kind: &str, loc: &str, msg: &str) -> Response {
    let loc: Vec<&str> = if loc.is_empty() { vec!["body"] } else { vec!["body", loc] };
    (StatusCode::UNPROCESSABLE_ENTITY, axum::Json(json!({"detail": [{"type": kind, "loc": loc, "msg": msg}]}))).into_response()
}

fn parse_body(bytes: &Bytes) -> V<Body> {
    if bytes.is_empty() {
        return Err(invalid("missing", "", "Field required"));
    }
    match serde_json::from_slice::<Value>(bytes) {
        Ok(Value::Object(m)) => Ok(Body(m)),
        Ok(_) => Err(invalid("model_attributes_type", "", "Input should be a valid dictionary or object to extract fields from")),
        Err(_) => Err(invalid("json_invalid", "", "JSON decode error")),
    }
}

impl Body {
    fn opt_str(&self, k: &str) -> V<Option<String>> {
        match self.0.get(k) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(s)) => Ok(Some(s.clone())),
            Some(_) => Err(invalid("string_type", k, "Input should be a valid string")),
        }
    }
    fn str_(&self, k: &str) -> V<String> {
        match self.0.get(k) {
            None => Err(invalid("missing", k, "Field required")),
            Some(Value::String(s)) => Ok(s.clone()),
            Some(_) => Err(invalid("string_type", k, "Input should be a valid string")),
        }
    }
    fn bounded(&self, k: &str, required: bool, min: usize, max: usize) -> V<Option<String>> {
        let v = if required { Some(self.str_(k)?) } else { self.opt_str(k)? };
        if let Some(s) = &v {
            let n = s.chars().count();
            if n < min {
                return Err(invalid("string_too_short", k, &format!("String should have at least {min} character{}", if min == 1 { "" } else { "s" })));
            }
            if n > max {
                return Err(invalid("string_too_long", k, &format!("String should have at most {max} characters")));
            }
        }
        Ok(v)
    }
    /// pydantic lax bool: true/false, 0/1, and the usual strings.
    fn bool_(&self, k: &str, required: bool) -> V<Option<bool>> {
        let bad = || invalid("bool_parsing", k, "Input should be a valid boolean");
        match self.0.get(k) {
            None if required => Err(invalid("missing", k, "Field required")),
            None => Ok(None),
            Some(Value::Null) if !required => Ok(None),
            Some(Value::Bool(b)) => Ok(Some(*b)),
            Some(Value::Number(n)) => match n.as_f64() {
                Some(0.0) => Ok(Some(false)),
                Some(1.0) => Ok(Some(true)),
                _ => Err(bad()),
            },
            Some(Value::String(s)) => match s.to_lowercase().as_str() {
                "0" | "off" | "f" | "false" | "n" | "no" => Ok(Some(false)),
                "1" | "on" | "t" | "true" | "y" | "yes" => Ok(Some(true)),
                _ => Err(bad()),
            },
            Some(_) => Err(bad()),
        }
    }
    /// pydantic lax int: integers, integral floats, numeric strings.
    fn int_(&self, k: &str) -> V<Option<i64>> {
        let bad = || invalid("int_parsing", k, "Input should be a valid integer");
        match self.0.get(k) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.fract() == 0.0).map(|f| f as i64)).map(Some).ok_or_else(bad),
            Some(Value::String(s)) => s.trim().parse().map(Some).map_err(|_| bad()),
            Some(_) => Err(bad()),
        }
    }
    fn dict(&self, k: &str, required: bool) -> V<Option<Value>> {
        match self.0.get(k) {
            None if required => Err(invalid("missing", k, "Field required")),
            None => Ok(None),
            Some(Value::Null) if !required => Ok(None),
            Some(v @ Value::Object(_)) => Ok(Some(v.clone())),
            Some(_) => Err(invalid("dict_type", k, "Input should be a valid dictionary")),
        }
    }
}

macro_rules! tryv {
    ($e:expr) => {
        match $e {
            Ok(v) => v,
            Err(r) => return r,
        }
    };
}

fn cascade_path(headers: &HeaderMap) -> Result<Vec<String>, Response> {
    let raw = headers.get("x-hq-path").and_then(|v| v.to_str().ok()).unwrap_or("");
    if raw.len() > 2048 {
        return Err(detail(400, "HQ path is too long"));
    }
    let path: Vec<String> = raw.split(',').map(str::trim).filter(|p| !p.is_empty()).map(String::from).collect();
    if path.len() > 8 {
        return Err(detail(508, "HQ cascade depth exceeded"));
    }
    Ok(path)
}

fn local_hosts() -> Result<Vec<String>, FleetError> {
    Ok(fleet::hosts()?.into_iter().filter(|h| !h.contains_key("ssh") && !h.contains_key("url")).filter_map(|h| h.get("id").and_then(Value::as_str).map(String::from)).collect())
}

// ---------- routes ----------

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/api/auth", get(auth))
        .route("/api/auth/login", post(login))
        .route("/api/auth/logout", post(logout))
        .route("/api/sessions", get(sessions_list))
        .route("/api/sessions/{session_id}", get(session_detail))
        .route("/api/sessions/{session_id}/action", post(session_action))
        .route("/api/push", get(|State(s): State<AppState>| async move { ok(s.push.config()) }))
        .route("/api/push/subscribe", post(subscribe).delete(unsubscribe))
        .route("/api/push/preferences", get(push_preferences).post(push_preference))
        .route("/api/push/test", post(|| async { detail(503, "Push delivery is not available in the Rust build") }))
        .route("/api/workers", get(workers_list))
        .route("/api/workers/action", post(workers_action))
        .route("/api/routines/action", post(routines_action))
        .route("/api/oneshot", post(oneshot))
        .route("/", get(|| async { static_file("index.html", &[(header::CACHE_CONTROL, "no-cache")]) }))
        .route(
            "/sw.js",
            get(|| async { static_file("sw.js", &[(header::CACHE_CONTROL, "no-cache"), (header::HeaderName::from_static("service-worker-allowed"), "/")]) }),
        )
        .route("/manifest.webmanifest", get(|| async { static_file("manifest.webmanifest", &[]) }))
        .route("/cc-static/{*path}", get(cc_static))
        .fallback(|| async { detail(404, "Not Found") })
        .method_not_allowed_fallback(|| async { detail(405, "Method Not Allowed") })
        .layer(axum::middleware::from_fn_with_state(state.clone(), security::middleware))
        .with_state(state)
}

async fn auth(State(s): State<AppState>, headers: HeaderMap) -> Response {
    ok(json!({"required": !s.security.token.is_empty(), "authenticated": s.security.authenticated(&headers)}))
}

async fn login(State(s): State<AppState>, Extension(origin): Extension<Arc<Origin>>, body: Bytes) -> Response {
    let b = tryv!(parse_body(&body));
    let token = tryv!(b.bounded("token", true, 1, 1024)).unwrap();
    if !s.security.can_attempt(&origin.client) {
        return detail(429, "Too many pairing attempts. Try again in one minute.");
    }
    if s.security.token.is_empty() || token != s.security.token {
        return detail(401, "Pairing key not recognized");
    }
    let secure = !s.security.public_url.is_empty() || origin.scheme == "https";
    let cookie = format!(
        "{COOKIE}={}; HttpOnly; Max-Age={MAX_AGE}; Path=/; SameSite=strict{}",
        s.security.cookie(),
        if secure { "; Secure" } else { "" }
    );
    let mut r = ok(json!({"ok": true}));
    r.headers_mut().insert(header::SET_COOKIE, HeaderValue::from_str(&cookie).unwrap());
    r
}

async fn logout() -> Response {
    let mut r = ok(json!({"ok": true}));
    r.headers_mut().insert(
        header::SET_COOKIE,
        HeaderValue::from_static("hq_device=\"\"; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/; SameSite=lax"),
    );
    r
}

async fn sessions_list(headers: HeaderMap) -> Response {
    let path = tryv!(cascade_path(&headers));
    match blocking(move || fleet::snapshot(&path)).await {
        Ok(v) => ok(v),
        Err(e) => e.into_response(),
    }
}

async fn session_detail(Path(id): Path<String>, headers: HeaderMap) -> Response {
    let path = tryv!(cascade_path(&headers));
    match blocking(move || fleet::detail(&id, &path)).await {
        Ok(v) => ok(v),
        Err(e) => e.into_response(),
    }
}

const ACTIONS: [&str; 9] = ["send", "key", "type", "select", "submit", "respond", "interrupt", "set_model", "set_permission_mode"];

async fn session_action(Path(id): Path<String>, headers: HeaderMap, body: Bytes) -> Response {
    let b = tryv!(parse_body(&body));
    let action = tryv!(b.str_("action"));
    if !ACTIONS.contains(&action.as_str()) {
        return invalid("literal_error", "action", "Input should be one of the supported actions");
    }
    let revision = tryv!(b.bounded("revision", true, 1, 256)).unwrap();
    let request_id = tryv!(b.bounded("request_id", true, 8, 128)).unwrap();
    let text = tryv!(b.bounded("text", false, 0, 32000));
    let key = tryv!(b.bounded("key", false, 0, 16));
    let index = tryv!(b.int_("index"));
    if index.is_some_and(|i| !(0..=50).contains(&i)) {
        return invalid("less_than_equal", "index", "Input should be between 0 and 50");
    }
    let response = tryv!(b.dict("response", false));
    let path = tryv!(cascade_path(&headers));
    let args = ActArgs { action, revision: json!(revision), request_id: json!(request_id), text, key, index, response };
    match blocking(move || fleet::act(&id, args, &path)).await {
        Ok(v) => ok(v),
        Err(e) => e.into_response(),
    }
}

async fn subscribe(body: Bytes) -> Response {
    let b = tryv!(parse_body(&body));
    tryv!(b.dict("subscription", true));
    detail(503, "Push delivery is not available in the Rust build")
}

async fn unsubscribe(State(s): State<AppState>, body: Bytes) -> Response {
    let b = tryv!(parse_body(&body));
    let endpoint = tryv!(b.bounded("endpoint", true, 0, 4096)).unwrap();
    match blocking(move || s.push.remove(&endpoint)).await {
        Ok(()) => ok(json!({"ok": true})),
        Err(_) => internal(),
    }
}

async fn push_preferences(State(s): State<AppState>) -> Response {
    match blocking(move || s.push.preferences()).await {
        Ok(v) => ok(v),
        Err(_) => internal(),
    }
}

async fn push_preference(State(s): State<AppState>, body: Bytes) -> Response {
    let b = tryv!(parse_body(&body));
    let id = tryv!(b.bounded("session_id", true, 0, 1024)).unwrap();
    let muted = tryv!(b.bool_("muted", true)).unwrap();
    match blocking(move || s.push.set_muted(&id, muted)).await {
        Ok(v) => ok(v),
        Err(_) => internal(), // ValueError (empty id) was an unhandled 500 in Python too
    }
}

async fn workers_list() -> Response {
    let result = blocking(|| -> Result<Value, Response> {
        let local = local_hosts().map_err(IntoResponse::into_response)?;
        let snapshot = fleet::snapshot(&[]).map_err(IntoResponse::into_response)?;
        let mut controls: Map<String, Value> = Map::new();
        for item in snapshot["sessions"].as_array().into_iter().flatten() {
            let host = item.get("host").and_then(Value::as_str).unwrap_or("");
            if local.iter().any(|h| h == host) && item.get("alive").is_none_or(|a| a == &json!(true)) {
                let session = item.get("session").map(crate::py::str_of).unwrap_or_default();
                controls.insert(session, json!({"session_id": item.get("id"), "revision": item.get("revision")}));
            }
        }
        let workers = workers::list_workers().map_err(|_| internal())?;
        let workers: Vec<Value> = workers
            .into_iter()
            .map(|mut w| {
                let control = w["session"].as_str().and_then(|s| controls.get(s)).cloned().unwrap_or(Value::Null);
                w["control"] = control;
                w
            })
            .collect();
        Ok(json!({"workers": workers, "others": workers::list_others(), "routines": routines::list_routines()}))
    })
    .await;
    match result {
        Ok(v) => ok(v),
        Err(r) => r,
    }
}

async fn workers_action(body: Bytes) -> Response {
    let b = tryv!(parse_body(&body));
    let action = tryv!(b.str_("action"));
    let f = |k| b.opt_str(k);
    let (title, prompt, workdir, session, text, session_id, revision, request_id) = (
        tryv!(f("title")),
        tryv!(f("prompt")),
        tryv!(f("workdir")),
        tryv!(f("session")),
        tryv!(f("text")),
        tryv!(f("session_id")),
        tryv!(f("revision")),
        tryv!(f("request_id")),
    );
    let nonempty = |v: &Option<String>| v.as_deref().is_some_and(|s| !s.is_empty());
    match action.as_str() {
        "spawn" => {
            if !(nonempty(&title) && nonempty(&prompt)) {
                return ok(json!({"ok": false, "output": "title and prompt required"}));
            }
            let title = crate::py::strip(&title.unwrap()).to_string();
            match blocking(move || workers::spawn(&title, &prompt.unwrap(), workdir.as_deref(), "task")).await {
                Ok(Value::Object(mut info)) => {
                    let mut out = Map::new();
                    out.insert("ok".into(), json!(true));
                    out.append(&mut info);
                    ok(Value::Object(out))
                }
                _ => internal(), // RuntimeError from tmux new-session
            }
        }
        "send" => {
            if ![&session, &text, &session_id, &revision, &request_id].iter().all(|v| nonempty(v)) {
                return detail(400, "Refresh the worker: session identity, revision and request_id are required");
            }
            let (session, text, session_id) = (session.unwrap(), text.unwrap(), session_id.unwrap());
            let result = blocking(move || -> Result<Value, FleetError> {
                let target = fleet::detail(&session_id, &[])?;
                let local = local_hosts()?;
                let owned = workers::list_workers().unwrap_or_default().iter().any(|w| w["session"] == json!(session));
                let host = target.get("host").and_then(Value::as_str).unwrap_or("");
                if !local.iter().any(|h| h == host) || target.get("session") != Some(&json!(session)) || !owned {
                    return fleet::err("Worker identity changed or is not owned by this service", 409);
                }
                let args = ActArgs { action: "send".into(), revision: json!(revision), request_id: json!(request_id), text: Some(text), ..Default::default() };
                fleet::act(&session_id, args, &[])
            })
            .await;
            match result {
                Ok(v) => ok(v),
                Err(e) => e.into_response(),
            }
        }
        "kill" => match blocking(move || workers::kill(session.as_deref().unwrap_or(""))).await {
            Ok(()) => ok(json!({"ok": true})),
            Err(e) => ok(json!({"ok": false, "output": e})),
        },
        other => ok(json!({"ok": false, "output": format!("unknown action {other}")})),
    }
}

async fn routines_action(body: Bytes) -> Response {
    let b = tryv!(parse_body(&body));
    let action = tryv!(b.str_("action"));
    let name = tryv!(b.str_("name"));
    let enabled = tryv!(b.bool_("enabled", false));
    blocking(move || match action.as_str() {
        "run" => match routines::run_now(&name) {
            Ok(Value::Object(mut info)) => {
                let mut out = Map::new();
                out.insert("ok".into(), json!(true));
                out.append(&mut info);
                ok(Value::Object(out))
            }
            Ok(_) => internal(),
            Err(e) => ok(json!({"ok": false, "output": e})),
        },
        "toggle" if !routines::enabled() => internal(), // Python: TypeError on a None dir
        "toggle" => match routines::set_enabled(&name, enabled.unwrap_or(false)) {
            Ok(()) => ok(json!({"ok": true})),
            Err(e) => ok(json!({"ok": false, "output": e})),
        },
        other => ok(json!({"ok": false, "output": format!("unknown action {other}")})),
    })
    .await
}

async fn oneshot(body: Bytes) -> Response {
    let b = tryv!(parse_body(&body));
    let prompt = tryv!(b.str_("prompt"));
    let timeout = tryv!(b.int_("timeout")).unwrap_or(300);
    blocking(move || match workers::oneshot(&prompt, timeout.max(0) as u64) {
        Ok(r) => {
            let out = crate::py::strip(&r.stdout);
            let output = if out.is_empty() { crate::py::strip(&r.stderr) } else { out };
            ok(json!({"ok": r.code == 0, "output": output}))
        }
        Err(crate::proc::RunError::Timeout) => ok(json!({"ok": false, "output": format!("timed out after {timeout}s")})),
        Err(_) => internal(), // FileNotFoundError: no claude binary
    })
    .await
}

// ---------- static UI ----------

fn content_type(path: &str) -> &'static str {
    match path.rsplit('.').next().unwrap_or("") {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json",
        "webmanifest" => "application/manifest+json",
        "png" => "image/png",
        "svg" => "image/svg+xml",
        "txt" => "text/plain; charset=utf-8",
        "md" => "text/markdown; charset=utf-8",
        _ => "application/octet-stream",
    }
}

fn static_file(path: &str, extra: &[(header::HeaderName, &'static str)]) -> Response {
    let Some(file) = STATIC.get_file(path) else { return detail(404, "Not Found") };
    let mut r = (StatusCode::OK, [(header::CONTENT_TYPE, content_type(path))], file.contents()).into_response();
    for (k, v) in extra {
        r.headers_mut().insert(k.clone(), HeaderValue::from_static(v));
    }
    r
}

async fn cc_static(Path(path): Path<String>, req: Request) -> Response {
    if path.split('/').any(|p| p == ".." || p.is_empty()) {
        return detail(404, "Not Found");
    }
    let mut r = static_file(&path, &[]);
    if req.method() == Method::HEAD {
        *r.body_mut() = axum::body::Body::empty();
    }
    r
}

pub fn make_service(state: AppState) -> axum::extract::connect_info::IntoMakeServiceWithConnectInfo<Router, SocketAddr> {
    router(state).into_make_service_with_connect_info::<SocketAddr>()
}
