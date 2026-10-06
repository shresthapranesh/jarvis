//! `models` — the catalog the model pickers list (`queries/models.py`) —
//! and its writes (`mutations/models.py` with `db/ops.py`'s custom-model and
//! endpoint rows: change both).
//!
//! The chat page loads it, so it is served here: a page view must not be what
//! starts Python. The listing is the built-ins compiled in from the file
//! Python loads, plus the `models.custom` and `models.endpoints` settings rows
//! (`crate::catalog`). A write rewrites one of those rows (`default.model`
//! too), and a linked Python re-reads them into the caches it holds.

use std::sync::Arc;

use async_graphql::{Context, InputObject, Object, Result, SimpleObject};
use serde_json::{Map, Value, json};
use sqlx::{SqliteConnection, SqlitePool};

use super::defer;
use super::settings::{tell_worker, upsert};
use crate::catalog;
use crate::pyjson;
use crate::pystr;
use crate::runs::Registry;

#[derive(SimpleObject)]
pub struct ModelSpec {
    id: String,
    label: String,
    provider: String,
    builtin: bool,
    context_window: Option<i32>,
}

/// An endpoint as it may leave the server: whether it has a key, never the key.
#[derive(SimpleObject)]
pub struct ModelEndpoint {
    name: String,
    base_url: String,
    has_key: bool,
}

#[derive(SimpleObject)]
pub struct ModelCatalog {
    default: String,
    available: Vec<ModelSpec>,
    providers: Vec<String>,
    discoverable_providers: Vec<String>,
    endpoints: Vec<ModelEndpoint>,
}

/// `load_model_catalog`: what every catalog read and write returns.
async fn load(pool: &SqlitePool) -> Result<ModelCatalog> {
    let endpoints = catalog::endpoints(pool).await?;
    // `known_providers()` and `discoverable()`: an endpoint is both.
    let with_endpoints = |base: &[&str]| {
        let mut out: Vec<String> = base.iter().map(|p| p.to_string()).chain(endpoints.iter().map(|e| e.name.clone())).collect();
        out.sort();
        out
    };
    let providers = with_endpoints(catalog::KNOWN_PROVIDERS);
    let discoverable_providers = with_endpoints(catalog::DISCOVERABLE_PROVIDERS);
    let (default, specs) = catalog::catalog(pool)
        .await?
        // A row Python itself would fail on: let it give its own error.
        .map_err(|e| defer(format!("models.custom: {}", e.0)))?;
    Ok(ModelCatalog {
        default,
        available: specs
            .into_iter()
            .map(|s| ModelSpec {
                builtin: catalog::is_builtin(&s.id),
                context_window: s.context_window.and_then(|w| i32::try_from(w).ok()),
                id: s.id,
                label: s.label,
                provider: s.provider,
            })
            .collect(),
        providers,
        discoverable_providers,
        endpoints: endpoints
            .into_iter()
            .map(|e| ModelEndpoint { has_key: e.api_key.is_some(), name: e.name, base_url: e.base_url })
            .collect(),
    })
}

#[derive(Default)]
pub struct ModelsQuery;

#[Object]
impl ModelsQuery {
    async fn models(&self, ctx: &Context<'_>) -> Result<ModelCatalog> {
        load(ctx.data()?).await
    }
}

const CUSTOM: &str = "models.custom";
const ENDPOINTS: &str = "models.endpoints";
const DEFAULT: &str = "default.model";

async fn get(conn: &mut SqliteConnection, key: &str) -> sqlx::Result<Option<String>> {
    sqlx::query_scalar("SELECT value FROM config_settings WHERE key = ?").bind(key).fetch_optional(conn).await
}

/// `get_custom_models`: the row's objects, or none for anything but a list.
pub(crate) async fn custom_rows(conn: &mut SqliteConnection) -> sqlx::Result<Vec<Map<String, Value>>> {
    let raw = get(conn, CUSTOM).await?.unwrap_or_default();
    Ok(match serde_json::from_str::<Value>(&raw) {
        Ok(Value::Array(rows)) => rows.into_iter().filter_map(|r| if let Value::Object(m) = r { Some(m) } else { None }).collect(),
        _ => vec![],
    })
}

pub(crate) async fn put_custom(conn: &mut SqliteConnection, rows: Vec<Map<String, Value>>) -> sqlx::Result<()> {
    upsert(conn, CUSTOM, &pyjson::dumps(&Value::Array(rows.into_iter().map(Value::Object).collect()))).await
}

