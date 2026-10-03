//! The edge's mirror of live runs — `core/state.py:_tasks`, as reported over
//! the worker link (`link.rs`), plus the runs the edge started itself.
//!
//! A run's events are kept whole, in order, for as long as it is registered,
//! so a subscriber that arrives late replays from the first event — exactly
//! what `stream_task_events` did against `TaskState.events`.
//!
//! Every change bumps the run's `watch` version; subscribers wait on that.
//! A run that disappears without finishing (its worker restarted, or the
//! snapshot after a reconnect doesn't carry it) is marked `gone`, and its
//! subscribers end with the same DB fallback a fresh subscription would get.
//!
//! **Two owners.** A run a worker registers is the worker's: the worker sends
//! its every event. A run the edge starts (`startTask` and the other triggers,
//! `gql/start.rs`) is the edge's own until a worker claims its job — it is
//! *pending*, the edge may append events to it (a message queued onto it),
//! and a new worker process doesn't end it, because no worker had it. The
//! claim is the worker's `register` for that id: from then on the worker's
//! event 0 follows whatever the edge appended (`worker_base`).

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use indexmap::IndexMap;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::SqlitePool;
use tokio::sync::{mpsc, oneshot, watch};

use crate::pyjson;

/// How long a `call` waits for the worker's reply.
const CALL_TIMEOUT: Duration = Duration::from_secs(30);

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
    /// Where the worker's event 0 sits in `events`. `None` while the run is
    /// pending — the edge's own, not yet claimed by any worker.
    pub worker_base: Option<usize>,
    /// A message was queued onto the run while it was pending, so the worker
    /// that claims it is told to look for queued messages again.
    queued_while_pending: bool,
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

    /// Whether a worker has claimed the run.
    pub fn claimed(&self) -> bool {
        self.state.lock().expect("run state lock").worker_base.is_some()
    }

    /// Append an event the edge produced, as `emit_event` would have. Only
    /// while the run is pending: once a worker has claimed it, every event is
    /// the worker's, and this returns false.
    pub fn emit_local(&self, event: &str, data: &Value) -> bool {
        self.update(|st| {
            if st.worker_base.is_some() {
                return false;
            }
            st.events.push(record(event, data));
            true
        })
    }

    /// `emit_local` of a `queued_message`, remembering that the claiming
    /// worker must adopt it.
    pub fn queue_local(&self, message_id: &str, text: &str, position: i64) -> bool {
        self.update(|st| {
            if st.worker_base.is_some() {
                return false;
            }
            st.queued_while_pending = true;
            st.events.push(record(
                "queued_message",
                &json!({"message_id": message_id, "text": text, "position": position}),
            ));
            true
        })
    }
}

/// `{"event": name, "data": json.dumps(payload)}`.
fn record(event: &str, data: &Value) -> Value {
    json!({"event": event, "data": pyjson::dumps(data)})
}

/// Edge → worker control messages, already serialized.
pub type ControlTx = mpsc::UnboundedSender<String>;

type Reply = Result<Value, String>;

#[derive(Default)]
struct Inner {
    /// Registration order, as Python's dict keeps it: `runningTasks` sorts
    /// stably by start time, so ties keep this order.
    runs: IndexMap<String, Arc<Run>>,
    link: Option<Link>,
    /// The last worker instance seen, kept across a disconnect: whether a
    /// reconnect is the same process is a question about the one *before*.
    last_instance: Option<String>,
    /// Calls awaiting the worker's reply, by call id, with the session they
    /// were sent on.
    calls: HashMap<u64, (u64, oneshot::Sender<Reply>)>,
}

struct Link {
    control: ControlTx,
    session: u64,
}

pub struct Registry {
    inner: Mutex<Inner>,
    /// Signalled by `wake`: a job was written. The supervisor listens, to
    /// start a worker when none is up to be woken.
    pub work: tokio::sync::Notify,
    /// Bumped on every registration, so a subscriber waiting for a run that
    /// hasn't been reported yet can wake when it is.
    registered: watch::Sender<u64>,
    sessions: AtomicU64,
    call_ids: AtomicU64,
}

