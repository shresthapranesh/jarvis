//! The in-app log viewer's records — a port of `core/log_setup.py`'s
//! `BroadcastHandler` and `server/routes_logs.py`: the server's own events (a
//! `tracing` layer), in one buffer.
//!
//! A record is `{ts, level, logger, message}`, as Python's handler made it.

use std::collections::VecDeque;
use std::fmt::Write;
use std::sync::{LazyLock, Mutex};
use std::time::Duration;

use axum::http::{HeaderMap, StatusCode, header};
use axum::response::sse::{Event, Sse};
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};
use tokio::sync::broadcast;
use tracing::field::{Field, Visit};
use tracing_subscriber::Layer;
use tracing_subscriber::layer::Context;

/// `_BACKFILL_CAP`.
const BACKFILL_CAP: usize = 2000;
/// `_SUBSCRIBER_QUEUE_MAX`: a viewer further behind than this skips ahead.
const SUBSCRIBER_QUEUE_MAX: usize = 500;
/// `_MAX_MESSAGE_CHARS`.
const MAX_MESSAGE_CHARS: usize = 8192;
/// The stream's heartbeat across idle gaps.
const PING_EVERY: Duration = Duration::from_secs(15);

struct Store {
    backfill: Mutex<VecDeque<Value>>,
    live: broadcast::Sender<Value>,
}

static STORE: LazyLock<Store> =
    LazyLock::new(|| Store { backfill: Mutex::new(VecDeque::new()), live: broadcast::channel(SUBSCRIBER_QUEUE_MAX).0 });

/// Add a record to the backfill and every open stream.
pub fn push(record: Value) {
    {
        let mut backfill = STORE.backfill.lock().expect("log backfill lock");
        if backfill.len() == BACKFILL_CAP {
            backfill.pop_front();
        }
        backfill.push_back(record.clone());
    }
    let _ = STORE.live.send(record);
}

fn snapshot() -> Vec<Value> {
    STORE.backfill.lock().expect("log backfill lock").iter().cloned().collect()
}

/// The edge's events as records.
pub struct Capture;

#[derive(Default)]
struct Message {
    text: String,
    fields: String,
}

impl Visit for Message {
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            let _ = write!(self.text, "{value:?}");
        } else {
            let _ = write!(self.fields, " {}={value:?}", field.name());
        }
    }

    fn record_str(&mut self, field: &Field, value: &str) {
        if field.name() == "message" {
            self.text.push_str(value);
        } else {
            let _ = write!(self.fields, " {}={value}", field.name());
        }
    }
}

impl<S: tracing::Subscriber> Layer<S> for Capture {
    fn on_event(&self, event: &tracing::Event<'_>, _ctx: Context<'_, S>) {
        let meta = event.metadata();
        let mut message = Message::default();
        event.record(&mut message);
        let mut text = message.text + &message.fields;
        if text.chars().count() > MAX_MESSAGE_CHARS {
            text = format!("{}… [truncated]", crate::pystr::prefix(&text, MAX_MESSAGE_CHARS));
        }
        // Python's level names; its logger names are dotted.
        let level = match *meta.level() {
            tracing::Level::ERROR => "ERROR",
            tracing::Level::WARN => "WARNING",
            tracing::Level::INFO => "INFO",
            _ => "DEBUG",
        };
        let target = meta.target();
        let logger = match target.strip_prefix("jarvis_edge") {
            Some(rest) => format!("edge{}", rest.replace("::", ".")),
            None => target.replace("::", "."),
        };
        push(json!({
            "ts": chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string(),
            "level": level,
            "logger": logger,
            "message": text,
        }));
    }
}

/// `_require_localhost`'s Origin half; the peer half is `proxy::any`'s.
fn cross_origin(headers: &HeaderMap) -> Option<Response> {
    let origin = headers.get(header::ORIGIN).and_then(|v| v.to_str().ok()).unwrap_or("");
    let local = ["http://localhost", "http://127.0.0.1", "http://[::1]"].iter().any(|p| origin.starts_with(p));
    (!origin.is_empty() && !local).then(|| {
        (StatusCode::FORBIDDEN, [(header::CONTENT_TYPE, "application/json")], r#"{"error":"cross-origin not allowed"}"#)
            .into_response()
    })
}

/// `GET /server-logs`.
pub fn list(headers: &HeaderMap) -> Response {
    if let Some(refused) = cross_origin(headers) {
        return refused;
    }
    let body = json!({"logs": snapshot()});
    ([(header::CONTENT_TYPE, "application/json")], body.to_string()).into_response()
}

/// `GET /server-logs/stream`: the backfill, then each record as it comes,
/// with a ping across idle gaps. Data is `json.dumps`'s.
pub fn stream(headers: &HeaderMap) -> Response {
    if let Some(refused) = cross_origin(headers) {
        return refused;
    }
    let mut live = STORE.live.subscribe();
    let backfill = snapshot();
    let events = async_stream::stream! {
        yield Ok::<_, std::convert::Infallible>(Event::default().event("backfill").data(crate::pyjson::dumps(&Value::Array(backfill))));
        loop {
            match tokio::time::timeout(PING_EVERY, live.recv()).await {
                Ok(Ok(record)) => yield Ok(Event::default().event("log").data(crate::pyjson::dumps(&record))),
                // Fell behind: carry on from the newest.
                Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
                Ok(Err(broadcast::error::RecvError::Closed)) => break,
                Err(_) => yield Ok(Event::default().event("ping").data("{}")),
            }
        }
    };
    Sse::new(events).into_response()
}
