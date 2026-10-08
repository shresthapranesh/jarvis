//! The `config_settings` table as an administrable surface —
//! `core/settings_admin.py` (the `KNOWN_SETTINGS` registry, `validate`,
//! `redact`, `apply_setting`'s notes), `server/graphql/types/setting.py`,
//! `queries/setting.py` and `mutations/setting.py`. Change both.
//!
//! The edge writes and applies every key: an `mcp.*` key reconnects the MCP
//! servers (`crate::mcp`), and a linked Python is told about the keys it
//! caches (the embedding model, the catalog, the tool policy).


use async_graphql::{Context, ID, Object, Result, SimpleObject};
use serde_json::Value;
use sqlx::SqlitePool;

use super::codec::{DateTime, now_stored};
use crate::pystr;

/// What a key is for and who owns it — `SettingSpec`.
struct Spec {
    key: &'static str,
    label: &'static str,
    description: &'static str,
    /// Non-empty when another Settings tab owns the key.
    managed_by: &'static str,
    /// text | csv | json | select — a hint for the input widget.
    kind: &'static str,
    choices: &'static [&'static str],
    placeholder: &'static str,
    restart_required: bool,
}

/// `KNOWN_SETTINGS`, in its order (the listing's order).
const KNOWN: &[Spec] = &[
    Spec {
        key: "default.model",
        label: "Default model",
        description: "Model used when a run doesn't name one. Set it from the Models tab to pick from the catalog.",
        managed_by: "Models",
        kind: "text",
        choices: &[],
        placeholder: "",
        restart_required: false,
    },
    Spec {
        key: "embedding.model",
        label: "Embedding model",
        description: "Gemini embedding model for vector memory, skills and episodes. Needs GOOGLE_API_KEY. Applied immediately; already-embedded content keeps its old vectors.",
        managed_by: "",
        kind: "text",
        choices: &[],
        placeholder: "models/gemini-embedding-001",
        restart_required: false,
    },
    Spec {
        key: "scheduler.timezone",
        label: "Scheduler timezone",
        description: "Zone cron expressions are interpreted in. Falls back to JARVIS_TIMEZONE, then the machine's local zone.",
        managed_by: "",
        kind: "text",
        choices: &[],
        placeholder: "America/New_York",
        restart_required: true,
    },
    Spec {
        key: "telegram.allowed_users",
        label: "Telegram allowlist",
        description: "Comma-separated Telegram user IDs that may talk to the bot. Empty rejects everyone. Get an id from @userinfobot.",
        managed_by: "",
        kind: "csv",
        choices: &[],
        placeholder: "123456789,987654321",
        restart_required: false,
    },
    Spec {
        key: "discord.allowed_users",
        label: "Discord allowlist",
        description: "Comma-separated Discord user IDs that may talk to the bot. Empty rejects everyone. Enable Developer Mode → right-click a user → Copy User ID.",
        managed_by: "",
        kind: "csv",
        choices: &[],
        placeholder: "123456789,987654321",
        restart_required: false,
    },
    Spec {
        key: "approval.required_actions",
        label: "Actions requiring approval",
        description: "Which of the agent's destructive writes must be approved by a human first. `all`, `none`, or a comma-separated list of action names. Unset means none.",
        managed_by: "",
        kind: "csv",
        choices: &[],
        placeholder: "all",
        restart_required: false,
    },
    Spec {
        key: "browser.cdp_url",
        label: "Browser CDP endpoint",
        description: "DevTools endpoint `read(url, browser=True)` attaches to. A browser is launched here on demand if nothing is listening; point it elsewhere to use one on another machine.",
        managed_by: "",
        kind: "text",
        choices: &[],
        placeholder: "http://127.0.0.1:9222",
        restart_required: false,
    },
    Spec {
        key: "browser.executable",
        label: "Browser executable",
        description: "Path to the Chromium-based browser to launch — Chrome, Brave, Edge, Chromium, Vivaldi. Unset probes the usual install locations. Only the launch path uses this; attaching works with whatever is already running.",
        managed_by: "",
        kind: "text",
        choices: &[],
        placeholder: "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        restart_required: false,
    },
    Spec {
        key: "browser.profile_dir",
        label: "Browser profile directory",
        description: "Dedicated user-data-dir for that browser (default: <work_dir>/browser-profile). Log in here once and the cookies persist across runs. Never point this at your everyday profile: Chromium refuses remote debugging on the default profile, and a separate one keeps your other sessions out of the agent's reach.",
        managed_by: "",
        kind: "text",
        choices: &[],
        placeholder: "~/.jarvis/browser-profile",
        restart_required: false,
    },
    Spec {
        key: "models.custom",
        label: "Custom models",
        description: "Models added at runtime, as JSON.",
        managed_by: "Models",
        kind: "json",
        choices: &[],
        placeholder: "",
        restart_required: false,
    },
    Spec {
        key: "models.endpoints",
        label: "Model endpoints",
        description: "OpenAI-compatible servers, as JSON. API keys are hidden here.",
        managed_by: "Models",
        kind: "json",
        choices: &[],
        placeholder: "",
        restart_required: false,
    },
    Spec {
        key: "tools.policy",
        label: "Tool policy",
        description: "Per-tool enabled / requires-approval overrides, as JSON. Only non-default entries are stored.",
        managed_by: "Tools",
        kind: "json",
        choices: &[],
        placeholder: "",
        restart_required: false,
    },
    Spec {
        key: "mcp.servers",
        label: "MCP servers",
        description: "MCP server connection configs added through the UI, as JSON. Merged over env and mcp.json.",
        managed_by: "MCP servers",
        kind: "json",
        choices: &[],
        placeholder: "",
        restart_required: false,
    },
    Spec {
        key: "mcp.load_modes",
        label: "MCP load modes",
        description: "Per-server always/lazy overrides, as JSON. Kept apart from the configs so flipping a mode doesn't snapshot an env-defined server into the DB.",
        managed_by: "MCP servers",
        kind: "json",
        choices: &[],
        placeholder: "",
        restart_required: false,
    },
    Spec {
        key: "migration.artifact_message_ids",
        label: "Artifact attribution migration",
        description: "A one-time marker written by the server after backfilling artifact→message links. Nothing reads it but the migration.",
        managed_by: "the server",
        kind: "text",
        choices: &[],
        placeholder: "",
        restart_required: false,
    },
    Spec {
        key: "mcp.default_load_mode",
        label: "MCP default load mode",
        description: "Mode for servers that don't declare one. `always` binds their schemas to every LLM call; `lazy` keeps them out of the prompt until the agent asks.",
        managed_by: "MCP servers",
        kind: "select",
        choices: &["always", "lazy"],
        placeholder: "",
        restart_required: false,
    },
];

