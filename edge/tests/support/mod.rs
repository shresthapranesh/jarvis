//! Shared pieces for the integration tests, which drive the built binary
//! from outside: a server over a throwaway `WORK_DIR` (`Edge`), and the model
//! it calls (`FakeModel`, an Ollama that answers from a script).
//!
//! `mod support;` in a test file under `edge/tests/`.
#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::State;
use axum::http::header::CONTENT_TYPE;
use axum::response::IntoResponse;
use axum::routing::post;
use axum::{Json, Router};
use serde_json::{Value, json};
use sqlx::sqlite::{SqliteConnectOptions, SqlitePool, SqliteRow};
use sqlx::{Column, Row};
use tokio::sync::Semaphore;

/// The model id a conversation on the fake model has.
pub const MODEL: &str = "ollama:fake";

/// How rows are stamped, as SQLAlchemy stored a datetime.
const NOW: &str = "2026-10-04 00:00:00.000000";

/// No bot connects and no model call leaves the machine, unless a test says so.
const SCRUBBED: [&str; 10] = [
    "ARTIFACTS_DIR",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "JARVIS_MCP_SERVERS",
    "MCP_SERVERS",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "META_API_KEY",
    "OLLAMA_HOST",
];

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

fn binary(work_dir: &Path, db: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_jarvis-edge"));
    for var in SCRUBBED {
        command.env_remove(var);
    }
    command
        .env("DATABASE_URL", format!("sqlite+aiosqlite:///{}", db.display()))
        .env("WORK_DIR", work_dir)
        .env("HOME", work_dir)
        .env("JARVIS_EDGE_LOG", "warn")
        // So the server's .env lookup can't find the repo's.
        .current_dir(work_dir);
    command
}

/// One reply of the fake model: its text, then its tool calls.
#[derive(Clone, Default)]
pub struct Reply {
    pub text: String,
    pub calls: Vec<(String, Value)>,
}

impl Reply {
    pub fn text(text: &str) -> Self {
        Reply { text: text.into(), calls: Vec::new() }
    }
}

struct Script {
    replies: Vec<Reply>,
    requests: Vec<Value>,
    /// The request that waits for `release`.
    hold: Option<usize>,
}

/// An Ollama: `/api/chat` answers the n-th request with the n-th reply of
/// its script, `/api/embed` with a vector per input.
#[derive(Clone)]
pub struct FakeModel {
    pub url: String,
    script: Arc<Mutex<Script>>,
    arrived: Arc<Semaphore>,
    released: Arc<Semaphore>,
}

impl FakeModel {
    pub async fn start(replies: Vec<Reply>) -> Self {
        Self::serve(replies, None).await
    }

    /// As `start`, with request `hold` kept waiting until `release`.
    pub async fn holding(replies: Vec<Reply>, hold: usize) -> Self {
        Self::serve(replies, Some(hold)).await
    }

    async fn serve(replies: Vec<Reply>, hold: Option<usize>) -> Self {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let fake = FakeModel {
            url: format!("http://{}", listener.local_addr().unwrap()),
            script: Arc::new(Mutex::new(Script { replies, requests: Vec::new(), hold })),
            arrived: Arc::new(Semaphore::new(0)),
            released: Arc::new(Semaphore::new(0)),
        };
        let app = Router::new().route("/api/chat", post(chat)).route("/api/embed", post(embed)).with_state(fake.clone());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        fake
    }

    /// The requests so far, in the order they came.
    pub fn requests(&self) -> Vec<Value> {
        self.script.lock().unwrap().requests.clone()
    }

    /// Until the held request has arrived.
    pub async fn held(&self) {
        let arrived = tokio::time::timeout(Duration::from_secs(30), self.arrived.acquire()).await;
        arrived.expect("the held request never arrived").unwrap().forget();
    }

    pub fn release(&self) {
        self.released.add_permits(1);
    }
}

async fn chat(State(fake): State<FakeModel>, Json(body): Json<Value>) -> impl IntoResponse {
    let (n, reply, held) = {
        let mut script = fake.script.lock().unwrap();
        let n = script.requests.len();
        script.requests.push(body);
        let reply = script.replies.get(n).cloned().unwrap_or_else(|| Reply::text("(unscripted)"));
        (n, reply, script.hold == Some(n))
    };
    if held {
        fake.arrived.add_permits(1);
        if let Ok(Ok(permit)) = tokio::time::timeout(Duration::from_secs(30), fake.released.acquire()).await {
            permit.forget();
        }
    }
    let line = |message: Value, done: bool| {
        let mut chunk = json!({"model": "fake", "created_at": "2026-10-04T00:00:00Z", "message": message, "done": done});
        if done {
            for (key, value) in [
                ("done_reason", json!("stop")),
                ("total_duration", json!(5)),
                ("load_duration", json!(1)),
                ("prompt_eval_count", json!(100 + n)),
                ("prompt_eval_duration", json!(50_000_000)),
                ("eval_count", json!(7)),
                ("eval_duration", json!(70_000_000)),
            ] {
                chunk[key] = value;
            }
        }
        format!("{chunk}\n")
    };
    let mut out = String::new();
    if !reply.text.is_empty() {
        out += &line(json!({"role": "assistant", "content": reply.text}), false);
    }
    if !reply.calls.is_empty() {
        let calls: Vec<Value> =
            reply.calls.iter().map(|(name, arguments)| json!({"function": {"name": name, "arguments": arguments}})).collect();
        out += &line(json!({"role": "assistant", "content": "", "tool_calls": calls}), false);
    }
    out += &line(json!({"role": "assistant", "content": ""}), true);
    ([(CONTENT_TYPE, "application/x-ndjson")], out)
}

