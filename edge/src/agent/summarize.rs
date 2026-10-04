//! Summarizing compaction — `maybe_compact` (`core/compaction.py`) and
//! `record_episode` (`core/episodes.py`), for the turns the edge runs.
//!
//! Per-call compaction (`llm::compact::per_call`) only trims what a call
//! sees. Once the history still outgrows the model's threshold, the older
//! groups are summarized by the turn's own model, the summary replaces them
//! in the thread, and the summary of just the evicted stretch is kept as an
//! episode so its detail can be retrieved later.
//!
//! When Python's tokenizer is unavailable it counts with the same chars/4
//! heuristic used here — `transformers` isn't installed for Ollama, and
//! tiktoken doesn't know the OpenAI-compatible models. Google is the
//! exception: Python asks its countTokens API, one request per message, and
//! only when the previous call's usage can't be trusted. The edge counts
//! Google with the heuristic too.

use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::SqlitePool;

use super::embed::{self, Embedder, Space};
use crate::gql::codec::{new_id, now_stored};
use crate::llm::compact::{self, Eviction};
use crate::llm::shape::{Prompt, SystemBlock};
use crate::llm::transcript::{Content, Message, Part, Role, Typed};
use crate::llm::{self, Blobs, Endpoints, Request, Tool};
use crate::pyjson;

/// What one check produced. `messages` is what the call sees either way.
pub struct Compaction {
    pub messages: Vec<Message>,
    /// The thread's change, written with the reply: ids to evict and the
    /// running summary that replaces them.
    pub update: Option<(Vec<String>, Message)>,
    /// The evicted stretch's own summary and the ids it covers.
    pub episode: Option<(String, Vec<String>)>,
}