/// `_endpoint_rows`: the usable endpoints as rows; a write keeps only those.
async fn endpoint_rows(conn: &mut SqliteConnection) -> sqlx::Result<Vec<Map<String, Value>>> {
    let stored = match serde_json::from_str::<Value>(&get(conn, ENDPOINTS).await?.unwrap_or_default()) {
        Ok(Value::Array(rows)) => rows,
        _ => vec![],
    };
    Ok(catalog::parse_endpoints(&stored)
        .into_iter()
        .map(|e| {
            let mut row = Map::new();
            row.insert("name".into(), json!(e.name));
            row.insert("base_url".into(), json!(e.base_url));
            if let Some(key) = e.api_key {
                row.insert("api_key".into(), json!(key));
            }
            row
        })
        .collect())
}

pub(crate) fn has_id(row: &Map<String, Value>, id: &str) -> bool {
    row.get("id").and_then(Value::as_str) == Some(id)
}

/// Before a write: a catalog Python would fail to load is Python's to word.
async fn loadable(pool: &SqlitePool) -> Result<()> {
    catalog::catalog(pool).await?.map(|_| ()).map_err(|e| defer(format!("models.custom: {}", e.0)))
}

/// `_validated`: the normalized id and provider of a custom model.
async fn validated(pool: &SqlitePool, id: &str, provider: Option<&str>) -> Result<(String, String)> {
    let model_id = pystr::strip(id);
    let prov = match provider {
        Some(p) if !p.is_empty() => p,
        _ => model_id.split_once(':').map_or(model_id, |(p, _)| p),
    };
    let prov = pystr::strip(prov);
    if model_id.split_once(':').is_none_or(|(_, name)| name.is_empty()) {
        return Err(format!(
            "Invalid model ID '{model_id}' — expected 'provider:model_name', e.g. google_genai:gemini-3.5-flash"
        )
        .into());
    }
    let mut known: Vec<String> = catalog::KNOWN_PROVIDERS.iter().map(|p| p.to_string()).collect();
    known.extend(catalog::endpoints(pool).await?.into_iter().map(|e| e.name));
    known.sort();
    known.dedup();
    if !known.iter().any(|k| k == prov) {
        return Err(format!("Unsupported provider '{prov}' — must be one of: {}", known.join(", ")).into());
    }
    Ok((model_id.to_string(), prov.to_string()))
}

/// `int(x)` of a stored window, where Python's answer is plain; anything
/// else (it would raise mid-batch) defers.
fn py_int(v: &Value) -> Result<i64> {
    let plain = match v {
        Value::Bool(b) => Some(i64::from(*b)),
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite() && f.abs() < 9e18).map(|f| f.trunc() as i64)),
        Value::String(s) => {
            let t = pystr::strip(s);
            let digits = t.strip_prefix(['+', '-']).unwrap_or(t);
            (!digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit())).then(|| t.parse().ok()).flatten()
        }
        _ => None,
    };
    plain.ok_or_else(|| defer(format!("context_window {v}")))
}

/// `add_custom_model`: upsert by id, moved to the end. No window keeps the
/// one the existing row had.
pub(crate) fn add_custom(rows: &mut Vec<Map<String, Value>>, id: &str, label: &str, provider: &str, window: Option<i64>) -> Result<()> {
    let window = match window {
        Some(w) => Some(json!(w)),
        None => rows.iter().find(|m| has_id(m, id)).and_then(|m| m.get("context_window")).cloned(),
    };
    let mut row = Map::new();
    row.insert("id".into(), json!(id));
    row.insert("label".into(), json!(label));
    row.insert("provider".into(), json!(provider));
    if let Some(w) = window.filter(pyjson::truthy) {
        row.insert("context_window".into(), json!(py_int(&w)?));
    }
    rows.retain(|m| !has_id(m, id));
    rows.push(row);
    Ok(())
}

/// `label.strip() or model_id`.
fn label_or(label: &str, id: &str) -> String {
    let label = pystr::strip(label);
    if label.is_empty() { id.to_string() } else { label.to_string() }
}

/// `_base_url`.
fn base_url(raw: &str) -> Result<String> {
    let url = pystr::strip(raw).trim_end_matches('/');
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err(format!("Base URL must start with http:// or https:// — got '{raw}'").into());
    }
    Ok(url.to_string())
}

/// A non-blank key, stripped.
fn api_key(key: Option<&str>) -> Option<String> {
    key.map(pystr::strip).filter(|k| !k.is_empty()).map(str::to_string)
}

#[derive(InputObject)]
pub struct DiscoveredModelInput {
    id: String,
    label: String,
    provider: Option<String>,
    context_window: Option<i32>,
}

#[derive(Default)]
pub struct ModelsMutation;

