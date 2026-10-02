//! The model catalog: which model a run should use — `db.ops.resolve_model`
//! — and the `models` query's listing of it.
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

/// `KNOWN_PROVIDERS`, sorted as the `models` query lists them.
pub const KNOWN_PROVIDERS: &[&str] = &["anthropic", "bedrock", "google_genai", "meta", "ollama", "openrouter"];
/// `KNOWN_PROVIDERS & model_discovery.DISCOVERABLE`, sorted.
pub const DISCOVERABLE_PROVIDERS: &[&str] = &["anthropic", "bedrock", "google_genai", "ollama", "openrouter"];

/// One catalog entry — `ModelSpec`.
#[derive(Clone, Debug, Deserialize)]
pub struct Spec {
    pub id: String,
    pub label: String,
    pub provider: String,
    #[serde(default)]
    pub context_window: Option<i64>,
}

fn builtins() -> &'static [Spec] {
    static SPECS: OnceLock<Vec<Spec>> = OnceLock::new();
    SPECS.get_or_init(|| serde_json::from_str(BUILTIN_MODELS).expect("core/builtin_models.json parses"))
}

fn builtin_ids() -> Vec<&'static str> {
    builtins().iter().map(|s| s.id.as_str()).collect()
}

pub fn is_builtin(id: &str) -> bool {
    builtins().iter().any(|s| s.id == id)
}

/// A `models.custom` row Python would reject with an error rather than skip.
#[derive(Debug)]
pub struct Malformed(pub String);

/// `load_model_catalog`: the operator's default, and the built-ins followed by
/// the custom models, deduplicated by id (first wins).
pub async fn catalog(pool: &SqlitePool) -> sqlx::Result<Result<(String, Vec<Spec>), Malformed>> {
    let default = setting(pool, "default.model").await?.filter(|d| !d.is_empty());
    let custom = match custom_specs(pool).await? {
        Ok(c) => c,
        Err(e) => return Ok(Err(e)),
    };
    let mut seen = std::collections::HashSet::new();
    let specs = builtins()
        .iter()
        .cloned()
        .chain(custom)
        .filter(|s| seen.insert(s.id.clone()))
        .collect();
    Ok(Ok((default.unwrap_or_else(|| seed_model().to_string()), specs)))
}

/// `load_custom_models(get_custom_models(...))`.
async fn custom_specs(pool: &SqlitePool) -> sqlx::Result<Result<Vec<Spec>, Malformed>> {
    let Some(raw) = setting(pool, "models.custom").await?.filter(|r| !r.is_empty()) else {
        return Ok(Ok(vec![]));
    };
    let Ok(Value::Array(rows)) = serde_json::from_str::<Value>(&raw) else {
        return Ok(Ok(vec![]));
    };
    let mut out = vec![];
    for row in rows.iter().filter_map(Value::as_object) {
        let id = match row.get("id") {
            Some(Value::String(id)) if !id.is_empty() => id.clone(),
            Some(v) if truthy(v) => return Ok(Err(Malformed(format!("model id {v}")))),
            _ => continue,
        };
        let text = |key: &str, default: &str| match row.get(key) {
            Some(Value::String(v)) if !v.is_empty() => Ok(v.clone()),
            Some(v) if truthy(v) => Err(Malformed(format!("model {key} {v}"))),
            _ => Ok(default.to_string()),
        };
        let provider = match text("provider", id.split_once(':').map_or(id.as_str(), |(p, _)| p)) {
            Ok(p) => p,
            Err(e) => return Ok(Err(e)),
        };
        let label = match text("label", &id) {
            Ok(l) => l,
            Err(e) => return Ok(Err(e)),
        };
        let window = match py_int(row.get("context_window")) {
            Ok(w) => w,
            Err(e) => return Ok(Err(e)),
        };
        out.push(Spec { id, label, provider, context_window: (window > 0).then_some(window) });
    }
    Ok(Ok(out))
}

/// Python truthiness of a JSON value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `int(r["context_window"])`, with the `except (KeyError, TypeError,
/// ValueError)` that turns a bad value into 0. A window GraphQL's 32-bit Int
/// can't carry is an error there, as is an infinite float in Python.
fn py_int(v: Option<&Value>) -> Result<i64, Malformed> {
    let n = match v {
        Some(Value::Bool(b)) => i64::from(*b),
        Some(Value::Number(n)) => match (n.as_i64(), n.as_f64()) {
            (Some(i), _) => i,
            (None, Some(f)) if f.is_finite() && f.abs() < 9e18 => f.trunc() as i64,
            _ => return Err(Malformed(format!("context_window {n}"))),
        },
        Some(Value::String(s)) => {
            let t = s.trim();
            let digits = t.strip_prefix(['+', '-']).unwrap_or(t);
            let well_formed = !digits.is_empty()
                && !digits.starts_with('_')
                && !digits.ends_with('_')
                && !digits.contains("__")
                && digits.chars().all(|c| c.is_ascii_digit() || c == '_');
            if !well_formed {
                return Ok(0);
            }
            match t.replace('_', "").parse::<i64>() {
                Ok(i) => i,
                Err(_) => return Err(Malformed(format!("context_window {s:?}"))),
            }
        }
        _ => 0,
    };
    if n > i64::from(i32::MAX) {
        return Err(Malformed(format!("context_window {n}")));
    }
    Ok(n)
}

/// The compile-time seed, `DEFAULT_MODEL`: the first built-in.
pub fn seed_model() -> &'static str {
    &builtins()[0].id
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
    Ok(builtin_ids().contains(&id) || custom_ids(pool).await?.iter().any(|c| c == id))
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
