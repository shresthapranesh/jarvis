//! The chat bots — `server/telegram_bot.py` and `server/discord_bot.py`.
//!
//! A bot is a way to talk to the agent, so it runs where nothing has to be
//! started for it: here. A message from an allowed user becomes a chat turn
//! through the same `start_chat` the web UI's `startTask` uses — joining the
//! run already going on that chat, if there is one — and the reply is the
//! run's main-agent text, followed in the run mirror and edited into the chat
//! as it grows. Python is started for the run (its job) and, for a voice note,
//! for the transcription; never to keep a bot connected.
//!
//! Each bot is enabled by its token, as in Python: `TELEGRAM_BOT_TOKEN`,
//! `DISCORD_BOT_TOKEN`. Python behind the edge starts neither.

mod discord;
mod telegram;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use sqlx::SqlitePool;
use tokio::sync::watch;

use crate::catalog;
use crate::gql::start::{Attachment, ChatTurn, Dispatched, first_chars, start_chat};
use crate::runs::{Registry, Run};
use crate::supervisor::Supervisor;

/// Edits to a streaming reply, at most one per this.
const EDIT_INTERVAL: Duration = Duration::from_secs(1);

/// Sent instead of a second stream when a message lands mid-run. A bot chat is
/// one conversation, so the alternative is two runs on the same thread — and
/// the reply already in flight will answer this too.
const QUEUED_NOTE: &str = "📥 Added to what I'm working on — it'll be picked up in a moment.";

/// What every bot needs from the edge.
#[derive(Clone)]
pub struct Ctx {
    pool: SqlitePool,
    registry: Arc<Registry>,
    supervisor: Arc<Supervisor>,
    documents_dir: PathBuf,
    /// The Python server, for `/transcribe`.
    backend: String,
    /// For the chat services: HTTPS, and redirects followed.
    http: reqwest::Client,
}

/// Start the bots whose tokens are set.
pub fn spawn(
    pool: SqlitePool,
    registry: Arc<Registry>,
    supervisor: Arc<Supervisor>,
    documents_dir: PathBuf,
    backend: String,
) {
    let token = |key: &str| std::env::var(key).ok().filter(|t| !t.trim().is_empty());
    let telegram_token = token("TELEGRAM_BOT_TOKEN");
    let discord_token = token("DISCORD_BOT_TOKEN");
    if telegram_token.is_none() && discord_token.is_none() {
        return;
    }
    let ctx = Ctx {
        pool,
        registry,
        supervisor,
        documents_dir,
        backend,
        http: reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(20))
            .build()
            .expect("bot http client"),
    };
    if let Some(token) = telegram_token {
        match telegram::Bot::new(ctx.clone(), token) {
            Ok(bot) => {
                tokio::spawn(bot.run());
            }
            Err(e) => tracing::error!("telegram bot not started: {e}"),
        }
    }
    if let Some(token) = discord_token {
        tokio::spawn(discord::Bot::new(ctx, token).run());
    }
}

impl Ctx {
    /// The model for a message from `user_id`, or `None` when the user isn't
    /// on the `<surface>.allowed_users` list — `_check_and_get_model`.
    async fn model_for(&self, surface: &str, user_id: &str) -> Option<String> {
        let key = format!("{surface}.allowed_users");
        let raw: Option<String> = match sqlx::query_scalar("SELECT value FROM config_settings WHERE key = ?")
            .bind(&key)
            .fetch_optional(&self.pool)
            .await
        {
            Ok(raw) => raw,
            Err(e) => {
                tracing::warn!("{surface}: reading {key}: {e}");
                return None;
            }
        };
        if !allowed(raw.as_deref().unwrap_or(""), user_id) {
            return None;
        }
        match catalog::resolve_model(&self.pool, None).await {
            Ok(model) => Some(model),
            Err(e) => {
                tracing::warn!("{surface}: resolving the model: {e}");
                None
            }
        }
    }

