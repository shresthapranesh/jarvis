//! The Telegram bot — `server/telegram_bot.py`, on the Bot API directly.
//!
//! Long polling (`getUpdates`), message updates only, pending updates dropped
//! at start as `start_polling(drop_pending_updates=True)` did. A chat is the
//! conversation `telegram_<chat id>`. Handlers, first match wins as in
//! python-telegram-bot: a voice note or audio file, then a photo, then text
//! that isn't a command.
//!
//! `TELEGRAM_PROXY_URL` (else `HTTPS_PROXY` / `ALL_PROXY`, which reqwest
//! reads itself) proxies every call. `TELEGRAM_API_URL` points the bot at
//! another Bot API server — a local one, or the tests' fake.

use std::time::Duration;

use serde_json::{Value, json};

use super::{Ctx, Reply, Step, Turn, Typing, clip, download};
use crate::gql::start::Attachment;

/// Telegram's limit is 4096; Python cut at 4000.
const MAX_MESSAGE: usize = 4000;
/// How long a `getUpdates` waits for something to arrive.
const POLL_TIMEOUT: u64 = 30;

pub struct Bot {
    ctx: Ctx,
    api: String,
    files: String,
    http: reqwest::Client,
}

impl Bot {
    pub fn new(ctx: Ctx, token: String) -> Result<Self, String> {
        let base = std::env::var("TELEGRAM_API_URL")
            .ok()
            .filter(|u| !u.is_empty())
            .unwrap_or_else(|| "https://api.telegram.org".into());
        let base = base.trim_end_matches('/');
        let mut http = reqwest::Client::builder().connect_timeout(Duration::from_secs(20));
        if let Some(proxy) = std::env::var("TELEGRAM_PROXY_URL").ok().filter(|p| !p.is_empty()) {
            http = http.proxy(reqwest::Proxy::all(&proxy).map_err(|e| format!("TELEGRAM_PROXY_URL: {e}"))?);
        }
        Ok(Self {
            ctx,
            api: format!("{base}/bot{token}"),
            files: format!("{base}/file/bot{token}"),
            http: http.build().map_err(|e| e.to_string())?,
        })
    }

    pub async fn run(self) {
        let bot = std::sync::Arc::new(self);
        let mut backoff = Duration::from_secs(1);
        while let Err(e) = bot.call("deleteWebhook", json!({"drop_pending_updates": true})).await {
            tracing::warn!("telegram: {e}; retrying in {backoff:?}");
            tokio::time::sleep(backoff).await;
            backoff = (backoff * 2).min(Duration::from_secs(60));
        }
        tracing::info!("telegram bot polling");
        let mut offset: i64 = 0;
        backoff = Duration::from_secs(1);
        loop {
            let params = json!({"offset": offset, "timeout": POLL_TIMEOUT, "allowed_updates": ["message"]});
            let updates = match bot.call("getUpdates", params).await {
                Ok(Value::Array(updates)) => updates,
                Ok(other) => {
                    tracing::warn!("telegram: getUpdates returned {other}");
                    vec![]
                }
                Err(e) => {
                    tracing::warn!("telegram: {e}; retrying in {backoff:?}");
                    tokio::time::sleep(backoff).await;
                    backoff = (backoff * 2).min(Duration::from_secs(60));
                    continue;
                }
            };
            backoff = Duration::from_secs(1);
            for update in updates {
                if let Some(id) = update["update_id"].as_i64() {
                    offset = offset.max(id + 1);
                }
                if let Some(message) = update.get("message").cloned() {
                    let bot = bot.clone();
                    tokio::spawn(async move { bot.handle(message).await });
                }
            }
        }
    }