fn spec_for(key: &str) -> Option<&'static Spec> {
    KNOWN.iter().find(|s| s.key == key)
}

fn is_managed(key: &str) -> bool {
    spec_for(key).is_some_and(|s| !s.managed_by.is_empty())
}

/// One row of `config_settings`, with its key's guidance. Not a Node; the
/// plain `id` is `setting:{key}` so Relay normalizes a write's response.
#[derive(SimpleObject, Clone)]
pub struct Setting {
    id: ID,
    key: String,
    value: String,
    updated_at: Option<DateTime>,
    /// Whether a row actually exists.
    is_set: bool,
    label: String,
    description: String,
    managed_by: String,
    kind: String,
    choices: Vec<String>,
    placeholder: String,
    restart_required: bool,
    /// False for a free-form key the registry doesn't know.
    known: bool,
}

impl Setting {
    fn from_parts(key: &str, value: &str, updated_at: Option<DateTime>, is_set: bool) -> Self {
        let spec = spec_for(key);
        Setting {
            id: ID(format!("setting:{key}")),
            key: key.to_string(),
            value: redact(key, value),
            updated_at,
            is_set,
            label: spec.map_or(key, |s| s.label).to_string(),
            description: spec.map_or("", |s| s.description).to_string(),
            managed_by: spec.map_or("", |s| s.managed_by).to_string(),
            kind: spec.map_or("text", |s| s.kind).to_string(),
            choices: spec.map_or(vec![], |s| s.choices.iter().map(|c| c.to_string()).collect()),
            placeholder: spec.map_or("", |s| s.placeholder).to_string(),
            restart_required: spec.is_some_and(|s| s.restart_required),
            known: spec.is_some(),
        }
    }

    fn unset(key: &str) -> Self {
        Self::from_parts(key, "", None, false)
    }
}

#[derive(sqlx::FromRow)]
struct Row {
    key: String,
    value: String,
    updated_at: Option<DateTime>,
}

