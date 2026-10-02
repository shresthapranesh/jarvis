//! Every timer the Python server ran (`core/scheduler.py`), so that between
//! jobs there is nothing for Python to do.
//!
//! | timer | when | does |
//! |---|---|---|
//! | each enabled automation | its cron schedule | enqueues an `automation` job |
//! | board dispatch | every 15 s, and on request | `dispatch_board_tasks`, here |
//! | memory consolidation | `0 */6 * * *` | enqueues a `maintenance` job |
//! | project memory | every 30 min | enqueues a `maintenance` job |
//! | checkpoint prune | `20 * * * *` | enqueues a `maintenance` job |
//! | staging cleanup | `0 * * * *` | deletes abandoned uploads, here |
//! | memory-activity prune | `0 4 * * *` | deletes old access-log rows, here |
//!
//! Only the idle-kernel reaper stays in Python: kernels are its children.
//! Python behind the edge registers none of these (`core/edge_link.py:
//! behind_edge`), and asks the edge instead — `dispatch` after a board
//! change, `schedules` after an automation's schedule changed.
//!
//! Firing follows APScheduler's rules for the jobs it replaces: times are
//! `cron.rs`'s, a run that is late by more than the job's grace period is
//! skipped, several missed runs coalesce into one, and nothing missed while
//! the edge was down is caught up on.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use serde_json::json;
use sqlx::SqlitePool;
use tokio::sync::{Mutex, Notify};

use crate::checkpoints::Checkpoints;
use crate::cron::{Trigger, Wall};
use crate::gql::codec::{iso_from_db, new_id, now_stored};
use crate::runs::{Meta, Registry};

/// How many board tasks may run at once — `task_board_runtime.MAX_IN_PROGRESS`.
const MAX_IN_PROGRESS: i64 = 3;
/// Re-read the automations this often even unprompted, for writes that
/// didn't come through Python (the CLI, another process).
const RELOAD_EVERY: Duration = Duration::from_secs(60);
/// Python reports a schedule change as it makes it, often just before the
/// commit; wait out the commit before re-reading.
const RELOAD_SETTLE: Duration = Duration::from_millis(300);

/// The zone cron expressions are read in — `get_scheduler_timezone`: the
/// `scheduler.timezone` setting, else `JARVIS_TIMEZONE`, else the machine's
/// zone (`TZ` first, as tzlocal does), else UTC. Read once at startup; Python
/// can't change it on a running scheduler either.
pub async fn resolve_tz(pool: &SqlitePool) -> Tz {
    let setting: Option<String> = sqlx::query_scalar("SELECT value FROM config_settings WHERE key = 'scheduler.timezone'")
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
    let parse = |source: &str, name: Option<String>| {
        let name = name.filter(|n| !n.is_empty())?;
        match name.trim_start_matches(':').parse::<Tz>() {
            Ok(tz) => Some(tz),
            Err(_) => {
                tracing::warn!("invalid timezone {name:?} ({source}) — ignoring it");
                None
            }
        }
    };
    parse("scheduler.timezone", setting)
        .or_else(|| parse("JARVIS_TIMEZONE", std::env::var("JARVIS_TIMEZONE").ok()))
        .or_else(|| parse("TZ", std::env::var("TZ").ok()))
        .or_else(|| parse("the system zone", iana_time_zone::get_timezone().ok()))
        .unwrap_or(Tz::UTC)
}

/// `Automation.nextRunAt`: when an enabled, scheduled automation fires next,
/// as `isoformat()` in the scheduler's zone. None for anything that won't.
pub fn next_run_at(schedule: Option<&str>, enabled: bool, tz: Tz) -> Option<String> {
    let schedule = schedule.filter(|s| !s.is_empty() && enabled)?;
    let trigger = Trigger::parse(schedule, tz).ok()?;
    trigger.next_fire(None, Utc::now()).map(|w| w.isoformat())
}