    /// One Bot API method. Its `result`, or the API's description of why not.
    async fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        let timeout = Duration::from_secs(POLL_TIMEOUT + 30);
        let resp = self
            .http
            .post(format!("{}/{method}", self.api))
            .json(&params)
            .timeout(timeout)
            .send()
            .await
            .map_err(|e| format!("{method}: {}", e.without_url()))?;
        let body: Value = resp.json().await.map_err(|e| format!("{method}: {}", e.without_url()))?;
        if body["ok"].as_bool() == Some(true) {
            return Ok(body["result"].clone());
        }
        Err(format!("{method}: {}", body["description"].as_str().unwrap_or("failed")))
    }

    /// A call whose failure only matters to the log.
    async fn call_quietly(&self, method: &str, params: Value) -> Option<Value> {
        self.call(method, params).await.inspect_err(|e| tracing::debug!("telegram: {e}")).ok()
    }

    async fn send(&self, chat_id: i64, text: &str) -> Option<i64> {
        let sent = self.call_quietly("sendMessage", json!({"chat_id": chat_id, "text": text})).await?;
        sent["message_id"].as_i64()
    }

    async fn edit(&self, chat_id: i64, message_id: i64, text: &str) {
        self.call_quietly("editMessageText", json!({"chat_id": chat_id, "message_id": message_id, "text": text}))
            .await;
    }

    fn typing(self: &std::sync::Arc<Self>, chat_id: i64) -> Typing {
        let bot = self.clone();
        Typing::start(Duration::from_secs(4), move || {
            let bot = bot.clone();
            async move {
                bot.call_quietly("sendChatAction", json!({"chat_id": chat_id, "action": "typing"})).await;
            }
        })
    }

    /// A file the user sent, by its `file_id`.
    async fn file(&self, file_id: &str) -> Result<Vec<u8>, String> {
        let file = self.call("getFile", json!({"file_id": file_id})).await?;
        let path = file["file_path"].as_str().ok_or("getFile: no file_path")?;
        download(&self.http, &format!("{}/{path}", self.files)).await.map_err(|e| format!("download: {e}"))
    }

    async fn handle(self: std::sync::Arc<Self>, message: Value) {
        let Some(chat_id) = message["chat"]["id"].as_i64() else { return };
        let voice = message.get("voice").or_else(|| message.get("audio")).filter(|v| v.is_object());
        let photo = message["photo"].as_array().and_then(|sizes| sizes.last());
        let text = message["text"].as_str().filter(|t| !is_command(&message, t));
        if voice.is_none() && photo.is_none() && text.is_none() {
            return;
        }
        let user_id = message["from"]["id"].as_i64().map(|id| id.to_string()).unwrap_or_default();
        let Some(model) = self.ctx.model_for("telegram", &user_id).await else { return };

        if let Some(voice) = voice {
            let suffix = if message.get("voice").is_some_and(Value::is_object) { ".ogg" } else { ".mp3" };
            let file_id = voice["file_id"].as_str().unwrap_or_default().to_string();
            return self.handle_voice(chat_id, model, &file_id, suffix).await;
        }
        if let Some(photo) = photo {
            let bytes = match self.file(photo["file_id"].as_str().unwrap_or_default()).await {
                Ok(bytes) => bytes,
                Err(e) => return tracing::warn!("telegram photo: {e}"),
            };
            let query = message["caption"].as_str().unwrap_or("What's in this image?").to_string();
            let attachment = Attachment::image("photo.jpg".into(), "image/jpeg".into(), bytes);
            let display = format!("[Photo] {query}");
            return self.dispatch(chat_id, None, model, query, display.clone(), &display, vec![attachment]).await;
        }
        if let Some(text) = text {
            self.dispatch(chat_id, None, model, text.to_string(), text.to_string(), text, vec![]).await;
        }
    }

    async fn handle_voice(self: std::sync::Arc<Self>, chat_id: i64, model: String, file_id: &str, suffix: &str) {
        // A status for a real wait — the one message sent before the agent
        // has anything to say.
        let placeholder = self.send(chat_id, "⏳ Transcribing...").await;
        let text = match self.file(file_id).await {
            Ok(audio) => self.ctx.transcribe(audio, suffix).await,
            Err(e) => Err(e),
        };
        let text = match text {
            Ok(text) if !text.is_empty() => text,
            failed => {
                if let Err(e) = failed {
                    tracing::warn!("telegram voice: {e}");
                }
                let note = "(could not transcribe audio)";
                match placeholder {
                    Some(id) => self.edit(chat_id, id, note).await,
                    None => drop(self.send(chat_id, note).await),
                }
                return;
            }
        };
        // Titled by what was said, not by the stored "[Voice] …".
        let display = format!("[Voice] {text}");
        self.dispatch(chat_id, placeholder, model, text.clone(), display, &text, vec![]).await;
    }

    /// `_dispatch`: start the turn, then stream its reply into `message_id`
    /// (a status message to replace) or a new message.
    #[allow(clippy::too_many_arguments)]
    async fn dispatch(
        self: std::sync::Arc<Self>,
        chat_id: i64,
        message_id: Option<i64>,
        model: String,
        query: String,
        display: String,
        title: &str,
        attachments: Vec<Attachment>,
    ) {
        let typing = self.typing(chat_id);
        let conversation_id = format!("telegram_{chat_id}");
        let turn = self.ctx.dispatch("telegram", conversation_id, model, query, display, title, attachments).await;
        let reply = match turn {
            Ok(Turn::Started(reply)) => reply,
            Ok(Turn::Note(note)) => {
                drop(typing);
                match message_id {
                    Some(id) => self.edit(chat_id, id, &note).await,
                    None => drop(self.send(chat_id, &note).await),
                }
                return;
            }
            Err(e) => return tracing::warn!("telegram: starting a turn: {e}"),
        };
        self.stream(chat_id, message_id, reply, typing).await;
    }

    /// `_stream_to_telegram`. The reply's message is created on its first
    /// text, never before.
    async fn stream(&self, chat_id: i64, mut message_id: Option<i64>, mut reply: Reply, typing: Typing) {
        let mut typing = Some(typing);
        loop {
            match reply.next().await {
                Step::Speaking => typing = None,
                Step::Render(text) => {
                    let text = clip(&text, MAX_MESSAGE);
                    match message_id {
                        Some(id) => self.edit(chat_id, id, text).await,
                        None => message_id = self.send(chat_id, text).await,
                    }
                }
                Step::Finished(text) => {
                    drop(typing);
                    let text = if text.is_empty() { "(no response)" } else { clip(&text, MAX_MESSAGE) };
                    match message_id {
                        Some(id) => self.edit(chat_id, id, text).await,
                        None => drop(self.send(chat_id, text).await),
                    }
                    return;
                }
            }
        }
    }
}

/// `filters.COMMAND`: a message that opens with a bot command.
fn is_command(message: &Value, text: &str) -> bool {
    let opens_with_command = message["entities"].as_array().is_some_and(|entities| {
        entities.iter().any(|e| e["type"].as_str() == Some("bot_command") && e["offset"].as_i64() == Some(0))
    });
    opens_with_command && text.starts_with('/')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commands_are_not_chat() {
        let cmd = json!({"text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}]});
        assert!(is_command(&cmd, "/start"));
        let plain = json!({"text": "/not a command"});
        assert!(!is_command(&plain, "/not a command"));
        let later = json!({"text": "hi /start", "entities": [{"type": "bot_command", "offset": 3, "length": 6}]});
        assert!(!is_command(&later, "hi /start"));
    }
}
