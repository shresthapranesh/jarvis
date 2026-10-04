//! Each project's shared memory from its recent conversations — a port of
//! `core/project_memory_consolidation.py` (change both).
//!
//! Two modes with different authority: **merge** may only add (whatever
//! the model returns, only lines not already said are appended), and
//! **rewrite** — at most daily, or when memory is at its cap — may also
//! remove. A project waits until it has gone quiet (or material has waited
//! a day), and until there's enough new material to be worth a call.

use std::sync::atomic::{AtomicBool, Ordering};

use chrono::{DateTime, FixedOffset};
use serde_json::json;
use sqlx::{Row, SqlitePool};

use super::{ask, aware, bind, dedupe::dedupe_against, fromisoformat, isoformat, kv_get, kv_put, now, stored};
use crate::pystr;

const META_NS: &str = "project_memory_consolidation";

const MEMORY_CAP: usize = 24_000;
const MAX_BULLETS: usize = 20;
const NO_UPDATE_MARKER: &str = "__NO_UPDATE__";

/// A conversation must be idle this long before its material is read.
const QUIET_MINUTES: f64 = 15.0;
/// Even a project that never goes quiet gets a pass eventually.
const MAX_STALENESS_HOURS: f64 = 24.0;
const REWRITE_INTERVAL_HOURS: f64 = 24.0;
const MIN_NEW_CHARS: i64 = 600;

/// One pass reads at most this much transcript; the rest waits.
const MATERIAL_BUDGET: usize = 24_000;
const MESSAGE_FETCH_LIMIT: i64 = 400;
/// Assistant messages carry the decisions, so they get more room.
const USER_MSG_CAP: usize = 1_200;
const ASSISTANT_MSG_CAP: usize = 3_000;
const MAX_PROJECTS_PER_TICK: usize = 8;

/// One sweep at a time — APScheduler never overlapped the job either.
static SWEEPING: AtomicBool = AtomicBool::new(false);

const MERGE_SYSTEM_PROMPT: &str = "You extract durable facts for a project's shared memory: a compact summary that every conversation in this project re-reads on every turn.

You are given the project's existing memory (possibly empty) and a transcript of recent conversations from that project. Output ONLY the bullets that should be ADDED — never restate, reword, or reorganize what the existing memory already says.

Add a fact only if a future conversation in this project would act *differently* for knowing it, and only if the transcript ties it to THIS project: stack and versions, architecture decisions, project-specific conventions, key file paths/modules, API contracts, goals/status.

Never add: the user's personal info or background; communication preferences; coding preferences that aren't specific to this project; how a task went or what you did; general knowledge; small talk; secrets or tokens. Never invent anything absent from the transcript.

Output: markdown bullets, one line each, no preamble and no code fences. At most 5 new bullets — usually zero or one.

If the transcript adds nothing that clears the bar, output exactly: __NO_UPDATE__
That is the common case and a correct answer — do not pad.";

const REWRITE_SYSTEM_PROMPT: &str = "You are rewriting a project's shared memory: a compact summary that every conversation in this project re-reads on every turn. This is the only pass allowed to remove things, so pruning is the job.

You are given the current memory and a transcript of recent conversations from the project. Return the complete replacement memory: keep what still matters, drop what is outdated, redundant, or too trivial to justify permanent context, prefer the transcript wherever it contradicts the memory, and merge in genuinely new project-specific facts.

Keep only: stack and versions, architecture decisions, project-specific conventions, key file paths/modules, API contracts, goals/status — all tied to THIS project. Remove personal info, general preferences, task-progress notes, small talk, and anything you would not bother telling a new teammate. Never invent beyond the transcript and the existing memory.

Output: markdown bullets, one line each, no preamble and no code fences. **Hard limit 20 bullets** — if more than that survive, drop the least useful until you are under it.

If the memory is already correct and the transcript changes nothing, output exactly: __NO_UPDATE__";

fn count_bullets(memory: &str) -> usize {
    pystr::splitlines(memory).into_iter().filter(|l| pystr::strip(l).starts_with(['-', '*', '•'])).count()
}

/// `(now - since).total_seconds()`.
fn seconds(since: &DateTime<FixedOffset>, now: &DateTime<FixedOffset>) -> f64 {
    (*now - *since).num_microseconds().unwrap_or(i64::MAX) as f64 / 1e6
}

