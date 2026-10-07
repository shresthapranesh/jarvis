//! The LLM layer: a model call from a thread's transcript, written for jarvis
//! rather than borrowed from a framework — the request shape is where the
//! requirements live (byte-stable cache prefixes, thought signatures, one
//! system prompt), so no library sits above our request builder.
//!
//! - `transcript` — the v1 record Python and Rust both read and write.
//! - `compact` — clip stale tool output, collapse old tool-call groups.
//! - `perf` — prefill/decode throughput per call and per run.
//! - `shape` — strip thinking, repair orphaned calls, lay the prompt out.
//! - `anthropic`, `google`, `ollama`, `openai_chat` (OpenRouter),
//!   `openai_responses` (Meta) — one module per wire format: render a
//!   [`Prompt`], stream the reply, build the assistant record.
//!
//! [`complete`] is the one entry point.

pub mod anthropic;
pub mod compact;
pub mod google;
mod lines;
pub mod ollama;
pub mod openai_chat;
pub mod openai_responses;
pub mod perf;
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

/// What arrives while the reply streams: its text and thinking, and the
/// signals per-call throughput is measured from.
#[derive(Debug, PartialEq)]
pub enum Delta<'a> {
    Text(&'a str),
    Thinking(&'a str),
    /// Part of a tool call came in. Most agent steps are nothing but calls,
    /// and the first output of any kind marks where prefill ended.
    ToolCall,
    /// The server's own measure of the call (Ollama), sent at the end.
    Timings(perf::ServerTimings),
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

    /// An error object sent inside a stream, with an HTTP-like `code` when
    /// the server gives one.
    fn from_stream(err: &Value) -> Self {
        let status = err.get("code").and_then(Value::as_u64).and_then(|c| u16::try_from(c).ok());
        Error { transient: status.is_some_and(|c| c == 429 || c >= 500), status, message: err.to_string() }
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
    pub anthropic_base: String,
    pub anthropic_key: Option<String>,
    pub google_base: String,
    pub google_key: Option<String>,
    pub ollama_base: String,
    pub openrouter_base: String,
    pub openrouter_key: Option<String>,
    pub meta_base: String,
    pub meta_key: Option<String>,
    /// The operator's OpenAI-compatible servers (`models.endpoints`), each
    /// spoken to over Chat Completions under its own name.
    pub compatible: Vec<crate::catalog::Endpoint>,
}

impl Endpoints {
    pub fn from_env() -> Self {
        let var = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
        Endpoints {
            // langchain-anthropic's order.
            anthropic_base: var("ANTHROPIC_API_URL")
                .or_else(|| var("ANTHROPIC_BASE_URL"))
                .unwrap_or_else(|| "https://api.anthropic.com".into())
                .trim_end_matches('/')
                .to_string(),
            anthropic_key: var("ANTHROPIC_API_KEY"),
            google_base: var("JARVIS_GOOGLE_BASE_URL")
                .unwrap_or_else(|| "https://generativelanguage.googleapis.com".into())
                .trim_end_matches('/')
                .to_string(),
            // The google-genai SDK's order.
            google_key: var("GOOGLE_API_KEY").or_else(|| var("GEMINI_API_KEY")),
            ollama_base: ollama::host(var("OLLAMA_HOST").as_deref()),
            openrouter_base: var("JARVIS_OPENROUTER_BASE_URL")
                .unwrap_or_else(|| "https://openrouter.ai/api/v1".into())
                .trim_end_matches('/')
                .to_string(),
            openrouter_key: var("OPENROUTER_API_KEY"),
            // `ChatMetaModel` reads MODEL_API_BASE; jarvis names the key META_API_KEY.
            meta_base: var("MODEL_API_BASE").unwrap_or_else(|| "https://api.meta.ai/v1".into()).trim_end_matches('/').to_string(),
            meta_key: var("META_API_KEY"),
            compatible: vec![],
        }
    }
}

/// A finished call: the assistant record, and how fast it ran.
pub struct Reply {
    pub message: Message,
    pub perf: perf::CallPerf,
}

/// One model call: the reply as an assistant record, its text and thinking
/// handed to `on_delta` as they arrive.
///
/// A transient failure before any text streamed is retried once; after,
/// it isn't — a retry would show the user the same tokens twice. The
/// throughput is the attempt that answered.
pub async fn complete(
    http: &reqwest::Client,
    ends: &Endpoints,
    req: &Request<'_>,
    on_delta: &mut (dyn FnMut(Delta) + Send),
) -> Result<Reply, Error> {
    let mut clock = perf::Clock::start();
    let mut shown = false;
    let first = call(http, ends, req, &mut |d: Delta| {
        shown |= matches!(d, Delta::Text(_) | Delta::Thinking(_));
        clock.saw(&d);
        on_delta(d)
    })
    .await;
    let message = match first {
        Err(e) if e.transient && !shown => {
            tracing::warn!("{}: {e} — retrying once", req.model);
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            clock = perf::Clock::start();
            call(http, ends, req, &mut |d: Delta| {
                clock.saw(&d);
                on_delta(d)
            })
            .await?
        }
        r => r?,
    };
    let name = req.model.split_once(':').map_or(req.model, |(_, n)| n);
    let perf = perf::CallPerf::measure(name, &message, &clock);
    Ok(Reply { message, perf })
}

async fn call(
    http: &reqwest::Client,
    ends: &Endpoints,
    req: &Request<'_>,
    on_delta: &mut (dyn FnMut(Delta) + Send),
) -> Result<Message, Error> {
    let (provider, name) = req.model.split_once(':').ok_or_else(|| Error::fatal(format!("model id {:?}", req.model)))?;
    match provider {
        "anthropic" => {
            let key = needs(&ends.anthropic_key, "ANTHROPIC_API_KEY", req.model)?;
            anthropic::complete(http, &ends.anthropic_base, key, name, req, on_delta).await
        }
        "google_genai" => google::complete(http, ends, name, req, on_delta).await,
        "ollama" => ollama::complete(http, ends, name, req, on_delta).await,
        "openrouter" => {
            let key = needs(&ends.openrouter_key, "OPENROUTER_API_KEY", req.model)?;
            openai_chat::complete(http, &ends.openrouter_base, Some(key), provider, name, req, on_delta).await
        }
        "meta" => {
            let key = needs(&ends.meta_key, "META_API_KEY", req.model)?;
            openai_responses::complete(http, &ends.meta_base, key, provider, name, req, on_delta).await
        }
        other => match ends.compatible.iter().find(|e| e.name == other) {
            Some(ep) => openai_chat::complete(http, &ep.base_url, ep.api_key.as_deref(), provider, name, req, on_delta).await,
            None => Err(Error::fatal(format!("provider {other} isn't served by the edge yet"))),
        },
    }
}

/// `build_llm().ainvoke([SystemMessage(system), HumanMessage(user)])`: a
/// one-shot call, no tools, nothing streamed — the reply's text blocks.
pub async fn ask(
    pool: &sqlx::SqlitePool,
    http: &reqwest::Client,
    model: &str,
    system: &str,
    user: String,
) -> Result<Vec<String>, Error> {
    use transcript::{Content, Part, Role, Typed};

    let prompt = Prompt {
        system: vec![shape::SystemBlock { text: system.into(), breakpoint: false }],
        messages: vec![Message::new(Role::User, Content::Text(user))],
        history_breakpoint: None,
        cached: false,
    };
    let ends = Endpoints { compatible: crate::catalog::endpoints(pool).await.unwrap_or_default(), ..Endpoints::from_env() };
    let req = Request { model, prompt: &prompt, tools: &[], blobs: &Default::default() };
    let reply = complete(http, &ends, &req, &mut |_| {}).await?;
    Ok(match reply.message.content {
        Content::Text(s) => vec![s],
        Content::Parts(parts) => parts
            .into_iter()
            .filter_map(|p| match p {
                Part::Typed(Typed::Text { text, .. }) => Some(text),
                _ => None,
            })
            .collect(),
    })
}

fn needs<'a>(key: &'a Option<String>, var: &str, model: &str) -> Result<&'a str, Error> {
    key.as_deref().ok_or_else(|| Error::fatal(format!("{var} is not set (required for '{model}')")))
}

