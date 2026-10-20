//! Sorting memories into topics — the edge's own (Python had no topics).
//!
//! A memory made before topics, or written without one, has none. This
//! sweep asks the model for each one's topic (`retrieve::TOPICS`) and, while
//! it is there, rewrites "The user lives in Austin" as the fragment the
//! topic now carries: "Lives in Austin". It runs on the maintenance timer
//! while any memory is unsorted, and from `sortMemoryTopics`.

use std::collections::HashMap;

use serde_json::Value;
use sqlx::SqlitePool;

use super::ask;
use crate::agent::retrieve::{TOPICS, topic_of};

/// Memories per call, and calls per pass.
const BATCH: i64 = 60;
const MAX_BATCHES_PER_RUN: usize = 5;

const SORT_SYSTEM_PROMPT: &str = r#"You sort an AI assistant's memory items about its user into topics, and tidy
their wording.

Topics:
- profile: who the user is — name, role, expertise, where they live, background
- work: their job, employer, team, tools they use at work
- projects: things they are building or working on
- preferences: how they want things done, and how the assistant should behave
- people: the people in their life
- interests: hobbies, tastes, things they follow
- other: anything else

Each item will be shown under its topic's heading, so its subject — the user — goes without saying.
Rewrite the text as a short fragment: "The user lives in Austin" → "Lives in Austin", "User prefers
concise answers" → "Prefers concise answers", "The user's sister Maya is a nurse" → "Maya: sister, nurse".
Keep every detail and the meaning exactly; only drop the "The user ..." wording. Text already in this
style stays as it is. Name other people and things outright, never by a pronoun.

Output ONLY a JSON array with one element per item:
  {"id": "<id>", "topic": "<topic>", "text": "<the text, tidied>"}
No prose, no markdown fences."#;

/// One pass: up to `MAX_BATCHES_PER_RUN` batches of unsorted memories,
/// archived ones included (they come back sorted). The summary it logs.
pub async fn sort(pool: &SqlitePool, http: &reqwest::Client, model: Option<&str>) -> Result<String, String> {
    // The consolidation pass writes the same rows.
    let Ok(_held) = super::memory::RUN_LOCK.try_lock() else {
        return Ok("skipped: a consolidation pass is running".into());
    };
    let model = crate::catalog::resolve_model(pool, model).await.map_err(|e| e.to_string())?;
    let (mut sorted, mut reworded, mut batches) = (0, 0, 0);
    while batches < MAX_BATCHES_PER_RUN {
        let rows: Vec<(String, String, String)> = sqlx::query_as(
            "SELECT id, kind, text FROM memories WHERE topic IS NULL ORDER BY updated_at DESC LIMIT ?",
        )
        .bind(BATCH)
        .fetch_all(pool)
        .await
        .map_err(|e| e.to_string())?;
        if rows.is_empty() {
            break;
        }
        let block = rows.iter().map(|(id, kind, text)| format!("- id={id} [{kind}] {text}")).collect::<Vec<_>>().join("\n");
        let reply = ask(pool, http, &model, SORT_SYSTEM_PROMPT, format!("Memory items:\n---\n{block}\n---\n\nSort them:")).await?;
        let Some(answers) = parse(&reply) else {
            return Err("the model's reply was not a JSON array of sorted items".into());
        };
        for (id, _, stored) in &rows {
            // An item the reply left out is filed under other, or the next
            // pass would ask about it again, and again.
            let (topic, text) = match answers.get(id.as_str()) {
                Some((topic, text)) => (*topic, text.as_deref().unwrap_or(stored)),
                None => ("other", stored.as_str()),
            };
            if text != stored {
                reword(pool, http, id, text, topic).await?;
                reworded += 1;
            } else {
                sqlx::query("UPDATE memories SET topic = ? WHERE id = ?")
                    .bind(topic)
                    .bind(id)
                    .execute(pool)
                    .await
                    .map_err(|e| e.to_string())?;
            }
            sorted += 1;
        }
        batches += 1;
    }
    if batches == 0 {
        return Ok("skipped: every memory has a topic".into());
    }
    tracing::info!("memory_topics: sorted {sorted} memories, reworded {reworded}");
    Ok(format!("sorted {sorted} memories into topics, reworded {reworded}"))
}

/// The reply's items by id: (topic, tidied text when there is one). None
/// when it isn't a JSON array at all.
fn parse(reply: &str) -> Option<HashMap<String, (&'static str, Option<String>)>> {
    let (start, end) = (reply.find('[')?, reply.rfind(']')?);
    let Ok(Value::Array(items)) = serde_json::from_str(reply.get(start..=end)?) else { return None };
    Some(
        items
            .iter()
            .filter_map(|el| {
                let id = el.get("id")?.as_str()?.trim().to_string();
                let topic = el.get("topic").and_then(Value::as_str).and_then(topic_of).unwrap_or("other");
                let text = el.get("text").and_then(Value::as_str).map(str::trim).filter(|t| !t.is_empty()).map(String::from);
                Some((id, (topic, text)))
            })
            .collect(),
    )
}

/// New text and its embedding, with the topic. Not a confirmation: the
/// user said nothing new.
async fn reword(pool: &SqlitePool, http: &reqwest::Client, id: &str, text: &str, topic: &str) -> Result<(), String> {
    debug_assert!(TOPICS.contains(&topic));
    let blob = crate::agent::embed::for_storage(pool, http, text).await?;
    sqlx::query("UPDATE memories SET text = ?, embedding = ?, topic = ?, updated_at = ? WHERE id = ?")
        .bind(text)
        .bind(&blob)
        .bind(topic)
        .bind(crate::gql::codec::now_stored())
        .bind(id)
        .execute(pool)
        .await
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replies_parse_to_topics_by_id() {
        let reply = r#"```json
        [{"id": "a", "topic": "Profile", "text": " Lives in Austin "}, {"id": "b", "topic": "nonsense"},
         {"id": "c", "topic": "people", "text": ""}, {"topic": "work"}, "junk"]
        ```"#;
        let got = parse(reply).unwrap();
        assert_eq!(got["a"], ("profile", Some("Lives in Austin".into())));
        assert_eq!(got["b"], ("other", None));
        assert_eq!(got["c"], ("people", None));
        assert_eq!(got.len(), 3);
        assert!(parse("no array here").is_none());
    }
}
