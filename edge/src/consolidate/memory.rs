//! The user's memory items from recent conversations — a port of
//! `core/memory_consolidation.py` (change both).
//!
//! The model reads the existing items and one batch of transcript, and
//! answers with add / update / delete operations. Batches go oldest first,
//! each advancing the watermark only past what it read, up to
//! `MAX_BATCHES_PER_RUN` a pass.
//!
//! Departure: only the item path. Python falls back to rewriting the
//! `AGENTS.md` blob when it has no embedder, which it always has (Gemini
//! with a key, else Ollama's), as the edge does.

use std::collections::HashSet;

use serde_json::{Value, json};
use sqlx::{Row, SqlitePool};
use tokio::sync::Mutex;

use super::{ask, aware, bind, fromisoformat, isoformat, kv_get, kv_put, now, stored};
use crate::pyjson::{py_str, repr_str};
use crate::pystr;

const META_NS: &str = "memory_consolidation";
const META_KEY: &str = "state";

/// One call reads at most this much transcript.
const BATCH_CHARS: usize = 16_384;
const MAX_BATCHES_PER_RUN: usize = 6;
const MSG_CAP: usize = 500;
const FETCH_LIMIT: i64 = 200;

/// `_run_lock`: the timer and the mutation read the same watermark, so one
/// pass at a time.
static RUN_LOCK: Mutex<()> = Mutex::const_new(());

const EXTRACT_SYSTEM_PROMPT: &str = r#"You maintain durable memory items about the user from recent
conversations, so an AI assistant can remember them across sessions.

You are given existing memory items with IDs. You must decide which to ADD, UPDATE, or DELETE
based on the recent transcript.

Output ONLY a JSON array. Each element is an operation:
  {"op": "add", "text": "<one atomic self-contained fact>", "kind": "core" | "fact"}
  {"op": "update", "id": "<existing_id>", "text": "<corrected version>", "kind": "core" | "fact"}
  {"op": "delete", "id": "<existing_id>", "reason": "<why — contradicted|temporary_expired|user_requested|outdated>"}

- "core": durable identity and strong preferences that should ALWAYS be in mind —
  who the user is, their role/expertise, hard preferences, how they want the assistant to behave.
- "fact": everything else worth remembering — project details, decisions, context, one-off facts.

Rules for ADD / UPDATE:
- One atomic fact per item. Keep each short and self-contained (no pronouns pointing outside the item).
- Only ADD information not already covered by existing items.
- If transcript contradicts an existing item, emit UPDATE with corrected version pointing to its id.
- DO NOT ADD temporary facts: if user says "for today only", "this week only", "temporarily", "just for now",
  "until Friday", "for this session", don't create a durable memory. If such a temporary fact already
  exists, DELETE it with reason temporary_expired.
- If user says "forget that", "don't remember X", "remove that memory", DELETE the matching id with reason user_requested.

Rules for DELETE:
- Delete when: contradicted by newer info, is temporary and no longer relevant, user explicitly asked to forget,
  or clearly outdated (e.g., job changed, moved, preference reversed).
- Be conservative: only delete when transcript explicitly contradicts or user requested. Don't mass-delete.
- Include reason: contradicted | temporary_expired | user_requested | outdated

If nothing to do, emit [].
Output ONLY the JSON array — no prose, no markdown fences.
"#;

const SPLIT_SYSTEM_PROMPT: &str = r#"You convert an existing free-text memory document into discrete
memory items. Output ONLY a JSON array of objects:
  {"text": "<one atomic, self-contained fact>", "kind": "core" | "fact"}

- "core": durable identity and strong preferences that should ALWAYS be in mind.
- "fact": everything else worth remembering.
- One atomic fact per item; keep each short and self-contained.
- Output ONLY the JSON array — no prose, no markdown fences.
"#;

