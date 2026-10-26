//! Port of security.py — optional device pairing (HMAC-signed cookie or
//! bearer token), allowed-host and same-origin enforcement, JSON-only mutations.

use std::collections::{HashMap, HashSet, VecDeque};
use std::net::SocketAddr;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use axum::extract::{ConnectInfo, Request, State};
use axum::http::{HeaderMap, HeaderValue, header};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use hmac::{KeyInit, Mac};
use sha2::Sha256;

use crate::py;
use crate::server::{AppState, detail};

pub const COOKIE: &str = "hq_device";
pub const MAX_AGE: i64 = 30 * 24 * 60 * 60;

pub struct Security {
    pub token: String,
    pub public_url: String,
    hosts: HashSet<String>,
    attempts: Mutex<HashMap<String, VecDeque<Instant>>>,
    forwarded_allow: Vec<String>,
}

/// What the Python app saw as request.url / request.client after uvicorn's proxy-header handling.
pub struct Origin {
    pub scheme: String,
    pub netloc: String,
    pub hostname: String,
    pub client: String,
}

fn sign(token: &str, payload: &str) -> String {
    let mut mac = hmac::Hmac::<Sha256>::new_from_slice(token.as_bytes()).expect("any key length");
    mac.update(payload.as_bytes());
    py::hex(&mac.finalize().into_bytes())
}

fn ct_eq(a: &str, b: &str) -> bool {
    a.len() == b.len() && a.bytes().zip(b.bytes()).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

impl Security {
    pub fn new(token: String, public_url: &str) -> Result<Self, String> {
        if !token.is_empty() && token.chars().count() < 32 {
            return Err("CC_TMUX_TOKEN must contain at least 32 characters".into());
        }
        let public_url = public_url.trim_end_matches('/').to_string();
        let mut hosts: HashSet<String> = ["localhost", "127.0.0.1", "::1"].iter().map(|s| s.to_string()).collect();
        if !public_url.is_empty() {
            let hostname = https_origin_host(&public_url).ok_or("CC_TMUX_PUBLIC_URL must be an HTTPS origin")?;
            hosts.insert(hostname);
        }
        hosts.extend(
            std::env::var("CC_TMUX_ALLOWED_HOSTS").unwrap_or_default().split(',').map(|h| h.trim().to_string()).filter(|h| !h.is_empty()),
        );
        let forwarded_allow = std::env::var("FORWARDED_ALLOW_IPS")
            .unwrap_or_else(|_| "127.0.0.1".into())
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();
        Ok(Self { token, public_url, hosts, attempts: Mutex::new(HashMap::new()), forwarded_allow })
    }

    pub fn cookie(&self) -> String {
        let payload = format!("{}.{}", py::now() as i64, py::token_hex(16));
        let signature = sign(&self.token, &payload);
        format!("{payload}.{signature}")
    }

    pub fn authenticated(&self, headers: &HeaderMap) -> bool {
        if self.token.is_empty() {
            return true;
        }
        let bearer = headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok()).unwrap_or("");
        if let Some(t) = bearer.strip_prefix("Bearer ") {
            if ct_eq(t, &self.token) {
                return true;
            }
        }
        let cookie = cookie_value(headers, COOKIE).unwrap_or_default();
        if cookie.len() > 200 {
            return false;
        }
        let parts: Vec<&str> = cookie.split('.').collect();
        let [issued, nonce, signature] = parts.as_slice() else { return false };
        let Ok(issued_n) = issued.trim().parse::<i64>() else { return false };
        let age = py::now() - issued_n as f64;
        let expected = sign(&self.token, &format!("{issued}.{nonce}"));
        (0.0..=MAX_AGE as f64).contains(&age) && ct_eq(signature, &expected)
    }

    /// 10 pairing attempts per address per minute.
    pub fn can_attempt(&self, address: &str) -> bool {
        let now = Instant::now();
        let mut attempts = self.attempts.lock().unwrap();
        if attempts.len() > 1000 {
            attempts.clear();
        }
        let list = attempts.entry(address.to_string()).or_default();
        while list.front().is_some_and(|t| now.duration_since(*t) > Duration::from_secs(60)) {
            list.pop_front();
        }
        if list.len() >= 10 {
            return false;
        }
        list.push_back(now);
        true
    }

    pub fn origin(&self, headers: &HeaderMap, peer: SocketAddr) -> Origin {
        let peer_ip = peer.ip().to_string();
        let trusted = self.forwarded_allow.iter().any(|a| a == "*" || *a == peer_ip);
        let mut scheme = "http".to_string();
        let mut client = peer_ip;
        if trusted {
            if let Some(p) = headers.get("x-forwarded-proto").and_then(|v| v.to_str().ok()) {
                let p = p.trim().to_lowercase();
                if p == "http" || p == "https" {
                    scheme = p;
                }
            }
            if let Some(f) = headers.get("x-forwarded-for").and_then(|v| v.to_str().ok()) {
                // uvicorn: the right-most address not in the trusted list
                let hops: Vec<&str> = f.split(',').map(str::trim).filter(|s| !s.is_empty()).collect();
                if let Some(h) = hops.iter().rev().find(|h| !self.forwarded_allow.iter().any(|a| a == *h)).or(hops.first()) {
                    client = h.to_string();
                }
            }
        }
        let netloc = headers.get(header::HOST).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        Origin { hostname: hostname_of(&netloc), scheme, netloc, client }
    }
}

