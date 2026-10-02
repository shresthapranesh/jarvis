//! LangGraph's database (`checkpoints.db`), read-only.
//!
//! Three things in it decide whether Python has to be woken: a conversation's
//! todo list (the chat page loads it), the memory jobs' watermarks (whether a
//! consolidation pass has anything to read), and checkpoints old enough for
//! the prune to delete. The edge reads them where LangGraph wrote them, and
//! writes nothing.
//!
//! A checkpoint is msgpack. Todos are plain maps and strings in it; messages
//! are extension types the edge has no need to understand, and skips. A
//! checkpoint in any other encoding (an encrypted serializer, say) is
//! `Unreadable`, and the caller defers to Python.

use std::path::{Path, PathBuf};
use std::time::Duration;

use rmpv::Value as Mp;
use serde_json::Value;
use sqlx::SqlitePool;
use sqlx::sqlite::{SqliteConnectOptions, SqlitePoolOptions};

/// LangGraph's uuid6 epoch (1582-10-15) to the Unix one, in 100 ns ticks —
/// `checkpoint_retention._UUID_EPOCH_100NS`.
const UUID_EPOCH_100NS: u64 = 0x01B2_1DD2_1381_4000;

#[derive(Debug)]
pub struct Unreadable(pub String);

#[derive(Clone)]
pub struct Checkpoints {
    path: PathBuf,
    pool: SqlitePool,
}

/// One normalised todo — `core.schemas._normalise_todos`.
#[derive(Debug, PartialEq)]
pub struct Todo {
    pub text: String,
    pub status: &'static str,
}

impl Checkpoints {
    pub fn open(path: &Path) -> Self {
        let options = SqliteConnectOptions::new()
            .filename(path)
            // Python creates it; a missing file only means "nothing yet".
            .create_if_missing(false)
            .busy_timeout(Duration::from_secs(5));
        let pool = SqlitePoolOptions::new()
            .max_connections(2)
            .min_connections(0)
            // Don't keep LangGraph's file open between the occasional reads.
            .idle_timeout(Some(Duration::from_secs(30)))
            .connect_lazy_with(options);
        Self { path: path.to_path_buf(), pool }
    }

    /// Run a read, treating a database or table that doesn't exist yet as no rows.
    async fn read<T: Default>(&self, read: impl AsyncFnOnce(&SqlitePool) -> sqlx::Result<T>) -> sqlx::Result<T> {
        if !self.path.exists() {
            return Ok(T::default());
        }
        match read(&self.pool).await {
            Err(sqlx::Error::Database(e)) if e.message().contains("no such table") => Ok(T::default()),
            other => other,
        }
    }

    /// The todo list in a thread's newest checkpoint — the `todos` query's
    /// `aget_tuple(thread_id)`.
    pub async fn todos(&self, thread_id: &str) -> Result<Vec<Todo>, Unreadable> {
        let row: Option<(String, Vec<u8>)> = self
            .read(async |pool| {
                sqlx::query_as(
                    "SELECT type, checkpoint FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = '' \
                     ORDER BY checkpoint_id DESC LIMIT 1",
                )
                .bind(thread_id)
                .fetch_optional(pool)
                .await
            })
            .await
            .map_err(|e| Unreadable(e.to_string()))?;
        let Some((kind, blob)) = row else { return Ok(vec![]) };
        let checkpoint = match kind.as_str() {
            "msgpack" => rmpv::decode::read_value(&mut blob.as_slice()).map_err(|e| Unreadable(e.to_string()))?,
            "json" => json_to_mp(&serde_json::from_slice(&blob).map_err(|e| Unreadable(e.to_string()))?),
            "null" => return Ok(vec![]),
            other => return Err(Unreadable(format!("a {other} checkpoint"))),
        };
        let todos = get(&checkpoint, "channel_values").and_then(|cv| get(cv, "todos"));
        normalise_todos(todos)
    }

    /// A LangGraph store item's value (`store.aget(namespace, key)`).
    pub async fn store_get(&self, namespace: &str, key: &str) -> sqlx::Result<Option<Value>> {
        let raw: Option<Vec<u8>> = self
            .read(async |pool| {
                sqlx::query_scalar("SELECT value FROM store WHERE prefix = ? AND key = ?")
                    .bind(namespace)
                    .bind(key)
                    .fetch_optional(pool)
                    .await
            })
            .await?;
        Ok(raw.and_then(|r| serde_json::from_slice(&r).ok()))
    }

    /// Whether `prune_checkpoints` would delete anything, ignoring its
    /// live-thread guard (a live thread means Python is up anyway): a root
    /// checkpoint beyond a thread's newest `keep`, or any subgraph one, older
    /// than `min_age`.
    pub async fn prunable(&self, keep: usize, min_age: Duration) -> sqlx::Result<bool> {
        let cutoff = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64()
            - min_age.as_secs_f64();
        let old = |id: &str| checkpoint_timestamp(id).is_some_and(|ts| ts < cutoff);
        self.read(async |pool| {
            let roots: Vec<(String, String)> = sqlx::query_as(
                "SELECT thread_id, checkpoint_id FROM checkpoints WHERE checkpoint_ns = '' \
                 ORDER BY thread_id, checkpoint_id DESC",
            )
            .fetch_all(pool)
            .await?;
            let mut rank = 0;
            let mut thread: Option<&str> = None;
            for (thread_id, id) in &roots {
                if thread != Some(thread_id.as_str()) {
                    thread = Some(thread_id);
                    rank = 0;
                }
                rank += 1;
                if rank > keep && old(id) {
                    return Ok(true);
                }
            }
            let subgraphs: Vec<String> =
                sqlx::query_scalar("SELECT checkpoint_id FROM checkpoints WHERE checkpoint_ns <> ''")
                    .fetch_all(pool)
                    .await?;
            Ok(subgraphs.iter().any(|id| old(id)))
        })
        .await
    }
}

