//! Retrieval for the prompt, and the memory write — `core/retrieval.py`,
//! `core/memory_store.py`, `core/skill_store.py:search_skills` and
//! `core/episodes.py`.
//!
//! Hybrid: a dense arm (cosine against stored float32 vectors) and a lexical
//! one (SQLite FTS5, BM25), fused by rank and cut by `select_hybrid`'s two
//! rules, so "nothing relevant" is a real answer. A change to any of these is
//! made in both.

use std::collections::{HashMap, HashSet};

use sqlx::{Row, SqlitePool};

use super::embed::{self, Embedder};
use crate::gql::codec::{new_id, now_stored};

pub(super) fn env_float(name: &str, default: f64) -> f64 {
    match std::env::var(name).ok().filter(|v| !v.is_empty()) {
        Some(raw) => raw.trim().parse().unwrap_or_else(|_| {
            tracing::warn!("ignoring non-numeric {name}={raw:?}");
            default
        }),
        None => default,
    }
}

// ── the primitives ──────────────────────────────────────────────────────────

/// `_STOPWORDS`.
const STOPWORDS: &[&str] = &[
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "how", "i", "if", "in", "is", "it",
    "its", "of", "on", "or", "that", "the", "their", "then", "there", "these", "they", "this", "to", "was", "what",
    "when", "where", "which", "who", "will", "with", "you", "your", "me", "my", "do", "does", "did", "but", "not",
    "can", "could", "would", "should",
];
const MAX_FTS_TERMS: usize = 24;
const RRF_K: f64 = 60.0;
const SPARSE_CANDIDATES: i64 = 20;

/// `fts_match_expr`: ASCII word tokens, quoted, OR'd — never raw user text.
pub fn fts_match_expr(query: &str) -> Option<String> {
    let mut seen = HashSet::new();
    let mut terms = vec![];
    for tok in query.split(|c: char| !(c.is_ascii_alphanumeric() || c == '_')) {
        let low = tok.to_ascii_lowercase();
        if low.len() < 2 || STOPWORDS.contains(&low.as_str()) || !seen.insert(low.clone()) {
            continue;
        }
        terms.push(format!("\"{low}\""));
        if terms.len() >= MAX_FTS_TERMS {
            break;
        }
    }
    (!terms.is_empty()).then(|| terms.join(" OR "))
}

/// `cosine_ranking`: best first; rows with no vector, or another model's
/// dimensionality, are skipped; a zero-norm row scores 0.
pub fn cosine_ranking(q: &[f32], items: &[(String, Option<Vec<u8>>)]) -> Vec<(String, f64)> {
    let qnorm = norm(q);
    let qnorm = if qnorm == 0.0 { 1.0 } else { qnorm };
    let mut out: Vec<(String, f64)> = items
        .iter()
        .filter_map(|(id, blob)| {
            let v = embed::from_blob(blob.as_deref()?);
            if v.len() != q.len() {
                return None;
            }
            let n = norm(&v);
            let n = if n == 0.0 { 1.0 } else { n };
            let dot: f32 = v.iter().zip(q).map(|(a, b)| a * b).sum();
            Some((id.clone(), f64::from(dot / (n * qnorm))))
        })
        .collect();
    out.sort_by(|a, b| b.1.total_cmp(&a.1));
    out
}

fn norm(v: &[f32]) -> f32 {
    v.iter().map(|x| x * x).sum::<f32>().sqrt()
}

/// `select_hybrid`: keep what is strong on either arm — a cosine at or above
/// `max(min_score, rel_drop * best)`, or a top-`k` lexical hit — then order
/// by rank fusion (dense 0.75, lexical 0.25) and keep `k`.
pub fn select_hybrid(dense: &[(String, f64)], sparse: &[String], k: usize, min_score: f64, rel_drop: f64) -> Vec<String> {
    let mut survivors: Vec<String> = vec![];
    if let Some((_, best)) = dense.first() {
        let floor = min_score.max(rel_drop * best);
        survivors.extend(dense.iter().filter(|(_, s)| *s >= floor).map(|(id, _)| id.clone()));
    }
    for id in sparse.iter().take(k) {
        if !survivors.contains(id) {
            survivors.push(id.clone());
        }
    }
    if survivors.is_empty() {
        return vec![];
    }
    let mut fused: HashMap<&str, f64> = HashMap::new();
    for (weight, ids) in [(0.75, dense.iter().map(|(i, _)| i.as_str()).collect::<Vec<_>>()), (0.25, sparse.iter().map(String::as_str).collect())] {
        for (rank, id) in ids.into_iter().enumerate() {
            *fused.entry(id).or_default() += weight / (RRF_K + rank as f64 + 1.0);
        }
    }
    // Stable: equal scores keep the dense-then-lexical order (Python's set
    // order there is arbitrary).
    survivors.sort_by(|a, b| fused.get(b.as_str()).unwrap_or(&0.0).total_cmp(fused.get(a.as_str()).unwrap_or(&0.0)));
    survivors.truncate(k);
    survivors
}

