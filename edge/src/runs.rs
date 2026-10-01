//! The edge's mirror of live runs — `core/state.py:_tasks`, as reported over
//! the worker link (`link.rs`).
//!
//! A run's events are kept whole, in order, for as long as Python keeps the
//! run registered, so a subscriber that arrives late replays from the first
//! event — exactly what `stream_task_events` did against `TaskState.events`.
//!
//! Every change bumps the run's `watch` version; subscribers wait on that.
//! A run that disappears without finishing (its worker restarted, or the
//! snapshot after a reconnect doesn't carry it) is marked `gone`, and its
//! subscribers end with the same DB fallback a fresh subscription would get.

use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use serde::Deserialize;
use serde_json::Value;
use tokio::sync::{mpsc, watch};

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
    fn new(id: String, meta: Meta, events: Vec<Value>, fields: Fields) -> Arc<Self> {
        Arc::new(Self {
            id,
            meta,
            state: Mutex::new(RunState { events, fields, gone: false }),
            version: watch::channel(0).0,
        })
    }

    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.version.subscribe()
    }

    /// Apply a change and wake every subscriber.
    pub fn update(&self, f: impl FnOnce(&mut RunState)) {
        f(&mut self.state.lock().expect("run state lock"));
        self.version.send_modify(|v| *v += 1);
    }

    pub fn fields(&self) -> Fields {
        self.state.lock().expect("run state lock").fields.clone()
    }
}

/// Edge → worker control messages, already serialized.
pub type ControlTx = mpsc::UnboundedSender<String>;

#[derive(Default)]
struct Inner {
    /// Registration order, as Python's dict keeps it: `runningTasks` sorts
    /// stably by start time, so ties keep this order.
    runs: IndexMap<String, Arc<Run>>,
    link: Option<Link>,
    /// The last worker instance seen, kept across a disconnect: whether a
    /// reconnect is the same process is a question about the one *before*.
    last_instance: Option<String>,
}

struct Link {
    control: ControlTx,
    session: u64,
}

pub struct Registry {
    inner: Mutex<Inner>,
    /// Bumped on every registration, so a subscriber waiting for a run that
    /// hasn't been reported yet can wake when it is.
    registered: watch::Sender<u64>,
    sessions: std::sync::atomic::AtomicU64,
}

impl Default for Registry {
    fn default() -> Self {
        Self {
            inner: Mutex::default(),
            registered: watch::channel(0).0,
            sessions: std::sync::atomic::AtomicU64::new(0),
        }
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

    pub fn link_up(&self) -> bool {
        self.lock().link.is_some()
    }

    pub fn on_registration(&self) -> watch::Receiver<u64> {
        self.registered.subscribe()
    }

    /// Send a control message to the worker. False when no worker is linked
    /// (the durable path — `jobs.cancel_requested` — still reaches it).
    pub fn control(&self, msg: &Value) -> bool {
        match &self.lock().link {
            Some(link) => link.control.send(msg.to_string()).is_ok(),
            None => false,
        }
    }

    // ── worker link ─────────────────────────────────────────────────────────

    /// A worker connected. Returns its session number, which every later call
    /// for this connection carries so a stale connection can't clobber a new
    /// one. A different `instance` than last time means a different process:
    /// none of the previous runs can still be live.
    pub fn attach(&self, instance: String, control: ControlTx) -> u64 {
        let session = self.sessions.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
        let mut inner = self.lock();
        let same_process = inner.last_instance.as_deref() == Some(instance.as_str());
        inner.last_instance = Some(instance);
        inner.link = Some(Link { control, session });
        if !same_process {
            for run in inner.runs.values() {
                mark_gone(run);
            }
            inner.runs.clear();
        }
        session
    }

    pub fn detach(&self, session: u64) {
        let mut inner = self.lock();
        if inner.link.as_ref().is_some_and(|l| l.session == session) {
            inner.link = None;
        }
    }

    fn current(&self, inner: &Inner, session: u64) -> bool {
        inner.link.as_ref().is_some_and(|l| l.session == session)
    }

    /// The full set of live runs. Runs it doesn't carry are gone; runs it
    /// does are reconciled (`register`).
    pub fn snapshot(&self, session: u64, runs: Vec<Reported>) {
        let ids: std::collections::HashSet<_> = runs.iter().map(|r| r.task_id.clone()).collect();
        {
            let mut inner = self.lock();
            if !self.current(&inner, session) {
                return;
            }
            inner.runs.retain(|id, run| {
                let keep = ids.contains(id);
                if !keep {
                    mark_gone(run);
                }
                keep
            });
        }
        for r in runs {
            self.register(session, r);
        }
    }

    /// A run (re)registered. If the edge already mirrors it and the reported
    /// history extends what it has, that's the same run — extend in place so
    /// attached subscribers keep their cursor. Otherwise it's a new run under
    /// an old id: the old one is gone.
    pub fn register(&self, session: u64, r: Reported) {
        let mut inner = self.lock();
        if !self.current(&inner, session) {
            return;
        }
        if let Some(existing) = inner.runs.get(&r.task_id) {
            let extends = {
                let st = existing.state.lock().expect("run state lock");
                r.events.len() >= st.events.len() && r.events[..st.events.len()] == st.events[..]
            };
            if extends {
                existing.update(|st| {
                    let have = st.events.len();
                    st.events.extend(r.events.into_iter().skip(have));
                    st.fields = r.fields;
                });
                return;
            }
            mark_gone(existing);
        }
        let run = Run::new(r.task_id.clone(), r.meta, r.events, r.fields);
        // IndexMap::insert keeps an existing key's position, as a dict does.
        inner.runs.insert(r.task_id, run);
        drop(inner);
        self.registered.send_modify(|v| *v += 1);
    }

    pub fn events(&self, session: u64, task_id: &str, from: usize, events: Vec<Value>) {
        let inner = self.lock();
        if !self.current(&inner, session) {
            return;
        }
        let Some(run) = inner.runs.get(task_id).cloned() else { return };
        drop(inner);
        run.update(|st| {
            let have = st.events.len();
            if from > have {
                tracing::warn!("run {task_id}: events from {from} but only {have} held; gap");
            }
            // Overlap is skipped, so a resend can't duplicate.
            let skip = have.saturating_sub(from);
            st.events.extend(events.into_iter().skip(skip));
        });
    }

    pub fn state(&self, session: u64, task_id: &str, fields: Fields) {
        let inner = self.lock();
        if !self.current(&inner, session) {
            return;
        }
        if let Some(run) = inner.runs.get(task_id).cloned() {
            drop(inner);
            run.update(|st| st.fields = fields);
        }
    }

    pub fn unregister(&self, session: u64, task_id: &str) {
        let mut inner = self.lock();
        if !self.current(&inner, session) {
            return;
        }
        if let Some(run) = inner.runs.shift_remove(task_id) {
            mark_gone(&run);
        }
    }
}

/// A run leaving the mirror. Finished runs need nothing: their subscribers
/// end on `done`. Unfinished ones are woken to fall back to the DB.
fn mark_gone(run: &Run) {
    run.update(|st| st.gone = true);
}

/// A run as the worker reports it in `snapshot` and `register`.
#[derive(Deserialize)]
pub struct Reported {
    pub task_id: String,
    #[serde(flatten)]
    pub meta: Meta,
    #[serde(default)]
    pub events: Vec<Value>,
    #[serde(flatten)]
    pub fields: Fields,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn reported(id: &str, events: Vec<Value>) -> Reported {
        serde_json::from_value(json!({
            "task_id": id, "kind": "chat", "label": "l", "parent_id": null,
            "started_at": "2026-10-01T00:00:00+00:00", "events": events,
            "done": false, "cancelled": false, "has_interrupt": false, "input_tokens": 0,
            "output_tokens": 0, "total_tokens": 0, "llm_calls": 0, "tool_calls": 0,
            "budget_exceeded": false, "budget_reason": null,
        }))
        .unwrap()
    }

