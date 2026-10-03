//! The LLM layer: a model call from a thread's transcript, written for jarvis
//! rather than borrowed from a framework — the request shape is where the
//! requirements live (byte-stable cache prefixes, thought signatures, one
//! system prompt), so no library sits above our request builder.
//!
//! - `transcript` — the v1 record Python and Rust both read and write.
//! - `shape` — strip thinking, repair orphaned calls, lay the prompt out.
//! - `google`, `ollama` — one module per wire format: render a [`Prompt`],
//!   stream the reply, build the assistant record.
//!
//! [`complete`] is the one entry point.

pub mod google;
mod lines;
pub mod ollama;
pub mod shape;
pub mod transcript;

use std::collections::HashMap;

use serde::Deserialize;
use serde_json::Value;

pub use shape::Prompt;
pub use transcript::Message;

/// A tool the model may call, as OpenAI's function spec spells it.
#[derive(Clone, Debug, Deserialize)]
pub struct Tool {
    pub name: String,
    #[serde(default)]
    pub description: String,
    /// JSON Schema for the arguments.
    pub parameters: Value,
}

/// Media bytes by the `blob` reference a part holds, base64-encoded.
pub type Blobs = HashMap<String, String>;

pub struct Request<'a> {
    /// A catalog id, `provider:model`.
    pub model: &'a str,
    pub prompt: &'a Prompt,
    pub tools: &'a [Tool],
    pub blobs: &'a Blobs,
}

/// A piece of the reply as it streams.
#[derive(Debug, PartialEq)]
pub enum Delta<'a> {
    Text(&'a str),
    Thinking(&'a str),
}

#[derive(Debug)]
pub struct Error {
    /// A server error, a rate limit or a dropped connection: worth one retry.
    pub transient: bool,
    pub status: Option<u16>,
    pub message: String,
}

impl Error {
    fn fatal(message: impl Into<String>) -> Self {
        Error { transient: false, status: None, message: message.into() }
    }

    fn connection(e: reqwest::Error) -> Self {
        Error { transient: true, status: None, message: e.to_string() }
    }

    /// From a non-2xx response: 429 and 5xx are worth a retry, the rest
    /// (bad input, context overflow, auth) fail fast.
    async fn from_response(resp: reqwest::Response) -> Self {
        let status = resp.status().as_u16();
        let body = resp.text().await.unwrap_or_default();
        Error { transient: status == 429 || status >= 500, status: Some(status), message: format!("{status}: {body}") }
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

/// Where each provider is, and the credentials for it.
#[derive(Clone, Debug)]
pub struct Endpoints {
    pub google_base: String,
    pub google_key: Option<String>,
    pub ollama_base: String,
}

impl Endpoints {
    pub fn from_env() -> Self {
        let var = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
        Endpoints {
            google_base: var("JARVIS_GOOGLE_BASE_URL")
                .unwrap_or_else(|| "https://generativelanguage.googleapis.com".into())
                .trim_end_matches('/')
                .to_string(),
            // The google-genai SDK's order.
            google_key: var("GOOGLE_API_KEY").or_else(|| var("GEMINI_API_KEY")),
            ollama_base: ollama::host(var("OLLAMA_HOST").as_deref()),
        }
    }
}

/// One model call: the reply as an assistant record, its text and thinking
/// handed to `on_delta` as they arrive.
///
/// A transient failure before anything streamed is retried once; after,
/// it isn't — a retry would show the user the same tokens twice.
pub async fn complete(
    http: &reqwest::Client,
    ends: &Endpoints,
    req: &Request<'_>,
    on_delta: &mut (dyn FnMut(Delta) + Send),
) -> Result<Message, Error> {
    let mut streamed = false;
    let mut first = |d: Delta| {
        streamed = true;
        on_delta(d)
    };
    match call(http, ends, req, &mut first).await {
        Err(e) if e.transient && !streamed => {
            tracing::warn!("{}: {e} — retrying once", req.model);
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            call(http, ends, req, on_delta).await
        }
        r => r,
    }
}

async fn call(
    http: &reqwest::Client,
    ends: &Endpoints,
    req: &Request<'_>,
    on_delta: &mut (dyn FnMut(Delta) + Send),
) -> Result<Message, Error> {
    let (provider, name) = req.model.split_once(':').ok_or_else(|| Error::fatal(format!("model id {:?}", req.model)))?;
    match provider {
        "google_genai" => google::complete(http, ends, name, req, on_delta).await,
        "ollama" => ollama::complete(http, ends, name, req, on_delta).await,
        other => Err(Error::fatal(format!("provider {other} isn't served by the edge yet"))),
    }
}

/// A media part's bytes as base64, from wherever the part keeps them.
fn media_base64<'a>(media: &'a transcript::Media, blobs: &'a Blobs) -> Result<&'a str, Error> {
    if let Some(data) = &media.data {
        return Ok(data);
    }
    if let Some(blob) = &media.blob {
        return blobs.get(blob).map(String::as_str).ok_or_else(|| Error::fatal(format!("missing blob {blob}")));
    }
    Err(Error::fatal(format!(
        "media by URL ({}) isn't sent yet",
        media.url.as_deref().unwrap_or("no url")
    )))
}

fn new_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

/// What `--llm-shape` / `--llm-call` read from stdin: a history and what the
/// agent step would lay out around it.
#[derive(Deserialize)]
struct CliInput {
    model: String,
    #[serde(default)]
    system: String,
    #[serde(default)]
    segments: Vec<shape::Segment>,
    #[serde(default)]
    volatile: String,
    #[serde(default)]
    cache: bool,
    history: Vec<Message>,
    #[serde(default)]
    tools: Vec<Tool>,
    #[serde(default)]
    blobs: Blobs,
}

/// `--llm-shape` prints the shaped [`Prompt`]; `--llm-call` makes the call
/// and prints one JSON line per delta, then `{"message": …}` or
/// `{"error": …}`. Endpoints and keys come from the environment.
pub async fn cli(call: bool) {
    use std::io::Read;
    let _ = dotenvy::dotenv();
    let mut raw = String::new();
    std::io::stdin().read_to_string(&mut raw).expect("stdin");
    let input: CliInput = match serde_json::from_str(&raw) {
        Ok(i) => i,
        Err(e) => {
            println!("{}", serde_json::json!({"error": {"message": format!("input: {e}")}}));
            return;
        }
    };
    let provider = input.model.split_once(':').map_or("", |(p, _)| p);
    let layout = shape::Layout {
        system: &input.system,
        segments: &input.segments,
        volatile: &input.volatile,
        cache: input.cache,
        provider,
    };
    let history = shape::repair_orphan_tool_calls(shape::strip_historical_thinking(input.history));
    let prompt = shape::build(&layout, history);
    if !call {
        println!("{}", serde_json::to_string(&prompt).expect("serializes"));
        return;
    }
    let http = reqwest::Client::new();
    let req = Request { model: &input.model, prompt: &prompt, tools: &input.tools, blobs: &input.blobs };
    let mut print = |d: Delta| {
        let line = match d {
            Delta::Text(t) => serde_json::json!({"text": t}),
            Delta::Thinking(t) => serde_json::json!({"thinking": t}),
        };
        println!("{line}");
    };
    match complete(&http, &Endpoints::from_env(), &req, &mut print).await {
        Ok(m) => println!("{}", serde_json::json!({"message": m})),
        Err(e) => println!(
            "{}",
            serde_json::json!({"error": {"message": e.message, "status": e.status, "transient": e.transient}})
        ),
    }
}
