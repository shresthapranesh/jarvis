//! The Discord bot — `server/discord_bot.py`, on the gateway and REST API
//! directly.
//!
//! The gateway is v10 JSON, uncompressed: identify with the guild-message,
//! direct-message and message-content intents, heartbeat on the interval
//! Hello gives, resume after a dropped connection or a `Reconnect`, identify
//! afresh when the session is gone, and stop for good on a close that no
//! retry can fix (a bad token, a privileged intent not enabled in the
//! developer portal).
//!
//! Who it answers, as in Python: DMs always; in a server only when mentioned,
//! replied to, or inside a thread it started. A server message opens a thread
//! off the user's message, so each exchange is its own conversation
//! (`discord_<channel id>`). `DISCORD_API_URL` / `DISCORD_GATEWAY_URL` point
//! it elsewhere (the tests' fake).

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use reqwest::Method;
use serde_json::{Value, json};
use tokio_tungstenite::tungstenite::Message as WsMessage;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;

use super::{Ctx, Reply, Step, Turn, Typing, clip, download};
use crate::gql::start::Attachment;

/// Discord's limit is 2000; Python left headroom.
const MAX_MESSAGE: usize = 1900;
/// GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT.
const INTENTS: u64 = 1 | (1 << 9) | (1 << 12) | (1 << 15);
/// Channel types that are threads: announcement, public, private.
const THREAD_TYPES: [i64; 3] = [10, 11, 12];

pub struct Bot {
    ctx: Ctx,
    token: String,
    api: String,
    /// The bot's own user id, from READY.
    me: Mutex<Option<String>>,
    /// Channel id → (is a thread, its owner), read once per channel.
    channels: Mutex<HashMap<String, (bool, Option<String>)>>,
}

/// The gateway session a reconnect resumes.
#[derive(Default)]
struct Session {
    id: Option<String>,
    resume_url: Option<String>,
    seq: Option<u64>,
}

/// Why a gateway connection ended.
enum End {
    /// Reconnect and resume.
    Resume,
    /// The session is gone: reconnect and identify.
    Identify,
    /// Nothing a retry would fix.
    Fatal(String),
}

impl Bot {
    pub fn new(ctx: Ctx, token: String) -> Self {
        let api = std::env::var("DISCORD_API_URL")
            .ok()
            .filter(|u| !u.is_empty())
            .unwrap_or_else(|| "https://discord.com/api/v10".into());
        Self {
            ctx,
            token,
            api: api.trim_end_matches('/').to_string(),
            me: Mutex::new(None),
            channels: Mutex::new(HashMap::new()),
        }
    }

    pub async fn run(self) {
        let bot = Arc::new(self);
        let mut session = Session::default();
        let mut backoff = Duration::from_secs(1);
        loop {
            let url = match session.resume_url.clone() {
                Some(url) if session.id.is_some() => url,
                _ => bot.gateway_url().await,
            };
            match bot.clone().connect(&url, &mut session).await {
                Ok(End::Resume) => backoff = Duration::from_secs(1),
                Ok(End::Identify) => {
                    session = Session::default();
                    // Discord asks for 1–5 s before identifying again.
                    tokio::time::sleep(Duration::from_secs(1) + jitter(Duration::from_secs(4))).await;
                }
                Ok(End::Fatal(reason)) => {
                    tracing::error!("discord bot stopped: {reason}");
                    return;
                }
                Err(e) => {
                    tracing::warn!("discord gateway: {e}; reconnecting in {backoff:?}");
                    tokio::time::sleep(backoff).await;
                    backoff = (backoff * 2).min(Duration::from_secs(60));
                }
            }
        }
    }

    async fn gateway_url(&self) -> String {
        if let Some(url) = std::env::var("DISCORD_GATEWAY_URL").ok().filter(|u| !u.is_empty()) {
            return url;
        }
        match self.rest(Method::GET, "/gateway/bot", None).await {
            Ok(v) if v["url"].is_string() => v["url"].as_str().unwrap_or_default().to_string(),
            Ok(_) | Err(_) => "wss://gateway.discord.gg".into(),
        }
    }

