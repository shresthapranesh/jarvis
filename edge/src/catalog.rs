//! The model catalog, as far as the edge needs it: which model a run should
//! use — `db.ops.resolve_model`.
//!
//! The built-in models are compiled in from the same file Python loads
//! (`core/builtin_models.json`); the runtime-added ones are a settings row
//! both processes read (`models.custom`). So the edge resolves exactly what
//! Python would, without asking it.

use std::sync::OnceLock;

use serde::Deserialize;
use serde_json::Value;
use sqlx::SqlitePool;

const BUILTIN_MODELS: &str = include_str!("../../core/builtin_models.json");

#[derive(Deserialize)]
struct Builtin {
    id: String,
}

fn builtin_ids() -> &'static [String] {
    static IDS: OnceLock<Vec<String>> = OnceLock::new();
    IDS.get_or_init(|| {
        let rows: Vec<Builtin> = serde_json::from_str(BUILTIN_MODELS).expect("core/builtin_models.json parses");
        rows.into_iter().map(|r| r.id).collect()
    })
}

/// The compile-time seed, `DEFAULT_MODEL`: the first built-in.
pub fn seed_model() -> &'static str {
    &builtin_ids()[0]
}

async fn setting(pool: &SqlitePool, key: &str) -> sqlx::Result<Option<String>> {
    sqlx::query_scalar("SELECT value FROM config_settings WHERE key = ?").bind(key).fetch_optional(pool).await
}

/// `get_custom_models`: the `models.custom` rows' ids, ignoring anything
/// that isn't a list of objects with one.
async fn custom_ids(pool: &SqlitePool) -> sqlx::Result<Vec<String>> {
    let Some(raw) = setting(pool, "models.custom").await?.filter(|r| !r.is_empty()) else {
        return Ok(vec![]);
    };
    let Ok(Value::Array(rows)) = serde_json::from_str::<Value>(&raw) else {
        return Ok(vec![]);
    };
    Ok(rows
        .iter()
        .filter_map(|row| row.get("id")?.as_str().filter(|id| !id.is_empty()).map(str::to_string))
        .collect())
}

async fn exists(pool: &SqlitePool, id: &str) -> sqlx::Result<bool> {
    Ok(builtin_ids().iter().any(|b| b == id) || custom_ids(pool).await?.iter().any(|c| c == id))
}

/// `explicit` if the catalog has it, else the operator's `default.model` if
/// the catalog has that, else the seed — a removed model degrades a run to the
/// default rather than failing it.
pub async fn resolve_model(pool: &SqlitePool, explicit: Option<&str>) -> sqlx::Result<String> {
    let explicit = explicit.filter(|m| !m.is_empty());
    if let Some(model) = explicit {
        if exists(pool, model).await? {
            return Ok(model.to_string());
        }
        tracing::warn!("model {model:?} is not in the catalog (removed?) — falling back to the default");
    }
    let default = setting(pool, "default.model").await?.filter(|d| !d.is_empty());
    let default = default.as_deref().unwrap_or(seed_model());
    if Some(default) != explicit && exists(pool, default).await? {
        return Ok(default.to_string());
    }
    if default != seed_model() {
        tracing::warn!("default.model {default:?} is not in the catalog (removed?) — falling back to {}", seed_model());
    }
    Ok(seed_model().to_string())
}
