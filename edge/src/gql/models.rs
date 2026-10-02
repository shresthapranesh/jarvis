//! `models` — the catalog the model pickers list (`queries/models.py`).
//!
//! The chat page loads it, so it is served here: a page view must not be what
//! starts Python. The listing is the built-ins compiled in from the file
//! Python loads, plus the `models.custom` settings row (`crate::catalog`).

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

#[derive(SimpleObject)]
pub struct ModelCatalog {
    default: String,
    available: Vec<ModelSpec>,
    providers: Vec<String>,
    discoverable_providers: Vec<String>,
}

#[derive(Default)]
pub struct ModelsQuery;

#[Object]
impl ModelsQuery {
    async fn models(&self, ctx: &Context<'_>) -> Result<ModelCatalog> {
        let (default, specs) = catalog::catalog(ctx.data::<SqlitePool>()?)
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
            providers: catalog::KNOWN_PROVIDERS.iter().map(|p| p.to_string()).collect(),
            discoverable_providers: catalog::DISCOVERABLE_PROVIDERS.iter().map(|p| p.to_string()).collect(),
        })
    }
}
