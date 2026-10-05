//! A run's live events and step rows, as `core/streaming.py` makes them:
//! streamed text batched per source (`TokenCoalescer`), every other event
//! after a flush so order holds, and each step written to `steps` before it
//! is announced — a subscriber never sees a step a reload wouldn't show.

use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use sqlx::SqlitePool;

use crate::gql::codec::{new_id, now_stored};
use crate::runs::Run;

/// `TokenCoalescer` defaults: flush at 64 characters or 50 ms, whichever first.
const MAX_CHARS: usize = 64;
const MAX_DELAY: Duration = Duration::from_millis(50);
/// `WORKER_RESULT_PERSIST_CAP`: how much of a worker's result its step keeps.
const WORKER_RESULT_CAP: usize = 2000;

/// One event kind's buffer (`_Bucket`), for the main agent only.
struct Bucket {
    event: &'static str,
    text: String,
    chars: usize,
    since: Option<Instant>,
}

impl Bucket {
    fn new(event: &'static str) -> Self {
        Bucket { event, text: String::new(), chars: 0, since: None }
    }

    /// Buffer `text`; the batch to emit when a threshold is hit.
    fn add(&mut self, text: &str) -> Option<String> {
        self.since.get_or_insert_with(Instant::now);
        self.text.push_str(text);
        self.chars += text.chars().count();
        (self.chars >= MAX_CHARS || self.since.is_some_and(|t| t.elapsed() >= MAX_DELAY)).then(|| self.take())?
    }

    fn take(&mut self) -> Option<String> {
        self.chars = 0;
        self.since = None;
        (!self.text.is_empty()).then(|| std::mem::take(&mut self.text))
    }
}

pub struct Emitter {
    run: Arc<Run>,
    pool: SqlitePool,
    task_id: String,
    conversation_id: String,
    /// The next `steps.seq`: a handed-over turn's Python side goes on from it.
    pub step_seq: i64,
    /// Whether steps are written as rows (chat) or only announced
    /// (`persist_steps=False`: automations, board tasks).
    rows: bool,
    tokens: Bucket,
    thinking: Bucket,
}

impl Emitter {
    pub fn new(run: Arc<Run>, pool: SqlitePool, task_id: &str, conversation_id: &str) -> Self {
        Emitter {
            run,
            pool,
            task_id: task_id.into(),
            conversation_id: conversation_id.into(),
            step_seq: 0,
            rows: true,
            tokens: Bucket::new("token"),
            thinking: Bucket::new("thinking_token"),
        }
    }

    /// Announce steps without writing their rows.
    pub fn without_rows(mut self) -> Self {
        self.rows = false;
        self
    }

    pub fn token(&mut self, text: &str) {
        if let Some(batch) = (!text.is_empty()).then(|| self.tokens.add(text)).flatten() {
            self.raw("token", &json!({"text": batch, "source": "main"}));
        }
    }

    pub fn thinking(&mut self, text: &str) {
        if let Some(batch) = (!text.is_empty()).then(|| self.thinking.add(text)).flatten() {
            self.raw("thinking_token", &json!({"text": batch, "source": "main"}));
        }
    }

    /// `flush_all`.
    pub fn flush(&mut self) {
        for bucket in [&mut self.tokens, &mut self.thinking] {
            if let Some(batch) = bucket.take() {
                let event = bucket.event;
                self.run.emit_local(event, &json!({"text": batch, "source": "main"}));
            }
        }
    }

    /// Any event but a token: after what's buffered.
    pub fn emit(&mut self, event: &str, data: &Value) {
        self.flush();
        self.raw(event, data);
    }

    fn raw(&self, event: &str, data: &Value) {
        if !self.run.emit_local(event, data) {
            tracing::warn!("agent: run {} is no longer the edge's; dropped its {event} event", self.task_id);
        }
    }

    /// A finished step: its row, then its event.
    pub async fn step(&mut self, node: &str, data: String) -> Result<(), String> {
        self.flush();
        if !self.rows {
            self.raw("step", &json!({"node": node, "source": "main", "subagent": null, "data": data}));
            return Ok(());
        }
        sqlx::query(
            "INSERT INTO steps (id, message_id, conversation_id, node, source, subagent, data, seq, created_at) \
             VALUES (?, ?, ?, ?, 'main', NULL, ?, ?, ?)",
        )
        .bind(new_id())
        .bind(&self.task_id)
        .bind(&self.conversation_id)
        .bind(node)
        .bind(&data)
        .bind(self.step_seq)
        .bind(now_stored())
        .execute(&self.pool)
        .await
        .map_err(|e| e.to_string())?;
        self.step_seq += 1;
        self.raw("step", &json!({"node": node, "source": "main", "subagent": null, "data": data}));
        Ok(())
    }

    /// One of `spawn_workers`' events. A worker's progress (all but its
    /// tokens) is first written as a `subagent` step of its group,
    /// `<role>:<idx>`, which the transcript rebuilds its card from.
    pub async fn worker(&mut self, event: &str, data: &Value) -> Result<(), String> {
        if !event.starts_with("worker_") {
            self.emit(event, data);
            return Ok(());
        }
        if event == "worker_token" {
            // Coalesced at the source already, and never beside main text.
            self.raw(event, data);
            return Ok(());
        }
        self.flush();
        if self.rows {
            let field = |k: &str| data.get(k).cloned().unwrap_or(Value::Null);
            let or = |k: &str, default: &str| match data.get(k) {
                Some(v) => crate::pyjson::py_str(v),
                None => default.to_string(),
            };
            let group = format!("{}:{}", or("role", "worker"), or("idx", "?"));
            let (node, step) = if event == "worker_step" {
                (or("node", "worker"), data.get("data").and_then(Value::as_str).map(str::to_string))
            } else {
                let mut record = json!({"idx": field("idx"), "role": field("role"), "task": field("task")});
                if event == "worker_done" {
                    record["status"] = data.get("status").cloned().unwrap_or_else(|| json!("done"));
                    let result = data.get("result").filter(|v| crate::pyjson::truthy(v)).map(crate::pyjson::py_str).unwrap_or_default();
                    record["result"] = json!(crate::pystr::prefix(&result, WORKER_RESULT_CAP));
                }
                (event.to_string(), Some(crate::pyjson::dumps(&record)))
            };
            sqlx::query(
                "INSERT INTO steps (id, message_id, conversation_id, node, source, subagent, data, seq, created_at) \
                 VALUES (?, ?, ?, ?, 'subagent', ?, ?, ?, ?)",
            )
            .bind(new_id())
            .bind(&self.task_id)
            .bind(&self.conversation_id)
            .bind(&node)
            .bind(&group)
            .bind(&step)
            .bind(self.step_seq)
            .bind(now_stored())
            .execute(&self.pool)
            .await
            .map_err(|e| e.to_string())?;
            self.step_seq += 1;
        }
        self.raw(event, data);
        Ok(())
    }
}