    fn ev(n: i64) -> Value {
        json!({"event": "token", "data": format!("{{\"text\": \"{n}\"}}")})
    }

    #[test]
    fn same_process_reconnect_extends_in_place() {
        let reg = Registry::default();
        let (tx, _rx) = mpsc::unbounded_channel();
        let s1 = reg.attach("A".into(), tx.clone());
        reg.register(s1, reported("t", vec![ev(1)]));
        let run = reg.get("t").unwrap();
        let s2 = reg.attach("A".into(), tx);
        reg.snapshot(s2, vec![reported("t", vec![ev(1), ev(2)])]);
        assert!(Arc::ptr_eq(&run, &reg.get("t").unwrap()));
        assert_eq!(run.state.lock().unwrap().events.len(), 2);
        assert!(!run.state.lock().unwrap().gone);
    }

    #[test]
    fn same_process_is_recognized_after_a_detach() {
        let reg = Registry::default();
        let (tx, _rx) = mpsc::unbounded_channel();
        let s1 = reg.attach("A".into(), tx.clone());
        reg.register(s1, reported("t", vec![ev(1)]));
        reg.detach(s1);
        reg.attach("A".into(), tx);
        assert!(reg.get("t").is_some());
    }

    #[test]
    fn new_process_drops_every_run() {
        let reg = Registry::default();
        let (tx, _rx) = mpsc::unbounded_channel();
        let s1 = reg.attach("A".into(), tx.clone());
        reg.register(s1, reported("t", vec![ev(1)]));
        let run = reg.get("t").unwrap();
        let s2 = reg.attach("B".into(), tx);
        assert!(run.state.lock().unwrap().gone);
        assert!(reg.get("t").is_none());
        // A stale session can't write into the new one.
        reg.register(s1, reported("u", vec![]));
        assert!(reg.get("u").is_none());
        reg.register(s2, reported("u", vec![]));
        assert!(reg.get("u").is_some());
    }

    #[test]
    fn overlapping_events_are_not_duplicated() {
        let reg = Registry::default();
        let (tx, _rx) = mpsc::unbounded_channel();
        let s = reg.attach("A".into(), tx);
        reg.register(s, reported("t", vec![ev(1)]));
        reg.events(s, "t", 0, vec![ev(1), ev(2)]);
        assert_eq!(reg.get("t").unwrap().state.lock().unwrap().events.len(), 2);
    }

    #[test]
    fn a_different_history_is_a_different_run() {
        let reg = Registry::default();
        let (tx, _rx) = mpsc::unbounded_channel();
        let s = reg.attach("A".into(), tx);
        reg.register(s, reported("t", vec![ev(1), ev(2)]));
        let old = reg.get("t").unwrap();
        reg.register(s, reported("t", vec![]));
        assert!(old.state.lock().unwrap().gone);
        assert!(!Arc::ptr_eq(&old, &reg.get("t").unwrap()));
    }
}
