//! The Python worker, started when there is work for it and stopped once it
//! has sat idle — so an idle jarvis is this process alone.
//!
//! Opt-in: `JARVIS_WORKER_CMD` names the command that runs Python. Without it
//! Python is someone else's to run (the dev setup, an older `serve.sh`) and
//! everything here is a no-op.
//!
//! **What starts Python**: a request the edge has to proxy (REST, the
//! GraphQL it hasn't ported, the other WebSockets); a voice note a chat bot
//! needs transcribed (`bots/`); a job it can claim, or one left `running` by a
//! worker that died; and the edge's own start, so the startup sweeps run and a
//! broken command shows up at once. Runs the edge
//! starts itself (`startTask` and the other triggers) need nothing more: they
//! write a job, `Registry::wake` kicks this loop, and the run stays pending in
//! the mirror until the new worker claims it.
//!
//! **What stops it**: `JARVIS_WORKER_IDLE` seconds (default 300) with no
//! proxied request or socket open, no run in the mirror, no claimable or
//! running job, and no `holds` — the reasons the worker reports itself (a
//! kernel still holding a conversation's variables). Before it
//! signals, the edge `call`s `drain`, so the worker claims nothing while the
//! job table is checked one last time; work that slipped in means `undrain`
//! instead. The process then gets SIGTERM — uvicorn's graceful shutdown, which
//! closes kernels and MCP servers — and SIGKILL if that hangs.
//!
//! A start that fails, or a worker that dies soon after starting, backs off
//! exponentially (to a minute); requests that arrive meanwhile get a 503 that
//! says why, rather than waiting on a command that cannot work.

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use sqlx::SqlitePool;
use tokio::process::{Child, Command};
use tokio::sync::{Notify, watch};

use crate::config::WorkerConfig;
use crate::gql::codec::now_stored;
use crate::runs::Registry;

/// A cold Python start, imports and all, on slow hardware.
const START_TIMEOUT: Duration = Duration::from_secs(120);
/// SIGTERM to SIGKILL.
const STOP_TIMEOUT: Duration = Duration::from_secs(30);
/// How often a stopped worker's job table is looked at, besides the kicks.
const POLL: Duration = Duration::from_secs(5);
/// A worker that dies sooner than this after starting counts as a failed start.
const STABLE_AFTER: Duration = Duration::from_secs(60);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Down,
    Starting,
    Up,
    /// Asked to stop claiming; deciding whether to stop. Requests wait.
    Draining,
    Stopping,
}

struct Inner {
    phase: Phase,
    pid: Option<u32>,
    /// When the current worker became ready.
    up_since: Option<Instant>,
    /// Proxied requests and sockets in progress.
    in_flight: usize,
    /// Requests waiting for a worker to come up.
    waiting: usize,
    /// The last moment the worker was seen doing anything.
    last_active: Instant,
    holds: Vec<String>,
    /// Consecutive failed starts, and when the next may be tried.
    failures: u32,
    retry_at: Option<Instant>,
    /// Bumped on every failed start, so waiters on that start can give up.
    failed_starts: u64,
    last_error: String,
    /// The last worker exited because this edge stopped it for being idle.
    clean_stop: bool,
    shutting_down: bool,
    booted: bool,
}

pub struct Supervisor {
    config: Option<WorkerConfig>,
    backend: String,
    backend_port: String,
    pool: SqlitePool,
    runs: Arc<Registry>,
    http: reqwest::Client,
    inner: Mutex<Inner>,
    /// Bumped on every phase change.
    changed: watch::Sender<u64>,
    /// Something may want a worker now.
    kick: Notify,
}

/// Held while a proxied request or socket is in progress; the worker isn't
/// stopped while any is alive.
pub struct Activity(Option<Arc<Supervisor>>);

impl Drop for Activity {
    fn drop(&mut self) {
        if let Some(s) = self.0.take() {
            let mut inner = s.lock();
            inner.in_flight -= 1;
            inner.last_active = Instant::now();
        }
    }
}

impl Supervisor {
    pub fn new(
        config: Option<WorkerConfig>,
        backend: String,
        backend_port: String,
        pool: SqlitePool,
        runs: Arc<Registry>,
        http: reqwest::Client,
    ) -> Arc<Self> {
        let now = Instant::now();
        Arc::new(Self {
            config,
            backend,
            backend_port,
            pool,
            runs,
            http,
            inner: Mutex::new(Inner {
                phase: Phase::Down,
                pid: None,
                up_since: None,
                in_flight: 0,
                waiting: 0,
                last_active: now,
                holds: vec![],
                failures: 0,
                retry_at: None,
                failed_starts: 0,
                last_error: String::new(),
                clean_stop: false,
                shutting_down: false,
                booted: false,
            }),
            changed: watch::channel(0).0,
            kick: Notify::new(),
        })
    }