    /// Start a turn on `conversation_id`, or queue it onto the run there —
    /// the bots' `_dispatch`. `display` is the user message as stored;
    /// `title` titles the conversation if this creates it.
    #[allow(clippy::too_many_arguments)]
    async fn dispatch(
        &self,
        surface: &'static str,
        conversation_id: String,
        model: String,
        query: String,
        display: String,
        title: &str,
        attachments: Vec<Attachment>,
    ) -> Result<Turn, String> {
        let turn = ChatTurn {
            query,
            model,
            conversation_id: Some(conversation_id),
            title: Some(first_chars(title, 60)),
            surface,
            display: Some(display),
            attachments,
            project_id: None,
            ephemeral: false,
        };
        match start_chat(&self.pool, &self.documents_dir, &self.registry, turn).await {
            Ok(Dispatched::Started { task_id, .. }) => match self.registry.get(&task_id) {
                Some(run) => Ok(Turn::Started(Reply::new(run))),
                // Registered as pending a moment ago; only a sweep of a job
                // that ended unclaimed removes it this fast.
                None => Ok(Turn::Note("(no response)".into())),
            },
            Ok(Dispatched::Queued { .. }) => Ok(Turn::Note(QUEUED_NOTE.into())),
            Ok(Dispatched::Refused(reason)) => Ok(Turn::Note(reason)),
            Err(e) => Err(e.message),
        }
    }

    /// Speech to text, by Python's `/transcribe` (Whisper). An empty string
    /// is no speech.
    async fn transcribe(&self, audio: Vec<u8>, suffix: &str) -> Result<String, String> {
        // Held until the answer is in, so the worker isn't stopped under it.
        let _worker = self.supervisor.ensure_up().await?;
        let part = reqwest::multipart::Part::bytes(audio).file_name(format!("audio{suffix}"));
        let form = reqwest::multipart::Form::new().part("audio", part);
        let resp = self
            .http
            .post(format!("{}/transcribe", self.backend))
            .multipart(form)
            .send()
            .await
            .map_err(|e| format!("transcribe: {e}"))?;
        let status = resp.status();
        let body: serde_json::Value = resp.json().await.map_err(|e| format!("transcribe: {e}"))?;
        match body.get("text").and_then(|t| t.as_str()) {
            Some(text) if status.is_success() => Ok(text.trim().to_string()),
            _ => Err(format!(
                "transcribe: {status} {}",
                body.get("error").and_then(|e| e.as_str()).unwrap_or_default()
            )),
        }
    }

}

/// A file from a chat service, with a size cap. Through the bot's own
/// client, so a Telegram proxy applies to downloads too.
async fn download(http: &reqwest::Client, url: &str) -> Result<Vec<u8>, String> {
    /// Telegram's bot API serves files up to 20 MB; Discord's attachments run
    /// to 25 MB on free servers.
    const MAX: usize = 32 * 1024 * 1024;
    let resp = http
        .get(url)
        .send()
        .await
        .and_then(reqwest::Response::error_for_status)
        .map_err(|e| e.without_url().to_string())?;
    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    if bytes.len() > MAX {
        return Err(format!("file too large ({} bytes)", bytes.len()));
    }
    Ok(bytes.to_vec())
}

/// Whether `user_id` is in an allowlist setting: comma-separated numeric ids.
/// Unset or empty allows no one. An entry that isn't a number is skipped —
/// in Python it made `int()` raise, so the bot answered nobody.
fn allowed(raw: &str, user_id: &str) -> bool {
    let Ok(user) = user_id.parse::<i128>() else { return false };
    raw.split(',').map(str::trim).filter(|x| !x.is_empty()).any(|x| x.parse::<i128>().ok() == Some(user))
}

/// What a message became.
enum Turn {
    /// A new run; stream its reply.
    Started(Reply),
    /// Nothing to stream; send this instead.
    Note(String),
}

/// What a reply's stream asks of the bot next.
#[derive(Debug, PartialEq)]
enum Step {
    /// The agent produced its first text: stop the typing indicator.
    Speaking,
    /// Show this much of the reply.
    Render(String),
    /// The run is over; this is the whole reply.
    Finished(String),
}

/// A run's main-agent text as it streams — `_stream_to_telegram` /
/// `_stream_to_discord` reading `stream_task_events`: `token` events whose
/// `source` is `main`, rendered at most once per `EDIT_INTERVAL`, and the
/// whole text once the run is done.
struct Reply {
    run: Arc<Run>,
    version: watch::Receiver<u64>,
    cursor: usize,
    text: String,
    speaking: bool,
    last_render: Option<Instant>,
    /// A render that came due with the first token, after `Speaking`.
    due: Option<String>,
    finished: bool,
}

impl Reply {
    fn new(run: Arc<Run>) -> Self {
        let version = run.subscribe();
        Self {
            run,
            version,
            cursor: 0,
            text: String::new(),
            speaking: false,
            last_render: None,
            due: None,
            finished: false,
        }
    }