/// `_load_meta`: (messages_through, last_rewrite_at). A value that won't
/// parse counts as none.
async fn load_meta(pool: &SqlitePool, project_id: &str) -> Result<(Option<DateTime<FixedOffset>>, Option<DateTime<FixedOffset>>), String> {
    let meta = kv_get(pool, META_NS, project_id).await?;
    let parse = |key: &str| meta.as_ref()?.get(key)?.as_str().filter(|s| !s.is_empty()).and_then(fromisoformat);
    Ok((parse("messages_through"), parse("last_rewrite_at")))
}

async fn save_meta(
    pool: &SqlitePool,
    project_id: &str,
    through: Option<&DateTime<FixedOffset>>,
    last_rewrite: Option<&DateTime<FixedOffset>>,
) -> Result<(), String> {
    let value = json!({
        "messages_through": through.map(isoformat),
        "last_rewrite_at": last_rewrite.map(isoformat),
    });
    kv_put(pool, META_NS, project_id, &value).await
}

/// `get_project_activity_since`: (count, oldest, newest, chars) of the
/// project's new user and assistant messages, incognito ones left out.
async fn activity(
    pool: &SqlitePool,
    project_id: &str,
    since: Option<&str>,
) -> Result<(i64, Option<String>, Option<String>, i64), String> {
    let sql = format!(
        "SELECT COUNT(m.id), MIN(m.created_at), MAX(m.created_at), COALESCE(SUM(LENGTH(m.content)), 0) \
         FROM messages m JOIN conversations c ON m.conversation_id = c.id \
         WHERE c.project_id = ? AND m.role IN ('user', 'assistant') AND c.ephemeral = 0{}",
        if since.is_some() { " AND m.created_at > ?" } else { "" }
    );
    sqlx::query_as(&sql).bind(project_id).bind(since).fetch_one(pool).await.map_err(|e| e.to_string())
}

/// `_render_material` over `get_project_messages_since`: the transcript
/// under the budget, oldest first, and the newest stamp in it.
async fn material(pool: &SqlitePool, project_id: &str, since: Option<&str>) -> Result<(String, Option<chrono::NaiveDateTime>), String> {
    let sql = format!(
        "SELECT m.role, m.content, m.created_at, c.title FROM messages m JOIN conversations c ON m.conversation_id = c.id \
         WHERE c.project_id = ? AND m.role IN ('user', 'assistant') AND c.ephemeral = 0{} \
         ORDER BY m.created_at ASC LIMIT ?",
        if since.is_some() { " AND m.created_at > ?" } else { "" }
    );
    let mut q = sqlx::query(&sql).bind(project_id);
    if let Some(since) = since {
        q = q.bind(since);
    }
    let rows = q.bind(MESSAGE_FETCH_LIMIT).fetch_all(pool).await.map_err(|e| e.to_string())?;
    let mut lines: Vec<String> = vec![];
    let mut total = 0;
    let mut through = None;
    for r in &rows {
        let role: String = r.get("role");
        let content: Option<String> = r.get("content");
        let title: Option<String> = r.get("title");
        let stamp = stored(r.get("created_at"))?;
        let cap = if role == "user" { USER_MSG_CAP } else { ASSISTANT_MSG_CAP };
        let body = pystr::strip(content.as_deref().unwrap_or(""));
        let body = if pystr::len(body) > cap { format!("{} …[truncated]", pystr::prefix(body, cap)) } else { body.to_string() };
        let line = format!(
            "[{}] {} | {}: {body}",
            stamp.format("%Y-%m-%d %H:%M"),
            title.filter(|t| !t.is_empty()).as_deref().unwrap_or("Untitled"),
            role.to_uppercase()
        );
        let n = pystr::len(&line);
        if total + n > MATERIAL_BUDGET && !lines.is_empty() {
            break;
        }
        lines.push(line);
        total += n;
        through = Some(stamp);
    }
    Ok((lines.join("\n"), through))
}

