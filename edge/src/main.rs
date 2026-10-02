//! jarvis-edge — the Rust front of the jarvis server.
//!
//! Phase 1 of moving off Python: the edge owns the public port, answers the
//! GraphQL operations it has been taught, and proxies everything else to the
//! Python server behind it. See `edge/README.md`.

mod catalog;
mod config;
mod db;
mod gql;
mod graphql;
mod link;
mod proxy;
mod pyjson;
mod runs;

use std::net::SocketAddr;
use std::sync::Arc;

use axum::Router;
use axum::extract::DefaultBodyLimit;
use axum::routing::{get, post};

use crate::config::Config;

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub schema: gql::EdgeSchema,
    pub owned: Arc<gql::router::Owned>,
    pub http: reqwest::Client,
    /// The live-run mirror the worker link feeds (`runs.rs`).
    pub runs: Arc<runs::Registry>,
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    // The parity tests diff this against the Python schema's SDL.
    if std::env::args().any(|a| a == "--print-schema") {
        let pool = sqlx::SqlitePool::connect_lazy("sqlite::memory:").expect("in-memory pool");
        let data = gql::EdgeData {
            artifacts_dir: Default::default(),
            documents_dir: Default::default(),
            staging_dir: Default::default(),
        };
        print!("{}", gql::build(pool, data, Default::default()).sdl());
        return;
    }

    let level = std::env::var("JARVIS_EDGE_LOG")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(tracing::Level::INFO);
    // The level applies to the edge's own logs; dependencies (hyper, sqlx)
    // stay at warn, or debug drowns in connection-pool chatter.
    use tracing_subscriber::layer::SubscriberExt;
    use tracing_subscriber::util::SubscriberInitExt;
    tracing_subscriber::registry()
        .with(tracing_subscriber::fmt::layer().with_target(false))
        .with(
            tracing_subscriber::filter::Targets::new()
                .with_target("jarvis_edge", level)
                .with_default(tracing::Level::WARN.min(level)),
        )
        .init();

    let config = match Config::from_env() {
        Ok(c) => c,
        Err(e) => {
            tracing::error!("{e}");
            std::process::exit(2);
        }
    };
    let pool = match db::pool(&config.db_path) {
        Ok(p) => p,
        Err(e) => {
            tracing::error!("database {}: {e}", config.db_path.display());
            std::process::exit(2);
        }
    };

    let data = gql::EdgeData {
        artifacts_dir: config.artifacts_dir.clone(),
        documents_dir: config.documents_dir.clone(),
        staging_dir: config.staging_dir.clone(),
    };
    let runs: Arc<runs::Registry> = Default::default();
    let schema = gql::build(pool.clone(), data, runs.clone());
    tokio::spawn(sweep_pending_runs(runs.clone(), pool));
    let owned = gql::owned_root_fields(&schema);
    let http = reqwest::Client::builder()
        // A proxy hands redirects to the client; it never follows them.
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .expect("http client");

    let mut fields: Vec<_> = owned.query.iter().chain(&owned.mutation).cloned().collect();
    fields.sort();
    tracing::info!(
        "edge on {} → backend {} · db {} · serving {}",
        config.bind,
        config.backend,
        config.db_path.display(),
        fields.join(", ")
    );

    let bind = config.bind;
    let state = AppState { config: Arc::new(config), schema, owned: Arc::new(owned), http, runs };
    let app = Router::new()
        // GET /graphql (the subscription WebSocket) falls through to the proxy.
        .route("/graphql", post(graphql::post).get(graphql::websocket).fallback(proxy::any))
        .route("/internal/worker", get(link::upgrade))
        .fallback(proxy::any)
        // Python sets no request-size limit on /graphql; neither does the edge.
        .layer(DefaultBodyLimit::disable())
        .with_state(state);

    let listener = match tokio::net::TcpListener::bind(bind).await {
        Ok(l) => l,
        Err(e) => {
            tracing::error!("bind {bind}: {e}");
            std::process::exit(2);
        }
    };
    axum::serve(listener, app.into_make_service_with_connect_info::<SocketAddr>())
        .with_graceful_shutdown(shutdown())
        .await
        .expect("server");
}

/// Runs this edge started whose job ended before any worker claimed it.
async fn sweep_pending_runs(runs: Arc<runs::Registry>, pool: sqlx::SqlitePool) {
    let mut tick = tokio::time::interval(std::time::Duration::from_secs(5));
    loop {
        tick.tick().await;
        runs.sweep_pending(&pool).await;
    }
}

async fn shutdown() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        if let Ok(mut s) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            s.recv().await;
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! {
        () = ctrl_c => {}
        () = term => {}
    }
}
