//! The stdio transport — `mcp.client.stdio.stdio_client`: the server as a
//! child process, one JSON-RPC message per line each way.
//!
//! The child gets a small inherited environment (`get_default_environment`)
//! with the config's `env` over it, `${VAR}` in those values expanded from
//! ours. It runs in its own process group, so closing the session can end
//! whatever it started too (an `npx` and its `node`): stdin is closed, then
//! SIGTERM to the group after 2 s, then SIGKILL after 2 more. Its stderr is
//! ours, as it is Python's.

use std::process::Stdio as Pipe;
use std::time::Duration;

use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::mpsc;

use super::config::Connection;
use super::session::Incoming;
use crate::pyjson;

/// `PROCESS_TERMINATION_TIMEOUT`.
const TERMINATION_TIMEOUT: Duration = Duration::from_secs(2);

/// `DEFAULT_INHERITED_ENV_VARS` off Windows.
const INHERITED: [&str; 6] = ["HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"];

pub struct Stdio {
    child: Child,
    stdin: Option<ChildStdin>,
    rx: mpsc::Receiver<Value>,
    /// The child's process group, until it has been reaped.
    pgid: Option<i32>,
}

/// `_expand_env_vars`: `${VAR}` from our environment; bare `$VAR` and
/// unknown names are left as written.
fn expand(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    let mut rest = value;
    while let Some(at) = rest.find("${") {
        out.push_str(&rest[..at]);
        let after = &rest[at + 2..];
        match after.find('}') {
            Some(end) if end > 0 => {
                let name = &after[..end];
                match std::env::var(name) {
                    Ok(v) => out.push_str(&v),
                    Err(_) => out.push_str(&rest[at..at + 2 + end + 1]),
                }
                rest = &after[end + 1..];
            }
            _ => {
                out.push_str("${");
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

fn string(value: &Value) -> Result<String, String> {
    match value {
        Value::String(s) => Ok(s.clone()),
        other => Err(format!("expected str, bytes or os.PathLike object, not {}", pyjson::py_type(other))),
    }
}

impl Stdio {
    pub fn open(params: &Connection) -> Result<Self, String> {
        let command = string(&params["command"])?;
        let args = match &params["args"] {
            Value::Array(items) => items.iter().map(string).collect::<Result<Vec<_>, _>>()?,
            other => return Err(format!("'{}' object is not iterable", pyjson::py_type(other))),
        };
        if let Some(enc) = params.get("encoding").filter(|v| !v.is_null()) {
            let enc = pyjson::py_str(enc).to_lowercase().replace('_', "-");
            if enc != "utf-8" && enc != "utf8" {
                return Err(format!("encoding {enc} is not supported"));
            }
        }
        let mut env: Vec<(String, String)> = INHERITED
            .iter()
            .filter_map(|k| std::env::var(k).ok().filter(|v| !v.starts_with("()")).map(|v| (k.to_string(), v)))
            .collect();
        match params.get("env") {
            None | Some(Value::Null) => {}
            Some(Value::Object(extra)) => {
                for (k, v) in extra {
                    let Value::String(v) = v else {
                        return Err(format!("expected string or bytes-like object, got '{}'", pyjson::py_type(v)));
                    };
                    let v = expand(v);
                    if v.contains("${") {
                        tracing::warn!("env['{k}'] contains unexpanded variable reference: {v:?}");
                    }
                    env.retain(|(name, _)| name != k);
                    env.push((k.clone(), v));
                }
            }
            Some(other) => return Err(format!("'{}' object has no attribute 'items'", pyjson::py_type(other))),
        }
        let mut cmd = Command::new(&command);
        cmd.args(&args).env_clear().envs(env).stdin(Pipe::piped()).stdout(Pipe::piped()).stderr(Pipe::inherit());
        cmd.process_group(0).kill_on_drop(true);
        if let Some(cwd) = params.get("cwd").filter(|v| !v.is_null()) {
            cmd.current_dir(string(cwd)?);
        }
        let mut child = cmd.spawn().map_err(|e| spawn_error(&e, &command))?;
        let stdin = child.stdin.take();
        let stdout = child.stdout.take().expect("piped stdout");
        let pgid = child.id().map(|id| id as i32);
        let (tx, rx) = mpsc::channel(16);
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                match serde_json::from_str::<Value>(&line) {
                    Ok(msg @ Value::Object(_)) => {
                        if tx.send(msg).await.is_err() {
                            return;
                        }
                    }
                    _ => tracing::debug!("MCP stdio: a line that isn't a JSON-RPC message: {line:?}"),
                }
            }
        });
        Ok(Stdio { child, stdin, rx, pgid })
    }

    pub async fn send(&mut self, msg: &Value) -> Result<(), String> {
        let stdin = self.stdin.as_mut().ok_or("Connection closed")?;
        let mut line = serde_json::to_string(msg).map_err(|e| e.to_string())?;
        line.push('\n');
        stdin.write_all(line.as_bytes()).await.map_err(|e| e.to_string())?;
        stdin.flush().await.map_err(|e| e.to_string())
    }

    pub async fn recv(&mut self) -> Incoming {
        match self.rx.recv().await {
            Some(msg) => Incoming::Message(msg),
            None => Incoming::Closed("Connection closed".into()),
        }
    }

    /// The spec's shutdown: stdin closed, then the group signalled.
    pub async fn close(mut self) {
        drop(self.stdin.take());
        if tokio::time::timeout(TERMINATION_TIMEOUT, self.child.wait()).await.is_err() {
            self.signal(libc::SIGTERM);
            if tokio::time::timeout(TERMINATION_TIMEOUT, self.child.wait()).await.is_err() {
                self.signal(libc::SIGKILL);
                let _ = self.child.wait().await;
            }
        }
        // Whatever it left in its group goes with it.
        self.signal(libc::SIGKILL);
        self.pgid = None;
    }

    fn signal(&self, sig: i32) {
        if let Some(pgid) = self.pgid {
            // SAFETY: signalling a process group we created; a stale id is ESRCH.
            unsafe {
                libc::killpg(pgid, sig);
            }
        }
    }
}

impl Drop for Stdio {
    /// A session dropped mid-call (a cancelled run) takes its server down.
    fn drop(&mut self) {
        self.signal(libc::SIGKILL);
    }
}

/// `OSError` as Python words a failed spawn: `[Errno 2] No such file or
/// directory: 'cmd'`.
fn spawn_error(e: &std::io::Error, command: &str) -> String {
    match e.raw_os_error() {
        Some(n) => {
            let text = e.to_string();
            let text = text.strip_suffix(&format!(" (os error {n})")).unwrap_or(&text).to_string();
            format!("[Errno {n}] {text}: {}", pyjson::repr_str(command))
        }
        None => e.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::expand;

    #[test]
    fn expands_braced_vars_only() {
        // SAFETY: tests in this module don't race on this variable.
        unsafe { std::env::set_var("JARVIS_MCP_TEST_VAR", "v") };
        assert_eq!(expand("a${JARVIS_MCP_TEST_VAR}b"), "avb");
        assert_eq!(expand("$JARVIS_MCP_TEST_VAR ${JARVIS_MCP_NOPE} ${} ${x"), "$JARVIS_MCP_TEST_VAR ${JARVIS_MCP_NOPE} ${} ${x");
    }
}