pub(super) async fn lexical(pool: &SqlitePool, sql: &str, binds: &[&str]) -> Vec<String> {
    let mut q = sqlx::query_scalar::<_, String>(sql);
    for b in binds {
        q = q.bind(*b);
    }
    match q.bind(SPARSE_CANDIDATES).fetch_all(pool).await {
        Ok(ids) => ids,
        Err(e) => {
            tracing::warn!("lexical search failed ({e}) — dense-only this turn");
            vec![]
        }
    }
}

// ── memory ──────────────────────────────────────────────────────────────────

/// `search_memory(query, k)`: the `fact` items that clear the cutoff, as
/// `(id, text)`. May be fewer than `k`, or none. Logs the access.
pub async fn search_memory(pool: &SqlitePool, http: &reqwest::Client, query: &str, k: usize) -> Result<Vec<String>, String> {
    if super::prompt::trivial(query) {
        return Ok(vec![]);
    }
    let embedder = Embedder::resolve(pool).await;
    let qvec = embedder.query(http, query, true).await;
    let rows = sqlx::query("SELECT id, text, embedding FROM memories WHERE kind = 'fact' ORDER BY updated_at DESC")
        .fetch_all(pool)
        .await
        .map_err(|e| e.to_string())?;
    if rows.is_empty() {
        return Ok(vec![]);
    }
    let sparse = match fts_match_expr(query) {
        Some(expr) => {
            lexical(
                pool,
                "SELECT m.id FROM memories_fts f JOIN memories m ON m.rowid = f.rowid \
                 WHERE memories_fts MATCH ? AND m.kind = ? ORDER BY bm25(memories_fts) LIMIT ?",
                &[&expr, "fact"],
            )
            .await
        }
        None => vec![],
    };
    let items: Vec<(String, Option<Vec<u8>>)> = rows.iter().map(|r| (r.get("id"), r.get("embedding"))).collect();
    let dense = qvec.map(|q| cosine_ranking(&q, &items)).unwrap_or_default();
    let keep = select_hybrid(
        &dense,
        &sparse,
        k,
        env_float("JARVIS_MEMORY_MIN_COSINE", 0.30),
        env_float("JARVIS_MEMORY_REL_DROP", 0.75),
    );
    let texts: HashMap<String, String> = rows.iter().map(|r| (r.get("id"), r.get("text"))).collect();
    let scores: HashMap<&str, f64> = dense.iter().map(|(i, s)| (i.as_str(), *s)).collect();
    let hits: Vec<(String, String)> = keep.into_iter().filter_map(|id| Some((texts.get(&id)?.clone(), id))).map(|(t, id)| (id, t)).collect();
    touch(pool, &hits.iter().map(|(id, _)| (id.as_str(), scores.get(id.as_str()).copied().unwrap_or(0.0))).collect::<Vec<_>>(), query).await;
    Ok(hits.into_iter().map(|(_, t)| t).collect())
}

/// `touch_memories`: the access log, best-effort. The score is rounded as
/// `search_memory` reports it.
async fn touch(pool: &SqlitePool, hits: &[(&str, f64)], query: &str) {
    let query: String = query.chars().take(500).collect();
    for (id, score) in hits {
        let r = sqlx::query(
            "INSERT INTO memory_activities (id, memory_id, conversation_id, kind, score, query, source, accessed_at) \
             VALUES (?, ?, NULL, 'fact', ?, ?, 'retrieval', ?)",
        )
        .bind(new_id())
        .bind(id)
        .bind((score * 10_000.0).round() / 10_000.0)
        .bind(&query)
        .bind(now_stored())
        .execute(pool)
        .await;
        if let Err(e) = r {
            tracing::warn!("memory_activity touch failed: {e}");
            return;
        }
    }
}

