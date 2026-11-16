//! The agent's notebooks — a port of `core/kernels.py`. One IPython kernel per
//! session key (a conversation, or a worker's own key), started on its first
//! cell, its variables kept between cells like a Jupyter notebook.
//!
//! - Cells in one session run one at a time.
//! - Every kernel preloads what `tools/preload.py` names — `search`/`read`,
//!   the `jarvis` SDK (scoped to the cell's conversation and project), and
//!   the cheap modules; not pandas and the like, for the RAM.
//! - A cell gets `timeout` seconds, then is interrupted — the session
//!   survives. Not while a tool approval for its conversation is open (the
//!   SDK waits for one inside the cell), up to 30 minutes.
//! - At most `MAX_KERNELS` live kernels (least recently used goes), and one
//!   untouched for 30 minutes is shut down.
//! - A cell's output is text; the PNG and JPEG images it shows are kept
//!   beside it (`Output`), for a caller whose model reads them.

mod kernel;
mod wire;

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use serde_json::{Map, Value};
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
        finish(&out, self.interrupted, self.timeout)
    }

    /// Whether the cell showed an image the model could be sent.
    pub fn has_images(&self) -> bool {
        self.pieces.iter().any(|p| matches!(p, Piece::Rich { picture: Some(Picture::Kept(_)), .. }))
    }

    /// The result as text naming each image where it was shown, and the
    /// images that fit in `room`, in order; one that doesn't fit, or can't
    /// be sent at all, is a line saying why.
    pub fn seen(self, mut room: Room) -> (String, Vec<Image>) {
        let mut out = String::new();
        let mut images = vec![];
        for piece in self.pieces {
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
        (finish(&out, self.interrupted, self.timeout), images)
    }
}

/// The line that says a display's rich types were there.
fn not_text<S: AsRef<str>>(types: &[S]) -> String {
    let types: Vec<&str> = types.iter().map(AsRef::as_ref).collect();
    format!("[{} output — not shown as text]", types.join(", "))
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

    async fn run(&self, kernels: &Kernels, cell: &Cell<'_>) -> Result<Output, String> {
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
        Ok(Output { pieces: out, interrupted, timeout: cell.timeout })
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
        Output { pieces, interrupted: false, timeout: Duration::from_secs(60) }
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
        assert!(bootstrap(Path::new("/app")).contains("    from tools.preload import *\n"));
    }
}
