//! The agent's notebooks — a port of `core/kernels.py`. One IPython kernel per
//! session key (a conversation, or a worker's own key), started on its first
//! cell, its variables kept between cells like a Jupyter notebook.
//!
//! - Cells in one session run one at a time.
//! - Every kernel preloads what `tools/preload.py` names — `search`/`read`,
//!   the `jarvis` SDK (scoped to the cell's conversation and project), and
//!   the cheap modules; not pandas and the like, for the RAM.
//! - A cell gets `timeout` seconds (`run_cell` allows up to `MAX_TIMEOUT`),
//!   then is interrupted — the session survives. Not while a tool approval
//!   for its conversation is open (the SDK waits for one inside the cell),
//!   up to 30 minutes.
//! - At most `Limits::max_kernels` live kernels (the least recently used
//!   goes, one with no cell in it first), and one untouched for
//!   `Limits::idle` is shut down — a sub-agent's (`surface = 'subagent'`)
//!   after `Limits::subagent_idle`, once its turn is over — unless a cell is
//!   running in it or waiting to.
//! - A kernel started again for a session whose last one ran cells — reaped,
//!   evicted, or dead — says so on its first cell's result.
//! - A cell's output is text; the PNG and JPEG images it shows are kept
//!   beside it (`Output`), for a caller whose model reads them.

mod kernel;
mod wire;

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use serde_json::{Map, Value};
use sqlx::SqlitePool;

pub use kernel::Launch;
use kernel::Kernel;

pub const MAX_KERNELS: usize = 12;
pub const IDLE_TIMEOUT: Duration = Duration::from_secs(30 * 60);
/// How long a sub-agent's kernel is kept once its turn is over: a sub-agent
/// given more work later starts a fresh one.
pub const SUBAGENT_IDLE_TIMEOUT: Duration = Duration::from_secs(5 * 60);
/// The longest a caller may give a cell.
pub const MAX_TIMEOUT: Duration = Duration::from_secs(600);
/// How many sessions whose kernel went with cells run in it are remembered.
const LOST_CAP: usize = 512;
const RESTARTED: &str = "[This kernel was restarted since your last cell (it sat idle, or stopped): variables, \
imports and data from earlier cells are gone.]";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const INTERRUPT_DRAIN_TIMEOUT: Duration = Duration::from_secs(5);
const HOLD_POLL: Duration = Duration::from_secs(5);
const MAX_HOLD: Duration = Duration::from_secs(30 * 60);
const MAX_OUTPUT_CHARS: usize = 30_000;
/// The reaper's pace (`register_kernel_reaper_job`: every 10 minutes).
const REAP_EVERY: Duration = Duration::from_secs(10 * 60);

/// The image types a cell's result can carry to the model, preferred first.
const SEEN_TYPES: [&str; 2] = ["image/png", "image/jpeg"];
/// The most images one cell's result carries; any after are named, not kept.
const MAX_CELL_IMAGES: usize = 4;
/// The largest image (decoded) a result carries.
const MAX_IMAGE_BYTES: usize = 1536 * 1024;
/// The longest side an image may have — Anthropic's and Bedrock's limit.
const MAX_IMAGE_SIDE: u32 = 8000;

/// One cell to run.
pub struct Cell<'a> {
    pub code: &'a str,
    pub timeout: Duration,
    pub conversation_id: Option<&'a str>,
    pub project_id: Option<&'a str>,
}

/// An image a cell showed, as its bytes.
pub struct Image {
    /// What the bytes are, read from them: `image/png` or `image/jpeg`.
    pub mime_type: String,
    pub data: Vec<u8>,
}

/// How many more images, and bytes of them, a result may carry.
#[derive(Clone, Copy, Debug)]
pub struct Room {
    pub images: usize,
    pub bytes: usize,
}

/// What a cell put out: text for any caller (`text`), or the text with the
/// images it showed for one whose model reads them (`seen`).
pub struct Output {
    pieces: Vec<Piece>,
    interrupted: bool,
    timeout: Duration,
    /// The kernel was started again since the session's last cell.
    restarted: bool,
}

/// One output message, in order.
enum Piece {
    Text(String),
    /// A rich output's types the text channel can't carry — said in one
    /// line by a `display_data`, never by an `execute_result` — and the
    /// image among them.
    Rich { types: Vec<String>, said: bool, picture: Option<Picture> },
}

/// An image a cell showed: kept, or why it can't go to the model.
enum Picture {
    Kept(Image),
    Unshown { mime_type: String, why: String },
}

impl Output {
    /// The result as text, each image a line saying it was there.
    pub fn text(&self) -> String {
        let mut out = String::new();
        for piece in &self.pieces {
            match piece {
                Piece::Text(t) => out.push_str(t),
                Piece::Rich { types, said: true, .. } => out.push_str(&not_text(types)),
                Piece::Rich { .. } => {}
            }
        }
        self.closed(&out)
    }

