//! `search_documents` and `read_document` — a port of `tools/documents.py`
//! with `search_chunks`, `read_chunks` and the index waits of
//! `core/doc_index.py`, for the workers bound to them; a change to either is
//! made in both.
//!
//! Attachments are indexed by Python; a document still `pending` is waited
//! on through its row, as Python waits on another process's index.

use std::time::{Duration, Instant};

use serde_json::{Value, json};
use sqlx::SqlitePool;

use super::embed::Embedder;
use super::retrieve::{cosine_ranking, env_float, fts_match_expr, lexical, select_hybrid};

/// `INDEX_WAIT_TIMEOUT`.
const INDEX_WAIT: Duration = Duration::from_secs(120);
/// `_READ_WINDOW_CHARS`.
const READ_WINDOW_CHARS: usize = 6000;
const PENDING: &str = "pending";
const FAILED: &str = "failed";

/// `search_documents(query, k)`. An `Err` is what Python raises (a failed
/// index wait); a failed search is the model's to read.
pub async fn search(pool: &SqlitePool, http: &reqwest::Client, conversation_id: Option<&str>, query: &str, k: i64) -> Result<String, String> {
    let Some(conversation_id) = conversation_id else {
        return Ok("No conversation context — document search is only available in chats.".into());
    };
    await_conversation(pool, conversation_id).await?;
    let hits = match search_chunks(pool, http, conversation_id, query, k).await {
        Ok(hits) => hits,
        Err(e) => {
            tracing::warn!("search_documents failed: {e}");
            return Ok(format!("Document search failed: {e}"));
        }
    };
    if hits.is_empty() {
        return Ok("No matching passages. Either no document in this conversation is indexed (small attachments are \
                   inlined directly in the message), or nothing cleared the relevance cutoff — try different wording, \
                   or read_document(document_id) to read it directly."
            .into());
    }
    Ok(crate::pyjson::dumps(&Value::Array(hits)))
}

/// `read_document(document_id, offset)`.
pub async fn read(pool: &SqlitePool, document_id: &str, offset: i64) -> Result<String, String> {
    if await_index(pool, document_id).await?.as_deref() == Some(FAILED) {
        return Ok(format!(
            "Document {document_id} could not be indexed (the embedding step failed), so its text isn't available \
             here. Ask the user to re-attach it, or work from what's already in the conversation."
        ));
    }
    match read_chunks(pool, document_id, offset).await {
        Ok(Some(window)) => Ok(crate::pyjson::dumps(&window)),
        Ok(None) => Ok(format!(
            "Document {document_id} has no index — it was either small enough to be included directly in the \
             conversation, or the id is wrong."
        )),
        Err(e) => {
            tracing::warn!("read_document failed: {e}");
            Ok(format!("Document read failed: {e}"))
        }
    }
}

async fn status(pool: &SqlitePool, document_id: &str) -> Result<Option<String>, String> {
    let row: Option<Option<String>> = sqlx::query_scalar("SELECT index_status FROM documents WHERE id = ?")
        .bind(document_id)
        .fetch_optional(pool)
        .await
        .map_err(|e| e.to_string())?;
    Ok(row.flatten())
}

/// `await_index_ready`, the cross-process way: poll the row until it isn't
/// `pending`, or the wait is up; the last status seen.
async fn await_index(pool: &SqlitePool, document_id: &str) -> Result<Option<String>, String> {
    let mut seen = status(pool, document_id).await?;
    let deadline = Instant::now() + INDEX_WAIT;
    let mut delay = 0.1_f64;
    while seen.as_deref() == Some(PENDING) {
        if Instant::now() >= deadline {
            tracing::warn!("timed out waiting for index of {document_id} (cross-process)");
            break;
        }
        tokio::time::sleep(Duration::from_secs_f64(delay)).await;
        delay = (delay * 1.5).min(2.0);
        seen = status(pool, document_id).await?;
    }
    Ok(seen)
}

/// `await_conversation_indexes`: every document of the conversation still
/// being indexed.
async fn await_conversation(pool: &SqlitePool, conversation_id: &str) -> Result<(), String> {
    let pending: Vec<String> = sqlx::query_scalar("SELECT id FROM documents WHERE conversation_id = ? AND index_status = ?")
        .bind(conversation_id)
        .bind(PENDING)
        .fetch_all(pool)
        .await
        .map_err(|e| e.to_string())?;
    for id in pending {
        await_index(pool, &id).await?;
    }
    Ok(())
}