/// `_schema_tokens`: chars/4 of the bound tools as `convert_to_openai_tool`
/// spells them.
pub fn schema_tokens(tools: &[Tool]) -> i64 {
    let chars: usize = tools
        .iter()
        .map(|t| {
            let spec = json!({"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}});
            pyjson::dumps(&spec).chars().count()
        })
        .sum();
    (chars / 4) as i64
}

pub struct Summarizer<'a> {
    pub http: &'a reqwest::Client,
    pub ends: &'a Endpoints,
    pub model: &'a str,
    pub blobs: &'a Blobs,
}

/// `maybe_compact`. `overhead` is the request's estimated non-history part
/// in tokens, to count from the last call's reported usage; `None` when the
/// provider's input count leaves out a cached prefix (Ollama).
pub async fn maybe_compact(s: &Summarizer<'_>, history: &[Message], threshold: i64, overhead: Option<i64>) -> Compaction {
    let leaned = compact::per_call(history.to_vec());
    let heuristic = compact::heuristic(&leaned);
    let from_usage = overhead
        .and_then(|o| compact::history_tokens_from_usage(history, o))
        .filter(|&n| n as f64 >= heuristic as f64 * compact::USAGE_SANITY_FLOOR);
    let (count, source) = match from_usage {
        Some(n) => (n, "usage"),
        None if heuristic <= (threshold as f64 * 0.8) as i64 => return keep(leaned),
        None => (heuristic, "count"),
    };
    if count <= threshold {
        return keep(leaned);
    }
    let Some(Eviction { for_summary, old_summary, kept, removed, evicted }) = compact::eviction(history) else {
        return keep(leaned);
    };
    tracing::info!(
        "compact triggered: {count} tokens ({source}) / {} raw msgs -> keeping {} msgs, summarizing {} msgs{}",
        history.len(),
        kept.len(),
        evicted.len(),
        if old_summary.is_some() { " (merging the existing summary)" } else { "" },
    );

    let delta = match summarize(s, compact::DELTA_PROMPT, for_summary).await {
        Ok(text) => text,
        Err(e) => {
            tracing::warn!("compaction delta summarization failed ({e}) — skip");
            return keep(leaned);
        }
    };
    let summary = match old_summary {
        None => delta.clone(),
        Some(old) => {
            let both = Message::new(
                Role::User,
                Content::Text(format!("Existing summary:\n{old}\n\nNew chunk summary:\n{delta}")),
            );
            summarize(s, compact::MERGE_PROMPT, vec![both]).await.unwrap_or_else(|e| {
                tracing::warn!("compaction merge failed ({e}) — falling back to concatenation");
                format!("{old}\n\nRecent: {delta}")
            })
        }
    };
    tracing::info!("compacted {} msgs into ~{} chars", evicted.len(), summary.chars().count());

    let summary = Message {
        id: Some(new_id()),
        ..Message::new(Role::System, Content::Text(format!("[Conversation summary]\n{summary}")))
    };
    let mut seen = vec![summary.clone()];
    seen.extend(kept);
    Compaction { messages: compact::per_call(seen), update: Some((removed, summary)), episode: Some((delta, evicted)) }
}

fn keep(leaned: Vec<Message>) -> Compaction {
    Compaction { messages: leaned, update: None, episode: None }
}

/// One summarizer call: `instructions` as the system prompt, no tools, the
/// reply's text. Nothing streams to the run.
async fn summarize(s: &Summarizer<'_>, instructions: &str, messages: Vec<Message>) -> Result<String, String> {
    let prompt = Prompt {
        system: vec![SystemBlock { text: instructions.into(), breakpoint: false }],
        messages,
        history_breakpoint: None,
        cached: false,
    };
    let req = Request { model: s.model, prompt: &prompt, tools: &[], blobs: s.blobs };
    let reply = llm::complete(s.http, s.ends, &req, &mut |_| {}).await.map_err(|e| e.message)?;
    Ok(reply_text(&reply.message))
}

/// The reply's text parts. Python takes a string reply as it is and a list
/// as its `str()`; a list here is only ever text beside thinking, and the
/// thinking isn't the summary.
fn reply_text(m: &Message) -> String {
    match &m.content {
        Content::Text(s) => s.clone(),
        Content::Parts(parts) => parts
            .iter()
            .filter_map(|p| match p {
                Part::Typed(Typed::Text { text, .. }) => Some(text.as_str()),
                Part::Str(s) => Some(s.as_str()),
                _ => None,
            })
            .collect(),
    }
}

// ── episodes ────────────────────────────────────────────────────────────────

/// `episode_id`: stable for the evicted ids, so a replayed compaction
/// writes the same row rather than a second.
pub fn episode_id(conversation_id: &str, evicted: &[String]) -> String {
    let mut parts = vec![conversation_id];
    parts.extend(evicted.iter().map(String::as_str));
    let digest = Sha256::digest(parts.join("\x1f").as_bytes());
    format!("ep_{}", &hex::encode(digest)[..32])
}

/// `record_episode`: store one evicted stretch's summary, embedded. `Ok(false)`
/// when there was nothing to write — no text, no Conversation row (nothing
/// would ever delete it), or an episode for these ids already.
pub async fn record_episode(
    pool: &SqlitePool,
    http: &reqwest::Client,
    conversation_id: &str,
    text: &str,
    evicted: &[String],
) -> Result<bool, String> {
    let text = text.trim();
    if conversation_id.is_empty() || text.is_empty() {
        return Ok(false);
    }
    let exists: Option<i64> = sqlx::query_scalar("SELECT 1 FROM conversations WHERE id = ?")
        .bind(conversation_id)
        .fetch_optional(pool)
        .await
        .map_err(|e| e.to_string())?;
    if exists.is_none() {
        return Ok(false);
    }
    let vector = Embedder::resolve(pool).await.embed(http, text, Space::Document).await?;
    let written = sqlx::query(
        "INSERT INTO conversation_episodes (id, conversation_id, text, embedding, created_at) VALUES (?, ?, ?, ?, ?) \
         ON CONFLICT(id) DO NOTHING",
    )
    .bind(episode_id(conversation_id, evicted))
    .bind(conversation_id)
    .bind(text)
    .bind(embed::to_blob(&vector))
    .bind(now_stored())
    .execute(pool)
    .await
    .map_err(|e| e.to_string())?;
    let written = written.rows_affected() > 0;
    if written {
        tracing::info!("episode recorded for {conversation_id} ({} chars)", text.chars().count());
    }
    Ok(written)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn episode_id_matches_python() {
        // core.episodes.episode_id("c1", ["a", "b"])
        assert_eq!(episode_id("c1", &["a".into(), "b".into()]), "ep_590bfef0a95dfab9325843116bdce1f9");
    }

    #[test]
    fn schema_tokens_match_python() {
        // core.agents._schema_tokens over the main agent's tools
        let tools = crate::agent::tools::bound_for(&crate::agent::tools::Policy::default(), false);
        assert_eq!(schema_tokens(&tools), 952);
    }
}
