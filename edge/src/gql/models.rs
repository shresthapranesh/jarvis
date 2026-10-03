//! `models` — the catalog the model pickers list (`queries/models.py`).
//!
//! The chat page loads it, so it is served here: a page view must not be what
//! starts Python. The listing is the built-ins compiled in from the file
//! Python loads, plus the `models.custom` and `models.endpoints` settings rows
//! (`crate::catalog`).

use async_graphql::{Context, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use crate::catalog;

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

#[derive(Default)]
pub struct ModelsQuery;

#[Object]
impl ModelsQuery {
    async fn models(&self, ctx: &Context<'_>) -> Result<ModelCatalog> {
        let pool = ctx.data::<SqlitePool>()?;
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
            .map_err(|e| super::defer(format!("models.custom: {}", e.0)))?;
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
}