#[derive(Clone, Debug, PartialEq)]
enum Action {
    Automation { id: String, schedule: String },
    Dispatch,
    /// A sweep Python runs: `core/scheduler.py:MAINTENANCE_TASKS`.
    Maintenance(&'static str),
    StagingCleanup,
    ActivityPrune,
}

enum When {
    Cron(Trigger),
    Every(chrono::Duration),
}

struct Entry {
    action: Action,
    when: When,
    /// APScheduler's `misfire_grace_time`.
    grace: chrono::Duration,
    next: Option<Wall>,
}

impl Entry {
    fn new(action: Action, when: When, grace_secs: i64, tz: Tz, now: DateTime<Utc>) -> Self {
        let next = match &when {
            When::Cron(trigger) => trigger.next_fire(None, now),
            // IntervalTrigger: first fire one interval after it was added.
            When::Every(every) => Some(Wall::from_utc(now + *every, tz)),
        };
        Entry { action, when, grace: chrono::Duration::seconds(grace_secs), next }
    }

    /// If due, move past `now` and say whether to run: the latest due time,
    /// once (coalesced), if it isn't later than the grace period allows.
    fn advance(&mut self, now: DateTime<Utc>) -> bool {
        let Some(due) = self.next else { return false };
        if due.to_utc() > now {
            return false;
        }
        let mut last = due;
        loop {
            let following = match &self.when {
                When::Cron(trigger) => trigger.next_fire(Some(&last), now),
                When::Every(every) => Some(Wall::from_utc(last.to_utc() + *every, last.tz)),
            };
            match following {
                Some(f) if f.to_utc() <= now => last = f,
                other => {
                    self.next = other;
                    break;
                }
            }
        }
        let late = now - last.to_utc();
        if late > self.grace {
            tracing::warn!("{:?}: run due {} missed by {}s; skipped", self.action, last.isoformat(), late.num_seconds());
            return false;
        }
        true
    }
}

pub struct Scheduler {
    pool: SqlitePool,
    runs: Arc<Registry>,
    tz: Tz,
    staging_dir: PathBuf,
    /// The memory jobs' watermarks, and the checkpoints the prune would take.
    checkpoints: Checkpoints,
    /// Python changed an automation's schedule.
    changed: Notify,
    /// One dispatch pass at a time: a tick and a requested pass must not
    /// both claim the same card.
    dispatching: Mutex<()>,
}

impl Scheduler {
    pub fn new(pool: SqlitePool, runs: Arc<Registry>, tz: Tz, staging_dir: PathBuf, checkpoints: Checkpoints) -> Arc<Self> {
        Arc::new(Self {
            pool,
            runs,
            tz,
            staging_dir,
            checkpoints,
            changed: Notify::new(),
            dispatching: Mutex::new(()),
        })
    }

    /// Re-read the automations' schedules soon.
    pub fn schedules_changed(&self) {
        self.changed.notify_one();
    }

    fn system_entries(&self, now: DateTime<Utc>) -> Vec<Entry> {
        let cron = |expr: &str| When::Cron(Trigger::parse(expr, self.tz).expect("a valid built-in schedule"));
        let minutes = |m: i64| When::Every(chrono::Duration::minutes(m));
        vec![
            Entry::new(Action::Dispatch, When::Every(chrono::Duration::seconds(15)), 30, self.tz, now),
            Entry::new(Action::Maintenance("memory_consolidation"), cron("0 */6 * * *"), 300, self.tz, now),
            Entry::new(Action::Maintenance("project_memory"), minutes(30), 300, self.tz, now),
            Entry::new(Action::Maintenance("checkpoint_prune"), cron("20 * * * *"), 300, self.tz, now),
            Entry::new(Action::StagingCleanup, cron("0 * * * *"), 300, self.tz, now),
            Entry::new(Action::ActivityPrune, cron("0 4 * * *"), 300, self.tz, now),
        ]
    }