    /// The text trimmed and capped, with the notes on a restart and a timeout.
    fn closed(&self, out: &str) -> String {
        noted(finish(out, self.interrupted, self.timeout), self.restarted, self.interrupted.then_some(self.timeout))
    }

    /// Whether the cell showed an image the model could be sent.
    pub fn has_images(&self) -> bool {
        self.pieces.iter().any(|p| matches!(p, Piece::Rich { picture: Some(Picture::Kept(_)), .. }))
    }

    /// The result as text naming each image where it was shown, and the
    /// images that fit in `room`, in order; one that doesn't fit, or can't
    /// be sent at all, is a line saying why.
    pub fn seen(mut self, mut room: Room) -> (String, Vec<Image>) {
        let mut out = String::new();
        let mut images = vec![];
        for piece in std::mem::take(&mut self.pieces) {
            let (types, said, picture) = match piece {
                Piece::Text(t) => {
                    out.push_str(&t);
                    continue;
                }
                Piece::Rich { types, said, picture } => (types, said, picture),
            };
            let fits = |image: &Image| room.images > 0 && image.data.len() <= room.bytes;
            match &picture {
                Some(Picture::Kept(image)) if fits(image) => {
                    out.push_str(&format!("[{} output — image {}, shown after this text]", image.mime_type, images.len() + 1));
                }
                Some(Picture::Kept(image)) => out.push_str(&format!(
                    "[{} output — not shown: this conversation already carries as many images as the model is sent at once]",
                    image.mime_type
                )),
                Some(Picture::Unshown { mime_type, why }) => out.push_str(&format!("[{mime_type} output — not shown: {why}]")),
                None => {}
            }
            // The image's other renderings are the same picture.
            let rest: Vec<&String> = match picture {
                Some(_) => types.iter().filter(|t| !SEEN_TYPES.contains(&t.as_str())).collect(),
                None => types.iter().collect(),
            };
            if said && !rest.is_empty() {
                out.push_str(&not_text(&rest));
            }
            if let Some(Picture::Kept(image)) = picture
                && fits(&image)
            {
                room.images -= 1;
                room.bytes -= image.data.len();
                images.push(image);
            }
        }
        (self.closed(&out), images)
    }
}

/// The line that says a display's rich types were there.
fn not_text<S: AsRef<str>>(types: &[S]) -> String {
    let types: Vec<&str> = types.iter().map(AsRef::as_ref).collect();
    format!("[{} output — not shown as text]", types.join(", "))
}

/// How many kernels may live, and how long one may sit idle.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Limits {
    pub max_kernels: usize,
    pub idle: Duration,
    /// A sub-agent's kernel, once its turn is over.
    pub subagent_idle: Duration,
}

impl Default for Limits {
    fn default() -> Self {
        Limits { max_kernels: MAX_KERNELS, idle: IDLE_TIMEOUT, subagent_idle: SUBAGENT_IDLE_TIMEOUT }
    }
}

impl Limits {
    /// `JARVIS_MAX_KERNELS`, and `JARVIS_KERNEL_IDLE_TIMEOUT` /
    /// `JARVIS_SUBAGENT_KERNEL_IDLE_TIMEOUT` in seconds — each else its
    /// default, as is a value that isn't a positive number.
    pub fn from_env() -> Self {
        Self::from_vars(|key| std::env::var(key).ok())
    }

    fn from_vars(var: impl Fn(&str) -> Option<String>) -> Self {
        let d = Limits::default();
        let read = |key: &str| var(key).map(|v| v.trim().to_string()).filter(|v| !v.is_empty());
        let secs = |key: &str, default: Duration| {
            let v = read(key)?;
            let s = v.parse::<f64>().ok().filter(|s| s.is_finite() && *s > 0.0);
            if s.is_none() {
                tracing::warn!("{key}={v:?} is not a number of seconds — using {}s", default.as_secs());
            }
            s.map(Duration::from_secs_f64)
        };
        let max_kernels = read("JARVIS_MAX_KERNELS").and_then(|v| {
            let n = v.parse::<usize>().ok().filter(|n| *n > 0);
            if n.is_none() {
                tracing::warn!("JARVIS_MAX_KERNELS={v:?} is not a positive whole number — using {}", d.max_kernels);
            }
            n
        });
        Limits {
            max_kernels: max_kernels.unwrap_or(d.max_kernels),
            idle: secs("JARVIS_KERNEL_IDLE_TIMEOUT", d.idle).unwrap_or(d.idle),
            subagent_idle: secs("JARVIS_SUBAGENT_KERNEL_IDLE_TIMEOUT", d.subagent_idle).unwrap_or(d.subagent_idle),
        }
    }
}

