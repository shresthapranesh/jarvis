//! `run "<query>"` — the chat agent on one query, nothing kept: its own
//! history and kernel, no conversation, the last reply printed.

use std::io::{IsTerminal, Write};

use sqlx::SqlitePool;

use super::{Done, Fail, red, yellow};
use crate::agent::{Agent, workflow};
use crate::catalog;
use crate::config::Config;
use crate::llm::transcript::{Content, Part, Typed};

pub async fn run(config: &Config, pool: &SqlitePool, query: String, model: Option<String>, no_save: bool) -> Done {
    // `_run_db` hydrates the catalog; one that can't load fails there.
    let (default, _) = catalog::catalog(pool).await?.map_err(|e| Fail::Error(e.message()))?;
    let seed = catalog::seed_model();
    // `--model` defaults to the seed, which stands for "the default".
    let asked = match model {
        Some(m) if m != seed => m,
        _ => default.clone(),
    };
    let model = if catalog::is_valid_model(pool, &asked).await? {
        asked.clone()
    } else if catalog::is_valid_model(pool, &default).await? {
        default
    } else {
        seed.to_string()
    };
    if model != asked {
        // On a CLI a typo'd --model would otherwise answer from a model the
        // operator never asked for, with nothing on screen to say so.
        eprintln!("{}", yellow(&format!("Unknown model '{asked}' — running on {model} instead.")));
    }

    let mut full_query = query;
    if no_save {
        full_query.push_str(" Do not save the report to disk.");
    }

    let kernels = crate::kernels::Kernels::new(
        crate::kernels::Launch { python: config.kernel_python.clone(), dir: config.app_dir.clone(), env: vec![] },
        &config.app_dir,
        pool.clone(),
    );
    let mcp = crate::mcp::Mcp::new(pool.clone(), config.app_dir.clone());
    let agent = Agent::new(pool.clone(), Default::default(), kernels.clone(), mcp, None, config.artifacts_dir.clone());

    let spinner = std::io::stderr().is_terminal();
    if spinner {
        eprint!("Running agents...");
        let _ = std::io::stderr().flush();
    }
    let reply = workflow::run_once(&agent, &model, full_query).await;
    if spinner {
        eprint!("\r\x1b[K");
    }
    kernels.shutdown_all().await;
    match reply {
        Ok(reply) => {
            println!("{}", text(&reply.content));
            Ok(0)
        }
        Err(e) => {
            println!("{} {e}", red("Error:"));
            Ok(1)
        }
    }
}

/// The reply's text as `main.py` prints it: a string as is, or its text
/// blocks joined by spaces.
fn text(content: &Content) -> String {
    match content {
        Content::Text(s) => s.clone(),
        Content::Parts(parts) => parts
            .iter()
            .filter_map(|p| match p {
                Part::Typed(Typed::Text { text, .. }) => Some(text.as_str()),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join(" "),
    }
}
