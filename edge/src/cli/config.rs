//! `config set|get|list|delete` — raw `config_settings` rows, as `main.py`
//! writes them with `db/ops.py`: no validation, nothing applied (a running
//! server reads the row when it next needs it).

use clap::Subcommand;
use sqlx::SqlitePool;

use super::{Done, ok, table, yellow};

#[derive(Subcommand)]
pub enum Cmd {
    /// Set a config value.
    Set {
        /// Config key (e.g. telegram.allowed_users)
        key: String,
        /// Value to store
        value: String,
    },
    /// Get a config value.
    Get {
        /// Config key to retrieve
        key: String,
    },
    /// List all config settings.
    List,
    /// Delete a config setting.
    Delete {
        /// Config key to delete
        key: String,
    },
}

pub async fn run(pool: &SqlitePool, cmd: Cmd) -> Done {
    match cmd {
        Cmd::Set { key, value } => {
            let mut conn = pool.acquire().await?;
            crate::gql::settings::upsert(&mut conn, &key, &value).await?;
            println!("{}", ok(&format!("{key} = {value}")));
        }
        Cmd::Get { key } => match crate::catalog::setting(pool, &key).await? {
            None => println!("{} {key}", yellow("Not set:")),
            Some(value) => println!("{key} = {value}"),
        },
        Cmd::List => {
            let rows: Vec<(String, String, String)> =
                sqlx::query_as("SELECT key, value, updated_at FROM config_settings ORDER BY key").fetch_all(pool).await?;
            if rows.is_empty() {
                println!("{}", yellow("No config settings found."));
                return Ok(0);
            }
            // `updated_at.strftime("%Y-%m-%d %H:%M")`.
            let rows: Vec<Vec<String>> = rows
                .into_iter()
                .map(|(key, value, updated)| vec![key, value, updated.get(..16).unwrap_or(&updated).to_string()])
                .collect();
            table("Config Settings", &["Key", "Value", "Updated"], &rows);
        }
        Cmd::Delete { key } => {
            let deleted = sqlx::query("DELETE FROM config_settings WHERE key = ?").bind(&key).execute(pool).await?.rows_affected();
            if deleted > 0 {
                println!("{}", ok(&format!("Deleted: {key}")));
            } else {
                println!("{} {key}", yellow("Not found:"));
            }
        }
    }
    Ok(0)
}
