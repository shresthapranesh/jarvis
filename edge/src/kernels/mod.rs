//! The agent's notebooks — a port of `core/kernels.py`. One IPython kernel per
//! session key (a conversation, or a worker's own key), started on its first
//! cell, its variables kept between cells like a Jupyter notebook.
//!
//! - Cells in one session run one at a time.
//! - Every kernel preloads `search`/`read` and the `jarvis` SDK; the SDK is
//!   scoped to the cell's conversation and project.
//! - A cell gets `timeout` seconds, then is interrupted — the session
//!   survives. Not while a tool approval for its conversation is open (the
//!   SDK waits for one inside the cell), up to 30 minutes.
//! - At most `MAX_KERNELS` live kernels (least recently used goes), and one
//!   untouched for 30 minutes is shut down.

mod kernel;
mod wire;

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::Value;
use sqlx::SqlitePool;

pub use kernel::Launch;
use kernel::Kernel;

pub const MAX_KERNELS: usize = 12;
pub const IDLE_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const INTERRUPT_DRAIN_TIMEOUT: Duration = Duration::from_secs(5);
const HOLD_POLL: Duration = Duration::from_secs(5);
const MAX_HOLD: Duration = Duration::from_secs(30 * 60);
const MAX_OUTPUT_CHARS: usize = 30_000;
/// The reaper's pace (`register_kernel_reaper_job`: every 10 minutes).
const REAP_EVERY: Duration = Duration::from_secs(10 * 60);

/// One cell to run.
pub struct Cell<'a> {
    pub code: &'a str,
    pub timeout: Duration,
    pub conversation_id: Option<&'a str>,
    pub project_id: Option<&'a str>,
}

pub struct Kernels {
    launch: Launch,
    /// Run silently in every kernel right after it starts.
    bootstrap: String,
    /// For the approval hold.
    pool: SqlitePool,
    sessions: tokio::sync::Mutex<HashMap<String, Arc<Session>>>,
}

struct Session {
    key: String,
    state: tokio::sync::Mutex<State>,
    last_used: std::sync::Mutex<Instant>,
    /// A cell whose caller went away, interrupted but not yet settled.
    abandoned: std::sync::Mutex<Option<String>>,
}

#[derive(Default)]
struct State {
    kernel: Option<Kernel>,
    /// The (conversation, project) the SDK was last scoped to; reset with
    /// each start, so a restarted kernel is scoped again.
    scope: Option<(Option<String>, Option<String>)>,
}

impl Kernels {
    pub fn new(launch: Launch, root: &Path, pool: SqlitePool) -> Arc<Self> {
        Arc::new(Kernels { bootstrap: bootstrap(root), launch, pool, sessions: Default::default() })
    }

    /// Run one cell in `key`'s kernel, starting it if need be. The output
    /// is what the agent reads: text streams, results, tracebacks.
    pub async fn run(&self, key: &str, cell: &Cell<'_>) -> Result<String, String> {
        let session = self.session(key).await;
        session.run(self, cell).await
    }

    async fn session(&self, key: &str) -> Arc<Session> {
        let (session, victim) = {
            let mut sessions = self.sessions.lock().await;
            if let Some(s) = sessions.get(key) {
                return s.clone();
            }
            let victim = (sessions.len() >= MAX_KERNELS)
                .then(|| sessions.iter().min_by_key(|(_, s)| s.last_used()).map(|(k, _)| k.clone()))
                .flatten()
                .and_then(|k| sessions.remove(&k));
            let session = Arc::new(Session::new(key));
            sessions.insert(key.to_string(), session.clone());
            (session, victim)
        };
        if let Some(victim) = victim {
            tracing::info!("evicting LRU kernel {} (capacity {MAX_KERNELS})", victim.key);
            // Outside the registry's lock: the victim may be mid-cell, and
            // waiting for it mustn't stall every other session.
            victim.shutdown().await;
        }
        session
    }

    pub async fn shutdown(&self, key: &str) {
        let session = self.sessions.lock().await.remove(key);
        if let Some(s) = session {
            s.shutdown().await;
        }
    }

    pub async fn shutdown_all(&self) {
        let sessions: Vec<_> = self.sessions.lock().await.drain().map(|(_, s)| s).collect();
        for s in sessions {
            s.shutdown().await;
        }
    }