    /// One gateway connection, until it ends.
    async fn connect(self: Arc<Self>, url: &str, session: &mut Session) -> Result<End, String> {
        // `wss://gateway.discord.gg/?v=10…`; a URL with a path keeps it as is.
        let base = url.trim_end_matches('/');
        let slash = if base.split('/').count() > 3 { "" } else { "/" };
        let url = format!("{base}{slash}?v=10&encoding=json");
        let (ws, _) = tokio_tungstenite::connect_async(&url).await.map_err(|e| e.to_string())?;
        let (mut tx, mut rx) = ws.split();

        let hello = loop {
            match rx.next().await {
                Some(Ok(WsMessage::Text(text))) => break serde_json::from_str::<Value>(&text).map_err(|e| e.to_string())?,
                Some(Ok(WsMessage::Close(frame))) => return Ok(on_close(frame.map(|f| f.code))),
                Some(Ok(_)) => continue,
                Some(Err(e)) => return Err(e.to_string()),
                None => return Err("closed before Hello".into()),
            }
        };
        if hello["op"].as_u64() != Some(10) {
            return Err(format!("expected Hello, got op {}", hello["op"]));
        }
        let interval = Duration::from_millis(hello["d"]["heartbeat_interval"].as_u64().unwrap_or(41_250));

        let opening = match (&session.id, session.seq) {
            (Some(id), Some(seq)) => json!({"op": 6, "d": {"token": self.token, "session_id": id, "seq": seq}}),
            _ => json!({"op": 2, "d": {
                "token": self.token,
                "intents": INTENTS,
                "properties": {"os": std::env::consts::OS, "browser": "jarvis", "device": "jarvis"},
            }}),
        };
        send(&mut tx, &opening).await?;

        // The first beat at a random point in the interval, as Discord asks.
        let mut beat = tokio::time::interval_at(tokio::time::Instant::now() + jitter(interval), interval);
        let mut acked = true;
        loop {
            tokio::select! {
                _ = beat.tick() => {
                    if !acked {
                        // A zombie connection: no ack since the last beat.
                        return Ok(End::Resume);
                    }
                    acked = false;
                    send(&mut tx, &json!({"op": 1, "d": session.seq})).await?;
                }
                frame = rx.next() => {
                    let text = match frame {
                        Some(Ok(WsMessage::Text(text))) => text,
                        Some(Ok(WsMessage::Close(frame))) => return Ok(on_close(frame.map(|f| f.code))),
                        Some(Ok(_)) => continue,
                        Some(Err(e)) => return Err(e.to_string()),
                        None => return Ok(End::Resume),
                    };
                    let Ok(payload) = serde_json::from_str::<Value>(&text) else { continue };
                    if let Some(seq) = payload["s"].as_u64() {
                        session.seq = Some(seq);
                    }
                    match payload["op"].as_u64() {
                        Some(0) => self.on_event(payload["t"].as_str().unwrap_or_default(), &payload["d"], session),
                        Some(1) => send(&mut tx, &json!({"op": 1, "d": session.seq})).await?,
                        Some(7) => return Ok(End::Resume),
                        Some(9) if payload["d"].as_bool() == Some(true) => return Ok(End::Resume),
                        Some(9) => return Ok(End::Identify),
                        Some(11) => acked = true,
                        _ => {}
                    }
                }
            }
        }
    }

    fn on_event(self: &Arc<Self>, event: &str, data: &Value, session: &mut Session) {
        match event {
            "READY" => {
                session.id = data["session_id"].as_str().map(str::to_string);
                session.resume_url = data["resume_gateway_url"].as_str().map(str::to_string);
                let me = data["user"]["id"].as_str().map(str::to_string);
                tracing::info!("discord bot connected as {}", data["user"]["username"].as_str().unwrap_or("?"));
                *self.me.lock().expect("discord me") = me;
            }
            "MESSAGE_CREATE" => {
                let bot = self.clone();
                let message = data.clone();
                tokio::spawn(async move {
                    if let Err(e) = bot.handle(message).await {
                        tracing::warn!("discord message: {e}");
                    }
                });
            }
            _ => {}
        }
    }