    /// The enabled, scheduled automations, as `list_enabled_scheduled_automations`
    /// reads them. An unchanged schedule keeps its next fire time; a new or
    /// changed one starts from now, as APScheduler's `replace_existing` does.
    async fn reload(&self, automations: &mut HashMap<String, Entry>, broken: &mut HashMap<String, String>) {
        let rows: Vec<(String, String)> = match sqlx::query_as(
            "SELECT id, schedule FROM automations WHERE enabled = 1 AND schedule IS NOT NULL",
        )
        .fetch_all(&self.pool)
        .await
        {
            Ok(rows) => rows,
            Err(e) => {
                tracing::warn!("reading automation schedules: {e}");
                return;
            }
        };
        let now = Utc::now();
        let mut next = HashMap::new();
        for (id, schedule) in rows {
            if let Some(entry) = automations.remove(&id) {
                if matches!(&entry.action, Action::Automation { schedule: s, .. } if *s == schedule) {
                    next.insert(id, entry);
                    continue;
                }
            }
            match Trigger::parse(&schedule, self.tz) {
                Ok(trigger) => {
                    let action = Action::Automation { id: id.clone(), schedule };
                    next.insert(id, Entry::new(action, When::Cron(trigger), 60, self.tz, now));
                }
                // Warned once per schedule, not on every re-read.
                Err(e) if broken.get(&id) != Some(&schedule) => {
                    tracing::warn!("failed to register automation {id} (schedule={schedule:?}): {e}");
                    broken.insert(id, schedule);
                }
                Err(_) => {}
            }
        }
        *automations = next;
    }

    pub async fn run(self: Arc<Self>) {
        tracing::info!("scheduler timezone: {}", self.tz);
        let mut system = self.system_entries(Utc::now());
        let (mut automations, mut broken) = (HashMap::new(), HashMap::new());
        self.reload(&mut automations, &mut broken).await;
        let mut reloaded = tokio::time::Instant::now();
        loop {
            let now = Utc::now();
            let mut due = vec![];
            for entry in system.iter_mut().chain(automations.values_mut()) {
                if entry.advance(now) {
                    due.push(entry.action.clone());
                }
            }
            for action in due {
                self.fire(action).await;
            }

            // Sleep to the next fire time, but wake at least once a minute:
            // a sleep is measured on the monotonic clock, and the wall clock
            // can jump (a laptop waking, an NTP step).
            let next = system.iter().chain(automations.values()).filter_map(|e| e.next.map(|w| w.to_utc())).min();
            let wait = next
                .and_then(|n| (n - Utc::now()).to_std().ok())
                .unwrap_or(Duration::ZERO)
                .min(RELOAD_EVERY);
            tokio::select! {
                () = tokio::time::sleep(wait) => {}
                () = self.changed.notified() => {
                    tokio::time::sleep(RELOAD_SETTLE).await;
                    self.reload(&mut automations, &mut broken).await;
                    reloaded = tokio::time::Instant::now();
                }
            }
            if reloaded.elapsed() >= RELOAD_EVERY {
                self.reload(&mut automations, &mut broken).await;
                reloaded = tokio::time::Instant::now();
            }
        }
    }

    async fn fire(&self, action: Action) {
        let result = match &action {
            Action::Automation { id, schedule } => self.fire_automation(id, schedule).await,
            Action::Dispatch => self.dispatch().await.map(|_| ()),
            Action::Maintenance(task) => self.fire_maintenance(task).await,
            Action::StagingCleanup => {
                self.cleanup_staging();
                Ok(())
            }
            Action::ActivityPrune => self.prune_activities().await,
        };
        if let Err(e) = result {
            tracing::error!("{action:?} failed: {e}");
        }
    }

    /// `_run_scheduled_automation`: enqueue a run. Checked against the row
    /// first — a schedule removed a moment ago must not fire on the strength
    /// of a copy read before the change.
    async fn fire_automation(&self, id: &str, schedule: &str) -> sqlx::Result<()> {
        let row: Option<(bool, Option<String>)> =
            sqlx::query_as("SELECT enabled, schedule FROM automations WHERE id = ?").bind(id).fetch_optional(&self.pool).await?;
        if !matches!(&row, Some((true, Some(s))) if s == schedule) {
            self.changed.notify_one();
            return Ok(());
        }
        let job_id = new_id();
        crate::jobs::insert(&self.pool, &job_id, "automation", &json!({"automation_id": id, "triggered_by": "schedule"}))
            .await?;
        tracing::info!("automation {id} scheduled run enqueued (job {job_id})");
        self.runs.wake();
        Ok(())
    }

