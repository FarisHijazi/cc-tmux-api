//! Blocking subprocess helpers with Python-subprocess-like timeouts.

use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub struct Out {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

impl Out {
    pub fn ok(&self) -> bool {
        self.code == 0
    }
}

#[derive(Debug)]
pub enum RunError {
    Timeout,
    Spawn(std::io::Error),
}

/// Run `cmd`, feeding `input` on stdin; kill it after `timeout` (subprocess.TimeoutExpired).
pub fn run(cmd: &str, args: &[&str], input: Option<&str>, timeout: Duration) -> Result<Out, RunError> {
    let mut child = Command::new(cmd)
        .args(args)
        .stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(RunError::Spawn)?;
    let writer = input.map(|text| {
        let mut stdin = child.stdin.take().unwrap();
        let text = text.to_owned();
        std::thread::spawn(move || {
            let _ = stdin.write_all(text.as_bytes());
        })
    });
    let mut stdout = child.stdout.take().unwrap();
    let mut stderr = child.stderr.take().unwrap();
    let out_t = std::thread::spawn(move || {
        let mut b = Vec::new();
        let _ = stdout.read_to_end(&mut b);
        b
    });
    let err_t = std::thread::spawn(move || {
        let mut b = Vec::new();
        let _ = stderr.read_to_end(&mut b);
        b
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        if let Some(status) = child.try_wait().map_err(RunError::Spawn)? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(RunError::Timeout);
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    if let Some(w) = writer {
        let _ = w.join();
    }
    let stdout = String::from_utf8_lossy(&out_t.join().unwrap_or_default()).into_owned();
    let stderr = String::from_utf8_lossy(&err_t.join().unwrap_or_default()).into_owned();
    Ok(Out { code: status.code().unwrap_or(-1), stdout, stderr })
}

/// `tmux ...` with the workers module's 10 s timeout; a spawn failure reads as a failed command.
pub fn tmux_t(args: &[&str]) -> Result<Out, RunError> {
    match run("tmux", args, None, Duration::from_secs(10)) {
        Err(RunError::Spawn(e)) => Ok(Out { code: 127, stdout: String::new(), stderr: e.to_string() }),
        other => other,
    }
}

/// `tmux ...` where a timeout is treated like any failure (stdout empty, nonzero code).
pub fn tmux(args: &[&str]) -> Out {
    tmux_t(args).unwrap_or(Out { code: -1, stdout: String::new(), stderr: "timeout".into() })
}
