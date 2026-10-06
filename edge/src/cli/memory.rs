//! `memory show|set|reset` — the `AGENTS.md` document in `kv_store`
//! (namespace `memory`), as `main.py` reads and writes it through `KvStore`:
//! `set` stores `{"content": …}` and nothing else.
//!
//! `main.py` first copies the LangGraph store out of `checkpoints.db`, once
//! (`import_store_once`); an install where that hasn't happened yet is
//! Python's to run.

use std::io::{BufRead, Write};
use std::path::PathBuf;

use clap::Subcommand;
use serde_json::{Value, json};
use sqlx::SqlitePool;

use super::{Done, Fail, cyan, dim, ok, red, yellow};
use crate::pystr;

const NS: &str = "memory";
const KEY: &str = "AGENTS.md";

#[derive(Subcommand)]
pub enum Cmd {
    /// Print the current AGENTS.md memory.
    Show,
    /// Delete the AGENTS.md memory entry. The agent will fall back to the
    /// hardcoded system prompt.
    Reset {
        /// Skip the confirmation prompt.
        #[arg(long, short)]
        yes: bool,
    },
    /// Replace AGENTS.md memory with the contents of a local file.
    Set {
        /// Path to a markdown file whose contents replace the stored AGENTS.md.
        file: PathBuf,
    },
}

pub async fn run(pool: &SqlitePool, cmd: Cmd) -> Done {
    match cmd {
        Cmd::Show => {
            imported(pool).await?;
            let Some((value, updated_at)) = get(pool).await? else {
                println!(
                    "{} The agent will use only the hardcoded system prompt.",
                    yellow("No memory entry stored.")
                );
                return Ok(0);
            };
            let content = match value.get("content") {
                None => String::new(),
                Some(Value::String(s)) => s.clone(),
                Some(_) => return Err(Fail::Python("AGENTS.md content isn't text".into())),
            };
            println!("{}\n", dim(&format!("Updated: {} ({} chars)", py_datetime(&updated_at), pystr::len(&content))));
            println!("{}", cyan("── AGENTS.md ──"));
            println!("{content}");
            Ok(0)
        }
        Cmd::Reset { yes } => {
            if !yes && !confirm("Delete the agent memory?") {
                println!("{}", yellow("Aborted."));
                return Ok(1);
            }
            imported(pool).await?;
            if get(pool).await?.is_none() {
                println!("{}", yellow("No memory entry to delete."));
                return Ok(0);
            }
            sqlx::query("DELETE FROM kv_store WHERE namespace = ? AND key = ?").bind(NS).bind(KEY).execute(pool).await?;
            println!("{}", ok("Deleted the memory entry"));
            Ok(0)
        }
        Cmd::Set { file } => {
            if !file.exists() {
                println!("{} {}", red("File not found:"), file.display());
                return Ok(1);
            }
            let bytes = std::fs::read(&file).map_err(|e| Fail::Error(format!("{}: {e}", file.display())))?;
            // `read_text(encoding="utf-8")`: strict, with universal newlines.
            let text = String::from_utf8(bytes).map_err(|_| Fail::Python(format!("{} isn't UTF-8", file.display())))?;
            let content = text.replace("\r\n", "\n").replace('\r', "\n");
            if pystr::strip(&content).is_empty() {
                println!("{} Use 'memory reset' instead.", red("Refusing to set an empty memory entry."));
                return Ok(1);
            }
            imported(pool).await?;
            put(pool, &json!({"content": content})).await?;
            println!("{}", ok(&format!("Wrote {} chars from {} to memory", pystr::len(&content), file.display())));
            Ok(0)
        }
    }
}

/// The store already came over from `checkpoints.db`, or Python does it.
async fn imported(pool: &SqlitePool) -> Result<(), Fail> {
    let done: Option<i64> =
        sqlx::query_scalar("SELECT 1 FROM kv_store WHERE namespace = 'jarvis.migrations' AND key = 'langgraph_store'")
            .fetch_optional(pool)
            .await?;
    done.map(drop).ok_or_else(|| Fail::Python("the LangGraph store hasn't been imported yet".into()))
}

/// `KvStore.aget`: the document and its stored `updated_at`.
async fn get(pool: &SqlitePool) -> Result<Option<(Value, String)>, Fail> {
    let row: Option<(String, String)> = sqlx::query_as("SELECT value, updated_at FROM kv_store WHERE namespace = ? AND key = ?")
        .bind(NS)
        .bind(KEY)
        .fetch_optional(pool)
        .await?;
    let Some((raw, updated_at)) = row else { return Ok(None) };
    match serde_json::from_str::<Value>(&raw) {
        Ok(value @ Value::Object(_)) => Ok(Some((value, updated_at))),
        _ => Err(Fail::Python("the AGENTS.md row isn't a JSON object".into())),
    }
}

/// `KvStore.aput`: insert, or replace the value — `updated_at` moves only
/// when it changed, as the ORM only writes a changed attribute.
async fn put(pool: &SqlitePool, value: &Value) -> Result<(), Fail> {
    let text = crate::pyjson::dumps_unicode(value);
    let now = crate::gql::codec::now_stored();
    sqlx::query(
        "INSERT INTO kv_store (namespace, key, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?) \
         ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at \
         WHERE kv_store.value IS NOT excluded.value",
    )
    .bind(NS)
    .bind(KEY)
    .bind(&text)
    .bind(&now)
    .bind(&now)
    .execute(pool)
    .await?;
    Ok(())
}

/// `str(datetime)` of a stored timestamp: a space, and no fraction when it
/// is zero.
fn py_datetime(stored: &str) -> String {
    crate::gql::codec::iso_from_db(stored).0.replacen('T', " ", 1)
}

/// `typer.confirm(prompt, default=False)`.
fn confirm(prompt: &str) -> bool {
    print!("{prompt} [y/N]: ");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    if std::io::stdin().lock().read_line(&mut line).is_err() {
        return false;
    }
    matches!(line.trim().to_lowercase().as_str(), "y" | "yes")
}