    /// A maintenance tick: enqueue the sweep only with something for it to
    /// do (`maintenance_due`). A job starts Python, and on an idle box most
    /// ticks would start it just to find nothing new.
    async fn fire_maintenance(&self, task: &str) -> sqlx::Result<()> {
        match self.maintenance_due(task).await {
            Ok(false) => {
                tracing::debug!("maintenance {task}: nothing to do");
                return Ok(());
            }
            Ok(true) => {}
            // Python's own checks decide, as they did before this one.
            Err(e) => tracing::warn!("maintenance {task}: could not tell whether it is due ({e}); enqueuing"),
        }
        self.enqueue_maintenance(task).await
    }

    /// One at a time per task: a box that was off for a day shouldn't come
    /// back to four queued consolidation passes.
    async fn enqueue_maintenance(&self, task: &str) -> sqlx::Result<()> {
        let payload = json!({"task": task});
        let waiting: Option<String> = sqlx::query_scalar(
            "SELECT id FROM jobs WHERE kind = 'maintenance' AND status IN ('pending', 'running') AND payload = ? LIMIT 1",
        )
        .bind(crate::pyjson::dumps(&payload))
        .fetch_optional(&self.pool)
        .await?;
        if waiting.is_some() {
            tracing::debug!("maintenance {task}: one is already queued");
            return Ok(());
        }
        crate::jobs::insert(&self.pool, &new_id(), "maintenance", &payload).await?;
        self.runs.wake();
        Ok(())
    }

    /// Whether a maintenance sweep would find work — the checks each one
    /// makes before doing any, read from the same rows. A wrong "yes" costs a
    /// pointless start of Python; a wrong "no" would stall the sweep, so where
    /// in doubt this says yes and lets Python decide.
    pub async fn maintenance_due(&self, task: &str) -> sqlx::Result<bool> {
        match task {
            "memory_consolidation" => self.memory_due().await,
            "project_memory" => self.project_memory_due().await,
            // `checkpoint_retention`: KEEP_PER_THREAD, MIN_AGE_SECONDS.
            "checkpoint_prune" => self.checkpoints.prunable(3, Duration::from_secs(3600)).await,
            _ => Ok(true),
        }
    }

    /// `consolidate_memory`: a message past the watermark, and the first of
    /// them not a reply still being written (`_transcript_block` stops there).
    async fn memory_due(&self) -> sqlx::Result<bool> {
        let meta = self.checkpoints.store_get("memory_consolidation", "state").await?;
        let raw = meta.as_ref().and_then(|m| {
            [m.get("messages_through"), m.get("last_run_at")].into_iter().flatten().find(|v| py_truthy(v)).cloned()
        });
        let since = match raw {
            None => None,
            Some(serde_json::Value::String(iso)) => match stored_from_iso(&iso) {
                Some(s) => Some(s),
                None => return Ok(true), // unreadable: Python's to judge
            },
            Some(_) => return Ok(true),
        };
        let first: Option<Option<String>> = sqlx::query_scalar(&format!(
            "SELECT m.status FROM messages m JOIN conversations c ON m.conversation_id = c.id \
             WHERE m.role IN ('user', 'assistant') AND c.ephemeral = 0{} ORDER BY m.created_at ASC LIMIT 1",
            if since.is_some() { " AND m.created_at > ?" } else { "" }
        ))
        .bind(since)
        .fetch_optional(&self.pool)
        .await?;
        Ok(first.is_some_and(|status| status.as_deref() != Some("running")))
    }