    /// One REST call, waiting out a rate limit (twice at most).
    async fn rest(&self, method: Method, path: &str, body: Option<Value>) -> Result<Value, String> {
        for _ in 0..3 {
            let mut req = self
                .ctx
                .http
                .request(method.clone(), format!("{}{path}", self.api))
                .header(reqwest::header::AUTHORIZATION, format!("Bot {}", self.token))
                .timeout(Duration::from_secs(30));
            if let Some(body) = &body {
                req = req.json(body);
            }
            let resp = req.send().await.map_err(|e| format!("{method} {path}: {}", e.without_url()))?;
            let status = resp.status();
            let value: Value = if status == reqwest::StatusCode::NO_CONTENT {
                Value::Null
            } else {
                resp.json().await.unwrap_or(Value::Null)
            };
            if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
                let wait = value["retry_after"].as_f64().unwrap_or(1.0).clamp(0.0, 30.0);
                tokio::time::sleep(Duration::from_secs_f64(wait)).await;
                continue;
            }
            if !status.is_success() {
                return Err(format!("{method} {path}: {status} {}", value["message"].as_str().unwrap_or_default()));
            }
            return Ok(value);
        }
        Err(format!("{method} {path}: rate limited"))
    }

    /// Send `content` into `channel_id`, as a reply to `reply_to` (a message
    /// id) when that message is in the same channel. Never pings anyone.
    async fn send(&self, channel_id: &str, content: &str, reply_to: Option<&Value>) -> Result<String, String> {
        let mut body = json!({"content": content, "allowed_mentions": {"parse": [], "replied_user": false}});
        if let Some(original) = reply_to.filter(|m| m["channel_id"].as_str() == Some(channel_id)) {
            body["message_reference"] = json!({
                "message_id": original["id"], "channel_id": channel_id, "fail_if_not_exists": false,
            });
        }
        let sent = self.rest(Method::POST, &format!("/channels/{channel_id}/messages"), Some(body)).await?;
        Ok(sent["id"].as_str().unwrap_or_default().to_string())
    }

    async fn edit(&self, channel_id: &str, message_id: &str, content: &str) -> Result<(), String> {
        let path = format!("/channels/{channel_id}/messages/{message_id}");
        self.rest(Method::PATCH, &path, Some(json!({"content": content}))).await.map(drop)
    }

    fn typing(self: &Arc<Self>, channel_id: &str) -> Typing {
        let bot = self.clone();
        let path = format!("/channels/{channel_id}/typing");
        Typing::start(Duration::from_secs(5), move || {
            let bot = bot.clone();
            let path = path.clone();
            async move {
                if let Err(e) = bot.rest(Method::POST, &path, None).await {
                    tracing::debug!("discord typing: {e}");
                }
            }
        })
    }

    /// Whether a channel is a thread, and who started it.
    async fn channel(&self, channel_id: &str) -> (bool, Option<String>) {
        if let Some(known) = self.channels.lock().expect("discord channels").get(channel_id) {
            return known.clone();
        }
        let info = match self.rest(Method::GET, &format!("/channels/{channel_id}"), None).await {
            Ok(c) => (
                c["type"].as_i64().is_some_and(|t| THREAD_TYPES.contains(&t)),
                c["owner_id"].as_str().map(str::to_string),
            ),
            Err(e) => {
                tracing::debug!("discord channel {channel_id}: {e}");
                return (false, None);
            }
        };
        self.channels.lock().expect("discord channels").insert(channel_id.to_string(), info.clone());
        info
    }

    /// `_should_respond`: DMs always; in a server when mentioned, replied to,
    /// or inside a thread this bot started.
    async fn should_respond(&self, message: &Value, me: Option<&str>) -> bool {
        if message["guild_id"].is_null() {
            return true;
        }
        let Some(me) = me else { return false };
        let mentioned = message["mentions"].as_array().is_some_and(|m| m.iter().any(|u| u["id"].as_str() == Some(me)));
        let replied_to = message["referenced_message"]["author"]["id"].as_str() == Some(me);
        if mentioned || replied_to {
            return true;
        }
        let (thread, owner) = self.channel(message["channel_id"].as_str().unwrap_or_default()).await;
        thread && owner.as_deref() == Some(me)
    }

    /// `_resolve_target_channel`: in a server channel, a thread off the
    /// user's message; DMs and threads answer where they are.
    async fn target(&self, message: &Value, prompt: &str) -> String {
        let channel_id = message["channel_id"].as_str().unwrap_or_default().to_string();
        if message["guild_id"].is_null() || self.channel(&channel_id).await.0 {
            return channel_id;
        }
        let name = match clip(prompt.trim().lines().next().unwrap_or(""), 90) {
            "" => "Conversation",
            line => line,
        };
        let path = format!("/channels/{channel_id}/messages/{}/threads", message["id"].as_str().unwrap_or_default());
        match self.rest(Method::POST, &path, Some(json!({"name": name, "auto_archive_duration": 1440}))).await {
            Ok(thread) => {
                let id = thread["id"].as_str().unwrap_or_default().to_string();
                let me = self.me.lock().expect("discord me").clone();
                self.channels.lock().expect("discord channels").insert(id.clone(), (true, me));
                id
            }
            Err(e) => {
                tracing::debug!("discord create thread: {e}");
                channel_id
            }
        }
    }

    async fn handle(self: Arc<Self>, message: Value) -> Result<(), String> {
        if message["author"]["bot"].as_bool() == Some(true) {
            return Ok(());
        }
        let me = self.me.lock().expect("discord me").clone();
        if !self.should_respond(&message, me.as_deref()).await {
            return Ok(());
        }
        let Some(model) = self.ctx.model_for("discord", message["author"]["id"].as_str().unwrap_or_default()).await
        else {
            return Ok(());
        };
        let text = strip_mention(message["content"].as_str().unwrap_or_default(), me.as_deref());

        let attachments = message["attachments"].as_array().cloned().unwrap_or_default();
        let content_type = |a: &Value| a["content_type"].as_str().unwrap_or_default().to_lowercase();
        let is_voice = |a: &Value| {
            (!a["duration_secs"].is_null() && !a["waveform"].is_null()) || content_type(a).starts_with("audio/")
        };
        // The first voice note wins; images before it still count, as in Python.
        let voice = attachments.iter().position(is_voice);
        let images: Vec<&Value> = attachments[..voice.unwrap_or(attachments.len())]
            .iter()
            .filter(|a| content_type(a).starts_with("image/"))
            .collect();

        if let Some(i) = voice {
            return self.handle_voice(&message, &attachments[i], model).await;
        }
        if !images.is_empty() {
            let mut parts = vec![];
            for image in images {
                let url = image["url"].as_str().unwrap_or_default();
                let bytes = download(&self.ctx.http, url).await.map_err(|e| format!("image: {e}"))?;
                let mime = match image["content_type"].as_str() {
                    Some(m) if !m.is_empty() => m.to_string(),
                    _ => "image/jpeg".into(),
                };
                parts.push(Attachment::image(image["filename"].as_str().unwrap_or("image").to_string(), mime, bytes));
            }
            let query = if text.is_empty() { "What's in this image?".to_string() } else { text };
            let target = self.target(&message, &query).await;
            let display = format!("[Image] {query}");
            self.dispatch(&target, &message, model, query, display.clone(), &display, parts).await;
            return Ok(());
        }
        if text.is_empty() {
            return Ok(());
        }
        let target = self.target(&message, &text).await;
        self.dispatch(&target, &message, model, text.clone(), text.clone(), &text, vec![]).await;
        Ok(())
    }

    async fn handle_voice(self: Arc<Self>, message: &Value, audio: &Value, model: String) -> Result<(), String> {
        let channel_id = message["channel_id"].as_str().unwrap_or_default().to_string();
        let ctype = audio["content_type"].as_str().unwrap_or_default().to_lowercase();
        let suffix = if ctype.contains("ogg") {
            ".ogg"
        } else if ctype.contains("mp") {
            ".mp3"
        } else {
            ".ogg"
        };
        // Typing while Whisper runs: a real wait, shown without a message.
        let typing = self.typing(&channel_id);
        let text = match download(&self.ctx.http, audio["url"].as_str().unwrap_or_default()).await {
            Ok(bytes) => self.ctx.transcribe(bytes, suffix).await,
            Err(e) => Err(e),
        };
        drop(typing);
        let text = match text {
            Ok(text) if !text.is_empty() => text,
            failed => {
                if let Err(e) = failed {
                    tracing::warn!("discord voice: {e}");
                }
                self.send(&channel_id, "(could not transcribe audio)", None).await?;
                return Ok(());
            }
        };
        let target = self.target(message, &text).await;
        // Titled by what was said, not by the stored "[Voice] …".
        let display = format!("[Voice] {text}");
        self.dispatch(&target, message, model, text.clone(), display, &text, vec![]).await;
        Ok(())
    }

    /// `_dispatch`: start the turn in `channel_id`, then stream its reply.
    #[allow(clippy::too_many_arguments)]
    async fn dispatch(
        self: &Arc<Self>,
        channel_id: &str,
        original: &Value,
        model: String,
        query: String,
        display: String,
        title: &str,
        attachments: Vec<Attachment>,
    ) {
        let typing = self.typing(channel_id);
        let conversation_id = format!("discord_{channel_id}");
        match self.ctx.dispatch("discord", conversation_id, model, query, display, title, attachments).await {
            Ok(Turn::Started(reply)) => self.stream(channel_id, original, reply, typing).await,
            Ok(Turn::Note(note)) => {
                drop(typing);
                if let Err(e) = self.send(channel_id, &note, Some(original)).await {
                    tracing::debug!("discord: {e}");
                }
            }
            Err(e) => tracing::warn!("discord: starting a turn: {e}"),
        }
    }

    /// `_stream_to_discord`: the reply as a run of messages, each edited as
    /// its chunk grows; the first replies to the user's message.
    async fn stream(&self, channel_id: &str, original: &Value, mut reply: Reply, typing: Typing) {
        let mut typing = Some(typing);
        let mut sent: Vec<(String, String)> = vec![];
        loop {
            let (text, last) = match reply.next().await {
                Step::Speaking => {
                    typing = None;
                    continue;
                }
                Step::Render(text) => (text, false),
                Step::Finished(text) => (if text.is_empty() { "(no response)".into() } else { text }, true),
            };
            if let Err(e) = self.render(channel_id, original, &mut sent, &text).await {
                tracing::debug!("discord edit: {e}");
            }
            if last {
                drop(typing);
                return;
            }
        }
    }

    async fn render(
        &self,
        channel_id: &str,
        original: &Value,
        sent: &mut Vec<(String, String)>,
        text: &str,
    ) -> Result<(), String> {
        for (i, chunk) in split(text, MAX_MESSAGE).into_iter().enumerate() {
            match sent.get_mut(i) {
                Some((id, shown)) => {
                    if *shown != chunk {
                        self.edit(channel_id, id, &chunk).await?;
                        *shown = chunk;
                    }
                }
                None => {
                    let id = self.send(channel_id, &chunk, (i == 0).then_some(original)).await?;
                    sent.push((id, chunk));
                }
            }
        }
        Ok(())
    }
}