/// `_DEDUP_THRESHOLD`: a new memory this close to one of its kind replaces it.
const DEDUP: f64 = 0.88;

/// `upsert_memory`: embed `text` in document space, then merge it into a
/// near-duplicate of the same kind or insert it. The id it landed on.
pub async fn upsert_memory(pool: &SqlitePool, http: &reqwest::Client, text: &str, kind: &str) -> Result<String, String> {
    let text = text.trim();
    let blob = embed::for_storage(pool, http, text).await?;
    let vec = embed::from_blob(&blob);
    let rows = sqlx::query("SELECT id, embedding FROM memories WHERE kind = ? ORDER BY updated_at DESC")
        .bind(kind)
        .fetch_all(pool)
        .await
        .map_err(|e| e.to_string())?;
    let items: Vec<(String, Option<Vec<u8>>)> = rows.iter().map(|r| (r.get("id"), r.get("embedding"))).collect();
    let now = now_stored();
    match cosine_ranking(&vec, &items).first() {
        Some((id, score)) if *score >= DEDUP => {
            sqlx::query("UPDATE memories SET text = ?, embedding = ?, updated_at = ? WHERE id = ?")
                .bind(text)
                .bind(&blob)
                .bind(&now)
                .bind(id)
                .execute(pool)
                .await
                .map_err(|e| e.to_string())?;
            tracing::info!("memory: merged into {id} (cosine={score:.3})");
            Ok(id.clone())
        }
        _ => {
            let id = new_id();
            sqlx::query("INSERT INTO memories (id, kind, text, embedding, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)")
                .bind(&id)
                .bind(kind)
                .bind(text)
                .bind(&blob)
                .bind(&now)
                .bind(&now)
                .execute(pool)
                .await
                .map_err(|e| e.to_string())?;
            Ok(id)
        }
    }
}

// ── skills ──────────────────────────────────────────────────────────────────

/// `search_skills(query, k)`: enabled skills by cosine of their description,
/// as `(name, description)`. Empty without a query vector.
pub async fn search_skills(pool: &SqlitePool, http: &reqwest::Client, query: &str, k: usize) -> Vec<(String, String)> {
    if query.trim().is_empty() {
        return vec![];
    }
    let Some(q) = Embedder::resolve(pool).await.query(http, query, false).await else { return vec![] };
    let rows = sqlx::query("SELECT name, description, embedding FROM skills WHERE enabled = 1 ORDER BY name ASC")
        .fetch_all(pool)
        .await
        .unwrap_or_default();
    let items: Vec<(String, Option<Vec<u8>>)> = rows.iter().map(|r| (r.get("name"), r.get("embedding"))).collect();
    let descriptions: HashMap<String, String> = rows.iter().map(|r| (r.get("name"), r.get("description"))).collect();
    cosine_ranking(&q, &items)
        .into_iter()
        .take(k)
        .map(|(name, _)| {
            let d = descriptions[&name].clone();
            (name, d)
        })
        .collect()
}

// ── episodes ────────────────────────────────────────────────────────────────

/// `EPISODES_PER_TURN`, `_EPISODE_INJECT_CHARS`.
const EPISODES_PER_TURN: usize = 3;
const EPISODE_CHARS: usize = 2_000;

