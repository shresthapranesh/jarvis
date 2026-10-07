//! `model list|add|remove|set-default|sync` — the catalog as `main.py` edits
//! it: the `models.custom` and `default.model` rows, through the same helpers
//! the edge's model mutations use (`gql/models.rs`), and the drift report
//! through `discovery.rs`.

use clap::Subcommand;
use futures_util::future::join_all;
use sqlx::SqlitePool;

use super::{Done, Fail, cyan, dim, ok, red, table, thousands, yellow};
use crate::catalog::{self, Spec};
use crate::discovery::{self, Found};
use crate::gql::models::{add_custom, custom_rows, has_id, put_custom};
use crate::llm::Endpoints;

#[derive(Subcommand)]
pub enum Cmd {
    /// List all available models (built-in + custom).
    List,
    /// Add a model to the catalog at runtime — no code change needed.
    ///
    /// The ID must be 'provider:model_name' where provider is one of the
    /// supported backends. The model_name is passed verbatim to the
    /// provider's SDK.
    Add {
        /// Model ID, e.g. google_genai:gemini-3.5-flash
        model_id: String,
        /// Display label shown in selectors, e.g. 'Gemini 3.5 Flash'
        label: String,
        /// Provider; inferred from the ID prefix (text before ':') when omitted.
        #[arg(long)]
        provider: Option<String>,
        /// Input token limit, when you know it — sizes this model's compaction
        /// threshold. Omit rather than guess; unknown falls back to a flat 80k.
        #[arg(long)]
        context_window: Option<i64>,
    },
    /// Remove a custom model. Built-in models cannot be removed.
    Remove {
        /// Custom model ID to remove
        model_id: String,
    },
    /// Diff the catalog against what each provider actually offers.
    ///
    /// Read-only by default: discovery reports drift and supplies metadata,
    /// but what the catalog says stays with the built-ins + the custom layer.
    Sync {
        /// Provider to sync; omit to sync every discoverable provider.
        provider: Option<String>,
        /// Also issue a real one-token call per catalog model. Listing is not
        /// entitlement — this is what catches a model that is published but
        /// 404s for your account.
        #[arg(long)]
        probe: bool,
        /// Register newly discovered models into the custom-model layer (same
        /// store as 'model add').
        #[arg(long)]
        add_new: bool,
        /// Include models whose names suggest they generate speech/images/
        /// music. Shown either way; this only affects what --add-new registers.
        #[arg(long)]
        include_non_chat: bool,
    },
    /// Set the default model used when no model is specified.
    SetDefault {
        /// Model ID (copy from 'model list')
        model_id: String,
    },
}

pub async fn run(pool: &SqlitePool, cmd: Cmd) -> Done {
    // `_run_db` hydrates the catalog first, and a catalog Python can't load
    // fails there — before anything is written.
    let (default, specs) = loaded(pool).await?;
    match cmd {
        Cmd::List => {
            let rows: Vec<Vec<String>> = specs
                .iter()
                .map(|m| {
                    let source = if catalog::is_builtin(&m.id) { "built-in" } else { "custom" };
                    let marker = if m.id == default { "◀ default" } else { "" };
                    vec![m.id.clone(), m.label.clone(), source.into(), marker.into()]
                })
                .collect();
            table("Available Models", &["ID", "Label", "Source", ""], &rows);
            Ok(0)
        }
        Cmd::Add { model_id, label, provider, context_window } => add(pool, model_id, label, provider, context_window).await,
        Cmd::Remove { model_id } => {
            let mut tx = crate::db::write_tx(pool).await?;
            let rows = custom_rows(&mut tx).await?;
            let remaining: Vec<_> = rows.iter().filter(|m| !has_id(m, &model_id)).cloned().collect();
            if remaining.len() == rows.len() {
                println!("{} {model_id} (built-ins can't be removed)", yellow("Not a custom model:"));
                return Ok(0);
            }
            put_custom(&mut tx, remaining).await?;
            tx.commit().await?;
            println!("{}", ok(&format!("Removed model: {model_id}")));
            Ok(0)
        }
        Cmd::SetDefault { model_id } => {
            if !catalog::is_valid_model(pool, &model_id).await? {
                println!("{} {model_id}\nRun 'model list' to see available IDs.", red("Unknown model:"));
                return Ok(1);
            }
            let mut conn = pool.acquire().await?;
            crate::gql::settings::upsert(&mut conn, "default.model", &model_id).await?;
            println!("{}", ok(&format!("Default model set to: {model_id}")));
            Ok(0)
        }
        Cmd::Sync { provider, probe, add_new, include_non_chat } => {
            sync(pool, &specs, provider, probe, add_new, include_non_chat).await
        }
    }
}

