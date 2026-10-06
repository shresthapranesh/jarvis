//! A Chrome DevTools Protocol client, as much of one as the live view needs:
//! one browser-level WebSocket, flattened sessions (a command for a page
//! carries its `sessionId`), replies matched by id, events on a channel that
//! ends when the connection does.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::Message;

type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Result<Value, String>>>>>;

/// An event: its method, params and the session it came from ("" for the
/// browser's own).
pub struct Event {
    pub method: String,
    pub params: Value,
    pub session: String,
}

pub struct Cdp {
    out: mpsc::UnboundedSender<Message>,
    pending: Pending,
    next: AtomicU64,
    alive: Arc<std::sync::atomic::AtomicBool>,
}

impl Cdp {
    /// Connect to the browser behind an http(s) DevTools endpoint, as
    /// `connect_over_cdp` does: `/json/version` names its WebSocket.
    pub async fn connect(
        http: &reqwest::Client,
        endpoint: &str,
    ) -> Result<(Self, mpsc::UnboundedReceiver<Event>), String> {
        let version: Value = http
            .get(format!("{}/json/version", endpoint.trim_end_matches('/')))
            .send()
            .await
            .map_err(|e| e.to_string())?
            .json()
            .await
            .map_err(|e| format!("{endpoint}/json/version: {e}"))?;
        let ws_url = version["webSocketDebuggerUrl"]
            .as_str()
            .ok_or_else(|| format!("{endpoint}/json/version names no webSocketDebuggerUrl"))?;
        let mut config = tokio_tungstenite::tungstenite::protocol::WebSocketConfig::default();
        // A full-page JPEG can be several MB once base64-encoded.
        config.max_message_size = None;
        config.max_frame_size = None;
        let (stream, _) =
            tokio_tungstenite::connect_async_with_config(ws_url, Some(config), false).await.map_err(|e| e.to_string())?;
        Ok(Self::over(stream))
    }

    fn over<S>(stream: tokio_tungstenite::WebSocketStream<S>) -> (Self, mpsc::UnboundedReceiver<Event>)
    where
        S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
    {
        let (mut sink, mut source) = stream.split();
        let (out, mut outgoing) = mpsc::unbounded_channel::<Message>();
        let (events, received) = mpsc::unbounded_channel::<Event>();
        let pending: Pending = Default::default();
        let alive = Arc::new(std::sync::atomic::AtomicBool::new(true));
        tokio::spawn(async move {
            while let Some(msg) = outgoing.recv().await {
                let close = matches!(msg, Message::Close(_));
                if sink.send(msg).await.is_err() || close {
                    break;
                }
            }
        });
        let (replies, up) = (pending.clone(), alive.clone());
        tokio::spawn(async move {
            while let Some(Ok(frame)) = source.next().await {
                let text = match frame {
                    Message::Text(text) => text,
                    Message::Close(_) => break,
                    _ => continue,
                };
                let Ok(msg) = serde_json::from_str::<Value>(&text) else { continue };
                if let Some(id) = msg.get("id").and_then(Value::as_u64) {
                    if let Some(reply) = replies.lock().expect("pending lock").remove(&id) {
                        let result = match msg.get("error") {
                            Some(err) => Err(err["message"].as_str().unwrap_or("CDP error").to_string()),
                            None => Ok(msg.get("result").cloned().unwrap_or(Value::Null)),
                        };
                        let _ = reply.send(result);
                    }
                } else if let Some(method) = msg.get("method").and_then(Value::as_str) {
                    let _ = events.send(Event {
                        method: method.to_string(),
                        params: msg.get("params").cloned().unwrap_or(Value::Null),
                        session: msg.get("sessionId").and_then(Value::as_str).unwrap_or_default().to_string(),
                    });
                }
            }
            up.store(false, Ordering::SeqCst);
            // Whoever still waits on a reply gets none.
            replies.lock().expect("pending lock").clear();
        });
        (Cdp { out, pending, next: AtomicU64::new(1), alive }, received)
    }

    fn message(&self, session: Option<&str>, method: &str, params: Value) -> (u64, Message) {
        let id = self.next.fetch_add(1, Ordering::Relaxed);
        let mut msg = json!({"id": id, "method": method, "params": params});
        if let Some(session) = session {
            msg["sessionId"] = json!(session);
        }
        (id, Message::Text(msg.to_string().into()))
    }

    /// A command and its result.
    pub async fn call(&self, session: Option<&str>, method: &str, params: Value) -> Result<Value, String> {
        let (id, msg) = self.message(session, method, params);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().expect("pending lock").insert(id, tx);
        if !self.alive() || self.out.send(msg).is_err() {
            self.pending.lock().expect("pending lock").remove(&id);
            return Err("the browser connection is closed".into());
        }
        rx.await.unwrap_or_else(|_| Err(format!("{method}: the browser connection closed")))
    }

    /// A command whose reply nobody reads.
    pub fn send(&self, session: Option<&str>, method: &str, params: Value) {
        let (_, msg) = self.message(session, method, params);
        let _ = self.out.send(msg);
    }

    pub fn alive(&self) -> bool {
        self.alive.load(Ordering::SeqCst)
    }

    /// Drop the connection; the browser carries on.
    pub fn close(&self) {
        let _ = self.out.send(Message::Close(None));
    }
}

impl Drop for Cdp {
    /// An attach abandoned halfway (its timeout) leaves no reader behind.
    fn drop(&mut self) {
        self.close();
    }
}
