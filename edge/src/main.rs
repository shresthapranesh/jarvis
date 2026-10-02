//! jarvis-edge — the Rust front of the jarvis server.
//!
//! Phase 1 of moving off Python: the edge owns the public port, answers the
//! GraphQL operations it has been taught, and proxies everything else to the
//! Python server behind it. See `edge/README.md`.

mod catalog;
mod checkpoints;
mod config;
mod cron;
mod db;
mod gql;
mod graphql;
mod jobs;
mod link;
mod proxy;
mod pyjson;
mod runs;
mod schedule;
mod supervisor;

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
    /// Every timer, and the board dispatcher (`schedule.rs`).
    pub scheduler: Arc<schedule::Scheduler>,
    /// The Python worker's process, when the edge owns it (`supervisor.rs`).
    pub supervisor: Arc<supervisor::Supervisor>,
}

impl AppState {
    /// Whether the run mirror is the truth, so the edge answers what reads,
    /// steers or starts a run: a worker is linked, or the edge owns the worker
    /// (and none being up means none is running anything).
    pub fn runs_here(&self) -> bool {
        self.supervisor.supervised() || self.runs.link_up()
    }
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
            tz: chrono_tz::Tz::UTC,
            checkpoints: checkpoints::Checkpoints::open("".as_ref()),
            http: reqwest::Client::new(),
        };
        print!("{}", gql::build(pool, data, Default::default()).sdl());
        return;
    }

    // The schedule tests diff this against APScheduler: one JSON case per
    // stdin line, `{"expr", "tz", "now", "count"}` → the next `count` fire
    // times, each computed from the one before, or `null` if it won't parse.
    if std::env::args().any(|a| a == "--cron-next") {
        cron_next();
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

    let mut config = match Config::from_env() {
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

    let tz = schedule::resolve_tz(&pool).await;

    // The maintenance tests diff this against the Python sweeps' own checks:
    // whether each would find work in this database, as one JSON line.
    if std::env::args().any(|a| a == "--maintenance-due") {
        let checkpoints = checkpoints::Checkpoints::open(&config.checkpoints_db);
        let s = schedule::Scheduler::new(pool, Default::default(), tz, config.staging_dir.clone(), checkpoints);
        let mut out = serde_json::Map::new();
        for task in ["memory_consolidation", "project_memory", "checkpoint_prune"] {
            let due = s.maintenance_due(task).await.map_or_else(|e| e.to_string().into(), serde_json::Value::from);
            out.insert(task.into(), due);
        }
        println!("{}", serde_json::Value::Object(out));
        return;
    }
    let http = reqwest::Client::builder()
        // A proxy hands redirects to the client; it never follows them.
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .expect("http client");
    let checkpoints = checkpoints::Checkpoints::open(&config.checkpoints_db);
    let data = gql::EdgeData {
        artifacts_dir: config.artifacts_dir.clone(),
        documents_dir: config.documents_dir.clone(),
        staging_dir: config.staging_dir.clone(),
        tz,
        checkpoints: checkpoints.clone(),
        http: http.clone(),
    };
    let runs: Arc<runs::Registry> = Default::default();
    let schema = gql::build(pool.clone(), data, runs.clone());
    let scheduler =
        schedule::Scheduler::new(pool.clone(), runs.clone(), tz, config.staging_dir.clone(), checkpoints);
    tokio::spawn(scheduler.clone().run());
    tokio::spawn(sweep_pending_runs(runs.clone(), pool.clone()));
    let supervisor = supervisor::Supervisor::new(
        config.worker.take(),
        config.backend.clone(),
        config.backend_port().to_string(),
        pool,
        runs.clone(),
        http.clone(),
    );
    tokio::spawn(supervisor.clone().run());
    let owned = gql::owned_root_fields(&schema);

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
    if let Some(dir) = &config.static_dir {
        tracing::info!("serving the SPA from {}", dir.display());
    }
    let state = AppState {
        config: Arc::new(config),
        schema,
        owned: Arc::new(owned),
        http,
        runs,
        scheduler,
        supervisor: supervisor.clone(),
    };
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
    // Python doesn't outlive the edge that started it.
    supervisor.shutdown().await;
}

/// Runs this edge started whose job ended before any worker claimed it.
async fn sweep_pending_runs(runs: Arc<runs::Registry>, pool: sqlx::SqlitePool) {
    let mut tick = tokio::time::interval(std::time::Duration::from_secs(5));
    loop {
        tick.tick().await;
        runs.sweep_pending(&pool).await;
    }
}

fn cron_next() {
    use std::io::BufRead;
    for line in std::io::stdin().lock().lines() {
        let case: serde_json::Value = serde_json::from_str(&line.expect("stdin")).expect("a JSON case");
        let tz: chrono_tz::Tz = case["tz"].as_str().expect("tz").parse().expect("a zone");
        let mut now = chrono::DateTime::parse_from_rfc3339(case["now"].as_str().expect("now")).expect("an instant").to_utc();
        let out = match cron::Trigger::parse(case["expr"].as_str().expect("expr"), tz) {
            Err(_) => serde_json::Value::Null,
            Ok(trigger) => {
                let mut fires = vec![];
                let mut prev = None;
                for _ in 0..case["count"].as_u64().unwrap_or(1) {
                    let Some(next) = trigger.next_fire(prev.as_ref(), now) else { break };
                    fires.push(serde_json::Value::String(next.isoformat()));
                    now = next.to_utc();
                    prev = Some(next);
                }
                serde_json::Value::Array(fires)
            }
        };
        println!("{out}");
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