    /// Shut down every kernel untouched for longer than `max_idle`.
    pub async fn reap_idle(&self, max_idle: Duration) -> usize {
        let stale: Vec<Arc<Session>> = {
            let mut sessions = self.sessions.lock().await;
            let keys: Vec<String> =
                sessions.iter().filter(|(_, s)| s.last_used().elapsed() > max_idle).map(|(k, _)| k.clone()).collect();
            keys.iter().filter_map(|k| sessions.remove(k)).collect()
        };
        for s in &stale {
            s.shutdown().await;
        }
        if !stale.is_empty() {
            let keys: Vec<&str> = stale.iter().map(|s| s.key.as_str()).collect();
            tracing::info!("reaped {} idle kernel(s): {}", stale.len(), keys.join(", "));
        }
        stale.len()
    }

    /// The idle reaper, for the life of the edge.
    pub async fn reap_forever(self: Arc<Self>, max_idle: Duration) {
        let mut tick = tokio::time::interval(REAP_EVERY.min(max_idle));
        tick.tick().await;
        loop {
            tick.tick().await;
            self.reap_idle(max_idle).await;
        }
    }

    /// Whether `conversation_id` has a tool call waiting on a human
    /// (`core/tool_gate.py:has_open_gate`).
    async fn has_open_gate(&self, conversation_id: Option<&str>) -> bool {
        let Some(conv) = conversation_id.filter(|c| !c.is_empty()) else { return false };
        sqlx::query_scalar::<_, i64>(
            "SELECT 1 FROM approvals WHERE source = 'tool' AND status = 'pending' AND parent_id = ? LIMIT 1",
        )
        .bind(conv)
        .fetch_optional(&self.pool)
        .await
        .map(|r| r.is_some())
        .unwrap_or(false)
    }
}

impl Session {
    fn new(key: &str) -> Self {
        Session {
            key: key.to_string(),
            state: Default::default(),
            last_used: std::sync::Mutex::new(Instant::now()),
            abandoned: Default::default(),
        }
    }

    fn last_used(&self) -> Instant {
        *self.last_used.lock().expect("not poisoned")
    }

    fn touch(&self) {
        *self.last_used.lock().expect("not poisoned") = Instant::now();
    }

    async fn run(&self, kernels: &Kernels, cell: &Cell<'_>) -> Result<String, String> {
        let mut st = self.state.lock().await;
        self.ensure_started(kernels, &mut st).await?;
        self.touch();
        let State { kernel, scope } = &mut *st;
        let kernel = kernel.as_mut().expect("started");
        let abandoned = self.abandoned.lock().expect("not poisoned").take();
        if let Some(id) = abandoned {
            // Let its interrupt land first. A request the kernel receives
            // before it has raised is aborted (`stop_on_error`), and would
            // come back as no output at all.
            let _ = tokio::time::timeout(INTERRUPT_DRAIN_TIMEOUT, drain(kernel, &id, &mut vec![])).await;
        }

        let wanted = (cell.conversation_id.map(str::to_string), cell.project_id.map(str::to_string));
        let any = |v: &Option<String>| v.as_deref().is_some_and(|s| !s.is_empty());
        if (any(&wanted.0) || any(&wanted.1)) && scope.as_ref() != Some(&wanted) {
            // Once per (re)start, or again if the conversation joins a project.
            kernel.execute(&scope_code(cell.conversation_id, cell.project_id), true).await?;
            *scope = Some(wanted);
        }

        let id = kernel.execute(cell.code, false).await?;
        let mut busy = Busy::new(self, kernel.pid(), &id);
        let mut out = vec![];
        let mut interrupted = false;
        if tokio::time::timeout(cell.timeout, drain(kernel, &id, &mut out)).await.is_err()
            && !hold(kernels, kernel, &id, &mut out, cell.conversation_id).await
        {
            interrupted = true;
            kernel::interrupt(kernel.pid());
            let _ = tokio::time::timeout(INTERRUPT_DRAIN_TIMEOUT, drain(kernel, &id, &mut out)).await;
        }
        busy.done();
        Ok(finish(&out.concat(), interrupted, cell.timeout))
    }

    /// Start the kernel if it isn't running — or restart one that died.
    async fn ensure_started(&self, kernels: &Kernels, st: &mut State) -> Result<(), String> {
        if let Some(k) = &mut st.kernel {
            if k.alive() {
                return Ok(());
            }
            st.kernel.take().expect("present").shutdown().await;
        }
        let mut k = Kernel::start(&kernels.launch, STARTUP_TIMEOUT).await?;
        // Its output carries another parent id, so the cell after ignores it.
        k.execute(&kernels.bootstrap, true).await?;
        st.kernel = Some(k);
        st.scope = None;
        tracing::info!("kernel started for session {}", self.key);
        Ok(())
    }