pub struct Kernels {
    launch: Launch,
    /// Run silently in every kernel right after it starts.
    bootstrap: String,
    /// For the approval hold, and to tell a sub-agent's session.
    pool: SqlitePool,
    limits: Limits,
    sessions: tokio::sync::Mutex<HashMap<String, Arc<Session>>>,
    /// Sessions whose kernel was reaped or evicted after cells ran in it: the
    /// next cell there is told its variables are gone.
    lost: std::sync::Mutex<HashSet<String>>,
}

struct Session {
    key: String,
    state: tokio::sync::Mutex<State>,
    last_used: std::sync::Mutex<Instant>,
    /// A cell whose caller went away, interrupted but not yet settled.
    abandoned: std::sync::Mutex<Option<String>>,
    /// Cells running or waiting for the one running (`InUse`).
    users: AtomicUsize,
    /// A cell has been sent to one of its kernels.
    ran: AtomicBool,
    /// Its kernel was started again after cells ran in an earlier one; the
    /// next cell's result says so.
    restarted: AtomicBool,
}

#[derive(Default)]
struct State {
    kernel: Option<Kernel>,
    /// The (conversation, project) the SDK was last scoped to; reset with
    /// each start, so a restarted kernel is scoped again.
    scope: Option<(Option<String>, Option<String>)>,
}

impl Kernels {
    /// With the limits the environment sets (`Limits::from_env`).
    pub fn new(launch: Launch, root: &Path, pool: SqlitePool) -> Arc<Self> {
        Self::with_limits(launch, root, pool, Limits::from_env())
    }

    pub fn with_limits(launch: Launch, root: &Path, pool: SqlitePool, limits: Limits) -> Arc<Self> {
        Arc::new(Kernels {
            bootstrap: bootstrap(root),
            launch,
            pool,
            limits,
            sessions: Default::default(),
            lost: Default::default(),
        })
    }

    /// Run one cell in `key`'s kernel, starting it if need be. The output
    /// is what the agent reads: text streams, results, tracebacks.
    pub async fn run(&self, key: &str, cell: &Cell<'_>) -> Result<String, String> {
        self.run_with_images(key, cell).await.map(|out| out.text())
    }

    /// `run`, with the images the cell showed kept beside its text.
    pub async fn run_with_images(&self, key: &str, cell: &Cell<'_>) -> Result<Output, String> {
        let session = self.session(key).await;
        session.run(self, cell).await
    }

    async fn session(&self, key: &str) -> Arc<Session> {
        let (session, victim) = {
            let mut sessions = self.sessions.lock().await;
            if let Some(s) = sessions.get(key) {
                // Under the lock, so the reaper can't take it before its cell starts.
                s.touch();
                return s.clone();
            }
            // The least recently used — one with no cell running, if any.
            let victim = (sessions.len() >= self.limits.max_kernels)
                .then(|| sessions.iter().min_by_key(|(_, s)| (s.in_use(), s.last_used())).map(|(k, _)| k.clone()))
                .flatten()
                .and_then(|k| sessions.remove(&k));
            let session = Arc::new(Session::new(key));
            if self.lost.lock().expect("not poisoned").remove(key) {
                session.restarted.store(true, Ordering::Relaxed);
            }
            sessions.insert(key.to_string(), session.clone());
            (session, victim)
        };
        if let Some(victim) = victim {
            tracing::info!("evicting LRU kernel {} (capacity {})", victim.key, self.limits.max_kernels);
            self.lose(&victim);
            // Outside the registry's lock: the victim may be mid-cell, and
            // waiting for it mustn't stall every other session.
            victim.shutdown().await;
        }
        session
    }

    /// Remember that `session`'s kernel went with cells run in it, so its
    /// next cell is told.
    fn lose(&self, session: &Session) {
        if !(session.ran.load(Ordering::Relaxed) || session.restarted.load(Ordering::Relaxed)) {
            return;
        }
        let mut lost = self.lost.lock().expect("not poisoned");
        if lost.len() >= LOST_CAP
            && let Some(any) = lost.iter().next().cloned()
        {
            lost.remove(&any);
        }
        lost.insert(session.key.clone());
    }