    /// Whether this edge owns the worker. Then the run mirror is the truth
    /// even with no worker linked — no worker means no live run — and the
    /// edge serves everything that reads or starts one.
    pub fn supervised(&self) -> bool {
        self.config.is_some()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().expect("supervisor lock")
    }

    fn set_phase(&self, inner: &mut Inner, phase: Phase) {
        if inner.phase != phase {
            tracing::debug!("worker {:?} → {phase:?}", inner.phase);
            inner.phase = phase;
            self.changed.send_modify(|v| *v += 1);
        }
    }

    pub fn phase(&self) -> Phase {
        self.lock().phase
    }

    /// Something may need a worker: a job was just written.
    pub fn kick(&self) {
        self.kick.notify_one();
    }

    /// The worker's own reasons to stay up (`core/edge_link.py:current_holds`).
    pub fn set_holds(&self, holds: Vec<String>) {
        let mut inner = self.lock();
        if holds != inner.holds {
            tracing::info!("worker holds: {}", if holds.is_empty() { "none".into() } else { holds.join(", ") });
        }
        inner.holds = holds;
        inner.last_active = Instant::now();
    }

    /// A worker, up and serving, for one request. Starts one if need be and
    /// waits for it; the returned guard keeps it up until dropped. Without
    /// supervision this is a no-op: Python is assumed to be there.
    pub async fn ensure_up(self: &Arc<Self>) -> Result<Activity, String> {
        if !self.supervised() {
            return Ok(Activity(None));
        }
        let mut changes = self.changed.subscribe();
        let failed_before = {
            let mut inner = self.lock();
            if inner.phase == Phase::Up {
                inner.in_flight += 1;
                inner.last_active = Instant::now();
                return Ok(Activity(Some(self.clone())));
            }
            if inner.phase == Phase::Down && inner.retry_at.is_some_and(|t| t > Instant::now()) {
                return Err(format!("the jarvis worker failed to start ({}); retrying shortly", inner.last_error));
            }
            inner.waiting += 1;
            inner.failed_starts
        };
        self.kick.notify_one();
        let deadline = tokio::time::Instant::now() + START_TIMEOUT + STOP_TIMEOUT;
        let out = loop {
            {
                let mut inner = self.lock();
                if inner.phase == Phase::Up {
                    inner.in_flight += 1;
                    inner.last_active = Instant::now();
                    break Ok(Activity(Some(self.clone())));
                }
                if inner.failed_starts != failed_before {
                    break Err(format!("the jarvis worker failed to start ({})", inner.last_error));
                }
                if inner.shutting_down {
                    break Err("the server is shutting down".into());
                }
            }
            match tokio::time::timeout_at(deadline, changes.changed()).await {
                Ok(Ok(())) => {}
                _ => break Err("timed out waiting for the jarvis worker".into()),
            }
        };
        self.lock().waiting -= 1;
        out
    }

    /// Owns the worker process for the life of the edge.
    pub async fn run(self: Arc<Self>) {
        let Some(config) = &self.config else { return };
        tracing::info!(
            "supervising the worker: `{}` in {}, idle stop {}",
            config.command,
            config.dir.display(),
            config.idle.map_or("off".into(), |d| format!("after {}s", d.as_secs())),
        );
        let mut child: Option<Child> = None;
        // Start at once, not a poll in: nothing to wait for on the first pass.
        self.kick.notify_one();
        loop {
            let exited = async {
                match child.as_mut() {
                    Some(c) => c.wait().await.ok(),
                    None => std::future::pending().await,
                }
            };
            tokio::select! {
                status = exited => {
                    child = None;
                    self.exited(status);
                }
                () = self.kick.notified() => {}
                () = self.runs.work.notified() => {}
                () = tokio::time::sleep(POLL) => {}
            }
            if self.lock().shutting_down {
                if child.is_some() {
                    continue; // the exit above ends it
                }
                return;
            }
            match self.phase() {
                Phase::Down => {
                    if self.wanted().await {
                        child = self.spawn(config);
                    }
                }
                Phase::Up => self.maybe_stop().await,
                Phase::Starting | Phase::Draining | Phase::Stopping => {}
            }
        }
    }