/// Unix seconds in a uuid6 checkpoint id — `checkpoint_timestamp`.
pub fn checkpoint_timestamp(id: &str) -> Option<f64> {
    let u = uuid::Uuid::parse_str(id).ok()?;
    if u.get_variant() != uuid::Variant::RFC4122 || u.get_version_num() != 6 {
        return None;
    }
    let (time_low, time_mid, time_hi_version, _) = u.as_fields();
    let ticks = ((time_low as u64) << 28) | ((time_mid as u64) << 12) | ((time_hi_version as u64) & 0x0FFF);
    Some((ticks as f64 - UUID_EPOCH_100NS as f64) / 1e7)
}

fn get<'a>(map: &'a Mp, key: &str) -> Option<&'a Mp> {
    map.as_map()?.iter().find(|(k, _)| k.as_str() == Some(key)).map(|(_, v)| v)
}

fn json_to_mp(v: &Value) -> Mp {
    match v {
        Value::Null => Mp::Nil,
        Value::Bool(b) => Mp::Boolean(*b),
        Value::Number(n) => n.as_i64().map(Mp::from).unwrap_or_else(|| Mp::F64(n.as_f64().unwrap_or_default())),
        Value::String(s) => Mp::from(s.as_str()),
        Value::Array(a) => Mp::Array(a.iter().map(json_to_mp).collect()),
        Value::Object(o) => Mp::Map(o.iter().map(|(k, v)| (Mp::from(k.as_str()), json_to_mp(v))).collect()),
    }
}

/// `_normalise_todos`: a list of strings or `{text, status}` maps, anything
/// else dropped; an unknown status reads as pending.
fn normalise_todos(raw: Option<&Mp>) -> Result<Vec<Todo>, Unreadable> {
    let Some(Mp::Array(items)) = raw else { return Ok(vec![]) };
    let mut out = vec![];
    for item in items {
        match item {
            Mp::String(s) => out.push(Todo { text: utf8(s)?, status: "pending" }),
            Mp::Map(_) => {
                let Some(text) = get(item, "text") else { continue };
                let status = match get(item, "status").and_then(Mp::as_str) {
                    Some("in_progress") => "in_progress",
                    Some("done") => "done",
                    _ => "pending",
                };
                out.push(Todo { text: py_str(text)?, status });
            }
            _ => {}
        }
    }
    Ok(out)
}

fn utf8(s: &rmpv::Utf8String) -> Result<String, Unreadable> {
    s.as_str().map(str::to_string).ok_or_else(|| Unreadable("a todo that isn't UTF-8".into()))
}

/// Python's `str()` of a decoded scalar.
fn py_str(v: &Mp) -> Result<String, Unreadable> {
    Ok(match v {
        Mp::String(s) => utf8(s)?,
        Mp::Nil => "None".into(),
        Mp::Boolean(b) => if *b { "True" } else { "False" }.into(),
        Mp::Integer(i) => i.to_string(),
        _ => return Err(Unreadable("a todo whose text isn't a string".into())),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uuid6_timestamps_match_python() {
        // uuid6() at 2026-10-01T12:00:00Z, as langgraph.checkpoint.base.id mints it.
        assert_eq!(checkpoint_timestamp("1f1bd8fa-0be6-6000-8000-000000000000"), Some(1_790_856_000.0));
        assert_eq!(checkpoint_timestamp("not-a-uuid"), None);
        // A v4 id has no time in it.
        assert_eq!(checkpoint_timestamp("6f1c6d3e-5b8a-4d0e-9c1f-2a3b4c5d6e7f"), None);
    }

    #[test]
    fn todos_normalise_as_python_does() {
        let raw = Mp::Array(vec![
            Mp::from("legacy"),
            Mp::Map(vec![(Mp::from("text"), Mp::from("a")), (Mp::from("status"), Mp::from("done"))]),
            Mp::Map(vec![(Mp::from("text"), Mp::from(5)), (Mp::from("status"), Mp::from("weird"))]),
            Mp::Map(vec![(Mp::from("status"), Mp::from("done"))]),
            Mp::Ext(5, vec![1, 2]),
        ]);
        let got = normalise_todos(Some(&raw)).unwrap();
        assert_eq!(
            got,
            [
                Todo { text: "legacy".into(), status: "pending" },
                Todo { text: "a".into(), status: "done" },
                Todo { text: "5".into(), status: "pending" },
            ]
        );
        assert_eq!(normalise_todos(Some(&Mp::from("x"))).unwrap(), []);
    }
}