#[derive(Debug, PartialEq)]
enum Op {
    Add { text: String, kind: &'static str },
    Update { id: String, text: String, kind: &'static str },
    Delete { id: String, reason: String },
}

fn kind_of(el: &serde_json::Map<String, Value>) -> &'static str {
    match el.get("kind").and_then(Value::as_str) {
        Some("core") => "core",
        _ => "fact",
    }
}

/// `str(el.get(key, "")).strip()`.
fn field(el: &serde_json::Map<String, Value>, key: &str) -> String {
    pystr::strip(&el.get(key).map(py_str).unwrap_or_default()).to_string()
}

/// `_coerce_items`: the outermost `[...]` of the reply, its malformed
/// elements dropped. An element with no (or an unknown) `op` is an add.
fn coerce_items(text: &str) -> Vec<Op> {
    let (Some(start), Some(end)) = (text.find('['), text.rfind(']')) else {
        tracing::warn!("memory extraction: no JSON array in response, skipping");
        return vec![];
    };
    if end < start {
        tracing::warn!("memory extraction: no JSON array in response, skipping");
        return vec![];
    }
    let data: Value = match serde_json::from_str(&text[start..=end]) {
        Ok(v) => v,
        Err(_) => {
            tracing::warn!("memory extraction: could not parse JSON array, skipping");
            return vec![];
        }
    };
    let Value::Array(items) = data else { return vec![] };
    let mut out = vec![];
    for el in &items {
        let Value::Object(el) = el else { continue };
        match el.get("op").and_then(Value::as_str) {
            Some("delete") => {
                let id = field(el, "id");
                let reason = field(el, "reason");
                if id.is_empty() {
                    continue;
                }
                out.push(Op::Delete { id, reason: if reason.is_empty() { "unknown".into() } else { reason } });
            }
            Some("update") => {
                let (id, text) = (field(el, "id"), field(el, "text"));
                if id.is_empty() || text.is_empty() {
                    continue;
                }
                out.push(Op::Update { id, text, kind: kind_of(el) });
            }
            _ => {
                let text = field(el, "text");
                if text.is_empty() {
                    continue;
                }
                out.push(Op::Add { text, kind: kind_of(el) });
            }
        }
    }
    out
}

/// `_transcript_block`: one batch from oldest-first messages — the text,
/// the newest stamp that made it in, and how many did. Stops at the char
/// budget, and before a reply still being written.
fn transcript_block(messages: &[Msg]) -> (String, Option<chrono::NaiveDateTime>, usize) {
    let mut lines: Vec<String> = vec![];
    let mut total = 0;
    let mut through = None;
    for m in messages {
        if m.status.as_deref() == Some("running") {
            break;
        }
        let line = format!(
            "[{}] {} | {}: {}",
            m.created_at.format("%Y-%m-%d %H:%M"),
            m.title,
            m.role.to_uppercase(),
            pystr::prefix(m.content.as_deref().unwrap_or(""), MSG_CAP)
        );
        let n = pystr::len(&line);
        if total + n > BATCH_CHARS && !lines.is_empty() {
            break;
        }
        lines.push(line);
        total += n;
        through = Some(m.created_at);
    }
    let n = lines.len();
    (lines.join("\n"), through, n)
}

struct Msg {
    role: String,
    content: Option<String>,
    created_at: chrono::NaiveDateTime,
    status: Option<String>,
    title: String,
}

/// `get_messages_since`: user and assistant messages after `since`, oldest
/// first, never from an incognito conversation.
async fn messages_since(pool: &SqlitePool, since: Option<&str>) -> Result<Vec<Msg>, String> {
    let sql = format!(
        "SELECT m.role, m.content, m.created_at, m.status, c.title FROM messages m \
         JOIN conversations c ON m.conversation_id = c.id \
         WHERE m.role IN ('user', 'assistant') AND c.ephemeral = 0{} ORDER BY m.created_at ASC LIMIT ?",
        if since.is_some() { " AND m.created_at > ?" } else { "" }
    );
    let mut q = sqlx::query(&sql);
    if let Some(since) = since {
        q = q.bind(since);
    }
    let rows = q.bind(FETCH_LIMIT).fetch_all(pool).await.map_err(|e| e.to_string())?;
    rows.iter()
        .map(|r| {
            let title: Option<String> = r.get("title");
            Ok(Msg {
                role: r.get("role"),
                content: r.get("content"),
                created_at: stored(r.get("created_at"))?,
                status: r.get("status"),
                title: title.filter(|t| !t.is_empty()).unwrap_or_else(|| "Untitled".into()),
            })
        })
        .collect()
}

/// `_load_watermark`: the stamp of the last message consolidated, as it is
/// bound — else `last_run_at`, which older installs stored instead.
async fn load_watermark(pool: &SqlitePool) -> Result<Option<String>, String> {
    let Some(meta) = kv_get(pool, META_NS, META_KEY).await? else { return Ok(None) };
    let raw = [meta.get("messages_through"), meta.get("last_run_at")]
        .into_iter()
        .flatten()
        .find(|v| crate::pyjson::truthy(v))
        .cloned();
    match raw {
        None => Ok(None),
        Some(Value::String(iso)) => match fromisoformat(&iso) {
            Some(at) => Ok(Some(bind(&at))),
            None => Err(format!("Invalid isoformat string: {}", repr_str(&iso))),
        },
        Some(_) => Err("fromisoformat: argument must be str".into()),
    }
}

async fn save_watermark(pool: &SqlitePool, through: chrono::NaiveDateTime) -> Result<(), String> {
    let value = json!({"messages_through": isoformat(&aware(through)), "last_run_at": isoformat(&now())});
    kv_put(pool, META_NS, META_KEY, &value).await
}

fn existing_block(existing: &[(String, String, String)]) -> String {
    if existing.is_empty() {
        return "(none yet)".into();
    }
    existing.iter().map(|(id, kind, text)| format!("- id={id} [{kind}] {text}")).collect::<Vec<_>>().join("\n")
}

/// `list_memories`: (id, kind, text), most recently updated first.
async fn list_memories(pool: &SqlitePool) -> Result<Vec<(String, String, String)>, String> {
    sqlx::query_as("SELECT id, kind, text FROM memories ORDER BY updated_at DESC").fetch_all(pool).await.map_err(|e| e.to_string())
}

/// `consolidate_memory`: one pass, unless one is running. The summary
/// Python returns.
pub async fn consolidate(pool: &SqlitePool, http: &reqwest::Client, model: Option<&str>) -> Result<String, String> {
    let Ok(_held) = RUN_LOCK.try_lock() else {
        return Ok("skipped: a consolidation pass is already running".into());
    };
    let mut tx = crate::db::write_tx(pool).await.map_err(|e| e.to_string())?;
    crate::gql::memory::migrate_legacy_key(&mut tx).await.map_err(|e| e.message)?;
    tx.commit().await.map_err(|e| e.to_string())?;
    consolidate_items(pool, http, model).await
}

/// `_consolidate_items`.
async fn consolidate_items(pool: &SqlitePool, http: &reqwest::Client, model: Option<&str>) -> Result<String, String> {
    let mut watermark = load_watermark(pool).await?;
    let model = crate::catalog::resolve_model(pool, model).await.map_err(|e| e.to_string())?;
    let (mem_count,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM memories").fetch_one(pool).await.map_err(|e| e.to_string())?;

    let seeded = if mem_count == 0 { seed_from_blob(pool, http, &model).await? } else { 0 };
    // Sized once per pass, from the store as it stood.
    let max_delete = 5.max(((mem_count + seeded) as f64 * 0.3) as i64);

    let (mut consumed, mut added, mut updated, mut deleted, mut batches) = (0, 0, 0, 0, 0);
    let mut backlog = false;
    loop {
        let messages = messages_since(pool, watermark.as_deref()).await?;
        let existing = list_memories(pool).await?;
        let (transcript, through, n) = transcript_block(&messages);
        let Some(through) = through else { break };
        if batches == MAX_BATCHES_PER_RUN {
            backlog = true;
            break;
        }
        let reply = ask(
            pool,
            http,
            &model,
            EXTRACT_SYSTEM_PROMPT,
            format!(
                "Existing memory items:\n---\n{}\n---\n\nRecent conversations ({n} messages):\n---\n{transcript}\n---\n\n\
                 Decide add/update/delete operations:",
                existing_block(&existing)
            ),
        )
        .await?;
        let mut ids: HashSet<String> = existing.into_iter().map(|(id, _, _)| id).collect();
        let (a, u, d) = apply_ops(pool, http, coerce_items(&reply), &mut ids, max_delete - deleted).await?;
        (added, updated, deleted) = (added + a, updated + u, deleted + d);
        // Saved per batch: a later failure keeps this one's progress.
        save_watermark(pool, through).await?;
        watermark = Some(through.format("%Y-%m-%d %H:%M:%S%.6f").to_string());
        consumed += n;
        batches += 1;
    }

    if batches == 0 {
        return Ok(if seeded > 0 {
            format!("seeded {seeded} items from blob; no new messages since last run")
        } else {
            "skipped: no new messages since last run".into()
        });
    }
    let tail = if backlog { "; backlog remains for the next run" } else { "" };
    tracing::info!(
        "memory_consolidation: {consumed} messages in {batches} batch(es) → +{added} ~{updated} -{deleted} (+{seeded} seeded){}",
        if backlog { "; backlog remains" } else { "" }
    );
    Ok(format!("consolidated {consumed} messages in {batches} batch(es) → +{added} ~{updated} -{deleted} (+{seeded} seeded){tail}"))
}

/// `_seed_from_blob`: once, with no items yet, the legacy `AGENTS.md` blob
/// split into items. The blob stays as a backup.
async fn seed_from_blob(pool: &SqlitePool, http: &reqwest::Client, model: &str) -> Result<i64, String> {
    let Some(value) = kv_get(pool, "memory", "AGENTS.md").await? else { return Ok(0) };
    let raw = match value.get("content") {
        None => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(lines)) => lines.iter().map(py_str).collect::<Vec<_>>().join("\n"),
        Some(other) => return Err(format!("'{}' object has no attribute 'strip'", crate::pyjson::py_type(other))),
    };
    let blob = pystr::strip(&raw);
    if blob.is_empty() {
        return Ok(0);
    }
    let reply = ask(
        pool,
        http,
        model,
        SPLIT_SYSTEM_PROMPT,
        format!("Existing memory document:\n---\n{}\n---\n\nSplit it into items:", pystr::prefix(blob, 32_000)),
    )
    .await?;
    let mut written = 0;
    for op in coerce_items(&reply) {
        // Python upserts whatever carries a text, op or no op.
        let (text, kind) = match op {
            Op::Add { text, kind } | Op::Update { text, kind, .. } => (text, kind),
            Op::Delete { .. } => continue,
        };
        crate::agent::retrieve::upsert_memory(pool, http, &text, kind).await?;
        written += 1;
    }
    tracing::info!("memory: seeded {written} items from legacy AGENTS.md blob");
    Ok(written)
}

/// `_apply_ops`: one batch's operations — (added, updated, deleted).
/// `max_delete` is what's left of the pass's deletion budget.
async fn apply_ops(
    pool: &SqlitePool,
    http: &reqwest::Client,
    mut ops: Vec<Op>,
    existing: &mut HashSet<String>,
    max_delete: i64,
) -> Result<(i64, i64, i64), String> {
    // A hallucinated mass delete is cut to the budget, first ones kept.
    let deletes = ops.iter().filter(|o| matches!(o, Op::Delete { .. })).count() as i64;
    if deletes > max_delete {
        tracing::warn!("memory_consolidation: LLM wants to delete {deletes} > cap {max_delete}, truncating");
        let mut seen = 0;
        ops.retain(|o| {
            if !matches!(o, Op::Delete { .. }) {
                return true;
            }
            seen += 1;
            seen <= max_delete
        });
    }
    let (mut added, mut updated, mut deleted) = (0, 0, 0);
    for op in ops {
        match op {
            Op::Delete { id, reason } => {
                if !existing.contains(&id) {
                    tracing::debug!("memory_consolidation: skip delete id={id} not in existing");
                    continue;
                }
                match sqlx::query("DELETE FROM memories WHERE id = ?").bind(&id).execute(pool).await {
                    Ok(r) if r.rows_affected() > 0 => {
                        deleted += 1;
                        existing.remove(&id);
                        tracing::info!("memory_consolidation: deleted {id} reason={reason}");
                    }
                    Ok(_) => {}
                    Err(e) => tracing::warn!("memory delete failed {id}: {e}"),
                }
            }
            // An id it doesn't know is an add.
            Op::Update { id, text, kind } if existing.contains(&id) => {
                if update_memory(pool, http, &id, &text, kind).await? {
                    updated += 1;
                }
            }
            Op::Update { text, kind, .. } | Op::Add { text, kind } => {
                crate::agent::retrieve::upsert_memory(pool, http, &text, kind).await?;
                added += 1;
            }
        }
    }
    Ok((added, updated, deleted))
}

/// `update_memory_with_embedding`: new text, kind and embedding. An
/// embedder failure fails the pass; a failed write only this item.
async fn update_memory(pool: &SqlitePool, http: &reqwest::Client, id: &str, text: &str, kind: &str) -> Result<bool, String> {
    let text = pystr::strip(text);
    if text.is_empty() {
        return Ok(false);
    }
    let blob = crate::agent::embed::for_storage(pool, http, text).await?;
    let done = sqlx::query("UPDATE memories SET text = ?, kind = ?, embedding = ?, updated_at = ? WHERE id = ?")
        .bind(text)
        .bind(kind)
        .bind(&blob)
        .bind(crate::gql::codec::now_stored())
        .bind(id)
        .execute(pool)
        .await;
    Ok(match done {
        Ok(r) => r.rows_affected() > 0,
        Err(e) => {
            tracing::warn!("memory update failed {id}: {e}");
            false
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replies_coerce_as_pythons() {
        let reply = r#"Sure! ```json
        [{"op": "add", "text": "  Likes tea ", "kind": "core"}, {"text": "No op", "kind": "weird"},
         {"op": "update", "id": "m1", "text": "Fixed"}, {"op": "update", "id": "", "text": "x"},
         {"op": "delete", "id": 7}, {"op": "delete", "id": null, "reason": " old "}, {"op": "delete"},
         {"op": "explode", "text": 5}, "junk", {"op": "add", "text": "  "}]
        ```"#;
        assert_eq!(
            coerce_items(reply),
            [
                Op::Add { text: "Likes tea".into(), kind: "core" },
                Op::Add { text: "No op".into(), kind: "fact" },
                Op::Update { id: "m1".into(), text: "Fixed".into(), kind: "fact" },
                Op::Delete { id: "7".into(), reason: "unknown".into() },
                Op::Delete { id: "None".into(), reason: "old".into() },
                Op::Add { text: "5".into(), kind: "fact" },
            ]
        );
        assert_eq!(coerce_items("] nothing ["), []);
        assert_eq!(coerce_items("[not json]"), []);
        assert_eq!(coerce_items(r#"[{"a": 1}] and {"b": [2]}"#), []);
    }
}
