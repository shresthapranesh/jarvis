//! Memory consolidation: the two passes that turn finished conversations
//! into memory, run here instead of in Python.
//!
//! - `memory` — `core/memory_consolidation.py`: the user's memory items,
//!   added, corrected and deleted from the transcript (every 6 hours, and
//!   `consolidateMemory`).
//! - `project` — `core/project_memory_consolidation.py`: each project's
//!   shared memory, merged into or rewritten (every 30 minutes, and
//!   `consolidateProjectMemory`).
//! - `dedupe` — `core/text_dedupe.py`, the merge's "already said?".
//!
//! Ports, all three: a change on either side is made in both. Diffed against
//! Python in `tests/test_edge_loop.py`.
//!
//! Both read the messages table past a watermark kept in `kv_store`, call
//! the model once per batch (`llm::ask`), and write rows. With the agent
//! loop off (`JARVIS_AGENT_RUNTIME=python`) they are Python's — a queued
//! `maintenance` job, or the mutation proxied.

pub mod dedupe;
pub mod memory;
pub mod project;

use chrono::{DateTime, FixedOffset, NaiveDateTime, Utc};
use serde_json::Value;
use sqlx::SqlitePool;

/// One maintenance sweep by name, as `MAINTENANCE_TASKS` runs it: the
/// summary Python logs.
pub async fn sweep(pool: &SqlitePool, http: &reqwest::Client, task: &str) -> Result<String, String> {
    match task {
        "memory_consolidation" => memory::consolidate(pool, http, None).await,
        "project_memory" => project::consolidate_all(pool, http).await,
        other => Err(format!("unknown maintenance task {}", crate::pyjson::repr_str(other))),
    }
}

/// The model's reply as one string — `_flatten` / `_coerce_text`: text
/// blocks joined with nothing between.
async fn ask(pool: &SqlitePool, http: &reqwest::Client, model: &str, system: &str, user: String) -> Result<String, String> {
    Ok(crate::llm::ask(pool, http, model, system, user).await.map_err(|e| e.message)?.concat())
}

// ── kv_store ─────────────────────────────────────────────────────────────────

/// `KvStore.aget(namespace, key).value`. A value that isn't JSON reads as
/// none, as the watermark readers treat a value they can't use.
async fn kv_get(pool: &SqlitePool, namespace: &str, key: &str) -> Result<Option<Value>, String> {
    let raw: Option<String> = sqlx::query_scalar("SELECT value FROM kv_store WHERE namespace = ? AND key = ?")
        .bind(namespace)
        .bind(key)
        .fetch_optional(pool)
        .await
        .map_err(|e| e.to_string())?;
    Ok(raw.and_then(|r| serde_json::from_str(&r).ok()))
}

/// `KvStore.aput`: insert, or replace the value and bump `updated_at`.
async fn kv_put(pool: &SqlitePool, namespace: &str, key: &str, value: &Value) -> Result<(), String> {
    let now = crate::gql::codec::now_stored();
    sqlx::query(
        "INSERT INTO kv_store (namespace, key, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?) \
         ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
    )
    .bind(namespace)
    .bind(key)
    .bind(crate::pyjson::dumps_unicode(value))
    .bind(&now)
    .bind(&now)
    .execute(pool)
    .await
    .map_err(|e| e.to_string())?;
    Ok(())
}

// ── time ─────────────────────────────────────────────────────────────────────

/// A stored `DATETIME` (naive UTC).
fn stored(raw: &str) -> Result<NaiveDateTime, String> {
    NaiveDateTime::parse_from_str(raw, "%Y-%m-%d %H:%M:%S%.f")
        .or_else(|_| NaiveDateTime::parse_from_str(raw, "%Y-%m-%dT%H:%M:%S%.f"))
        .or_else(|_| NaiveDateTime::parse_from_str(raw, "%Y-%m-%d %H:%M:%S"))
        .map_err(|_| format!("unreadable timestamp {raw:?}"))
}

/// A datetime as SQLAlchemy binds it: the wall clock, six fractional
/// digits, any offset dropped (not converted).
fn bind(at: &DateTime<FixedOffset>) -> String {
    at.naive_local().format("%Y-%m-%d %H:%M:%S%.6f").to_string()
}

/// `_aware(stored)`: a row's timestamp, taken as UTC.
fn aware(at: NaiveDateTime) -> DateTime<FixedOffset> {
    at.and_utc().fixed_offset()
}

/// `dt.isoformat()` for an aware datetime: microseconds only when set.
fn isoformat(at: &DateTime<FixedOffset>) -> String {
    let wall = if at.timestamp_subsec_micros() == 0 { "%Y-%m-%dT%H:%M:%S" } else { "%Y-%m-%dT%H:%M:%S%.6f" };
    let offset = at.offset().local_minus_utc();
    let (sign, secs) = if offset < 0 { ('-', -offset) } else { ('+', offset) };
    let mut out = format!("{}{sign}{:02}:{:02}", at.format(wall), secs / 3600, secs % 3600 / 60);
    if secs % 60 != 0 {
        out.push_str(&format!(":{:02}", secs % 60));
    }
    out
}

fn now() -> DateTime<FixedOffset> {
    Utc::now().fixed_offset()
}

/// `_aware(datetime.fromisoformat(raw))` for the shapes jarvis writes and
/// Python reads: a date, or a date and time (`T` or space, minutes,
/// seconds and a fraction optional), with `Z` or `±HH:MM` or no offset (UTC).
fn fromisoformat(raw: &str) -> Option<DateTime<FixedOffset>> {
    let (wall, offset) = match raw.strip_suffix('Z') {
        Some(wall) => (wall, Some(0)),
        None => match raw.rfind(['+', '-']).filter(|&i| i > 10) {
            Some(i) => {
                let (h, m) = raw[i + 1..].split_once(':')?;
                let secs = h.parse::<i32>().ok()? * 3600 + m.parse::<i32>().ok()? * 60;
                (&raw[..i], Some(if raw.as_bytes()[i] == b'-' { -secs } else { secs }))
            }
            None => (raw, None),
        },
    };
    let naive = ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"]
        .iter()
        .find_map(|f| NaiveDateTime::parse_from_str(wall, f).ok())
        .or_else(|| chrono::NaiveDate::parse_from_str(wall, "%Y-%m-%d").ok().and_then(|d| d.and_hms_opt(0, 0, 0)))?;
    let offset = FixedOffset::east_opt(offset.unwrap_or(0))?;
    naive.and_local_timezone(offset).single()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn isoformat_round_trips_as_pythons() {
        for raw in ["2026-10-04T12:00:00+00:00", "2026-10-04T12:00:00.123456+00:00", "2026-10-04T12:00:00-05:30"] {
            assert_eq!(isoformat(&fromisoformat(raw).unwrap()), raw);
        }
        assert_eq!(isoformat(&fromisoformat("2026-10-04T12:00:00Z").unwrap()), "2026-10-04T12:00:00+00:00");
        assert_eq!(isoformat(&fromisoformat("2026-10-04").unwrap()), "2026-10-04T00:00:00+00:00");
        assert_eq!(bind(&fromisoformat("2026-10-04T12:00:00.5+05:00").unwrap()), "2026-10-04 12:00:00.500000");
        assert!(fromisoformat("garbage").is_none());
    }
}