async fn send<S>(tx: &mut S, payload: &Value) -> Result<(), String>
where
    S: futures_util::Sink<WsMessage> + Unpin,
    S::Error: std::fmt::Display,
{
    tx.send(WsMessage::Text(payload.to_string().into())).await.map_err(|e| e.to_string())
}

/// What a close from Discord means for the next connection.
fn on_close(code: Option<CloseCode>) -> End {
    match code.map(u16::from) {
        Some(4004) => End::Fatal("the token was rejected (4004)".into()),
        Some(4014) => End::Fatal(
            "a privileged intent is not enabled (4014) — turn on Message Content Intent in the developer portal".into(),
        ),
        Some(code @ (4010..=4013)) => End::Fatal(format!("gateway close {code}")),
        Some(4007 | 4009) => End::Identify,
        _ => End::Resume,
    }
}

/// Up to `max`, from the clock: enough to spread heartbeats and identifies.
fn jitter(max: Duration) -> Duration {
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().subsec_nanos();
    max.mul_f64(f64::from(nanos) / 1e9)
}

/// `_strip_bot_mention`.
fn strip_mention(content: &str, me: Option<&str>) -> String {
    let Some(me) = me else { return content.trim().to_string() };
    content.replace(&format!("<@{me}>"), "").replace(&format!("<@!{me}>"), "").trim().to_string()
}