    async fn shutdown(&self) {
        let kernel = self.state.lock().await.kernel.take();
        if let Some(k) = kernel {
            k.shutdown().await;
        }
        tracing::info!("kernel shut down for session {}", self.key);
    }
}

/// Marks a session busy for a cell. Dropped mid-cell — the caller went away,
/// as a cancelled run's does — it interrupts, so the kernel doesn't churn on.
struct Busy<'a> {
    session: &'a Session,
    pid: i32,
    id: &'a str,
}

impl<'a> Busy<'a> {
    fn new(session: &'a Session, pid: i32, id: &'a str) -> Self {
        Busy { session, pid, id }
    }

    fn done(&mut self) {
        self.pid = 0;
    }
}

impl Drop for Busy<'_> {
    fn drop(&mut self) {
        self.session.touch();
        if self.pid > 0 {
            tracing::info!("cell in {} abandoned — interrupting", self.session.key);
            kernel::interrupt(self.pid);
            *self.session.abandoned.lock().expect("not poisoned") = Some(self.id.to_string());
        }
    }
}

/// Read output until our cell's `idle`. Output of anything else — a silent
/// preload, the tail of an interrupted cell — is skipped.
async fn drain(kernel: &mut Kernel, id: &str, out: &mut Vec<String>) {
    while let Some(msg) = kernel.next_output().await {
        if msg.parent_id.as_deref() != Some(id) {
            continue;
        }
        let c = &msg.content;
        let text = |v: &Value| v.as_str().unwrap_or_default().to_string();
        match msg.msg_type.as_str() {
            "stream" => out.push(text(&c["text"])),
            "execute_result" => out.push(text(&c["data"]["text/plain"])),
            "display_data" => {
                let data = c["data"].as_object().cloned().unwrap_or_default();
                if let Some(t) = data.get("text/plain") {
                    out.push(text(t));
                }
                // Rich output can't ride the text channel; say it was there.
                let mut rich: Vec<&str> = data.keys().map(String::as_str).filter(|k| *k != "text/plain").collect();
                if !rich.is_empty() {
                    rich.sort_unstable();
                    out.push(format!("[{} output — not shown as text]", rich.join(", ")));
                }
            }
            "error" => {
                let lines: Vec<String> =
                    c["traceback"].as_array().map(|a| a.iter().map(text).collect()).unwrap_or_default();
                out.push(strip_ansi(&lines.join("\n")));
            }
            "status" if c["execution_state"] == "idle" => return,
            _ => {}
        }
    }
}

/// Past its timeout, keep waiting while a tool approval for this
/// conversation is open. True if the cell finished meanwhile.
async fn hold(kernels: &Kernels, kernel: &mut Kernel, id: &str, out: &mut Vec<String>, conv: Option<&str>) -> bool {
    let mut waited = Duration::ZERO;
    while waited < MAX_HOLD {
        if !kernels.has_open_gate(conv).await {
            return false;
        }
        if waited.is_zero() {
            tracing::info!("cell held past its timeout: waiting on a tool approval");
        }
        if tokio::time::timeout(HOLD_POLL, drain(kernel, id, out)).await.is_ok() {
            return true;
        }
        waited += HOLD_POLL;
    }
    tracing::warn!("approval hold hit its {}s ceiling — interrupting the cell", MAX_HOLD.as_secs());
    false
}

/// The tool result: output trimmed and capped, a note if it was cut short.
fn finish(output: &str, interrupted: bool, timeout: Duration) -> String {
    let mut result = output.trim().to_string();
    let len = result.chars().count();
    if len > MAX_OUTPUT_CHARS {
        result = result.chars().take(MAX_OUTPUT_CHARS).collect();
        result.push_str(&format!("\n... [truncated {} chars]", len - MAX_OUTPUT_CHARS));
    }
    if interrupted {
        let note = format!(
            "[execution timed out after {}s — kernel interrupted; session state is preserved]",
            timeout.as_secs_f64() as i64
        );
        result = if result.is_empty() { note } else { format!("{result}\n{note}").trim().to_string() };
    }
    if result.is_empty() { "(no output)".into() } else { result }
}