async fn embed(Json(body): Json<Value>) -> Json<Value> {
    let inputs = body["input"].as_array().map_or(0, Vec::len);
    Json(json!({"embeddings": vec![vec![0.1, 0.2, 0.3]; inputs]}))
}

/// A server over a new database in a throwaway `WORK_DIR`, stopped and
/// removed when dropped.
pub struct Edge {
    pub url: String,
    pub pool: SqlitePool,
    http: reqwest::Client,
    work_dir: PathBuf,
    server: Option<Child>,
}

impl Edge {
    /// A new database, as the server's first start leaves one. Seed it, then
    /// `start`.
    pub async fn new() -> Self {
        let work_dir = std::env::temp_dir().join(format!("jarvis-edge-test-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&work_dir).unwrap();
        let db = work_dir.join("database.db");
        let swept = binary(&work_dir, &db).arg("--startup-sweep").status().unwrap();
        assert!(swept.success(), "the database could not be made");
        let pool = SqlitePool::connect_with(SqliteConnectOptions::new().filename(&db)).await.unwrap();
        Edge { url: String::new(), pool, http: reqwest::Client::new(), work_dir, server: None }
    }

    /// Start the server, running its jobs on `model`.
    pub async fn start(&mut self, model: &FakeModel) {
        let port = free_port();
        let server = binary(&self.work_dir, &self.work_dir.join("database.db"))
            .env("JARVIS_EDGE_BIND", format!("127.0.0.1:{port}"))
            .env("JARVIS_RUN_JOBS", "1")
            .env("OLLAMA_HOST", &model.url)
            .env("JARVIS_APP_DIR", Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap())
            // No browser to find.
            .env("JARVIS_BROWSER_CDP_URL", format!("http://127.0.0.1:{}", free_port()))
            .stdin(Stdio::null())
            .spawn()
            .unwrap();
        self.server = Some(server);
        self.url = format!("http://127.0.0.1:{port}");
        for _ in 0..200 {
            if tokio::net::TcpStream::connect(("127.0.0.1", port)).await.is_ok() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        panic!("the server did not start");
    }

    /// Rows are written as the ORM wrote them: every column it defaulted is given.
    pub async fn execute(&self, sql: &str, args: &[&str]) {
        let mut query = sqlx::query(sql);
        for arg in args {
            query = query.bind(*arg);
        }
        query.execute(&self.pool).await.unwrap();
    }

    /// A conversation on the fake model, as the web UI's would be stored.
    pub async fn conversation(&self, id: &str, title: &str, ephemeral: bool) {
        sqlx::query(
            "INSERT INTO conversations (id, title, model, created_at, surface, pinned, ephemeral) VALUES (?, ?, ?, ?, 'web', 0, ?)",
        )
        .bind(id)
        .bind(title)
        .bind(MODEL)
        .bind(NOW)
        .bind(ephemeral)
        .execute(&self.pool)
        .await
        .unwrap();
    }

    /// The fake model, in the catalog.
    pub async fn fake_model_listed(&self) {
        let models = json!([{"id": MODEL, "label": "Fake"}]).to_string();
        self.execute("INSERT INTO config_settings (key, value, updated_at) VALUES ('models.custom', ?, ?)", &[&models, NOW]).await;
    }

    /// The rows of a query, each a JSON object of its columns.
    pub async fn rows(&self, sql: &str) -> Vec<Value> {
        let rows = sqlx::query(sql).fetch_all(&self.pool).await.unwrap();
        rows.iter().map(object).collect()
    }

    /// A GraphQL request's whole answer, sent with `headers`.
    pub async fn graphql(&self, query: &str, variables: Value, headers: &[(&str, &str)]) -> Value {
        let mut request = self.http.post(format!("{}/graphql", self.url)).json(&json!({"query": query, "variables": variables}));
        for (name, value) in headers {
            request = request.header(*name, *value);
        }
        request.send().await.unwrap().json().await.unwrap()
    }

    /// Until `sql` finds a row.
    pub async fn until(&self, sql: &str) -> Vec<Value> {
        for _ in 0..300 {
            let rows = self.rows(sql).await;
            if !rows.is_empty() {
                return rows;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        panic!("never happened: {sql}");
    }

    /// Until every job has finished.
    pub async fn idle(&self) {
        for _ in 0..300 {
            if self.rows("SELECT id FROM jobs WHERE status IN ('pending', 'running')").await.is_empty() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        panic!("jobs never finished");
    }
}

fn object(row: &SqliteRow) -> Value {
    let mut out = serde_json::Map::new();
    for (i, column) in row.columns().iter().enumerate() {
        let value = if let Ok(v) = row.try_get::<Option<i64>, _>(i) {
            json!(v)
        } else if let Ok(v) = row.try_get::<Option<String>, _>(i) {
            json!(v)
        } else {
            json!(row.try_get::<Option<f64>, _>(i).unwrap())
        };
        out.insert(column.name().to_string(), value);
    }
    Value::Object(out)
}

impl Drop for Edge {
    fn drop(&mut self) {
        if let Some(mut server) = self.server.take() {
            let _ = server.kill();
            let _ = server.wait();
        }
        let _ = std::fs::remove_dir_all(&self.work_dir);
    }
}