/// urlsplit(...).hostname for a Host header value: lowercased, port and IPv6 brackets dropped.
fn hostname_of(netloc: &str) -> String {
    let host = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let host = if let Some(rest) = host.strip_prefix('[') {
        rest.split(']').next().unwrap_or("")
    } else {
        host.split(':').next().unwrap_or("")
    };
    host.to_lowercase()
}

fn https_origin_host(url: &str) -> Option<String> {
    let rest = url.strip_prefix("https://")?;
    if rest.is_empty() || rest.contains(['/', '?', '#', '@']) {
        return None;
    }
    let host = hostname_of(rest);
    (!host.is_empty()).then_some(host)
}

pub fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers.get_all(header::COOKIE).iter().filter_map(|v| v.to_str().ok()).flat_map(|v| v.split(';')).find_map(|pair| {
        let (k, v) = pair.trim().split_once('=')?;
        (k.trim() == name).then(|| v.trim().trim_matches('"').to_string())
    })
}

pub async fn middleware(State(state): State<AppState>, ConnectInfo(peer): ConnectInfo<SocketAddr>, req: Request, next: Next) -> Response {
    let security = &state.security;
    let origin = security.origin(req.headers(), peer);
    if !security.hosts.contains(&origin.hostname) {
        return detail(400, "Unrecognized server address");
    }
    let path = req.uri().path().to_string();
    let api = path.starts_with("/api/");
    if api {
        let headers = req.headers();
        let expected = [security.public_url.clone(), format!("{}://{}", origin.scheme, origin.netloc)];
        let origin_header = headers.get(header::ORIGIN).and_then(|v| v.to_str().ok());
        let cross = headers.get("sec-fetch-site").and_then(|v| v.to_str().ok()) == Some("cross-site");
        if origin_header.is_some_and(|o| !o.is_empty() && !expected.iter().any(|e| e == o)) || cross {
            return detail(403, "Cross-site requests are not allowed");
        }
        let public = path == "/api/auth" || path == "/api/auth/login";
        if !public && !security.authenticated(headers) {
            return detail(401, "Pair this device to continue");
        }
        if ["POST", "PUT", "PATCH", "DELETE"].contains(&req.method().as_str()) {
            let ct = headers.get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("");
            if !ct.starts_with("application/json") {
                return detail(415, "JSON content type required");
            }
        }
    }
    let mut req = req;
    req.extensions_mut().insert(std::sync::Arc::new(origin));
    let mut response = next.run(req).await.into_response();
    let h = response.headers_mut();
    h.insert("x-content-type-options", HeaderValue::from_static("nosniff"));
    h.insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    h.insert("x-frame-options", HeaderValue::from_static("DENY"));
    if api {
        h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    }
    response
}