    async fn next(&mut self) -> Step {
        if let Some(text) = self.due.take() {
            return Step::Render(text);
        }
        loop {
            if self.finished {
                return Step::Finished(self.text.clone());
            }
            self.version.borrow_and_update();
            let (event, end) = {
                let st = self.run.state.lock().expect("run state lock");
                (st.events.get(self.cursor).cloned(), st.fields.done || st.gone)
            };
            let Some(event) = event else {
                // Drained: done, or wait for more. A run that leaves the
                // mirror unfinished ends the reply with what it has.
                if end || self.version.changed().await.is_err() {
                    self.finished = true;
                }
                continue;
            };
            self.cursor += 1;
            let Some(text) = main_token(&event) else { continue };
            self.text.push_str(&text);
            let render = (!self.text.is_empty()
                && self.last_render.is_none_or(|t| t.elapsed() >= EDIT_INTERVAL))
            .then(|| {
                self.last_render = Some(Instant::now());
                self.text.clone()
            });
            if !self.speaking {
                self.speaking = true;
                self.due = render;
                return Step::Speaking;
            }
            if let Some(text) = render {
                return Step::Render(text);
            }
        }
    }
}

/// The text of a `token` event from the main agent.
fn main_token(record: &serde_json::Value) -> Option<String> {
    if record.get("event")?.as_str()? != "token" {
        return None;
    }
    let data: serde_json::Value = serde_json::from_str(record.get("data")?.as_str()?).ok()?;
    if data.get("source")?.as_str()? != "main" {
        return None;
    }
    Some(data.get("text").and_then(|t| t.as_str()).unwrap_or_default().to_string())
}

/// A blocking wait the user should see as "typing…": runs `pulse` every
/// `every` until dropped.
struct Typing(tokio::task::JoinHandle<()>);

impl Typing {
    fn start<F, Fut>(every: Duration, pulse: F) -> Self
    where
        F: Fn() -> Fut + Send + 'static,
        Fut: std::future::Future<Output = ()> + Send,
    {
        Self(tokio::spawn(async move {
            loop {
                pulse().await;
                tokio::time::sleep(every).await;
            }
        }))
    }
}

impl Drop for Typing {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Python's `s[:n]` over code points.
fn clip(s: &str, n: usize) -> &str {
    s.char_indices().nth(n).map_or(s, |(i, _)| &s[..i])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allowlist() {
        assert!(allowed("123, 456", "456"));
        assert!(allowed("+123", "123"));
        assert!(!allowed("", "1"));
        assert!(!allowed("12,x", "1"));
        assert!(allowed("x,12", "12"));
        assert!(allowed("1234567890123456789", "1234567890123456789"));
        assert!(!allowed("1", "not-a-number"));
    }

    #[test]
    fn clip_counts_code_points() {
        assert_eq!(clip("héllo", 2), "hé");
        assert_eq!(clip("ab", 10), "ab");
    }

    fn record(event: &str, data: serde_json::Value) -> serde_json::Value {
        serde_json::json!({"event": event, "data": data.to_string()})
    }

    #[tokio::test]
    async fn reply_follows_main_tokens() {
        let registry = Registry::default();
        let meta = crate::runs::Meta {
            kind: "chat".into(),
            label: "x".into(),
            parent_id: Some("c".into()),
            started_at: "2026-01-01T00:00:00+00:00".into(),
        };
        let run = registry.pre_register("t", meta);
        let mut reply = Reply::new(run.clone());
        run.update(|st| {
            st.events.push(record("step", serde_json::json!({"node": "agent"})));
            st.events.push(record("token", serde_json::json!({"text": "Hel", "source": "main"})));
            st.events.push(record("token", serde_json::json!({"text": "worker", "source": "researcher"})));
            st.events.push(record("token", serde_json::json!({"text": "lo", "source": "main"})));
        });
        assert_eq!(reply.next().await, Step::Speaking);
        assert_eq!(reply.next().await, Step::Render("Hel".into()));
        run.update(|st| st.fields.done = true);
        // "lo" lands inside the edit interval: no render, only the final text.
        assert_eq!(reply.next().await, Step::Finished("Hello".into()));
    }

    #[tokio::test]
    async fn a_run_that_goes_away_ends_the_reply() {
        let registry = Registry::default();
        let meta = crate::runs::Meta {
            kind: "chat".into(),
            label: "x".into(),
            parent_id: Some("c".into()),
            started_at: "2026-01-01T00:00:00+00:00".into(),
        };
        let run = registry.pre_register("t", meta);
        let mut reply = Reply::new(run.clone());
        let waiter = tokio::spawn(async move { reply.next().await });
        tokio::time::sleep(Duration::from_millis(20)).await;
        run.update(|st| st.gone = true);
        assert_eq!(waiter.await.unwrap(), Step::Finished(String::new()));
    }
}
