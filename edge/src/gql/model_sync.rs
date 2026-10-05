//! `modelSync` — the catalog-vs-provider drift report
//! (`server/graphql/types/model_sync.py`, a port: change both). The listing
//! and probe calls are `crate::discovery`'s. Read-only: registering what it
//! finds is `addDiscoveredModels`.
//!
//! Python runs one provider after another; here the listings run at once
//! (the report keeps Python's order) and the probes one after another per
//! provider, as a probe is a model call that costs a request.

use async_graphql::{ComplexObject, Context, Object, Result, SimpleObject};
use futures_util::future::join_all;
use sqlx::SqlitePool;

use super::defer;
use crate::catalog;
use crate::discovery::{self, Fail, Found};
use crate::llm::Endpoints;

#[derive(SimpleObject)]
pub struct DiscoveredModel {
    id: String,
    label: String,
    provider: String,
    context_window: Option<i32>,
    description: Option<String>,
    likely_chat: bool,
}

impl From<Found> for DiscoveredModel {
    fn from(f: Found) -> Self {
        DiscoveredModel {
            id: f.id,
            label: f.label,
            provider: f.provider,
            context_window: f.context_window,
            description: f.description,
            likely_chat: f.likely_chat,
        }
    }
}

#[derive(SimpleObject)]
pub struct UnreachableModel {
    id: String,
    reason: String,
}

/// A window the provider states that the catalog doesn't match: a backfill
/// (`catalogWindow` null) or a disagreement.
#[derive(SimpleObject)]
pub struct WindowFinding {
    id: String,
    label: String,
    provider: String,
    catalog_window: Option<i32>,
    provider_window: i32,
    builtin: bool,
}

#[derive(SimpleObject)]
#[graphql(complex)]
pub struct ModelSyncReport {
    provider: String,
    /// What the provider offered; 0 with `skipped` set means discovery never ran.
    offered: i32,
    /// Why the provider was skipped: every list below is empty for lack of
    /// data, not for lack of drift.
    skipped: Option<String>,
    probed: bool,
    missing: Vec<String>,
    unreachable: Vec<UnreachableModel>,
    windows: Vec<WindowFinding>,
    new_models: Vec<DiscoveredModel>,
}

#[ComplexObject]
impl ModelSyncReport {
    /// True only when discovery ran and found nothing to report.
    async fn clean(&self) -> bool {
        self.skipped.is_none()
            && self.missing.is_empty()
            && self.unreachable.is_empty()
            && self.windows.is_empty()
            && self.new_models.is_empty()
    }
}

fn skipped(provider: &str, why: String) -> ModelSyncReport {
    ModelSyncReport {
        provider: provider.into(),
        offered: 0,
        skipped: Some(why),
        probed: false,
        missing: vec![],
        unreachable: vec![],
        windows: vec![],
        new_models: vec![],
    }
}

#[derive(Default)]
pub struct ModelSyncQuery;

#[Object]
impl ModelSyncQuery {
    /// Diff the catalog against what each provider offers; `probe` also
    /// makes a one-token call per catalog model.
    async fn model_sync(
        &self,
        ctx: &Context<'_>,
        provider: Option<String>,
        #[graphql(default = false)] probe: bool,
    ) -> Result<Vec<ModelSyncReport>> {
        let pool = ctx.data::<SqlitePool>()?;
        let endpoints = catalog::endpoints(pool).await?;
        let (_, specs) = catalog::catalog(pool).await?.map_err(|e| defer(format!("models.custom: {}", e.0)))?;
        let names = |base: &[&str]| -> Vec<String> {
            let mut out: Vec<String> = base.iter().map(|p| p.to_string()).chain(endpoints.iter().map(|e| e.name.clone())).collect();
            out.sort();
            out
        };
        let discoverable = names(catalog::DISCOVERABLE_PROVIDERS);
        let targets = match provider {
            Some(p) => {
                let known = names(catalog::KNOWN_PROVIDERS);
                if !known.contains(&p) {
                    return Err(format!("Unknown provider '{p}' — must be one of: {}", known.join(", ")).into());
                }
                if !discoverable.contains(&p) {
                    return Err(format!("No discovery adapter for '{p}' — discoverable: {}", discoverable.join(", ")).into());
                }
                // `[provider] if provider else …`: an empty name was refused above.
                vec![p]
            }
            None => discoverable,
        };

        let listings = join_all(targets.iter().map(|p| discovery::discover(p, &endpoints))).await;
        let ends = Endpoints { compatible: endpoints.clone(), ..Endpoints::from_env() };
        let mut out = vec![];
        for (prov, listing) in targets.iter().zip(listings) {
            let found = match listing {
                Ok(found) => found,
                Err(Fail::Skip(why)) => {
                    out.push(skipped(prov, why));
                    continue;
                }
                Err(Fail::Defer(why)) => return Err(defer(format!("modelSync {prov}: {why}"))),
            };
            let report = discovery::build_report(prov, &specs, &found);
            let mut unreachable = vec![];
            if probe {
                for spec in specs.iter().filter(|s| &s.provider == prov) {
                    match discovery::probe(spec, &ends).await {
                        Ok(Ok(())) => {}
                        Ok(Err(reason)) => unreachable.push(UnreachableModel { id: spec.id.clone(), reason }),
                        Err(Fail::Skip(why) | Fail::Defer(why)) => return Err(defer(format!("probing {}: {why}", spec.id))),
                    }
                }
            }
            let label = |id: &str| specs.iter().find(|s| s.id == id).map_or_else(|| id.to_string(), |s| s.label.clone());
            let finding = |id: String, ours: Option<i64>, theirs: i32| WindowFinding {
                label: label(&id),
                provider: prov.clone(),
                // A catalog window is at most i32 (`catalog::py_int`).
                catalog_window: ours.and_then(|w| i32::try_from(w).ok()),
                provider_window: theirs,
                builtin: catalog::is_builtin(&id),
                id,
            };
            let windows = report
                .window_backfill
                .into_iter()
                .map(|(id, theirs)| finding(id, None, theirs))
                .chain(report.window_drift.into_iter().map(|(id, ours, theirs)| finding(id, Some(ours), theirs)))
                .collect();
            out.push(ModelSyncReport {
                provider: prov.clone(),
                offered: i32::try_from(found.len()).unwrap_or(i32::MAX),
                skipped: None,
                probed: probe,
                missing: report.missing,
                unreachable,
                windows,
                new_models: report.new.into_iter().map(DiscoveredModel::from).collect(),
            });
        }
        Ok(out)
    }
}