    /// `consolidate_project_memories`: a project with new messages that has
    /// gone quiet (or waited a day), with enough of them to be worth a call —
    /// `consolidate_project_memory`'s gates.
    async fn project_memory_due(&self) -> sqlx::Result<bool> {
        let projects: Vec<String> = sqlx::query_scalar("SELECT id FROM projects").fetch_all(&self.pool).await?;
        let now = Utc::now().naive_utc();
        for project_id in projects {
            let meta = self.checkpoints.store_get("project_memory_consolidation", &project_id).await?;
            // `_load_meta`: a value that won't parse counts as none.
            let since = meta
                .as_ref()
                .and_then(|m| m.get("messages_through"))
                .and_then(|v| v.as_str())
                .filter(|v| !v.is_empty())
                .and_then(stored_from_iso);
            let (count, oldest, newest, chars): (i64, Option<String>, Option<String>, i64) =
                sqlx::query_as(&format!(
                    "SELECT COUNT(m.id), MIN(m.created_at), MAX(m.created_at), COALESCE(SUM(LENGTH(m.content)), 0) \
                     FROM messages m JOIN conversations c ON m.conversation_id = c.id \
                     WHERE c.project_id = ? AND m.role IN ('user', 'assistant') AND c.ephemeral = 0{}",
                    if since.is_some() { " AND m.created_at > ?" } else { "" }
                ))
                .bind(&project_id)
                .bind(since)
                .fetch_one(&self.pool)
                .await?;
            let (Some(oldest), Some(newest)) = (oldest.as_deref().and_then(naive), newest.as_deref().and_then(naive))
            else {
                continue;
            };
            if count == 0 {
                continue;
            }
            let quiet_minutes = (now - newest).num_microseconds().unwrap_or(i64::MAX) as f64 / 60e6;
            let waiting_hours = (now - oldest).num_microseconds().unwrap_or(i64::MAX) as f64 / 3600e6;
            // _QUIET_MINUTES, _MAX_STALENESS_HOURS, _MIN_NEW_CHARS.
            if quiet_minutes < 15.0 && waiting_hours < 24.0 {
                continue;
            }
            if chars < 600 {
                continue;
            }
            return Ok(true);
        }
        Ok(false)
    }

    /// `_cleanup_staged_uploads`: uploads staged over an hour ago and never
    /// claimed by a `startTask`.
    fn cleanup_staging(&self) {
        let Ok(entries) = std::fs::read_dir(&self.staging_dir) else { return };
        let cutoff = std::time::SystemTime::now() - Duration::from_secs(3600);
        for entry in entries.flatten() {
            let stale = entry.metadata().and_then(|m| m.modified()).is_ok_and(|t| t < cutoff);
            if stale {
                if let Err(e) = std::fs::remove_file(entry.path()) {
                    tracing::warn!("staging cleanup: failed to unlink {}: {e}", entry.path().display());
                }
            }
        }
    }

    /// `prune_memory_activities(older_than_days=90)`.
    async fn prune_activities(&self) -> sqlx::Result<()> {
        let cutoff = (Utc::now() - chrono::Duration::days(90)).format("%Y-%m-%d %H:%M:%S%.6f").to_string();
        let pruned =
            sqlx::query("DELETE FROM memory_activities WHERE accessed_at < ?").bind(cutoff).execute(&self.pool).await?;
        if pruned.rows_affected() > 0 {
            tracing::info!("pruned {} old memory_activities", pruned.rows_affected());
        }
        Ok(())
    }