/// A tool call's streamed arguments as JSON — empty is `{}` — or why not.
fn parse_args(raw: &str) -> Result<Value, String> {
    if raw.trim().is_empty() {
        return Ok(Value::Object(Default::default()));
    }
    serde_json::from_str(raw).map_err(|e| format!("arguments are not valid JSON: {e}"))
}

/// A media part as a `data:` URL, or the URL it already is.
fn data_url(media: &transcript::Media, blobs: &Blobs) -> Result<String, Error> {
    if let (Some(url), None, None) = (&media.url, &media.data, &media.blob) {
        return Ok(url.clone());
    }
    let mime = media.mime_type.as_deref().unwrap_or("application/octet-stream");
    Ok(format!("data:{mime};base64,{}", media_base64(media, blobs)?))
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
    /// `models.endpoints` rows, as stored.
    #[serde(default)]
    endpoints: Vec<Value>,
    /// The run kind whose budget the call counts against.
    #[serde(default = "chat")]
    kind: String,
}

fn chat() -> String {
    "chat".into()
}

/// `--llm-shape` prints the shaped [`Prompt`]; `--llm-call` makes the call
/// as a run of one call would: one JSON line per text or thinking delta, the
/// `{"event", "data"}` records the run would emit for its budget and
/// throughput, then `{"message": …, "perf": …}` (`perf` as the Message row
/// stores it) or `{"error": …}`. Endpoints and keys come from the
/// environment.
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
    // The agent step's order: compact, then strip and repair.
    let history = compact::per_call(input.history);
    let history = shape::repair_orphan_tool_calls(shape::strip_historical_thinking(history));
    let prompt = shape::build(&layout, history);
    if !call {
        println!("{}", serde_json::to_string(&prompt).expect("serializes"));
        return;
    }
    let http = reqwest::Client::new();
    let req = Request { model: &input.model, prompt: &prompt, tools: &input.tools, blobs: &input.blobs };
    let mut print = |d: Delta| match d {
        Delta::Text(t) => println!("{}", serde_json::json!({"text": t})),
        Delta::Thinking(t) => println!("{}", serde_json::json!({"thinking": t})),
        Delta::ToolCall | Delta::Timings(_) => {}
    };
    let ends = Endpoints { compatible: crate::catalog::parse_endpoints(&input.endpoints), ..Endpoints::from_env() };
    let mut budget = crate::budget::Budget::new(crate::budget::Limits::for_kind(&input.kind));
    let mut perf = perf::PerfTracker::default();
    match complete(&http, &ends, &req, &mut print).await {
        Ok(Reply { message, perf: call }) => {
            let usage = message.usage.clone().unwrap_or_default();
            let mut events = budget.record_llm(usage.input, usage.output);
            events.push(("perf_update", perf.record(call)));
            events.extend(budget.check());
            for (event, data) in events {
                println!("{}", serde_json::json!({"event": event, "data": data}));
            }
            println!("{}", serde_json::json!({"message": message, "perf": perf.message_perf()}));
        }
        Err(e) => println!(
            "{}",
            serde_json::json!({"error": {"message": e.message, "status": e.status, "transient": e.transient}})
        ),
    }
}