async fn rows(pool: &SqlitePool) -> Result<Vec<Row>> {
    Ok(sqlx::query_as("SELECT key, value, updated_at FROM config_settings ORDER BY key").fetch_all(pool).await?)
}

/// `merge_inventory`: stored rows ∪ every known key — known keys first in
/// registry order, then free-form ones by key. `written` is the key a
/// mutation just stamped: Python hands back that row's in-memory, UTC-aware
/// `updated_at` rather than the stored naive one.
fn inventory(rows: Vec<Row>, written: Option<&str>) -> Vec<Setting> {
    let mut out: Vec<Setting> = rows
        .into_iter()
        .map(|r| {
            let at = if written == Some(r.key.as_str()) { r.updated_at.map(|at| at.utc()) } else { r.updated_at };
            Setting::from_parts(&r.key, &r.value, at, true)
        })
        .collect();
    for spec in KNOWN {
        if !out.iter().any(|s| s.key == spec.key) {
            out.push(Setting::unset(spec.key));
        }
    }
    let rank = |key: &str| KNOWN.iter().position(|s| s.key == key).unwrap_or(KNOWN.len());
    out.sort_by(|a, b| (rank(&a.key), &a.key).cmp(&(rank(&b.key), &b.key)));
    out
}

/// `redact`: endpoint API keys never leave the server.
fn redact(key: &str, value: &str) -> String {
    if key != "models.endpoints" || value.is_empty() {
        return value.to_string();
    }
    let Ok(Value::Array(rows)) = serde_json::from_str::<Value>(value) else {
        return value.to_string();
    };
    let rows = rows
        .into_iter()
        .map(|r| match r {
            Value::Object(mut m) if m.get("api_key").is_some_and(crate::pyjson::truthy) => {
                m.insert("api_key".into(), Value::String("••••".into()));
                Value::Object(m)
            }
            other => other,
        })
        .collect();
    crate::pyjson::dumps(&Value::Array(rows))
}

/// `validate`.
fn validate(key: &str, value: &str) -> Result<()> {
    if key.is_empty() {
        return Err("key is empty".into());
    }
    if key.chars().any(pystr::is_space) {
        return Err(format!("key {} contains whitespace", crate::pyjson::repr_str(key)).into());
    }
    let Some(spec) = spec_for(key) else { return Ok(()) };
    let value = pystr::strip(value);
    if spec.kind == "json" && !value.is_empty() {
        if let Err(e) = serde_json::from_str::<serde::de::IgnoredAny>(value) {
            return Err(format!("{key} must be valid JSON: {e}").into());
        }
    }
    if spec.kind == "select" && !value.is_empty() && !spec.choices.contains(&value) {
        return Err(format!("{key} must be one of: {}", spec.choices.join(", ")).into());
    }
    Ok(())
}

/// The managed-key refusal.
fn owner_check(key: &str, allow_managed: bool) -> Result<()> {
    if is_managed(key) && !allow_managed {
        return Err(format!(
            "{key} is managed by another Settings tab; edit it there, or pass allowManaged: true to override."
        )
        .into());
    }
    Ok(())
}

/// `apply_setting`. Everything is read from the table at the point of use,
/// except the MCP servers, which are reconnected.
async fn apply(ctx: &Context<'_>, key: &str) -> Result<String> {
    if key.starts_with("mcp.") {
        let mcp = &ctx.data::<super::EdgeData>()?.mcp;
        let merged = mcp.reload().await;
        return Ok(format!("Applied. Reconnected {} MCP server(s).", merged.connections.len()));
    }
    match key {
        "embedding.model" => Ok("Applied. New embeddings use this model; existing vectors are unchanged.".into()),
        "tools.policy" => Ok("Applied. New runs use the updated policy.".into()),
        // The scheduler's zone is read once at startup.
        "scheduler.timezone" => Ok("Saved. Takes effect when the server restarts.".into()),
        _ => Ok("Applied.".into()),
    }
}

/// `set_setting`: insert, or replace the value and stamp `updated_at`.
pub async fn upsert(tx: &mut sqlx::SqliteConnection, key: &str, value: &str) -> sqlx::Result<()> {
    sqlx::query(
        "INSERT INTO config_settings (key, value, updated_at) VALUES (?, ?, ?) \
         ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
    )
    .bind(key)
    .bind(value)
    .bind(now_stored())
    .execute(tx)
    .await?;
    Ok(())
}