    /// Whether a stopped worker should be started now.
    async fn wanted(&self) -> bool {
        {
            let mut inner = self.lock();
            if inner.retry_at.is_some_and(|t| t > Instant::now()) {
                return false;
            }
            inner.retry_at = None;
            let boot = !inner.booted;
            inner.booted = true;
            if boot || inner.waiting > 0 || self.config.as_ref().is_some_and(|c| c.idle.is_none()) {
                return true;
            }
        }
        !self.runs.all().is_empty() || self.jobs_waiting().await
    }

    /// A job a worker would claim now, or one a dead worker left running.
    async fn jobs_waiting(&self) -> bool {
        let found: sqlx::Result<Option<i64>> = sqlx::query_scalar(
            "SELECT 1 FROM jobs WHERE status = 'running' OR (status = 'pending' AND run_at <= ?) LIMIT 1",
        )
        .bind(now_stored())
        .fetch_optional(&self.pool)
        .await;
        match found {
            Ok(found) => found.is_some(),
            Err(e) => {
                tracing::warn!("supervisor: reading jobs: {e}");
                false
            }
        }
    }

    fn spawn(self: &Arc<Self>, config: &WorkerConfig) -> Option<Child> {
        let respawn = {
            let mut inner = self.lock();
            inner.last_error.clear();
            std::mem::take(&mut inner.clean_stop)
        };
        let mut cmd = Command::new("/bin/sh");
        cmd.arg("-c")
            .arg(&config.command)
            .current_dir(&config.dir)
            .env("JARVIS_EDGE_URL", &config.edge_url)
            .env("JARVIS_BACKEND_PORT", &self.backend_port)
            .env("JARVIS_EDGE_RESPAWN", if respawn { "1" } else { "0" })
            .stdin(std::process::Stdio::null())
            // Its own group, so a stop reaches whatever the command started
            // (`uv run` puts Python one process further down).
            .process_group(0)
            .kill_on_drop(true);
        let child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                self.start_failed(format!("could not run `{}`: {e}", config.command));
                return None;
            }
        };
        let pid = child.id();
        {
            let mut inner = self.lock();
            inner.pid = pid;
            self.set_phase(&mut inner, Phase::Starting);
        }
        tracing::info!("worker starting (pid {})", pid.unwrap_or_default());
        tokio::spawn(self.clone().await_ready(pid));
        Some(child)
    }

    /// Up once Python answers HTTP and its link has said hello.
    async fn await_ready(self: Arc<Self>, pid: Option<u32>) {
        let started = Instant::now();
        let health = format!("{}/health", self.backend);
        loop {
            if self.lock().pid != pid || self.phase() != Phase::Starting {
                return; // it exited meanwhile
            }
            let answers = self
                .http
                .get(&health)
                .timeout(Duration::from_secs(2))
                .send()
                .await
                .is_ok_and(|r| r.status().is_success());
            if answers && self.runs.link_up() {
                let mut inner = self.lock();
                if inner.pid != pid || inner.phase != Phase::Starting {
                    return;
                }
                inner.up_since = Some(Instant::now());
                inner.last_active = Instant::now();
                self.set_phase(&mut inner, Phase::Up);
                drop(inner);
                tracing::info!("worker up after {:.1}s", started.elapsed().as_secs_f32());
                self.kick.notify_one();
                return;
            }
            if started.elapsed() > START_TIMEOUT {
                tracing::error!("worker not ready after {}s; stopping it", START_TIMEOUT.as_secs());
                self.lock().last_error = format!("not ready after {}s", START_TIMEOUT.as_secs());
                self.signal(libc::SIGKILL);
                return;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }

    fn exited(&self, status: Option<std::process::ExitStatus>) {
        let (was, up_for) = {
            let mut inner = self.lock();
            inner.pid = None;
            (inner.phase, inner.up_since.take().map(|t| t.elapsed()))
        };
        let how = status.map_or("an unknown status".into(), |s| s.to_string());
        match was {
            Phase::Stopping => {
                tracing::info!("worker stopped ({how})");
                let mut inner = self.lock();
                // Stopped for being idle, so it left nothing behind — however
                // the exit reads (`sh` itself dies of the SIGTERM it relays).
                inner.clean_stop = true;
                self.set_phase(&mut inner, Phase::Down);
            }
            Phase::Starting => {
                let error = {
                    let inner = self.lock();
                    if inner.last_error.is_empty() { format!("exited with {how}") } else { inner.last_error.clone() }
                };
                tracing::error!("worker failed to start: {error}");
                self.start_failed(error);
            }
            _ => {
                tracing::error!("worker exited unexpectedly ({how})");
                if up_for.is_some_and(|d| d < STABLE_AFTER) {
                    self.start_failed(format!("exited with {how} soon after starting"));
                } else {
                    let mut inner = self.lock();
                    inner.failures = 0;
                    self.set_phase(&mut inner, Phase::Down);
                }
            }
        }
    }

    fn start_failed(&self, error: String) {
        let mut inner = self.lock();
        inner.failures += 1;
        let backoff = Duration::from_secs((1u64 << inner.failures.min(6)).min(60));
        inner.retry_at = Some(Instant::now() + backoff);
        inner.failed_starts += 1;
        inner.last_error = error;
        self.set_phase(&mut inner, Phase::Down);
        tracing::warn!("next worker start in {}s at the earliest", backoff.as_secs());
    }

    /// Stop the worker if it has had nothing to do for the idle period.
    async fn maybe_stop(self: &Arc<Self>) {
        let Some(idle) = self.config.as_ref().and_then(|c| c.idle) else { return };
        {
            let mut inner = self.lock();
            if inner.up_since.is_some_and(|t| t.elapsed() >= STABLE_AFTER) {
                inner.failures = 0;
            }
        }
        if self.busy().await {
            self.lock().last_active = Instant::now();
            return;
        }
        {
            let mut inner = self.lock();
            if inner.phase != Phase::Up || inner.in_flight > 0 || inner.last_active.elapsed() < idle {
                return;
            }
            self.set_phase(&mut inner, Phase::Draining);
        }
        // Claims stop before the last look, so the look is final.
        let drained = self.runs.call("drain", serde_json::json!({})).await;
        let quiet = match &drained {
            Ok(v) => v["tasks"].as_u64() == Some(0) && !self.busy().await,
            Err(e) => {
                tracing::warn!("worker did not drain ({e}); keeping it up");
                false
            }
        };
        let stopping = {
            let mut inner = self.lock();
            let stopping = quiet && inner.in_flight == 0 && inner.waiting == 0 && !inner.shutting_down;
            if stopping {
                self.set_phase(&mut inner, Phase::Stopping);
            } else {
                inner.last_active = Instant::now();
                self.set_phase(&mut inner, Phase::Up);
            }
            stopping
        };
        if stopping {
            tracing::info!("worker idle for {}s; stopping it", idle.as_secs());
            self.stop();
        } else if drained.is_ok() {
            let _ = self.runs.call("undrain", serde_json::json!({})).await;
        }
    }

    /// Anything the worker is, or is about to be, doing.
    async fn busy(&self) -> bool {
        if !self.lock().holds.is_empty() || !self.runs.all().is_empty() {
            return true;
        }
        self.jobs_waiting().await
    }

    /// SIGTERM, then SIGKILL if it hasn't gone by `STOP_TIMEOUT`.
    fn stop(self: &Arc<Self>) {
        let Some(pid) = self.lock().pid else { return };
        self.signal(libc::SIGTERM);
        let me = self.clone();
        tokio::spawn(async move {
            tokio::time::sleep(STOP_TIMEOUT).await;
            if me.lock().pid == Some(pid) {
                tracing::warn!("worker ignored SIGTERM for {}s; killing it", STOP_TIMEOUT.as_secs());
                me.signal(libc::SIGKILL);
            }
        });
    }

    fn signal(&self, sig: libc::c_int) {
        if let Some(pid) = self.lock().pid {
            // The whole group: the command and everything it started.
            unsafe {
                libc::kill(-(pid as libc::pid_t), sig);
            }
        }
    }

    /// The edge is exiting: stop the worker, and wait for it.
    pub async fn shutdown(&self) {
        let mut changes = self.changed.subscribe();
        {
            let mut inner = self.lock();
            inner.shutting_down = true;
            if inner.pid.is_none() {
                return;
            }
            self.set_phase(&mut inner, Phase::Stopping);
        }
        tracing::info!("stopping the worker");
        self.signal(libc::SIGTERM);
        let gone = tokio::time::timeout(STOP_TIMEOUT, async {
            while self.lock().pid.is_some() {
                if changes.changed().await.is_err() {
                    break;
                }
            }
        })
        .await;
        if gone.is_err() {
            tracing::warn!("worker ignored SIGTERM for {}s; killing it", STOP_TIMEOUT.as_secs());
            self.signal(libc::SIGKILL);
        }
    }
}