/// `search_chunks`: dense cosine and BM25 over the conversation's chunks,
/// fused by rank — `[{document_id, filename, seq, score, text}]`.
async fn search_chunks(pool: &SqlitePool, http: &reqwest::Client, conversation_id: &str, query: &str, k: i64) -> Result<Vec<Value>, String> {
    let match_expr = fts_match_expr(query);
    let embedder = Embedder::resolve(pool).await;
    let qvec = embedder.query(http, query, true).await;
    let rows: Vec<(String, Option<Vec<u8>>)> =
        sqlx::query_as("SELECT id, embedding FROM document_chunks WHERE conversation_id = ?")
            .bind(conversation_id)
            .fetch_all(pool)
            .await
            .map_err(|e| e.to_string())?;
    let sparse = match &match_expr {
        Some(expr) => {
            lexical(
                pool,
                "SELECT c.id AS id FROM document_chunks_fts f JOIN document_chunks c ON c.rowid = f.rowid \
                 WHERE document_chunks_fts MATCH ? AND c.conversation_id = ? ORDER BY bm25(document_chunks_fts) LIMIT ?",
                &[expr, conversation_id],
            )
            .await
        }
        None => vec![],
    };
    if rows.is_empty() {
        return Ok(vec![]);
    }
    let dense = qvec.map(|q| cosine_ranking(&q, &rows)).unwrap_or_default();
    let keep = select_hybrid(
        &dense,
        &sparse,
        usize::try_from(k).unwrap_or(0),
        env_float("JARVIS_DOCS_MIN_COSINE", 0.25),
        env_float("JARVIS_DOCS_REL_DROP", 0.60),
    );
    if keep.is_empty() {
        return Ok(vec![]);
    }
    let marks = vec!["?"; keep.len()].join(", ");
    let sql = format!(
        "SELECT c.id, c.document_id, c.seq, c.text, d.filename FROM document_chunks c \
         JOIN documents d ON c.document_id = d.id WHERE c.id IN ({marks})"
    );
    let mut q = sqlx::query_as::<_, (String, String, i64, String, String)>(&sql);
    for id in &keep {
        q = q.bind(id);
    }
    let hydrated = q.fetch_all(pool).await.map_err(|e| e.to_string())?;
    let mut out = vec![];
    for id in &keep {
        let Some((_, document_id, seq, text, filename)) = hydrated.iter().find(|r| &r.0 == id) else { continue };
        let score = dense.iter().find(|(d, _)| d == id).map_or(0.0, |(_, s)| *s);
        out.push(json!({
            "document_id": document_id,
            "filename": filename,
            "seq": seq,
            "score": round4(score),
            "text": text,
        }));
    }
    Ok(out)
}

/// `round(x, 4)`: correctly rounded, as Python's is.
fn round4(x: f64) -> f64 {
    format!("{x:.4}").parse().unwrap_or(x)
}

/// `read_chunks`: chunks from `offset` until about a window's worth of text.
async fn read_chunks(pool: &SqlitePool, document_id: &str, offset: i64) -> Result<Option<Value>, String> {
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT c.text, d.filename FROM document_chunks c JOIN documents d ON c.document_id = d.id \
         WHERE c.document_id = ? ORDER BY c.seq",
    )
    .bind(document_id)
    .fetch_all(pool)
    .await
    .map_err(|e| e.to_string())?;
    if rows.is_empty() {
        return Ok(None);
    }
    let total = rows.len();
    let offset = offset.clamp(0, total as i64 - 1) as usize;
    let (mut parts, mut used, mut i) = (vec![], 0, offset);
    while i < total && used < READ_WINDOW_CHARS {
        parts.push(rows[i].0.as_str());
        used += rows[i].0.chars().count();
        i += 1;
    }
    Ok(Some(json!({
        "filename": rows[0].1,
        "text": parts.join("\n\n"),
        "offset": offset,
        "next_offset": if i < total { json!(i) } else { Value::Null },
        "total_chunks": total,
    })))
}

#[cfg(test)]
mod tests {
    #[test]
    fn scores_round_like_python() {
        // round(0.12345, 4): the double is a hair above the half.
        assert_eq!(super::round4(0.123_45), 0.1235);
        assert_eq!(super::round4(0.876_549_9), 0.8765);
        assert_eq!(super::round4(1.0), 1.0);
    }
}