    /// Shut `key`'s kernel down for good — its conversation or worker is gone.
    pub async fn shutdown(&self, key: &str) {
        self.lost.lock().expect("not poisoned").remove(key);
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

    /// Shut down every kernel with no cell running that has been untouched
    /// for longer than `Limits::idle` — or, a sub-agent's whose turn is
    /// over, `Limits::subagent_idle`.
    pub async fn reap_idle(&self) -> usize {
        let Limits { idle, subagent_idle, .. } = self.limits;
        let idle_for = |s: &Session, d: Duration| !s.in_use() && s.last_used().elapsed() > d;
        let early: Vec<String> =
            self.sessions.lock().await.values().filter(|s| idle_for(s, subagent_idle)).map(|s| s.key.clone()).collect();
        let finished = if early.is_empty() { HashSet::new() } else { self.finished_subagents(&early).await };
        let stale: Vec<Arc<Session>> = {
            let mut sessions = self.sessions.lock().await;
            let keys: Vec<String> = sessions
                .iter()
                .filter(|(k, s)| idle_for(s, idle) || (finished.contains(*k) && idle_for(s, subagent_idle)))
                .map(|(k, _)| k.clone())
                .collect();
            keys.iter().filter_map(|k| sessions.remove(k)).collect()
        };
        for s in &stale {
            self.lose(s);
            s.shutdown().await;
        }
        if !stale.is_empty() {
            let keys: Vec<&str> = stale.iter().map(|s| s.key.as_str()).collect();
            tracing::info!("reaped {} idle kernel(s): {}", stale.len(), keys.join(", "));
        }
        stale.len()
    }

    /// The idle reaper, for the life of the edge: a kernel goes at most half
    /// its timeout late.
    pub async fn reap_forever(self: Arc<Self>) {
        let Limits { idle, subagent_idle, .. } = self.limits;
        let every = REAP_EVERY.min(idle / 2).min(subagent_idle / 2).max(Duration::from_millis(100));
        let mut tick = tokio::time::interval(every);
        tick.tick().await;
        loop {
            tick.tick().await;
            self.reap_idle().await;
        }
    }

    /// Which of `keys` are sub-agents' conversations with no turn running or
    /// waiting to run — whose kernels can go early.
    async fn finished_subagents(&self, keys: &[String]) -> HashSet<String> {
        let keys = serde_json::to_string(keys).expect("strings serialize");
        sqlx::query_scalar::<_, String>(
            "SELECT c.id FROM conversations c WHERE c.surface = 'subagent' AND c.id IN (SELECT value FROM json_each(?)) \
             AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.thread_id = c.id AND j.status IN ('pending', 'running'))",
        )
        .bind(keys)
        .fetch_all(&self.pool)
        .await
        .map(|ids| ids.into_iter().collect())
        .unwrap_or_else(|e| {
            tracing::warn!("kernel reaper: can't tell which kernels are sub-agents': {e}");
            HashSet::new()
        })
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
            users: AtomicUsize::new(0),
            ran: AtomicBool::new(false),
            restarted: AtomicBool::new(false),
        }
    }

    fn last_used(&self) -> Instant {
        *self.last_used.lock().expect("not poisoned")
    }

    fn touch(&self) {
        *self.last_used.lock().expect("not poisoned") = Instant::now();
    }

    fn in_use(&self) -> bool {
        self.users.load(Ordering::SeqCst) > 0
    }

    async fn run(&self, kernels: &Kernels, cell: &Cell<'_>) -> Result<Output, String> {
        let _in_use = InUse::new(self);
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

        self.ran.store(true, Ordering::Relaxed);
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
        let restarted = self.restarted.swap(false, Ordering::Relaxed);
        Ok(Output { pieces: out, interrupted, timeout: cell.timeout, restarted })
    }