/// `_commit_memory`: compare-and-set on `Project.updated_at`. False when
/// someone else wrote since the pass read it — the next pass re-derives.
async fn commit_memory(pool: &SqlitePool, project_id: &str, memory: &str, seen: &str) -> Result<bool, String> {
    let mut tx = crate::db::write_tx(pool).await.map_err(|e| e.to_string())?;
    let current: Option<String> = sqlx::query_scalar("SELECT updated_at FROM projects WHERE id = ?")
        .bind(project_id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(|e| e.to_string())?;
    let Some(current) = current else { return Ok(false) };
    if stored(&current)? != stored(seen)? {
        tracing::info!("project memory: {project_id} changed under us — skipping, next pass re-derives");
        return Ok(false);
    }
    sqlx::query("UPDATE projects SET memory = ?, updated_at = ? WHERE id = ?")
        .bind(memory)
        .bind(crate::gql::codec::now_stored())
        .bind(project_id)
        .execute(&mut *tx)
        .await
        .map_err(|e| e.to_string())?;
    tx.commit().await.map_err(|e| e.to_string())?;
    Ok(true)
}

/// `consolidate_project_memory`: one pass for one project — the summary
/// Python returns. `force` (the mutation) skips the quiet and minimum-
/// material gates, not the "anything new at all" check.
pub async fn consolidate(
    pool: &SqlitePool,
    http: &reqwest::Client,
    project_id: &str,
    model: Option<&str>,
    force: bool,
) -> Result<String, String> {
    let now = now();
    let (through_before, last_rewrite) = load_meta(pool, project_id).await?;
    let since = through_before.as_ref().map(bind);

    let row = sqlx::query("SELECT memory, updated_at FROM projects WHERE id = ?")
        .bind(project_id)
        .fetch_optional(pool)
        .await
        .map_err(|e| e.to_string())?;
    let Some(row) = row else { return Ok(format!("skipped: project {project_id} not found")) };
    let memory: Option<String> = row.get("memory");
    let existing = pystr::strip(memory.as_deref().unwrap_or("")).to_string();
    let seen: String = row.get("updated_at");
    let (count, oldest, newest, chars) = activity(pool, project_id, since.as_deref()).await?;

    let Some(newest) = newest.filter(|_| count > 0) else {
        return Ok("skipped: no new messages since last run".into());
    };
    let quiet_for = seconds(&aware(stored(&newest)?), &now) / 60.0;
    // From the oldest unread message, not the watermark: a project never
    // consolidated has none, and that mustn't count as maximally stale.
    let waiting_hours = match oldest {
        Some(oldest) => seconds(&aware(stored(&oldest)?), &now) / 3600.0,
        None => 0.0,
    };
    if !force {
        if quiet_for < QUIET_MINUTES && waiting_hours < MAX_STALENESS_HOURS {
            return Ok(format!("skipped: active {quiet_for:.0}m ago"));
        }
        if chars < MIN_NEW_CHARS {
            return Ok(format!("skipped: only {chars} new chars"));
        }
    }

    // Rewrite has to be earned; with no rewrite recorded the first pass
    // merges, and the clock starts there.
    let at_cap = count_bullets(&existing) >= MAX_BULLETS || pystr::len(&existing) as f64 > MEMORY_CAP as f64 * 0.8;
    let rewrite_due = last_rewrite.as_ref().is_some_and(|at| seconds(at, &now) / 3600.0 >= REWRITE_INTERVAL_HOURS);
    let mut rewrite = !existing.is_empty() && (at_cap || rewrite_due);

    let (material, through) = material(pool, project_id, since.as_deref()).await?;
    let Some(through) = through.filter(|_| !pystr::strip(&material).is_empty()) else {
        return Ok("skipped: no usable material".into());
    };
    let through = aware(through);
    let model = crate::catalog::resolve_model(pool, model).await.map_err(|e| e.to_string())?;

    if !rewrite {
        let raw = pystr::strip(
            &ask(
                pool,
                http,
                &model,
                MERGE_SYSTEM_PROMPT,
                format!(
                    "Existing project memory:\n---\n{}\n---\n\nRecent conversations:\n---\n{material}\n---\n\nNew bullets to add:",
                    if existing.is_empty() { "(empty)" } else { &existing }
                ),
            )
            .await?,
        )
        .to_string();
        if raw.is_empty() || raw == NO_UPDATE_MARKER {
            save_meta(pool, project_id, Some(&through), Some(last_rewrite.as_ref().unwrap_or(&now))).await?;
            return Ok(format!("merge: nothing new ({count} messages read)"));
        }
        // Add-only is enforced here: only lines not already said go in,
        // after the existing memory, verbatim.
        let (addition, dropped) = dedupe_against(&existing, &raw);
        if addition.is_empty() {
            save_meta(pool, project_id, Some(&through), Some(last_rewrite.as_ref().unwrap_or(&now))).await?;
            return Ok(format!("merge: {dropped} proposed line(s) already present"));
        }
        let candidate = if existing.is_empty() {
            addition.clone()
        } else {
            pystr::strip(&format!("{existing}\n\n{addition}")).to_string()
        };
        if pystr::len(&candidate) > MEMORY_CAP || count_bullets(&candidate) > MAX_BULLETS {
            // Would overflow: the mode that may evict takes it.
            tracing::info!("project memory: {project_id} merge overflowed, escalating to rewrite");
            rewrite = true;
        } else {
            if !commit_memory(pool, project_id, &candidate, &seen).await? {
                return Ok("skipped: concurrent write, will retry next pass".into());
            }
            save_meta(pool, project_id, Some(&through), Some(last_rewrite.as_ref().unwrap_or(&now))).await?;
            let added = pystr::splitlines(&addition).len();
            tracing::info!(
                "project memory merge: {project_id} +{added} line(s), {} → {} chars",
                pystr::len(&existing),
                pystr::len(&candidate)
            );
            return Ok(format!("merge: added {added} line(s)"));
        }
    }
    debug_assert!(rewrite);

    let current = pystr::prefix(&existing, MEMORY_CAP);
    let mut updated = pystr::strip(
        &ask(
            pool,
            http,
            &model,
            REWRITE_SYSTEM_PROMPT,
            format!(
                "Current project memory:\n---\n{}\n---\n\nRecent conversations:\n---\n{material}\n---\n\nReplacement memory:",
                if current.is_empty() { "(empty)" } else { current }
            ),
        )
        .await?,
    )
    .to_string();
    if updated.is_empty() || updated == NO_UPDATE_MARKER || updated == existing {
        save_meta(pool, project_id, Some(&through), Some(&now)).await?;
        return Ok(format!("rewrite: no change ({count} messages read)"));
    }
    if pystr::len(&updated) > MEMORY_CAP {
        updated = pystr::prefix(&updated, MEMORY_CAP).to_string();
    }
    if !commit_memory(pool, project_id, &updated, &seen).await? {
        return Ok("skipped: concurrent write, will retry next pass".into());
    }
    save_meta(pool, project_id, Some(&through), Some(&now)).await?;
    tracing::info!(
        "project memory rewrite: {project_id} {} → {} chars ({} → {} bullets)",
        pystr::len(&existing),
        pystr::len(&updated),
        count_bullets(&existing),
        count_bullets(&updated)
    );
    Ok(format!("rewrite: {} → {} chars", pystr::len(&existing), pystr::len(&updated)))
}

/// `consolidate_project_memories`: every project with new material that
/// has gone quiet, up to `MAX_PROJECTS_PER_TICK` that did something. One
/// project failing doesn't stop the sweep.
pub async fn consolidate_all(pool: &SqlitePool, http: &reqwest::Client) -> Result<String, String> {
    if SWEEPING.swap(true, Ordering::AcqRel) {
        return Ok("skipped: a sweep is already running".into());
    }
    let result = sweep(pool, http).await;
    SWEEPING.store(false, Ordering::Release);
    result
}

async fn sweep(pool: &SqlitePool, http: &reqwest::Client) -> Result<String, String> {
    let projects: Vec<(String, String)> = sqlx::query_as("SELECT id, name FROM projects ORDER BY updated_at DESC")
        .fetch_all(pool)
        .await
        .map_err(|e| e.to_string())?;
    if projects.is_empty() {
        return Ok("no projects".into());
    }
    let mut results = vec![];
    for (id, name) in &projects {
        if results.len() >= MAX_PROJECTS_PER_TICK {
            tracing::info!("project memory sweep: hit the {MAX_PROJECTS_PER_TICK}-project cap, remainder next tick");
            break;
        }
        match consolidate(pool, http, id, None, false).await {
            Ok(outcome) if !outcome.starts_with("skipped") => results.push(format!("{name}: {outcome}")),
            Ok(_) => {}
            Err(e) => tracing::warn!("project memory: {id} failed: {e}"),
        }
    }
    if results.is_empty() {
        return Ok(format!("nothing to do ({} project(s) checked)", projects.len()));
    }
    Ok(results.join("; "))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bullets_count_as_pythons() {
        assert_eq!(count_bullets("- a\n  * b\n• c\n+ d\n1. e\n\n-f"), 4);
    }
}