    /// `dispatch_board_tasks`: promote waiting cards whose parents are all
    /// done, then start ready ones by priority, up to `MAX_IN_PROGRESS`.
    /// Returns how many it started.
    pub async fn dispatch(&self) -> sqlx::Result<usize> {
        let _one = self.dispatching.lock().await;
        let mut tx = self.pool.begin().await?;

        // Only cards that have parents promote; a parentless todo is parked.
        let waiting: Vec<String> = sqlx::query_scalar(
            "SELECT DISTINCT b.id FROM board_tasks b JOIN board_task_links l ON l.child_id = b.id WHERE b.status = 'todo'",
        )
        .fetch_all(&mut *tx)
        .await?;
        for id in waiting {
            let parents: Vec<String> = sqlx::query_scalar(
                "SELECT b.status FROM board_tasks b JOIN board_task_links l ON l.parent_id = b.id WHERE l.child_id = ?",
            )
            .bind(&id)
            .fetch_all(&mut *tx)
            .await?;
            if !parents.is_empty() && parents.iter().all(|s| s == "done") {
                sqlx::query("UPDATE board_tasks SET status = 'ready', updated_at = ? WHERE id = ?")
                    .bind(now_stored())
                    .bind(&id)
                    .execute(&mut *tx)
                    .await?;
            }
        }

        let in_flight: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM jobs WHERE kind = 'board_task' AND status IN ('pending', 'running')",
        )
        .fetch_one(&mut *tx)
        .await?;
        let capacity = MAX_IN_PROGRESS - in_flight;
        if capacity <= 0 {
            tx.commit().await?;
            return Ok(0);
        }
        // A card whose previous run is still wrapping up (re-readied by its
        // own tool call) waits, or two runs would share its thread.
        let ready: Vec<(String, String)> = sqlx::query_as(
            "SELECT id, title FROM board_tasks WHERE status = 'ready' AND \
             (job_id IS NULL OR job_id NOT IN (SELECT id FROM jobs WHERE status IN ('pending', 'running'))) \
             ORDER BY priority DESC, created_at ASC LIMIT ?",
        )
        .bind(capacity)
        .fetch_all(&mut *tx)
        .await?;

        let mut started = vec![];
        for (task_id, title) in &ready {
            let run_id = new_id();
            sqlx::query("UPDATE board_tasks SET status = 'running', job_id = ?, updated_at = ? WHERE id = ?")
                .bind(&run_id)
                .bind(now_stored())
                .bind(task_id)
                .execute(&mut *tx)
                .await?;
            let enqueued_at = crate::jobs::insert(&mut *tx, &run_id, "board_task", &json!({"task_id": task_id})).await?;
            let meta = Meta {
                kind: "board_task".into(),
                label: title.clone(),
                parent_id: Some(task_id.clone()),
                started_at: iso_from_db(&enqueued_at).utc().0,
            };
            self.runs.pre_register(&run_id, meta);
            started.push(run_id);
        }
        if let Err(e) = tx.commit().await {
            for run_id in &started {
                self.runs.discard_pending(run_id);
            }
            return Err(e);
        }
        if !started.is_empty() {
            tracing::info!("board dispatch: {} task(s) enqueued", started.len());
            self.runs.wake();
        }
        Ok(started.len())
    }
}

/// An `isoformat()` watermark → the text SQLAlchemy binds for it: the wall
/// clock as written (an aware value isn't converted), six fractional digits.
fn stored_from_iso(iso: &str) -> Option<String> {
    let iso = iso.trim();
    let wall = match iso.len().checked_sub(6).map(|i| iso.split_at(i)) {
        Some((wall, offset)) if offset.starts_with(['+', '-']) && offset.as_bytes()[3] == b':' => wall,
        _ => iso.strip_suffix('Z').unwrap_or(iso),
    };
    crate::gql::codec::db_from_iso(wall)
}

fn naive(stored: &str) -> Option<chrono::NaiveDateTime> {
    chrono::NaiveDateTime::parse_from_str(stored, "%Y-%m-%d %H:%M:%S%.f")
        .or_else(|_| chrono::NaiveDateTime::parse_from_str(stored, "%Y-%m-%dT%H:%M:%S%.f"))
        .ok()
}