impl ModelsMutation {
    /// Commit, tell a linked Python, and return the catalog as it now is
    /// (`_catalog_changed`).
    async fn changed(ctx: &Context<'_>, key: &str) -> Result<ModelCatalog> {
        tell_worker(ctx.data::<Arc<Registry>>()?, key).await;
        load(ctx.data()?).await
    }

    async fn upsert_custom(ctx: &Context<'_>, adding: bool, id: &str, label: &str, provider: Option<&str>) -> Result<ModelCatalog> {
        let pool = ctx.data::<SqlitePool>()?;
        loadable(pool).await?;
        let (model_id, prov) = validated(pool, id, provider).await?;
        if catalog::is_builtin(&model_id) {
            return Err(if adding {
                format!("'{model_id}' is a built-in model — it already exists")
            } else {
                format!("'{model_id}' is a built-in model and cannot be edited")
            }
            .into());
        }
        let mut tx = crate::db::write_tx(pool).await?;
        let mut rows = custom_rows(&mut tx).await?;
        let exists = rows.iter().any(|m| has_id(m, &model_id));
        if adding && exists {
            return Err(format!("Model '{model_id}' already exists — edit it instead").into());
        }
        if !adding && !exists {
            return Err(format!("No custom model '{model_id}'").into());
        }
        add_custom(&mut rows, &model_id, &label_or(label, &model_id), &prov, None)?;
        put_custom(&mut tx, rows).await?;
        tx.commit().await?;
        Self::changed(ctx, CUSTOM).await
    }
}

#[Object]
impl ModelsMutation {
    /// Add a custom model to the catalog. `provider` defaults to the id prefix.
    async fn add_model(&self, ctx: &Context<'_>, id: String, label: String, provider: Option<String>) -> Result<ModelCatalog> {
        Self::upsert_custom(ctx, true, &id, &label, provider.as_deref()).await
    }

    /// Update a custom model's label/provider; the id can't change.
    async fn update_model(&self, ctx: &Context<'_>, id: String, label: String, provider: Option<String>) -> Result<ModelCatalog> {
        Self::upsert_custom(ctx, false, &id, &label, provider.as_deref()).await
    }

    /// Register models found by `modelSync` — every entry validated before
    /// anything is written.
    async fn add_discovered_models(&self, ctx: &Context<'_>, models: Vec<DiscoveredModelInput>) -> Result<ModelCatalog> {
        if models.is_empty() {
            return Err("No models given".into());
        }
        let pool = ctx.data::<SqlitePool>()?;
        loadable(pool).await?;
        let mut validated_models = vec![];
        for m in &models {
            let (model_id, prov) = validated(pool, &m.id, m.provider.as_deref()).await?;
            if catalog::is_builtin(&model_id) {
                return Err(format!(
                    "'{model_id}' is a built-in model — its catalog entry, including context_window, is compiled in and needs a code change"
                )
                .into());
            }
            if m.context_window.is_some_and(|w| w <= 0) {
                return Err(format!("'{model_id}': context_window must be positive").into());
            }
            let label = label_or(&m.label, &model_id);
            validated_models.push((model_id, label, prov, m.context_window.map(i64::from)));
        }
        let mut tx = crate::db::write_tx(pool).await?;
        let mut rows = custom_rows(&mut tx).await?;
        for (model_id, label, prov, window) in &validated_models {
            add_custom(&mut rows, model_id, label, prov, *window)?;
        }
        put_custom(&mut tx, rows).await?;
        tx.commit().await?;
        Self::changed(ctx, CUSTOM).await
    }

    /// Remove a custom model; a default that named it goes back to the seed.
    async fn remove_model(&self, ctx: &Context<'_>, id: String) -> Result<ModelCatalog> {
        if catalog::is_builtin(&id) {
            return Err(format!("'{id}' is a built-in model and cannot be removed").into());
        }
        let pool = ctx.data::<SqlitePool>()?;
        loadable(pool).await?;
        let mut tx = crate::db::write_tx(pool).await?;
        let rows = custom_rows(&mut tx).await?;
        let before = rows.len();
        let remaining: Vec<_> = rows.into_iter().filter(|m| m.get("id") != Some(&json!(id))).collect();
        if remaining.len() == before {
            return Err(format!("No custom model '{id}'").into());
        }
        put_custom(&mut tx, remaining).await?;
        let default = get(&mut tx, DEFAULT).await?.filter(|d| !d.is_empty());
        if default.as_deref().unwrap_or(catalog::seed_model()) == id {
            upsert(&mut tx, DEFAULT, catalog::seed_model()).await?;
        }
        tx.commit().await?;
        Self::changed(ctx, CUSTOM).await
    }