/// The catalog as `hydrate_catalog` loads it: the default and every model.
async fn loaded(pool: &SqlitePool) -> Result<(String, Vec<Spec>), Fail> {
    catalog::catalog(pool).await?.map_err(|e| Fail::Error(e.message()))
}

/// Every provider a model id may name — `known_providers()` — or every one
/// discovery lists (`discoverable()`), endpoints included, sorted.
async fn providers(pool: &SqlitePool, base: &[&str]) -> Result<Vec<String>, Fail> {
    let mut out: Vec<String> = base.iter().map(|p| p.to_string()).collect();
    out.extend(catalog::endpoints(pool).await?.into_iter().map(|e| e.name));
    out.sort();
    out.dedup();
    Ok(out)
}

async fn add(pool: &SqlitePool, model_id: String, label: String, provider: Option<String>, window: Option<i64>) -> Done {
    // `provider or provider_from_id(model_id)`.
    let prov = provider.filter(|p| !p.is_empty()).unwrap_or_else(|| model_id.split(':').next().unwrap_or_default().to_string());
    if prov.is_empty() || !model_id.contains(':') {
        println!(
            "{} {model_id}\nExpected 'provider:model_name', e.g. google_genai:gemini-3.5-flash",
            red("Invalid model ID:")
        );
        return Ok(1);
    }
    let known = providers(pool, catalog::KNOWN_PROVIDERS).await?;
    if !known.contains(&prov) {
        println!("{} {prov}\nMust be one of: {}", red("Unsupported provider:"), known.join(", "));
        return Ok(1);
    }
    let mut tx = crate::db::write_tx(pool).await?;
    let mut rows = custom_rows(&mut tx).await?;
    add_custom(&mut rows, &model_id, &label, &prov, window).map_err(|e| Fail::Error(e.message))?;
    put_custom(&mut tx, rows).await?;
    tx.commit().await?;
    let win = match window {
        Some(w) if w != 0 => format!(" [{} ctx]", thousands(w)),
        _ => String::new(),
    };
    println!("{}", ok(&format!("Added model: {model_id} ({label}) [{prov}]{win}")));
    println!("{}", dim("Web UI picks it up on the next 'models' query; a running server validates it after that."));
    Ok(0)
}

/// One provider's findings, gathered before any is printed: a provider that
/// fails the sync must be found out before the output starts.
enum Finding {
    Skipped(String),
    Report { offered: usize, report: discovery::Report, unreachable: Vec<(String, String)> },
}

