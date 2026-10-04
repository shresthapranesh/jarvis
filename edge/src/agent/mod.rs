//! The edge's agent runtime (phase 2d): chat turns run here instead of in
//! Python, so a conversation needs no Python process at all.
//!
//! A turn is routed when it is queued (`route.rs`): one the edge can serve
//! start to finish gets `jobs.runtime = 'edge'`, and its run is mirrored as
//! the edge's own (`runs.rs`), so the supervisor doesn't start Python for
//! it. This loop claims those jobs (`queue.rs`) under the same one-turn-per-
//! conversation lease Python's workers use, renews their locks, and runs
//! them. A turn that needs something only Python has is handed over: the job
//! goes back to pending with `runtime` cleared and what the turn carried in
//! its payload, and Python's chat handler continues it.
//!
//! Python never claims, reaps or sweeps an edge job. The edge recovers its
//! own at start (`queue::recover`), and Python running without the edge
//! adopts them (`db/ops.py:adopt_edge_jobs`).
//!
//! The turn itself (`turn.rs`) runs the model and the tools the edge has
//! (`tools.rs`) against the transcript tables (`thread.rs`), with the
//! prompt built as Python builds it (`prompt.rs`) and its events and step
//! rows as Python emits them (`events.rs`).

mod automation;
mod embed;
mod events;
mod prompt;
mod queue;
mod retrieve;
pub mod route;
mod summarize;
mod thread;
mod tools;
mod turn;

pub use queue::EDGE as EDGE_RUNTIME;

use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;
use sqlx::SqlitePool;
use tokio::sync::Semaphore;

use crate::gql::codec::iso_from_db;
use crate::kernels::Kernels;
use crate::runs::{Meta, Registry, Run};
use queue::Job;

/// How often the job table is looked at, besides the wakes.
const POLL: Duration = Duration::from_secs(5);
/// Turns running here at once.
const MAX_RUNNING: usize = 8;

pub struct Agent {
    pool: SqlitePool,
    runs: Arc<Registry>,
    kernels: Arc<Kernels>,
    http: reqwest::Client,
    /// `locked_by` on the jobs this process claims.
    worker: String,
    slots: Arc<Semaphore>,
}

/// What a turn came to, for the job.
enum Outcome {
    /// Finished here — answered, stopped or failed; the run's rows say which.
    Finished,
    /// Python runs the rest: the job is released to it, carrying the turn so
    /// far (`None`: nothing ran here, Python starts it from the beginning).
    HandOver(Option<Value>),
}

impl Agent {
    pub fn new(pool: SqlitePool, runs: Arc<Registry>, kernels: Arc<Kernels>) -> Arc<Self> {
        Arc::new(Self {
            pool,
            runs,
            kernels,
            // Model calls stream for as long as the reply takes; no overall timeout.
            http: reqwest::Client::new(),
            worker: format!("edge-{}", std::process::id()),
            slots: Arc::new(Semaphore::new(MAX_RUNNING)),
        })
    }

    /// Recover what a previous edge left, then claim and run edge jobs until
    /// the process ends. With the agent loop off, only the recovery: every
    /// edge job goes to Python.
    pub async fn run(self: Arc<Self>) {
        let serving = route::enabled();
        match queue::recover(&self.pool, serving).await {
            Ok(0) => {}
            Ok(n) if serving => tracing::info!("agent: {n} job(s) a previous edge was running are pending again"),
            Ok(n) => {
                tracing::info!("agent: handed {n} edge job(s) to Python (JARVIS_AGENT_RUNTIME is not edge)");
                self.runs.wake();
            }
            Err(e) => tracing::warn!("agent: recovering jobs: {e}"),
        }
        if !serving {
            return;
        }
        loop {
            // A slot before the claim: a claimed job's lock is ticking.
            let Ok(slot) = self.slots.clone().acquire_owned().await else { return };
            match queue::claim(&self.pool, &["chat", "automation"], &self.worker).await {
                Ok(Some(job)) => {
                    let me = self.clone();
                    tokio::spawn(async move {
                        me.process(job).await;
                        drop(slot);
                    });
                    continue;
                }
                Ok(None) => {}
                Err(e) => tracing::warn!("agent: claiming: {e}"),
            }
            drop(slot);
            tokio::select! {
                () = self.runs.agent_work.notified() => {}
                () = tokio::time::sleep(POLL) => {}
            }
        }
    }

    /// One claimed job, start to finish, its lock renewed meanwhile.
    async fn process(self: &Arc<Self>, job: Job) {
        let meta = self.meta(&job).await;
        let Some(run) = self.runs.take(&job.id, || meta) else {
            // A worker has the run under this id — not ours to touch.
            tracing::warn!("agent: job {} is a worker's run; handing it back", job.id);
            self.hand_over(&job, None, None).await;
            return;
        };
        let outcome = tokio::select! {
            outcome = self.serve(&job, &run) => outcome,
            () = self.keep_lock(&job.id) => {
                tracing::warn!("agent: lost the lock on job {}; abandoning it", job.id);
                return;
            }
        };
        match outcome {
            Outcome::Finished => match queue::complete(&self.pool, &job.id, &self.worker).await {
                // The conversation's next turn may be waiting on its lease.
                Ok(_) => self.runs.wake(),
                Err(e) => tracing::error!("agent: completing job {}: {e}", job.id),
            },
            Outcome::HandOver(carried) => self.hand_over(&job, Some(&run), carried).await,
        }
    }