    /// Persist the default model used when a request names none.
    async fn set_default_model(&self, ctx: &Context<'_>, id: String) -> Result<ModelCatalog> {
        let pool = ctx.data::<SqlitePool>()?;
        loadable(pool).await?;
        if !catalog::is_valid_model(pool, &id).await? {
            return Err(format!("Unknown model '{id}'").into());
        }
        let mut tx = crate::db::write_tx(pool).await?;
        upsert(&mut tx, DEFAULT, &id).await?;
        tx.commit().await?;
        Self::changed(ctx, DEFAULT).await
    }

    /// Name an OpenAI-compatible server.
    async fn add_endpoint(&self, ctx: &Context<'_>, name: String, base_url: String, api_key: Option<String>) -> Result<ModelCatalog> {
        let name = pystr::strip(&name);
        if let Some(error) = catalog::endpoint_name_error(name) {
            return Err(error.into());
        }
        let pool = ctx.data::<SqlitePool>()?;
        loadable(pool).await?;
        let mut tx = crate::db::write_tx(pool).await?;
        let mut rows = endpoint_rows(&mut tx).await?;
        if rows.iter().any(|r| r.get("name") == Some(&json!(name))) {
            return Err(format!("Endpoint '{name}' already exists — edit it instead").into());
        }
        let mut row = Map::new();
        row.insert("name".into(), json!(name));
        row.insert("base_url".into(), json!(self::base_url(&base_url)?));
        if let Some(key) = self::api_key(api_key.as_deref()) {
            row.insert("api_key".into(), json!(key));
        }
        rows.push(row);
        put_endpoints(&mut tx, rows).await?;
        tx.commit().await?;
        Self::changed(ctx, ENDPOINTS).await
    }

    /// Change an endpoint's URL or key; an absent key keeps the stored one.
    async fn update_endpoint(
        &self,
        ctx: &Context<'_>,
        name: String,
        base_url: String,
        api_key: Option<String>,
        #[graphql(default = false)] clear_key: bool,
    ) -> Result<ModelCatalog> {
        let pool = ctx.data::<SqlitePool>()?;
        loadable(pool).await?;
        let mut tx = crate::db::write_tx(pool).await?;
        let mut rows = endpoint_rows(&mut tx).await?;
        let Some(row) = rows.iter_mut().find(|r| r.get("name") == Some(&json!(name))) else {
            return Err(format!("No endpoint '{name}'").into());
        };
        row.insert("base_url".into(), json!(self::base_url(&base_url)?));
        if clear_key {
            row.shift_remove("api_key");
        } else if let Some(key) = self::api_key(api_key.as_deref()) {
            row.insert("api_key".into(), json!(key));
        }
        put_endpoints(&mut tx, rows).await?;
        tx.commit().await?;
        Self::changed(ctx, ENDPOINTS).await
    }

    /// Remove an endpoint no custom model uses.
    async fn remove_endpoint(&self, ctx: &Context<'_>, name: String) -> Result<ModelCatalog> {
        let pool = ctx.data::<SqlitePool>()?;
        loadable(pool).await?;
        let mut tx = crate::db::write_tx(pool).await?;
        let rows = endpoint_rows(&mut tx).await?;
        if !rows.iter().any(|r| r.get("name") == Some(&json!(name))) {
            return Err(format!("No endpoint '{name}'").into());
        }
        let using: Vec<String> = custom_rows(&mut tx)
            .await?
            .iter()
            .filter(|m| provider_of(m) == name)
            .map(|m| pyjson::py_str(m.get("id").unwrap_or(&Value::Null)))
            .collect();
        if !using.is_empty() {
            return Err(format!("Endpoint '{name}' is used by {} — remove those first", using.join(", ")).into());
        }
        put_endpoints(&mut tx, rows.into_iter().filter(|r| r.get("name") != Some(&json!(name))).collect()).await?;
        tx.commit().await?;
        Self::changed(ctx, ENDPOINTS).await
    }
}

/// `m.get("provider") or provider_from_id(str(m.get("id") or ""))`.
fn provider_of(m: &Map<String, Value>) -> String {
    match m.get("provider") {
        Some(p) if pyjson::truthy(p) => pyjson::py_str(p),
        _ => {
            let id = m.get("id").filter(|v| pyjson::truthy(v)).map(pyjson::py_str).unwrap_or_default();
            id.split_once(':').map_or(id.clone(), |(p, _)| p.to_string())
        }
    }
}

async fn put_endpoints(conn: &mut SqliteConnection, rows: Vec<Map<String, Value>>) -> sqlx::Result<()> {
    upsert(conn, ENDPOINTS, &pyjson::dumps(&Value::Array(rows.into_iter().map(Value::Object).collect()))).await
}
