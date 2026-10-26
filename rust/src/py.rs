//! Small Python-compatibility helpers. Some outputs (opaque session IDs, stored
//! JSON) must be byte-identical to what the Python service produced, so a
//! cutover keeps every link and database row valid.

use serde_json::Value;

/// `json.dumps(value)` (separators ", " / ": ") or, with `compact`,
/// `json.dumps(value, separators=(",", ":"))`. ensure_ascii=True, insertion order.
pub fn dumps(value: &Value, compact: bool) -> String {
    let mut out = String::new();
    write(&mut out, value, compact);
    out
}

fn write(out: &mut String, value: &Value, compact: bool) {
    let (item_sep, key_sep) = if compact { (",", ":") } else { (", ", ": ") };
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&number(n)),
        Value::String(s) => out.push_str(&string(s)),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                }
                write(out, item, compact);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (i, (k, v)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                }
                out.push_str(&string(k));
                out.push_str(key_sep);
                write(out, v, compact);
            }
            out.push('}');
        }
    }
}

fn number(n: &serde_json::Number) -> String {
    if n.is_f64() {
        let f = n.as_f64().unwrap_or(0.0);
        if f.is_finite() && f.fract() == 0.0 && f.abs() < 1e16 {
            return format!("{f:.1}"); // Python repr: 3.0
        }
    }
    n.to_string()
}

pub fn string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// str.isspace() set (Rust's trim misses the \x1c-\x1f separators).
fn is_py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

pub fn strip(s: &str) -> &str {
    s.trim_matches(is_py_space)
}

pub fn rstrip(s: &str) -> &str {
    s.trim_end_matches(is_py_space)
}

/// str.splitlines(): the full Python line-boundary set, \r\n as one break.
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        let is_break = matches!(
            c,
            '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}'
        );
        if !is_break {
            continue;
        }
        lines.push(&s[start..i]);
        let mut end = i + c.len_utf8();
        if c == '\r' {
            if let Some(&(j, '\n')) = chars.peek() {
                chars.next();
                end = j + 1;
            }
        }
        start = end;
    }
    if start < s.len() {
        lines.push(&s[start..]);
    }
    lines
}

/// Python truthiness for a YAML/JSON-ish value.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `str(value)` for a parsed YAML scalar/collection.
pub fn str_of(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => (if *b { "True" } else { "False" }).into(),
        Value::Number(n) => number(n),
        Value::String(s) => s.clone(),
        Value::Array(a) => format!("[{}]", a.iter().map(repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter().map(|(k, v)| format!("{}: {}", repr(&Value::String(k.clone())), repr(v))).collect::<Vec<_>>().join(", ")
        ),
    }
}

fn repr(v: &Value) -> String {
    match v {
        Value::String(s) if s.contains('\'') && !s.contains('"') => format!("\"{s}\""),
        Value::String(s) => format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'")),
        other => str_of(other),
    }
}

/// secrets.token_hex(n)
pub fn token_hex(n: usize) -> String {
    let mut buf = vec![0u8; n];
    getrandom::fill(&mut buf).expect("OS randomness");
    hex(&buf)
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// str(uuid.uuid4())
pub fn uuid4() -> String {
    let mut b = [0u8; 16];
    getrandom::fill(&mut b).expect("OS randomness");
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let h = hex(&b);
    format!("{}-{}-{}-{}-{}", &h[..8], &h[8..12], &h[12..16], &h[16..20], &h[20..])
}

pub fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

pub fn home() -> std::path::PathBuf {
    std::path::PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/".into()))
}

/// shutil.which
pub fn which(name: &str) -> Option<std::path::PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    std::env::var_os("PATH")?.to_str()?.split(':').map(|d| std::path::Path::new(if d.is_empty() { "." } else { d }).join(name)).find(|p| {
        p.metadata().is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
    })
}

/// Path.expanduser for a leading ~ / ~/
pub fn expanduser(path: &str) -> std::path::PathBuf {
    if path == "~" {
        return home();
    }
    match path.strip_prefix("~/") {
        Some(rest) => home().join(rest),
        None => path.into(),
    }
}

/// OSError text, e.g. "[Errno 2] No such file or directory: '/x'"
pub fn os_error(err: &std::io::Error, path: &std::path::Path) -> String {
    let code = err.raw_os_error().unwrap_or(0);
    let msg = err.to_string();
    let msg = msg.split(" (os error").next().unwrap_or(&msg);
    format!("[Errno {code}] {msg}: '{}'", path.display())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn dumps_matches_python() {
        let v = json!(["local", ["$1", "%3", "x\u{e9}", "🙂", 4, ""]]);
        assert_eq!(dumps(&v, true), "[\"local\",[\"$1\",\"%3\",\"x\\u00e9\",\"\\ud83d\\ude42\",4,\"\"]]");
        assert_eq!(dumps(&json!({"a": 1.0, "b": null}), false), r#"{"a": 1.0, "b": null}"#);
    }

    #[test]
    fn splitlines_matches_python() {
        assert_eq!(splitlines("a\r\nb\rc\n\nd\u{2028}e\n"), vec!["a", "b", "c", "", "d", "e"]);
        assert!(splitlines("").is_empty());
    }
}
