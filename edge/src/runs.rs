//! The live runs — `core/state.py:_tasks` as the edge keeps it.
//!
//! A run's events are kept whole, in order, for as long as it is registered,
//! so a subscriber that arrives late replays from the first event — exactly
//! what `stream_task_events` did against `TaskState.events`.
//!
//! Every change bumps the run's `watch` version; subscribers wait on that.
//! A run that disappears without finishing (its job ended before the agent
//! loop took it) is marked `gone`, and its subscribers end with the same DB
//! fallback a fresh subscription would get.
//!
//! A trigger (`startTask` and the others, `gql/start.rs`) registers the run
//! before it commits the job, so a subscriber can't race the agent loop
//! (`agent/`), which takes the run when it claims the job and appends its
//! every event.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use indexmap::IndexMap;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::SqlitePool;
use tokio::sync::watch;

use crate::pyjson;

/// How long a finished run stays mirrored (`TASK_LINGER_SECONDS`).
const LINGER: Duration = Duration::from_secs(5);

/// Everything `runningTasks` shows besides identity.
#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
pub struct Fields {
    pub done: bool,
    pub cancelled: bool,
    pub has_interrupt: bool,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub total_tokens: i64,
    pub llm_calls: i64,
    pub tool_calls: i64,
    pub budget_exceeded: bool,
    pub budget_reason: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Meta {
    pub kind: String,
    pub label: String,
    pub parent_id: Option<String>,
    /// Python's `isoformat()` of the aware start time, passed through.
    pub started_at: String,
}

#[derive(Default)]
pub struct RunState {
    /// Raw `{"event", "data"}` records, as `emit_event` appended them.
    pub events: Vec<Value>,
    pub fields: Fields,
    /// Removed without finishing; see the module docs.
    pub gone: bool,
}

pub struct Run {
    pub id: String,
    pub meta: Meta,
    pub state: Mutex<RunState>,
    version: watch::Sender<u64>,
}

impl Run {
    fn new(id: String, meta: Meta, state: RunState) -> Arc<Self> {
        Arc::new(Self { id, meta, state: Mutex::new(state), version: watch::channel(0).0 })
    }

    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.version.subscribe()
    }

    /// Apply a change and wake every subscriber.
    pub fn update<R>(&self, f: impl FnOnce(&mut RunState) -> R) -> R {
        let out = f(&mut self.state.lock().expect("run state lock"));
        self.version.send_modify(|v| *v += 1);
        out
    }

    pub fn fields(&self) -> Fields {
        self.state.lock().expect("run state lock").fields.clone()
    }

    /// Append an event, as `emit_event` would have.
    pub fn emit_local(&self, event: &str, data: &Value) {
        self.update(|st| st.events.push(record(event, data)));
    }
}

/// `{"event": name, "data": json.dumps(payload)}`.
fn record(event: &str, data: &Value) -> Value {
    json!({"event": event, "data": pyjson::dumps(data)})
}

#[derive(Default)]
struct Inner {
    /// Registration order, as Python's dict keeps it: `runningTasks` sorts
    /// stably by start time, so ties keep this order.
    runs: IndexMap<String, Arc<Run>>,
}

pub struct Registry {
    inner: Mutex<Inner>,
    /// Signalled by `wake`: a job was written, for the agent loop (`agent/`).
    pub agent_work: tokio::sync::Notify,
    /// Bumped on every registration, so a subscriber waiting for a run that
    /// hasn't been registered yet can wake when it is.
    registered: watch::Sender<u64>,
}

impl Default for Registry {
    fn default() -> Self {
        Self { inner: Mutex::default(), agent_work: tokio::sync::Notify::new(), registered: watch::channel(0).0 }
    }
}