/// `search_episodes` then `render_episodes`: the compacted-away stretches of
/// this conversation that bear on `query`, oldest first — `None` if none do.
pub async fn earlier_episodes(pool: &SqlitePool, http: &reqwest::Client, conversation_id: &str, query: &str) -> Option<String> {
    if super::prompt::trivial(query) {
        return None;
    }
    let rows = sqlx::query(
        "SELECT id, text, embedding, created_at FROM conversation_episodes WHERE conversation_id = ? \
         ORDER BY created_at ASC",
    )
    .bind(conversation_id)
    .fetch_all(pool)
    .await
    .ok()?;
    if rows.is_empty() {
        return None;
    }
    let mut sparse = match fts_match_expr(query) {
        Some(expr) => {
            lexical(
                pool,
                "SELECT e.id FROM conversation_episodes_fts f JOIN conversation_episodes e ON e.rowid = f.rowid \
                 WHERE conversation_episodes_fts MATCH ? AND e.conversation_id = ? \
                 ORDER BY bm25(conversation_episodes_fts) LIMIT ?",
                &[&expr, conversation_id],
            )
            .await
        }
        None => vec![],
    };
    let qvec = Embedder::resolve(pool).await.query(http, query, true).await;
    let items: Vec<(String, Option<Vec<u8>>)> = rows.iter().map(|r| (r.get("id"), r.get("embedding"))).collect();
    let dense = qvec.map(|q| cosine_ranking(&q, &items)).unwrap_or_default();
    if !dense.is_empty() {
        // Where cosine can score a row it decides; the lexical arm only
        // covers rows stored before an embedder was configured.
        let scored: HashSet<&str> = dense.iter().map(|(i, _)| i.as_str()).collect();
        sparse.retain(|i| !scored.contains(i.as_str()));
    }
    let keep: HashSet<String> = select_hybrid(
        &dense,
        &sparse,
        EPISODES_PER_TURN,
        env_float("JARVIS_EPISODE_MIN_COSINE", 0.30),
        env_float("JARVIS_EPISODE_REL_DROP", 0.75),
    )
    .into_iter()
    .collect();
    let blocks: Vec<String> = rows
        .iter()
        .filter(|r| keep.contains(&r.get::<String, _>("id")))
        .map(|r| {
            let text: String = r.get("text");
            let mut body = text.trim().to_string();
            if body.chars().count() > EPISODE_CHARS {
                let head: String = body.chars().take(EPISODE_CHARS).collect();
                body = format!("{} …", head.rsplit_once(' ').map_or(head.as_str(), |(h, _)| h));
            }
            let stamp: String = r.get("created_at");
            format!("### Summarized {}\n{body}", stamp.get(..16).unwrap_or(&stamp))
        })
        .collect();
    if blocks.is_empty() {
        return None;
    }
    Some(format!(
        "## Earlier in this conversation\n\nSummaries of stretches of this conversation that were compacted out of \
         context, retrieved because they look relevant to the current message. For the exact wording, \
         `jarvis.read_conversation(limit=...)` in run_cell returns the original messages.\n\n{}",
        blocks.join("\n\n")
    ))
}

/// What `search_memory` reports as text, for the prompt.
pub fn relevant_memories(hits: &[String]) -> String {
    let lines: Vec<String> = hits.iter().map(|t| format!("- {t}")).collect();
    format!("## Relevant Memories\n\n{}", lines.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn match_expr_is_fts_match_expr() {
        assert_eq!(fts_match_expr("What is the error E1234 in my_file?").as_deref(), Some(r#""error" OR "e1234" OR "my_file""#));
        assert_eq!(fts_match_expr("a the I"), None);
        assert_eq!(fts_match_expr("Café déjà-vu"), Some(r#""caf" OR "vu""#.into()));
    }

    #[test]
    fn hybrid_keeps_either_arm_and_cuts_the_rest() {
        let dense = vec![("a".to_string(), 0.9), ("b".into(), 0.7), ("c".into(), 0.2)];
        // floor = max(0.3, 0.75 * 0.9) = 0.675: a, b; c only via the lexical
        // arm — and, ranked on both arms, it fuses first (as in Python).
        assert_eq!(select_hybrid(&dense, &["c".into()], 6, 0.3, 0.75), vec!["c", "a", "b"]);
        assert_eq!(select_hybrid(&dense, &[], 6, 0.3, 0.75), vec!["a", "b"]);
        assert!(select_hybrid(&[("x".into(), 0.1)], &[], 6, 0.3, 0.75).is_empty());
    }

    #[test]
    fn cosine_skips_other_models() {
        let q = [1.0f32, 0.0];
        let items = vec![
            ("same".to_string(), Some(embed::to_blob(&[1.0, 0.0]))),
            ("orth".into(), Some(embed::to_blob(&[0.0, 1.0]))),
            ("dims".into(), Some(embed::to_blob(&[1.0, 0.0, 0.0]))),
            ("none".into(), None),
        ];
        let r = cosine_ranking(&q, &items);
        assert_eq!(r.iter().map(|(i, _)| i.as_str()).collect::<Vec<_>>(), ["same", "orth"]);
        assert!((r[0].1 - 1.0).abs() < 1e-6);
    }
}