/// IPython colours its tracebacks; the model reads plain text.
fn strip_ansi(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(at) = rest.find("\x1b[") {
        out.push_str(&rest[..at]);
        let after = &rest[at + 2..];
        let end = after.find(|c: char| !(c.is_ascii_digit() || c == ';'));
        match end {
            Some(e) if after[e..].starts_with('m') => rest = &after[e + 1..],
            _ => {
                out.push_str("\x1b[");
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

/// A Python string literal (a JSON string is one) or `None`.
fn py(s: Option<&str>) -> String {
    s.map_or_else(|| "None".into(), |s| serde_json::to_string(s).expect("a string serializes"))
}

/// Puts the checkout on `sys.path` and preloads `search`/`read` and the SDK
/// as `jarvis`. A failure only means the agent imports them itself.
fn bootstrap(root: &Path) -> String {
    format!(
        "try:\n    import sys as _sys\n    _root = {}\n    if _root not in _sys.path:\n        _sys.path.insert(0, _root)\n    from tools.research import search, read\nexcept Exception:\n    pass\ntry:\n    import tools.sdk as jarvis\nexcept Exception:\n    pass\n",
        py(Some(&root.to_string_lossy()))
    )
}

fn scope_code(conversation_id: Option<&str>, project_id: Option<&str>) -> String {
    format!(
        "try:\n    import tools.sdk as _jarvis_sdk\n    _jarvis_sdk.set_conversation({})\n    _jarvis_sdk.set_project({})\nexcept Exception:\n    pass\n",
        py(conversation_id),
        py(project_id)
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn results_are_trimmed_capped_and_noted() {
        let t = Duration::from_secs(60);
        assert_eq!(finish("  \n", false, t), "(no output)");
        assert_eq!(finish(" 2\n", false, t), "2");
        assert_eq!(
            finish("", true, t),
            "[execution timed out after 60s — kernel interrupted; session state is preserved]"
        );
        assert!(finish("partial\n", true, Duration::from_secs_f64(2.5)).starts_with("partial\n[execution timed out after 2s"));
        let long = "é".repeat(MAX_OUTPUT_CHARS + 7);
        assert!(finish(&long, false, t).ends_with("\n... [truncated 7 chars]"));
    }

    #[test]
    fn ansi_colours_come_off() {
        assert_eq!(strip_ansi("\x1b[0;31mNameError\x1b[0m: x\x1b[1;32m"), "NameError: x");
        assert_eq!(strip_ansi("a\x1b[2Jb\x1b["), "a\x1b[2Jb\x1b[");
    }

    async fn registry() -> Arc<Kernels> {
        let pool = SqlitePool::connect("sqlite::memory:").await.unwrap();
        let launch = Launch { python: "python3".into(), dir: ".".into(), env: vec![] };
        Kernels::new(launch, Path::new("/app"), pool)
    }

    async fn keys(k: &Kernels) -> Vec<String> {
        let mut keys: Vec<String> = k.sessions.lock().await.keys().cloned().collect();
        keys.sort();
        keys
    }

    #[tokio::test]
    async fn the_least_recently_used_session_goes_past_the_cap() {
        let k = registry().await;
        for i in 0..MAX_KERNELS {
            k.session(&format!("s{i:02}")).await;
        }
        // s00 was used last, so s01 is the oldest.
        k.session("s00").await.touch();
        k.session("new").await;
        let keys = keys(&k).await;
        assert_eq!(keys.len(), MAX_KERNELS);
        assert!(keys.contains(&"s00".to_string()) && !keys.contains(&"s01".to_string()));
        // An existing session is handed back, not replaced.
        assert!(Arc::ptr_eq(&k.session("new").await, &k.session("new").await));
    }

    #[tokio::test]
    async fn idle_sessions_are_reaped() {
        let k = registry().await;
        k.session("old").await;
        tokio::time::sleep(Duration::from_millis(20)).await;
        k.session("fresh").await;
        assert_eq!(k.reap_idle(Duration::from_millis(10)).await, 1);
        assert_eq!(keys(&k).await, ["fresh"]);
        k.shutdown("fresh").await;
        assert!(keys(&k).await.is_empty());
    }

    #[test]
    fn literals_are_python() {
        assert_eq!(py(None), "None");
        assert_eq!(py(Some("it's \"q\"\n")), r#""it's \"q\"\n""#);
        assert!(scope_code(Some("c1"), None).contains("set_conversation(\"c1\")\n    _jarvis_sdk.set_project(None)"));
    }
}