async fn sync(pool: &SqlitePool, specs: &[Spec], provider: Option<String>, probe: bool, add_new: bool, non_chat: bool) -> Done {
    let endpoints = catalog::endpoints(pool).await?;
    let discoverable = providers(pool, catalog::DISCOVERABLE_PROVIDERS).await?;
    let targets = match provider {
        Some(p) => {
            let known = providers(pool, catalog::KNOWN_PROVIDERS).await?;
            if !known.contains(&p) {
                println!("{} {p}\nMust be one of: {}", red("Unknown provider:"), known.join(", "));
                return Ok(1);
            }
            if !discoverable.contains(&p) {
                println!("{} Discoverable: {}", yellow(&format!("No discovery adapter for '{p}'.")), discoverable.join(", "));
                return Ok(1);
            }
            vec![p]
        }
        None => discoverable,
    };

    let listings = join_all(targets.iter().map(|p| discovery::discover(p, &endpoints))).await;
    let ends = Endpoints { compatible: endpoints.clone(), ..Endpoints::from_env() };
    let mut findings = vec![];
    for (prov, listing) in targets.iter().zip(listings) {
        let found: Vec<Found> = match listing {
            Ok(found) => found,
            Err(discovery::Fail::Skip(why)) => {
                findings.push(Finding::Skipped(why));
                continue;
            }
            Err(discovery::Fail::Defer(why)) => return Err(Fail::Error(format!("model sync {prov}: {why}"))),
        };
        let report = discovery::build_report(prov, specs, &found);
        let mut unreachable = vec![];
        if probe {
            for spec in specs.iter().filter(|s| &s.provider == prov) {
                match discovery::probe(spec, &ends).await {
                    Ok(Ok(())) => {}
                    Ok(Err(why)) => unreachable.push((spec.id.clone(), why)),
                    Err(discovery::Fail::Skip(why) | discovery::Fail::Defer(why)) => {
                        return Err(Fail::Error(format!("probing {}: {why}", spec.id)));
                    }
                }
            }
        }
        findings.push(Finding::Report { offered: found.len(), report, unreachable });
    }

    let mut any_drift = false;
    for (prov, finding) in targets.iter().zip(findings) {
        let (offered, report, unreachable) = match finding {
            Finding::Skipped(why) => {
                println!("\n{}  {} {why}", super::bold(prov), yellow("skipped:"));
                continue;
            }
            Finding::Report { offered, report, unreachable } => (offered, report, unreachable),
        };
        println!("\n{}  {}", super::bold(prov), dim(&format!("{offered} model(s) offered")));
        let clean = report.missing.is_empty()
            && report.new.is_empty()
            && report.window_backfill.is_empty()
            && report.window_drift.is_empty()
            && unreachable.is_empty();
        if clean {
            println!("  {}", ok("catalog matches the provider"));
            continue;
        }
        any_drift = true;
        if !report.missing.is_empty() {
            println!("  {} — in the catalog, no longer offered:", red("gone"));
            for id in &report.missing {
                println!("      {id}");
            }
        }
        if !unreachable.is_empty() {
            println!("  {} — offered but this credential cannot call it:", red("unreachable"));
            for (id, why) in &unreachable {
                println!("      {id}\n        {}", dim(why));
            }
        }
        if !report.window_backfill.is_empty() {
            println!("  {} — catalog has None:", cyan("context_window available"));
            for (id, theirs) in &report.window_backfill {
                println!("      {id}  →  {}", thousands(i64::from(*theirs)));
            }
        }
        if !report.window_drift.is_empty() {
            println!("  {}:", yellow("context_window differs"));
            for (id, ours, theirs) in &report.window_drift {
                println!("      {id}  catalog={}  provider={}", thousands(*ours), thousands(i64::from(*theirs)));
            }
        }
        if !report.new.is_empty() {
            let (chat, other): (Vec<&Found>, Vec<&Found>) = report.new.iter().partition(|m| m.likely_chat);
            println!("  {} — offered, not in the catalog ({}):", green_new(), chat.len());
            for m in &chat {
                let win = m.context_window.map_or(String::new(), |w| format!("  [{} ctx]", thousands(i64::from(w))));
                println!("      {}  {}{win}", m.id, dim(&m.label));
            }
            if !other.is_empty() {
                println!(
                    "  {}",
                    dim(&format!(
                        "non-chat (name suggests speech/image/music), not added unless --include-non-chat ({}):",
                        other.len()
                    ))
                );
                for m in &other {
                    println!("      {}", dim(&format!("{}  {}", m.id, m.label)));
                }
            }
            if add_new {
                let to_add: Vec<&Found> = if non_chat { report.new.iter().collect() } else { chat };
                let mut tx = crate::db::write_tx(pool).await?;
                let mut rows = custom_rows(&mut tx).await?;
                for m in &to_add {
                    let window = m.context_window.map(i64::from);
                    add_custom(&mut rows, &m.id, &m.label, &m.provider, window).map_err(|e| Fail::Error(e.message))?;
                }
                put_custom(&mut tx, rows).await?;
                tx.commit().await?;
                println!("  {}", ok(&format!("added {} model(s) to the custom layer", to_add.len())));
            }
        }
    }
    if any_drift && !add_new {
        println!(
            "\n{}",
            dim("Read-only. Re-run with --add-new to register the new models; 'gone' entries in BUILTIN_MODELS need a code change.")
        );
    }
    Ok(0)
}

fn green_new() -> String {
    super::green("new")
}