/// `_split_for_discord`: chunks of at most `limit` code points, cut at a
/// paragraph, line or word break in the last quarter of the window when there
/// is one, so the seams stay put as more text streams in.
fn split(text: &str, limit: usize) -> Vec<String> {
    if text.is_empty() {
        return vec![String::new()];
    }
    let mut chunks = vec![];
    let mut rest: Vec<char> = text.chars().collect();
    while rest.len() > limit {
        let window = limit - limit / 4;
        let cut = ["\n\n", "\n", " "].iter().find_map(|sep| rfind(&rest, sep, window, limit)).unwrap_or(limit);
        let head: String = rest[..cut].iter().collect();
        chunks.push(head.trim_end().to_string());
        let tail: String = rest[cut..].iter().collect();
        rest = tail.trim_start().chars().collect();
    }
    if !rest.is_empty() {
        chunks.push(rest.into_iter().collect());
    }
    chunks
}

/// Python's `s.rfind(sub, start, end)` over code points.
fn rfind(s: &[char], sub: &str, start: usize, end: usize) -> Option<usize> {
    let sub: Vec<char> = sub.chars().collect();
    let end = end.min(s.len());
    if end < start + sub.len() {
        return None;
    }
    (start..=end - sub.len()).rev().find(|&i| s[i..i + sub.len()] == sub[..])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_matches_python() {
        // _split_for_discord(text, limit) for each.
        assert_eq!(split("", 10), vec![""]);
        assert_eq!(split("short", 10), vec!["short"]);
        assert_eq!(split("aaaa bbbb cccc", 10), vec!["aaaa bbbb", "cccc"]);
        assert_eq!(split("aaaaaaa\n\nbbbbbbbb", 10), vec!["aaaaaaa", "bbbbbbbb"]);
        assert_eq!(split("abcdefghijklmnop", 10), vec!["abcdefghij", "klmnop"]);
        // A break before the last quarter doesn't count.
        assert_eq!(split("ab cdefghijklm", 10), vec!["ab cdefghi", "jklm"]);
        assert_eq!(split("ééééééééé éé", 10), vec!["ééééééééé", "éé"]);
    }

    #[test]
    fn rfind_is_pythons() {
        let s: Vec<char> = "a b a b".chars().collect();
        assert_eq!(rfind(&s, " ", 0, 7), Some(5));
        assert_eq!(rfind(&s, " ", 0, 5), Some(3));
        assert_eq!(rfind(&s, "a b", 1, 7), Some(4));
        assert_eq!(rfind(&s, "x", 0, 7), None);
        assert_eq!(rfind(&s, " ", 6, 7), None);
    }

    #[test]
    fn mentions_are_stripped() {
        assert_eq!(strip_mention("<@42> hi <@!42>", Some("42")), "hi");
        assert_eq!(strip_mention("  hi ", None), "hi");
    }

    #[test]
    fn closes() {
        assert!(matches!(on_close(Some(CloseCode::from(4004))), End::Fatal(_)));
        assert!(matches!(on_close(Some(CloseCode::from(4014))), End::Fatal(_)));
        assert!(matches!(on_close(Some(CloseCode::from(4009))), End::Identify));
        assert!(matches!(on_close(Some(CloseCode::from(1001))), End::Resume));
        assert!(matches!(on_close(None), End::Resume));
    }
}