fn py_truthy(v: &serde_json::Value) -> bool {
    !matches!(v, serde_json::Value::Null | serde_json::Value::Bool(false))
        && v.as_str() != Some("")
        && v.as_f64() != Some(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().to_utc()
    }

    #[test]
    fn missed_runs_coalesce_and_respect_the_grace_period() {
        let trigger = Trigger::parse("*/5 * * * *", Tz::UTC).unwrap();
        let mut entry = Entry::new(Action::Dispatch, When::Cron(trigger), 60, Tz::UTC, at("2026-10-02T10:00:30Z"));
        assert_eq!(entry.next.unwrap().isoformat(), "2026-10-02T10:05:00+00:00");
        assert!(!entry.advance(at("2026-10-02T10:04:59Z")));
        // Due at 10:05, 10:10 and 10:15; the last is 30 s late: run once.
        assert!(entry.advance(at("2026-10-02T10:15:30Z")));
        assert_eq!(entry.next.unwrap().isoformat(), "2026-10-02T10:20:00+00:00");
        // 10:20 missed by more than a minute: skipped, and moved past.
        assert!(!entry.advance(at("2026-10-02T10:21:30Z")));
        assert_eq!(entry.next.unwrap().isoformat(), "2026-10-02T10:25:00+00:00");
    }

    async fn scheduler() -> Arc<Scheduler> {
        let pool = sqlx::pool::PoolOptions::<sqlx::Sqlite>::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        for ddl in [
            "CREATE TABLE automations (id TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL, schedule TEXT)",
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT, payload TEXT, status TEXT, run_at TEXT, attempts INT, \
             max_attempts INT, last_error TEXT, locked_by TEXT, locked_until TEXT, cancel_requested BOOLEAN, \
             created_at TEXT, updated_at TEXT, completed_at TEXT)",
        ] {
            sqlx::query(ddl).execute(&pool).await.unwrap();
        }
        Scheduler::new(pool, Arc::default(), Tz::UTC, PathBuf::new(), Checkpoints::open("".as_ref()))
    }

    async fn jobs(s: &Scheduler) -> Vec<(String, String)> {
        sqlx::query_as("SELECT kind, payload FROM jobs ORDER BY rowid").fetch_all(&s.pool).await.unwrap()
    }

    #[tokio::test]
    async fn a_schedule_fires_only_as_the_row_still_has_it() {
        let s = scheduler().await;
        sqlx::query("INSERT INTO automations VALUES ('on', 1, '* * * * *'), ('off', 0, '* * * * *'), \
                     ('bad', 1, 'nope')")
            .execute(&s.pool)
            .await
            .unwrap();
        let (mut entries, mut broken) = (HashMap::new(), HashMap::new());
        s.reload(&mut entries, &mut broken).await;
        assert_eq!(entries.keys().collect::<Vec<_>>(), ["on"]);
        assert_eq!(broken.keys().collect::<Vec<_>>(), ["bad"]);

        s.fire_automation("on", "* * * * *").await.unwrap();
        // Disabled, or rescheduled, since the copy that's due was read.
        s.fire_automation("off", "* * * * *").await.unwrap();
        s.fire_automation("on", "0 9 * * *").await.unwrap();
        assert_eq!(jobs(&s).await, [("automation".into(), r#"{"automation_id": "on", "triggered_by": "schedule"}"#.into())]);

        // An unchanged schedule keeps its next fire time across a reload.
        let next = entries["on"].next.unwrap().to_utc();
        entries.get_mut("on").unwrap().next = Some(Wall::from_utc(next + chrono::Duration::hours(1), Tz::UTC));
        s.reload(&mut entries, &mut broken).await;
        assert_eq!(entries["on"].next.unwrap().to_utc(), next + chrono::Duration::hours(1));
    }

    #[tokio::test]
    async fn maintenance_waits_for_the_one_already_queued() {
        let s = scheduler().await;
        s.enqueue_maintenance("checkpoint_prune").await.unwrap();
        s.enqueue_maintenance("checkpoint_prune").await.unwrap();
        s.enqueue_maintenance("project_memory").await.unwrap();
        assert_eq!(jobs(&s).await.len(), 2);
        sqlx::query("UPDATE jobs SET status = 'done'").execute(&s.pool).await.unwrap();
        s.enqueue_maintenance("checkpoint_prune").await.unwrap();
        assert_eq!(jobs(&s).await[2], ("maintenance".into(), r#"{"task": "checkpoint_prune"}"#.into()));
    }

    #[test]
    fn intervals_count_from_when_they_were_added() {
        let start = at("2026-10-02T10:00:00Z");
        let mut entry = Entry::new(Action::Dispatch, When::Every(chrono::Duration::seconds(15)), 30, Tz::UTC, start);
        assert!(!entry.advance(at("2026-10-02T10:00:14Z")));
        assert!(entry.advance(at("2026-10-02T10:00:16Z")));
        assert_eq!(entry.next.unwrap().to_utc(), at("2026-10-02T10:00:30Z"));
    }
}