impl Default for Registry {
    fn default() -> Self {
        Self {
            inner: Mutex::default(),
            work: tokio::sync::Notify::new(),
            registered: watch::channel(0).0,
            sessions: AtomicU64::new(0),
            call_ids: AtomicU64::new(0),
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

    /// A job was just committed: have the worker claim it now rather than at
    /// its next poll.
    pub fn wake(&self) {
        self.work.notify_one();
        self.control(&json!({"type": "wake"}));
    }

    /// Run `method` in the worker — the operations that act on a run's
    /// in-memory state there — and return its result, or the error message
    /// the worker's own resolver would have raised.
    pub async fn call(&self, method: &str, params: Value) -> Reply {
        let id = self.call_ids.fetch_add(1, Ordering::SeqCst) + 1;
        let (tx, rx) = oneshot::channel();
        {
            let mut inner = self.lock();
            let Some(link) = &inner.link else {
                return Err("no worker is linked".into());
            };
            let msg = json!({"type": "call", "id": id, "method": method, "params": params});
            if link.control.send(msg.to_string()).is_err() {
                return Err("no worker is linked".into());
            }
            let session = link.session;
            inner.calls.insert(id, (session, tx));
        }
        let out = tokio::time::timeout(CALL_TIMEOUT, rx).await;
        self.lock().calls.remove(&id);
        match out {
            Ok(Ok(reply)) => reply,
            Ok(Err(_)) => Err("the worker link dropped before it answered".into()),
            Err(_) => Err("the worker did not answer in time".into()),
        }
    }

    // ── runs the edge starts ────────────────────────────────────────────────

    /// Mirror a run the edge is about to commit a job for, so a subscriber
    /// that gets its id back finds it — what the Python triggers did by
    /// setting `_tasks[task_id]` before their commit. If a worker somehow
    /// registered the id first, that run stands.
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
        let mut inner = self.lock();
        if inner.runs.get(id).is_some_and(|run| !run.claimed()) {
            if let Some(run) = inner.runs.shift_remove(id) {
                mark_gone(&run);
            }
        }
    }

    /// Tell the worker that claimed `id` to adopt messages queued onto it;
    /// `announce` names those it should also announce with `queued_message`.
    pub fn adopt_queued(&self, id: &str, announce: &[&str]) {
        self.control(&json!({"type": "adopt_queued", "task_id": id, "announce": announce}));
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

    /// End pending runs whose job finished without any worker reporting it —
    /// the handler returned before registering (its automation was deleted,
    /// say), or the job failed first. Their subscribers fall back to the DB.
    pub async fn sweep_pending(&self, pool: &SqlitePool) {
        let pending: Vec<Arc<Run>> = self.all().into_iter().filter(|run| !run.claimed()).collect();
        for run in pending {
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
            if inner.runs.get(&run.id).is_some_and(|r| Arc::ptr_eq(r, &run) && !run.claimed()) {
                inner.runs.shift_remove(&run.id);
                drop(inner);
                tracing::info!("run {} ended unclaimed (job {})", run.id, status.as_deref().unwrap_or("missing"));
                mark_gone(&run);
            }
        }
    }

    // ── worker link ─────────────────────────────────────────────────────────

    /// A worker connected. Returns its session number, which every later call
    /// for this connection carries so a stale connection can't clobber a new
    /// one. A different `instance` than last time means a different process:
    /// none of the runs it had can still be live. Pending runs were never any
    /// worker's, and stay.
    pub fn attach(&self, instance: String, control: ControlTx) -> u64 {
        let session = self.sessions.fetch_add(1, Ordering::SeqCst) + 1;
        let mut inner = self.lock();
        let same_process = inner.last_instance.as_deref() == Some(instance.as_str());
        inner.last_instance = Some(instance);
        inner.link = Some(Link { control, session });
        if !same_process {
            inner.runs.retain(|_, run| {
                let keep = !run.claimed();
                if !keep {
                    mark_gone(run);
                }
                keep
            });
        }
        session
    }

    pub fn detach(&self, session: u64) {
        let mut inner = self.lock();
        if inner.link.as_ref().is_some_and(|l| l.session == session) {
            inner.link = None;
        }
        // Dropping the senders fails the waiting calls.
        inner.calls.retain(|_, (s, _)| *s != session);
    }

    fn current(&self, inner: &Inner, session: u64) -> bool {
        inner.link.as_ref().is_some_and(|l| l.session == session)
    }

    /// The full set of the worker's live runs. Claimed runs it doesn't carry
    /// are gone; runs it does are reconciled (`register`).
    pub fn snapshot(&self, session: u64, runs: Vec<Reported>) {
        let ids: std::collections::HashSet<_> = runs.iter().map(|r| r.task_id.clone()).collect();
        {
            let mut inner = self.lock();
            if !self.current(&inner, session) {
                return;
            }
            inner.runs.retain(|id, run| {
                let keep = ids.contains(id) || !run.claimed();
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
    /// history extends what the worker sent before, that's the same run —
    /// extend in place so attached subscribers keep their cursor. A pending
    /// run is claimed: the worker's history follows the edge's events, and the
    /// edge's metadata (what the trigger registered) stands. Otherwise it's a
    /// new run under an old id: the old one is gone.
    pub fn register(&self, session: u64, r: Reported) {
        let mut inner = self.lock();
        if !self.current(&inner, session) {
            return;
        }
        if let Some(existing) = inner.runs.get(&r.task_id).cloned() {
            let mut reported = Some((r.events, r.fields));
            let (kept, adopt, cancel) = existing.update(|st| {
                let Some((events, mut fields)) = reported.take() else { unreachable!() };
                match st.worker_base {
                    None => {
                        st.worker_base = Some(st.events.len());
                        st.events.extend(events);
                        // Stopped while pending: the job carries the stop, so
                        // the worker normally starts it cancelled. If the stop
                        // landed after the claim read the job, pass it on.
                        let cancel = st.fields.cancelled && !fields.cancelled;
                        fields.cancelled |= st.fields.cancelled;
                        st.fields = fields;
                        (true, std::mem::take(&mut st.queued_while_pending), cancel)
                    }
                    Some(base) => {
                        let have = &st.events[base..];
                        if events.len() >= have.len() && events[..have.len()] == *have {
                            let skip = have.len();
                            st.events.extend(events.into_iter().skip(skip));
                            st.fields = fields;
                            (true, false, false)
                        } else {
                            reported = Some((events, fields));
                            (false, false, false)
                        }
                    }
                }
            });
            if kept {
                drop(inner);
                if adopt {
                    self.adopt_queued(&existing.id, &[]);
                }
                if cancel {
                    self.control(&json!({"type": "cancel", "task_id": existing.id, "resume": true}));
                }
                return;
            }
            mark_gone(&existing);
            let (events, fields) = reported.expect("returned when not kept");
            let state = RunState { events, fields, worker_base: Some(0), ..Default::default() };
            inner.runs.insert(r.task_id.clone(), Run::new(r.task_id, r.meta, state));
        } else {
            let state = RunState { events: r.events, fields: r.fields, worker_base: Some(0), ..Default::default() };
            // IndexMap::insert keeps an existing key's position, as a dict does.
            inner.runs.insert(r.task_id.clone(), Run::new(r.task_id, r.meta, state));
        }
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
            let len = st.events.len();
            let base = *st.worker_base.get_or_insert(len);
            let from = base + from;
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

    /// The worker answered a `call`.
    pub fn reply(&self, session: u64, id: u64, reply: Reply) {
        let mut inner = self.lock();
        if let Some((s, tx)) = inner.calls.remove(&id) {
            if s == session {
                let _ = tx.send(reply);
            }
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

    fn meta(label: &str) -> Meta {
        Meta { kind: "chat".into(), label: label.into(), parent_id: Some("c".into()), started_at: "s".into() }
    }

    fn ev(n: i64) -> Value {
        json!({"event": "token", "data": format!("{{\"text\": \"{n}\"}}")})
    }

    fn events(run: &Run) -> Vec<Value> {
        run.state.lock().unwrap().events.clone()
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

    #[test]
    fn a_claim_appends_the_workers_history_after_the_edges() {
        let reg = Registry::default();
        let (tx, mut rx) = mpsc::unbounded_channel();
        let s = reg.attach("A".into(), tx);
        let run = reg.pre_register("t", meta("edge's label"));
        assert!(run.queue_local("q1", "also", 1));
        reg.register(s, reported("t", vec![ev(1)]));
        // The same run, its subscribers' cursors intact, the trigger's label kept.
        assert!(Arc::ptr_eq(&run, &reg.get("t").unwrap()));
        assert_eq!(run.meta.label, "edge's label");
        assert_eq!(events(&run).len(), 2);
        // The worker counts from its own event 0.
        reg.events(s, "t", 1, vec![ev(2)]);
        reg.events(s, "t", 0, vec![ev(1), ev(2)]);
        assert_eq!(events(&run)[1..], [ev(1), ev(2)]);
        // A claimed run is the worker's to write; the queued message is its to adopt.
        assert!(!run.emit_local("token", &json!({})));
        let msg: Value = serde_json::from_str(&rx.try_recv().unwrap()).unwrap();
        assert_eq!(msg, json!({"type": "adopt_queued", "task_id": "t", "announce": []}));
    }

    #[test]
    fn pending_runs_outlive_a_new_worker_process() {
        let reg = Registry::default();
        let (tx, _rx) = mpsc::unbounded_channel();
        let s1 = reg.attach("A".into(), tx.clone());
        reg.pre_register("p", meta("l"));
        reg.snapshot(s1, vec![]);
        assert!(reg.get("p").is_some());
        reg.attach("B".into(), tx);
        assert!(!reg.get("p").unwrap().state.lock().unwrap().gone);
    }

    #[test]
    fn local_events_are_python_shaped() {
        let reg = Registry::default();
        let run = reg.pre_register("p", meta("l"));
        assert!(run.emit_local("queued_withdrawn", &json!({"message_id": "é"})));
        assert_eq!(events(&run), [json!({"event": "queued_withdrawn", "data": "{\"message_id\": \"\\u00e9\"}"})]);
    }

    #[tokio::test]
    async fn a_call_is_answered_by_its_reply() {
        let reg = Arc::new(Registry::default());
        let (tx, mut rx) = mpsc::unbounded_channel();
        let s = reg.attach("A".into(), tx);
        let call = tokio::spawn({
            let reg = reg.clone();
            async move { reg.call("queue_message", json!({"task_id": "t"})).await }
        });
        let sent: Value = serde_json::from_str(&rx.recv().await.unwrap()).unwrap();
        assert_eq!(sent["method"], "queue_message");
        reg.reply(s, sent["id"].as_u64().unwrap(), Err("task not found".into()));
        assert_eq!(call.await.unwrap(), Err("task not found".into()));
        // A dropped link fails the calls still waiting on it.
        let call = tokio::spawn({
            let reg = reg.clone();
            async move { reg.call("queue_message", json!({})).await }
        });
        rx.recv().await.unwrap();
        reg.detach(s);
        assert!(call.await.unwrap().is_err());
    }
}