/// The written row, the refreshed list, and what applying it actually did.
#[derive(SimpleObject)]
pub struct SettingWriteResult {
    setting: Setting,
    settings: Vec<Setting>,
    note: String,
}

async fn result(pool: &SqlitePool, key: &str, written: bool, note: String) -> Result<SettingWriteResult> {
    let settings = inventory(rows(pool).await?, written.then_some(key));
    let setting = settings.iter().find(|s| s.key == key).cloned().unwrap_or_else(|| Setting::unset(key));
    Ok(SettingWriteResult { setting, settings, note })
}

#[derive(Default)]
pub struct SettingQuery;

#[Object]
impl SettingQuery {
    /// Every stored setting, plus the known keys that aren't set yet.
    async fn settings(&self, ctx: &Context<'_>) -> Result<Vec<Setting>> {
        Ok(inventory(rows(ctx.data()?).await?, None))
    }

    /// One key; an `isSet: false` row rather than null when absent.
    async fn setting(&self, ctx: &Context<'_>, key: String) -> Result<Setting> {
        let row: Option<Row> = sqlx::query_as("SELECT key, value, updated_at FROM config_settings WHERE key = ?")
            .bind(&key)
            .fetch_optional(ctx.data::<SqlitePool>()?)
            .await?;
        Ok(match row {
            Some(r) => Setting::from_parts(&r.key, &r.value, r.updated_at, true),
            None => Setting::unset(&key),
        })
    }
}

#[derive(Default)]
pub struct SettingMutation;

#[Object]
impl SettingMutation {
    /// Write one config key. Keys owned by a dedicated tab are refused
    /// unless `allowManaged`.
    async fn set_setting(
        &self,
        ctx: &Context<'_>,
        key: String,
        value: String,
        #[graphql(default = false)] allow_managed: bool,
    ) -> Result<SettingWriteResult> {
        let key = pystr::strip(&key);
        validate(key, &value)?;
        owner_check(key, allow_managed)?;
        let pool = ctx.data::<SqlitePool>()?;
        let mut tx = crate::db::write_tx(pool).await?;
        upsert(&mut tx, key, &value).await?;
        tx.commit().await?;
        let note = apply(ctx, key).await?;
        result(pool, key, true, note).await
    }

    /// Remove one config key, reverting it to its built-in default.
    async fn delete_setting(
        &self,
        ctx: &Context<'_>,
        key: String,
        #[graphql(default = false)] allow_managed: bool,
    ) -> Result<SettingWriteResult> {
        let key = pystr::strip(&key);
        owner_check(key, allow_managed)?;
        let pool = ctx.data::<SqlitePool>()?;
        let deleted = sqlx::query("DELETE FROM config_settings WHERE key = ?").bind(key).execute(pool).await?.rows_affected() > 0;
        if !deleted {
            return result(pool, key, false, format!("Not set: {key}")).await;
        }
        let note = apply(ctx, key).await?;
        result(pool, key, false, format!("Deleted. {note}")).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redact_hides_only_set_keys_in_order() {
        let raw = r#"[{"name": "a", "api_key": "sk-1", "base_url": "u"}, {"name": "b", "api_key": ""}, "x"]"#;
        assert_eq!(
            redact("models.endpoints", raw),
            r#"[{"name": "a", "api_key": "\u2022\u2022\u2022\u2022", "base_url": "u"}, {"name": "b", "api_key": ""}, "x"]"#
        );
        assert_eq!(redact("models.endpoints", "{not json"), "{not json");
        assert_eq!(redact("models.custom", raw), raw);
    }

    #[test]
    fn validate_refuses_what_it_should() {
        assert_eq!(validate("", "v").unwrap_err().message, "key is empty");
        assert_eq!(validate("a b", "v").unwrap_err().message, "key 'a b' contains whitespace");
        assert_eq!(
            validate("mcp.default_load_mode", " sometimes ").unwrap_err().message,
            "mcp.default_load_mode must be one of: always, lazy"
        );
        assert!(validate("mcp.default_load_mode", " lazy ").is_ok());
        assert!(validate("tools.policy", "{}").is_ok());
        assert!(validate("tools.policy", "{").unwrap_err().message.starts_with("tools.policy must be valid JSON: "));
        assert!(validate("free.form", "{").is_ok());
    }
}