    async fn serve(&self, job: &Job, run: &Arc<Run>) -> Outcome {
        tracing::info!("agent: {} run {} claimed", job.kind, job.id);
        if job.kind == "automation" {
            return self.serve_automation(job, run).await;
        }
        match turn::Turn::chat(self, job, run.clone()) {
            Some(turn) => turn.run().await,
            None => {
                tracing::warn!("agent: job {} has no chat payload; handing it to Python", job.id);
                Outcome::HandOver(None)
            }
        }
    }

    /// `automation_job_handler` up to the agent: the run's row, then a
    /// stateful run that would overlap a sibling is skipped.
    async fn serve_automation(&self, job: &Job, run: &Arc<Run>) -> Outcome {
        let spec = match automation::prepare(&self.pool, job).await {
            Ok(automation::Prepared::Ready(spec)) => spec,
            Ok(automation::Prepared::Gone) => {
                tracing::warn!("agent: automation run {} has no automation; dropping it", job.id);
                self.end(run, "error");
                return Outcome::Finished;
            }
            Ok(automation::Prepared::Python) => return Outcome::HandOver(None),
            Err(e) => {
                tracing::warn!("agent: preparing automation run {}: {e}; handing it to Python", job.id);
                return Outcome::HandOver(None);
            }
        };
        if !route::serves_model(&self.pool, &spec.model).await {
            return Outcome::HandOver(None);
        }
        if spec.stateful && automation::sibling_running(&self.pool, &spec).await.unwrap_or(false) {
            let skip = "skipped: a previous run of this stateful automation is still in flight";
            automation::finish_run(&self.pool, &spec.run_id, "skipped", None, Some(skip)).await;
            run.emit_local("error", &serde_json::json!({"error": skip, "run_id": spec.run_id}));
            self.end(run, "skipped");
            return Outcome::Finished;
        }
        turn::Turn::automation(self, job, run.clone(), spec).run().await
    }

    /// `finish_task_state` for a run that ended before its turn began.
    fn end(&self, run: &Arc<Run>, status: &str) {
        tracing::info!("task complete: kind={} task={} status={status}", run.meta.kind, run.id);
        run.update(|st| st.fields.done = true);
        self.runs.retire(&run.id);
    }

    /// Release the job to Python and wake it.
    async fn hand_over(&self, job: &Job, run: Option<&Arc<Run>>, carried: Option<Value>) {
        // Pending in the mirror first, so the worker's claim continues the run.
        if let Some(run) = run {
            self.runs.release(run);
        }
        match queue::release(&self.pool, job, &self.worker, carried).await {
            Ok(true) => self.runs.wake(),
            Ok(false) => tracing::warn!("agent: job {} was no longer ours to hand over", job.id),
            Err(e) => tracing::error!("agent: handing job {} to Python: {e}", job.id),
        }
    }

    /// Renew the job's lock at a third of its TTL, as the Python worker's
    /// heartbeat does. Returns only when the lock is lost.
    async fn keep_lock(&self, id: &str) {
        let every = queue::LOCK_TTL / 3;
        loop {
            tokio::time::sleep(every).await;
            match queue::extend_lock(&self.pool, id, &self.worker).await {
                Ok(true) => {}
                Ok(false) => return,
                // A busy database isn't a lost lock; the next beat retries.
                Err(e) => tracing::warn!("agent: renewing the lock on {id}: {e}"),
            }
        }
    }

    /// The run as its trigger mirrored it, for one the mirror lost: the
    /// edge restarted between the trigger and this claim.
    /// A scheduled automation's run is first mirrored here: the schedule
    /// enqueues it without one, as Python's handler registers it on claim.
    async fn meta(&self, job: &Job) -> Meta {
        let (label, parent_id) = if job.kind == "automation" {
            let id = job.payload["automation_id"].as_str().unwrap_or_default().to_string();
            let name: Option<String> = sqlx::query_scalar("SELECT name FROM automations WHERE id = ?")
                .bind(&id)
                .fetch_optional(&self.pool)
                .await
                .ok()
                .flatten();
            (name.unwrap_or_default(), Some(id))
        } else {
            let query = job.payload["query"].as_str().unwrap_or_default();
            (query.chars().take(60).collect(), job.payload["conv_id"].as_str().map(str::to_string))
        };
        Meta { kind: job.kind.clone(), label, parent_id, started_at: iso_from_db(&job.created_at).utc().0 }
    }
}