impl Registry {
    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().expect("registry lock")
    }

    pub fn get(&self, id: &str) -> Option<Arc<Run>> {
        self.lock().runs.get(id).cloned()
    }

    pub fn all(&self) -> Vec<Arc<Run>> {
        self.lock().runs.values().cloned().collect()
    }

    pub fn on_registration(&self) -> watch::Receiver<u64> {
        self.registered.subscribe()
    }

    /// A job was just committed: have the agent loop claim it now rather
    /// than at its next poll.
    pub fn wake(&self) {
        self.agent_work.notify_one();
    }

    /// Register a run the edge is about to commit a job for, so a subscriber
    /// that gets its id back finds it — what the Python triggers did by
    /// setting `_tasks[task_id]` before their commit.
    pub fn pre_register(&self, id: &str, meta: Meta) -> Arc<Run> {
        let mut inner = self.lock();
        if let Some(run) = inner.runs.get(id) {
            return run.clone();
        }
        let run = Run::new(id.to_string(), meta, RunState::default());
        inner.runs.insert(id.to_string(), run.clone());
        drop(inner);
        self.registered.send_modify(|v| *v += 1);
        run
    }

    /// Undo `pre_register` for a job that was never committed.
    pub fn discard_pending(&self, id: &str) {
        if let Some(run) = self.lock().runs.shift_remove(id) {
            mark_gone(&run);
        }
    }

    /// The agent loop claimed the job `id`: its run. A run the registry lost
    /// (the edge restarted since its trigger) is registered again from `meta`.
    pub fn take(&self, id: &str, meta: impl FnOnce() -> Meta) -> Arc<Run> {
        let mut inner = self.lock();
        if let Some(run) = inner.runs.get(id).cloned() {
            return run;
        }
        let run = Run::new(id.to_string(), meta(), RunState::default());
        inner.runs.insert(id.to_string(), run.clone());
        drop(inner);
        self.registered.send_modify(|v| *v += 1);
        run
    }

    /// A finished run: it leaves the registry a little later, so a
    /// subscriber that arrives just after still replays it
    /// (`TASK_LINGER_SECONDS`).
    pub fn retire(self: &Arc<Self>, id: &str) {
        let Some(run) = self.get(id) else { return };
        let me = self.clone();
        tokio::spawn(async move {
            tokio::time::sleep(LINGER).await;
            let mut inner = me.lock();
            if inner.runs.get(&run.id).is_some_and(|r| Arc::ptr_eq(r, &run)) {
                inner.runs.shift_remove(&run.id);
            }
        });
    }

    /// The chat run up on a conversation — `in_flight_chat_task`: the first,
    /// in registration order, that hasn't finished.
    pub fn in_flight_chat(&self, conversation_id: &str) -> Option<Arc<Run>> {
        self.lock()
            .runs
            .values()
            .find(|run| {
                run.meta.kind == "chat"
                    && run.meta.parent_id.as_deref() == Some(conversation_id)
                    && !run.fields().done
            })
            .cloned()
    }

    /// End runs whose job finished without the agent loop reporting it — it
    /// failed or was stopped before the run started. Their subscribers fall
    /// back to the DB.
    pub async fn sweep_pending(&self, pool: &SqlitePool) {
        for run in self.all() {
            let status: Option<String> = match sqlx::query_scalar("SELECT status FROM jobs WHERE id = ?")
                .bind(&run.id)
                .fetch_optional(pool)
                .await
            {
                Ok(status) => status,
                Err(e) => {
                    tracing::warn!("sweeping pending runs: {e}");
                    return;
                }
            };
            if matches!(status.as_deref(), Some("pending" | "running")) {
                continue;
            }
            let mut inner = self.lock();
            if inner.runs.get(&run.id).is_some_and(|r| Arc::ptr_eq(r, &run)) {
                inner.runs.shift_remove(&run.id);
                drop(inner);
                tracing::info!("run {} ended unclaimed (job {})", run.id, status.as_deref().unwrap_or("missing"));
                mark_gone(&run);
            }
        }
    }
}

/// A run leaving the mirror. Finished runs need nothing: their subscribers
/// end on `done`. Unfinished ones are woken to fall back to the DB.
fn mark_gone(run: &Run) {
    run.update(|st| st.gone = true);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn meta(label: &str) -> Meta {
        Meta { kind: "chat".into(), label: label.into(), parent_id: Some("c".into()), started_at: "s".into() }
    }

    #[test]
    fn local_events_are_python_shaped() {
        let reg = Registry::default();
        let run = reg.pre_register("p", meta("l"));
        run.emit_local("queued_withdrawn", &json!({"message_id": "é"}));
        let events = run.state.lock().unwrap().events.clone();
        assert_eq!(events, [json!({"event": "queued_withdrawn", "data": "{\"message_id\": \"\\u00e9\"}"})]);
    }

    #[test]
    fn the_agent_loop_takes_the_registered_run() {
        let reg = Registry::default();
        let run = reg.pre_register("e", meta("trigger's"));
        assert!(Arc::ptr_eq(&run, &reg.take("e", || unreachable!("registered already"))));
        // A run the registry lost is registered again from its job.
        assert_eq!(reg.take("lost", || meta("from the job")).meta.label, "from the job");
    }
}