    /// Start the kernel if it isn't running — or restart one that died.
    async fn ensure_started(&self, kernels: &Kernels, st: &mut State) -> Result<(), String> {
        if let Some(k) = &mut st.kernel {
            if k.alive() {
                return Ok(());
            }
            st.kernel.take().expect("present").shutdown().await;
            self.restarted.store(true, Ordering::Relaxed);
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

/// A cell in a session, from before it waits for the one running to its
/// end: the reaper and the cap leave the session alone meanwhile, however
/// long the cell's timeout or approval hold.
struct InUse<'a>(&'a Session);

impl<'a> InUse<'a> {
    fn new(session: &'a Session) -> Self {
        session.users.fetch_add(1, Ordering::SeqCst);
        InUse(session)
    }
}

impl Drop for InUse<'_> {
    fn drop(&mut self) {
        self.0.touch();
        self.0.users.fetch_sub(1, Ordering::SeqCst);
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
async fn drain(kernel: &mut Kernel, id: &str, out: &mut Vec<Piece>) {
    while let Some(msg) = kernel.next_output().await {
        if msg.parent_id.as_deref() != Some(id) {
            continue;
        }
        let c = &msg.content;
        let text = |v: &Value| v.as_str().unwrap_or_default().to_string();
        match msg.msg_type.as_str() {
            "stream" => out.push(Piece::Text(text(&c["text"]))),
            kind @ ("execute_result" | "display_data") => {
                let data = c["data"].as_object().cloned().unwrap_or_default();
                if let Some(t) = data.get("text/plain") {
                    out.push(Piece::Text(text(t)));
                }
                // Rich output can't ride the text channel; a display says it was there.
                let mut types: Vec<String> = data.keys().filter(|k| *k != "text/plain").cloned().collect();
                if !types.is_empty() {
                    types.sort_unstable();
                    let kept = out.iter().filter(|p| matches!(p, Piece::Rich { picture: Some(Picture::Kept(_)), .. })).count();
                    out.push(Piece::Rich { types, said: kind == "display_data", picture: picture(&data, kept) });
                }
            }
            "error" => {
                let lines: Vec<String> =
                    c["traceback"].as_array().map(|a| a.iter().map(text).collect()).unwrap_or_default();
                out.push(Piece::Text(strip_ansi(&lines.join("\n"))));
            }
            "status" if c["execution_state"] == "idle" => return,
            _ => {}
        }
    }
}

/// The image in a rich output's `data`, if it holds one: kept, or why it
/// can't go to the model. `kept` is how many the cell has kept already.
fn picture(data: &Map<String, Value>, kept: usize) -> Option<Picture> {
    let (mime, encoded) = SEEN_TYPES.iter().find_map(|t| Some((*t, data.get(*t)?.as_str()?)))?;
    let unshown = |why: String| Some(Picture::Unshown { mime_type: mime.to_string(), why });
    if kept >= MAX_CELL_IMAGES {
        return unshown(format!("a result shows at most {MAX_CELL_IMAGES} images"));
    }
    let encoded: String = encoded.chars().filter(|c| !c.is_ascii_whitespace()).collect();
    let size = encoded.len() / 4 * 3;
    if size > MAX_IMAGE_BYTES {
        return unshown(format!("too large ({}; the limit is {})", megabytes(size), megabytes(MAX_IMAGE_BYTES)));
    }
    let unreadable = || unshown("not a PNG or JPEG image the model can read".into());
    let Ok(bytes) = STANDARD.decode(&encoded) else { return unreadable() };
    let (Some(kind), Some((w, h))) = (image_type(&bytes), dimensions(&bytes)) else { return unreadable() };
    if w == 0 || h == 0 {
        return unreadable();
    }
    if w.max(h) > MAX_IMAGE_SIDE {
        return unshown(format!("too large ({w}×{h} px; the limit is {MAX_IMAGE_SIDE} px a side)"));
    }
    Some(Picture::Kept(Image { mime_type: kind.into(), data: bytes }))
}

fn megabytes(bytes: usize) -> String {
    format!("{:.1} MB", bytes as f64 / (1024.0 * 1024.0))
}

/// What the bytes are — a provider refuses an image whose type is misnamed.
fn image_type(data: &[u8]) -> Option<&'static str> {
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if data.starts_with(&[0xFF, 0xD8, 0xFF]) {
        Some("image/jpeg")
    } else {
        None
    }
}

/// A PNG's or JPEG's width and height, from its header.
fn dimensions(data: &[u8]) -> Option<(u32, u32)> {
    let be16 = |i: usize| -> Option<u32> { Some(u16::from_be_bytes([*data.get(i)?, *data.get(i + 1)?]).into()) };
    let be32 = |i: usize| -> Option<u32> { Some(u32::from_be_bytes(data.get(i..i + 4)?.try_into().ok()?)) };
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        if data.get(12..16)? != b"IHDR" {
            return None;
        }
        return Some((be32(16)?, be32(20)?));
    }
    // JPEG: the segments after the start, to the first frame header.
    let mut i = 2;
    while i + 1 < data.len() {
        if data[i] != 0xFF {
            return None;
        }
        match data[i + 1] {
            0xFF => i += 1,
            0x01 | 0xD0..=0xD9 => i += 2,
            m @ 0xC0..=0xCF if !matches!(m, 0xC4 | 0xC8 | 0xCC) => return Some((be16(i + 7)?, be16(i + 5)?)),
            _ => i += 2 + be16(i + 2)? as usize,
        }
    }
    None
}

/// Past its timeout, keep waiting while a tool approval for this
/// conversation is open. True if the cell finished meanwhile.
async fn hold(kernels: &Kernels, kernel: &mut Kernel, id: &str, out: &mut Vec<Piece>, conv: Option<&str>) -> bool {
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
        result = cut_middle(&result, len);
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

/// `s`, `len` characters long, cut to `MAX_OUTPUT_CHARS` with the middle
/// left out: the head says what ran, the tail holds a traceback or the
/// final result.
fn cut_middle(s: &str, len: usize) -> String {
    let marker = |n: usize| format!("\n\n... [{n} chars truncated from the middle] ...\n\n");
    // Room for the widest count the marker could name, so the whole fits.
    let room = MAX_OUTPUT_CHARS - marker(len).chars().count();
    let (head, tail) = (room / 2, room - room / 2);
    let at = |n: usize| s.char_indices().nth(n).map_or(s.len(), |(i, _)| i);
    format!("{}{}{}", &s[..at(head)], marker(len - head - tail), &s[at(len - tail)..])
}

/// The result with what the agent should know besides: that its kernel was
/// restarted (first), and, when the cell ran out of time (`timed_out`), how
/// to give work that long room.
fn noted(result: String, restarted: bool, timed_out: Option<Duration>) -> String {
    let mut out = if restarted { format!("{RESTARTED}\n{result}") } else { result };
    if let Some(timeout) = timed_out {
        out.push_str(&if timeout < MAX_TIMEOUT {
            format!(
                "\n[Give longer work a larger `timeout` (at most {}s), or run it in the background with \
                 `jarvis.start_job(command)`.]",
                MAX_TIMEOUT.as_secs()
            )
        } else {
            "\n[Run work this long in the background with `jarvis.start_job(command)`.]".to_string()
        });
    }
    out
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

/// Puts the checkout on `sys.path` and preloads what `tools/preload.py`
/// names: `search`/`read`, the SDK as `jarvis`, `httpx`, `datetime` and
/// `subprocess`. A failure only means the agent imports them itself.
fn bootstrap(root: &Path) -> String {
    format!(
        "try:\n    import sys as _sys\n    _root = {}\n    if _root not in _sys.path:\n        _sys.path.insert(0, _root)\n    from tools.preload import *\nexcept Exception:\n    pass\n",
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
        let exact = "é".repeat(MAX_OUTPUT_CHARS);
        assert_eq!(finish(&exact, false, t), exact);
    }

    #[test]
    fn a_long_result_keeps_its_head_and_its_tail() {
        let t = Duration::from_secs(60);
        let long = format!("start{}Traceback: boom", "é".repeat(MAX_OUTPUT_CHARS + 7));
        let len = long.chars().count();
        let cut = finish(&long, false, t);
        assert!(cut.chars().count() <= MAX_OUTPUT_CHARS);
        assert!(cut.starts_with("startéé") && cut.ends_with("ééTraceback: boom"), "{cut}");
        let marker = cut.split("\n\n... [").nth(1).unwrap();
        assert!(marker.contains(" chars truncated from the middle] ...\n\n"));
        let dropped: usize = marker.split(' ').next().unwrap().parse().unwrap();
        // What is left out, and what is kept, add up to the whole.
        let said = format!("\n\n... [{dropped} chars truncated from the middle] ...\n\n").chars().count();
        assert_eq!(cut.chars().count() - said + dropped, len);
        // A note after the cut stays outside it.
        assert!(finish(&long, true, t).ends_with(
            "ééTraceback: boom\n[execution timed out after 60s — kernel interrupted; session state is preserved]"
        ));
    }

    #[test]
    fn ansi_colours_come_off() {
        assert_eq!(strip_ansi("\x1b[0;31mNameError\x1b[0m: x\x1b[1;32m"), "NameError: x");
        assert_eq!(strip_ansi("a\x1b[2Jb\x1b["), "a\x1b[2Jb\x1b[");
    }

    /// A 1×1 PNG.
    const DOT: &str = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

    fn data(pairs: &[(&str, &str)]) -> Map<String, Value> {
        pairs.iter().map(|(k, v)| (k.to_string(), Value::String(v.to_string()))).collect()
    }

    fn output(pieces: Vec<Piece>) -> Output {
        Output { pieces, interrupted: false, timeout: Duration::from_secs(60), restarted: false }
    }

    fn display(d: &Map<String, Value>, kept: usize) -> Piece {
        let mut types: Vec<String> = d.keys().filter(|k| *k != "text/plain").cloned().collect();
        types.sort_unstable();
        Piece::Rich { types, said: true, picture: picture(d, kept) }
    }

    #[test]
    fn images_are_read_from_their_bytes() {
        let png = STANDARD.decode(DOT).unwrap();
        assert_eq!((image_type(&png), dimensions(&png)), (Some("image/png"), Some((1, 1))));
        // A baseline JPEG's frame header: 640 wide, 480 high.
        let jpeg = [
            &[0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x04, 0x00, 0x00][..],
            &[0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x01, 0xE0, 0x02, 0x80, 0x01, 0x01, 0x11, 0x00],
        ]
        .concat();
        assert_eq!((image_type(&jpeg), dimensions(&jpeg)), (Some("image/jpeg"), Some((640, 480))));
        assert_eq!(dimensions(b"GIF89a"), None);
        // A PNG named a JPEG goes as what it is.
        let Some(Picture::Kept(image)) = picture(&data(&[("image/jpeg", DOT)]), 0) else { panic!("kept") };
        assert_eq!(image.mime_type, "image/png");
        // Bytes that are neither: said, not kept.
        let Some(Picture::Unshown { why, .. }) = picture(&data(&[("image/png", "aGVsbG8=")]), 0) else { panic!() };
        assert_eq!(why, "not a PNG or JPEG image the model can read");
        assert!(picture(&data(&[("image/svg+xml", "<svg/>")]), 0).is_none());
    }

    #[test]
    fn a_cell_keeps_a_few_images_of_a_size() {
        let big = "A".repeat(MAX_IMAGE_BYTES / 3 * 4 + 8);
        let Some(Picture::Unshown { why, .. }) = picture(&data(&[("image/png", &big)]), 0) else { panic!() };
        assert_eq!(why, "too large (1.5 MB; the limit is 1.5 MB)");
        let Some(Picture::Unshown { why, .. }) = picture(&data(&[("image/png", DOT)]), MAX_CELL_IMAGES) else { panic!() };
        assert_eq!(why, "a result shows at most 4 images");
    }

    #[test]
    fn images_are_named_in_the_text_or_said_as_before() {
        let chart = data(&[("text/plain", "<Figure>"), ("image/png", DOT), ("text/html", "<img>")]);
        let pieces = || {
            vec![
                Piece::Text("drew it\n".into()),
                Piece::Text("<Figure>".into()),
                display(&chart, 0),
                // An execute_result's image: not said as text, named when seen.
                Piece::Rich { types: vec!["image/png".into()], said: false, picture: picture(&chart, 1) },
            ]
        };
        let out = output(pieces());
        assert!(out.has_images());
        assert_eq!(out.text(), "drew it\n<Figure>[image/png, text/html output — not shown as text]");

        let (text, images) = output(pieces()).seen(Room { images: 10, bytes: 1 << 20 });
        assert_eq!(
            text,
            "drew it\n<Figure>[image/png output — image 1, shown after this text][text/html output — not shown as text]\
             [image/png output — image 2, shown after this text]"
        );
        assert_eq!(images.len(), 2);

        // Room for one: the other is said.
        let (text, images) = output(pieces()).seen(Room { images: 1, bytes: 1 << 20 });
        assert_eq!(images.len(), 1);
        assert!(text.ends_with(
            "[image/png output — not shown: this conversation already carries as many images as the model is sent at once]"
        ));
        let (_, images) = output(pieces()).seen(Room { images: 10, bytes: 10 });
        assert!(images.is_empty());

        let plain = output(vec![Piece::Text("1".into())]);
        assert!(!plain.has_images());
        assert_eq!(plain.seen(Room { images: 4, bytes: 1 << 20 }).0, "1");
    }

    #[test]
    fn notes_say_what_happened() {
        assert_eq!(noted("2".into(), false, None), "2");
        assert!(noted("2".into(), true, None).starts_with("[This kernel was restarted") && noted("2".into(), true, None).ends_with("]\n2"));
        assert!(noted("x".into(), false, Some(Duration::from_secs(60))).ends_with("(at most 600s), or run it in the background with `jarvis.start_job(command)`.]"));
        assert!(noted("x".into(), false, Some(MAX_TIMEOUT)).ends_with("\n[Run work this long in the background with `jarvis.start_job(command)`.]"));
    }

    #[test]
    fn limits_come_from_the_environment() {
        let vars = |pairs: &'static [(&'static str, &'static str)]| {
            move |k: &str| pairs.iter().find(|(name, _)| *name == k).map(|(_, v)| v.to_string())
        };
        assert_eq!(Limits::from_vars(vars(&[])), Limits::default());
        assert_eq!(Limits::default().subagent_idle, Duration::from_secs(300));
        let set = Limits::from_vars(vars(&[
            ("JARVIS_MAX_KERNELS", "3"),
            ("JARVIS_KERNEL_IDLE_TIMEOUT", "600"),
            ("JARVIS_SUBAGENT_KERNEL_IDLE_TIMEOUT", " 0.5 "),
        ]));
        assert_eq!(set, Limits { max_kernels: 3, idle: Duration::from_secs(600), subagent_idle: Duration::from_millis(500) });
        // A value that isn't a positive number is the default.
        let odd = Limits::from_vars(vars(&[
            ("JARVIS_MAX_KERNELS", "0"),
            ("JARVIS_KERNEL_IDLE_TIMEOUT", "soon"),
            ("JARVIS_SUBAGENT_KERNEL_IDLE_TIMEOUT", "-1"),
        ]));
        assert_eq!(odd, Limits::default());
    }

    async fn registry() -> Arc<Kernels> {
        registry_with(Limits::default()).await
    }

    /// A registry over one in-memory database with the tables the reaper reads.
    async fn registry_with(limits: Limits) -> Arc<Kernels> {
        let pool = sqlx::sqlite::SqlitePoolOptions::new().max_connections(1).connect("sqlite::memory:").await.unwrap();
        sqlx::query("CREATE TABLE conversations (id VARCHAR PRIMARY KEY, surface VARCHAR NOT NULL)")
            .execute(&pool)
            .await
            .unwrap();
        sqlx::query("CREATE TABLE jobs (id VARCHAR PRIMARY KEY, status VARCHAR NOT NULL, thread_id VARCHAR)")
            .execute(&pool)
            .await
            .unwrap();
        let launch = Launch { python: "python3".into(), dir: ".".into(), env: vec![] };
        Kernels::with_limits(launch, Path::new("/app"), pool, limits)
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
    async fn the_cap_takes_an_idle_session_before_a_busy_one() {
        let k = registry_with(Limits { max_kernels: 2, ..Limits::default() }).await;
        let busy = k.session("busy").await;
        let _cell = InUse::new(&busy);
        k.session("idle").await;
        k.session("new").await;
        assert_eq!(keys(&k).await, ["busy", "new"]);
    }

    #[tokio::test]
    async fn idle_sessions_are_reaped() {
        let k = registry_with(Limits { idle: Duration::from_millis(10), ..Limits::default() }).await;
        k.session("old").await;
        tokio::time::sleep(Duration::from_millis(20)).await;
        k.session("fresh").await;
        assert_eq!(k.reap_idle().await, 1);
        assert_eq!(keys(&k).await, ["fresh"]);
        k.shutdown("fresh").await;
        assert!(keys(&k).await.is_empty());
    }

    #[tokio::test]
    async fn a_session_with_a_cell_in_it_is_not_reaped() {
        let k = registry_with(Limits { idle: Duration::from_millis(10), ..Limits::default() }).await;
        let s = k.session("long").await;
        let cell = InUse::new(&s);
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(k.reap_idle().await, 0);
        // Its end counts as a use: idle from then.
        drop(cell);
        assert_eq!(k.reap_idle().await, 0);
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(k.reap_idle().await, 1);
    }

    #[tokio::test]
    async fn a_finished_sub_agents_kernel_goes_sooner_and_is_noted_when_it_comes_back() {
        let limits = Limits { subagent_idle: Duration::from_millis(10), ..Limits::default() };
        let k = registry_with(limits).await;
        for (id, surface) in [("sub", "subagent"), ("working", "subagent"), ("chat", "web")] {
            sqlx::query("INSERT INTO conversations VALUES (?, ?)").bind(id).bind(surface).execute(&k.pool).await.unwrap();
        }
        sqlx::query("INSERT INTO jobs VALUES ('j', 'running', 'working')").execute(&k.pool).await.unwrap();
        for key in ["sub", "working", "chat", "worker::w1"] {
            k.session(key).await.ran.store(true, Ordering::Relaxed);
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
        // A sub-agent's whose turn is over; not one still at work, nor a chat's.
        assert_eq!(k.reap_idle().await, 1);
        assert_eq!(keys(&k).await, ["chat", "worker::w1", "working"]);
        // Given work again, it starts afresh — and its first cell is told.
        assert!(k.session("sub").await.restarted.load(Ordering::Relaxed));
        assert!(!k.session("chat").await.restarted.load(Ordering::Relaxed));
        // A session shut down on purpose isn't.
        k.lost.lock().unwrap().insert("gone".into());
        k.shutdown("gone").await;
        assert!(!k.session("gone").await.restarted.load(Ordering::Relaxed));
    }

    /// A real kernel, when this checkout's `.venv` has one: a sub-agent's
    /// reaped kernel comes back fresh, scoped again, and says so.
    #[tokio::test]
    async fn a_reaped_kernel_restarts_scoped_and_says_so() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().expect("the checkout").to_path_buf();
        let python = root.join(".venv/bin/python");
        if !python.is_file() {
            eprintln!("skipped: no {}", python.display());
            return;
        }
        let pool = sqlx::sqlite::SqlitePoolOptions::new().max_connections(1).connect("sqlite::memory:").await.unwrap();
        sqlx::query("CREATE TABLE conversations (id VARCHAR PRIMARY KEY, surface VARCHAR NOT NULL)").execute(&pool).await.unwrap();
        sqlx::query("CREATE TABLE jobs (id VARCHAR PRIMARY KEY, status VARCHAR NOT NULL, thread_id VARCHAR)").execute(&pool).await.unwrap();
        sqlx::query("INSERT INTO conversations VALUES ('sub-1', 'subagent')").execute(&pool).await.unwrap();
        let launch = Launch { python, dir: root.clone(), env: vec![] };
        let limits = Limits { subagent_idle: Duration::from_millis(10), ..Limits::default() };
        let k = Kernels::with_limits(launch, &root, pool, limits);
        let cell = |code: &'static str| Cell { code, timeout: Duration::from_secs(30), conversation_id: Some("sub-1"), project_id: None };
        assert_eq!(k.run("sub-1", &cell("x = 1\nx")).await.unwrap(), "1");
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(k.reap_idle().await, 1);
        let after = k.run("sub-1", &cell("jarvis._conversation_id")).await.unwrap();
        assert_eq!(after, format!("{RESTARTED}\n'sub-1'"));
        let gone = k.run("sub-1", &cell("x")).await.unwrap();
        assert!(gone.contains("NameError") && !gone.contains("restarted"), "{gone}");
        k.shutdown_all().await;
    }

    #[test]
    fn literals_are_python() {
        assert_eq!(py(None), "None");
        assert_eq!(py(Some("it's \"q\"\n")), r#""it's \"q\"\n""#);
        assert!(scope_code(Some("c1"), None).contains("set_conversation(\"c1\")\n    _jarvis_sdk.set_project(None)"));
        assert!(bootstrap(Path::new("/app")).contains("    from tools.preload import *\n"));
    }
}
