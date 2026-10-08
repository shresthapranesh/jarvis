//! Provider model discovery — a port of `core/model_discovery.py` (change
//! both). A lint over the catalog, never the catalog: it lists what each
//! provider publishes, diffs that against the catalog, and on request makes
//! a one-token call per catalog model, because a listing is not entitlement.
//!
//! A provider that can't be listed is [`Fail::Skip`], and the reason is
//! shown in the UI: Python's `DiscoveryError` text where it had one, else a
//! plain account of what was wrong with the reply. A probe's reason follows
//! each Python SDK's spelling as far as it can; it's advice for the
//! operator, not a contract.

use std::collections::HashMap;
use std::sync::OnceLock;
use std::time::Duration;

use reqwest::Url;
use serde_json::{Value, json};

use crate::aws;
use crate::catalog::{Endpoint, Spec};
use crate::llm::Endpoints;
use crate::pyjson;
use crate::pystr;

/// `DiscoveredModel`.
#[derive(Clone, Debug, PartialEq)]
pub struct Found {
    pub id: String,
    pub label: String,
    pub provider: String,
    pub context_window: Option<i32>,
    pub description: Option<String>,
    pub likely_chat: bool,
}

#[derive(Debug, PartialEq)]
pub enum Fail {
    /// `DiscoveryError`: the provider is skipped, and this is why.
    Skip(String),
}

type Listing = Result<Vec<Found>, Fail>;

/// A reply that isn't the shape a listing has.
fn malformed<T>(why: impl Into<String>) -> Result<T, Fail> {
    Err(Fail::Skip(format!("unexpected reply: {}", why.into())))
}

/// `os.environ.get(k)`: set-but-empty is a value here, as it is there.
fn env(k: &str) -> Option<String> {
    std::env::var(k).ok()
}

// Substrings that mark a non-text generator; see `_NON_CHAT_MARKERS`.
const NON_CHAT_MARKERS: &[&str] =
    &["-tts", "-image", "lyria", "nano-banana", "veo-", "imagen", "-embedding", "-live-", "computer-use", "robotics"];

/// `looks_like_chat`: a hint, never a filter.
pub fn looks_like_chat(name: &str) -> bool {
    let low = name.to_lowercase();
    !NON_CHAT_MARKERS.iter().any(|m| low.contains(m))
}

/// httpx's `Client(timeout=30)` without its redirects: discovery's client.
fn http() -> &'static reqwest::Client {
    static HTTP: OnceLock<reqwest::Client> = OnceLock::new();
    HTTP.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .expect("the discovery client builds")
    })
}

/// The SDKs' clients: long timeouts, redirects followed.
fn sdk_http() -> &'static reqwest::Client {
    static HTTP: OnceLock<reqwest::Client> = OnceLock::new();
    HTTP.get_or_init(|| reqwest::Client::builder().timeout(Duration::from_secs(600)).build().expect("the SDK client builds"))
}

/// `str(exc)` for an httpx transport error, where it can be known: a socket
/// error is `[Errno 61] Connection refused`, a timeout `timed out`.
pub(crate) fn httpx_error(e: &reqwest::Error) -> Option<String> {
    if e.is_timeout() {
        return Some("timed out".into());
    }
    let mut src: Option<&(dyn std::error::Error + 'static)> = std::error::Error::source(e);
    while let Some(s) = src {
        if let Some(io) = s.downcast_ref::<std::io::Error>() {
            let n = io.raw_os_error()?;
            let text = io.to_string();
            let text = text.strip_suffix(&format!(" (os error {n})")).unwrap_or(&text).to_string();
            return Some(format!("[Errno {n}] {text}"));
        }
        src = s.source();
    }
    None
}

/// `str(HTTPStatusError)` from `raise_for_status()`.
pub(crate) fn httpx_status_error(resp: &reqwest::Response) -> String {
    let status = resp.status();
    let code = status.as_u16();
    let kind = match code / 100 {
        1 => "Informational response",
        3 => "Redirect response",
        4 => "Client error",
        5 => "Server error",
        _ => "Invalid status code",
    };
    let reason = status.canonical_reason().unwrap_or("");
    let location = resp.headers().get("location").and_then(|v| v.to_str().ok());
    let redirect = match location {
        Some(l) if matches!(code, 301 | 302 | 303 | 307 | 308) => format!("Redirect location: '{l}'\n"),
        _ => String::new(),
    };
    format!(
        "{kind} '{code} {reason}' for url '{}'\n{redirect}For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/{code}",
        resp.url()
    )
}

/// `httpx.get(url, ...)` + `raise_for_status()`, the failure as `str(exc)`.
async fn httpx_get(url: &str, headers: &[(&str, String)]) -> Result<reqwest::Response, Result<String, Fail>> {
    let Ok(parsed) = Url::parse(url) else {
        return Err(Err(Fail::Skip(format!("not a valid URL: {url}"))));
    };
    let mut req = http().get(parsed);
    for (k, v) in headers {
        req = req.header(*k, v);
    }
    match req.send().await {
        Ok(r) if r.status().is_success() => Ok(r),
        Ok(r) => Err(Ok(httpx_status_error(&r))),
        Err(e) => Err(httpx_error(&e).ok_or_else(|| Fail::Skip(format!("{url}: {e}")))),
    }
}

async fn json_body(resp: reqwest::Response) -> Result<Value, Fail> {
    let bytes = resp.bytes().await.map_err(|e| Fail::Skip(format!("reading a listing: {e}")))?;
    serde_json::from_slice(&bytes).map_err(|e| Fail::Skip(format!("a listing that isn't JSON: {e}")))
}

/// A field that must be a string, or absent.
fn opt_str(m: &serde_json::Map<String, Value>, key: &str) -> Result<Option<String>, Fail> {
    match m.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(other) => malformed(format!("{key} {other}")),
    }
}

/// `x or y` over optional strings: an empty string is falsy.
fn or(a: Option<String>, b: impl FnOnce() -> String) -> String {
    a.filter(|s| !s.is_empty()).unwrap_or_else(b)
}

/// A window GraphQL's Int carries.
fn window(n: i64) -> Result<i32, Fail> {
    i32::try_from(n).map_err(|_| Fail::Skip(format!("a context window too large to report: {n}")))
}

// ── Per-provider adapters ────────────────────────────────────────────────────

fn google_base() -> String {
    env("JARVIS_GOOGLE_BASE_URL")
        .filter(|u| !u.is_empty())
        .unwrap_or_else(|| "https://generativelanguage.googleapis.com".into())
        .trim_end_matches('/')
        .to_string()
}

async fn discover_google() -> Listing {
    let Some(key) = env("GOOGLE_API_KEY").filter(|k| !k.is_empty()) else {
        return Err(Fail::Skip("GOOGLE_API_KEY is not set".into()));
    };
    let mut out = vec![];
    let mut page: Option<String> = None;
    loop {
        let mut url = Url::parse(&format!("{}/v1beta/models", google_base())).map_err(|e| Fail::Skip(e.to_string()))?;
        url.query_pairs_mut().append_pair("pageSize", "1000");
        if let Some(p) = &page {
            url.query_pairs_mut().append_pair("pageToken", p);
        }
        let resp = match http().get(url).header("x-goog-api-key", &key).send().await {
            Ok(r) => r,
            // Python lets the transport error out of the query.
            Err(e) => return malformed(format!("ListModels: {e}")),
        };
        let status = resp.status().as_u16();
        if status != 200 {
            let text = resp.bytes().await.map(|b| String::from_utf8_lossy(&b).into_owned()).unwrap_or_default();
            return Err(Fail::Skip(format!("ListModels failed ({status}): {}", pystr::prefix(&text, 200))));
        }
        let Value::Object(body) = json_body(resp).await? else {
            return malformed("ListModels: not an object");
        };
        let models = match body.get("models") {
            None => vec![],
            Some(Value::Array(a)) => a.clone(),
            Some(_) => return malformed("ListModels: models isn't a list"),
        };
        for m in models {
            let Value::Object(m) = m else { return malformed("ListModels: a model that isn't an object") };
            let generates = match m.get("supportedGenerationMethods") {
                None => false,
                Some(Value::Array(ms)) => ms.iter().any(|v| v == "generateContent"),
                Some(_) => return malformed("supportedGenerationMethods isn't a list"),
            };
            if !generates {
                continue;
            }
            let Some(Value::String(full)) = m.get("name") else { return malformed("a model without a name") };
            let name = full.split_once('/').map_or(full.as_str(), |(_, n)| n).to_string();
            let context_window = match m.get("inputTokenLimit") {
                None | Some(Value::Null) => None,
                Some(Value::Number(n)) if n.is_i64() => Some(window(n.as_i64().unwrap_or_default())?),
                Some(other) => return malformed(format!("inputTokenLimit {other}")),
            };
            out.push(Found {
                id: format!("google_genai:{name}"),
                label: or(opt_str(&m, "displayName")?, || name.clone()),
                provider: "google_genai".into(),
                context_window,
                description: opt_str(&m, "description")?,
                likely_chat: looks_like_chat(&name),
            });
        }
        match body.get("nextPageToken") {
            Some(Value::String(p)) if !p.is_empty() => page = Some(p.clone()),
            None | Some(Value::Null) | Some(Value::String(_)) => return Ok(out),
            Some(_) => return malformed("nextPageToken isn't a string"),
        }
    }
}

fn anthropic_base() -> String {
    env("ANTHROPIC_BASE_URL")
        .filter(|u| !u.is_empty())
        .unwrap_or_else(|| "https://api.anthropic.com".into())
        .trim_end_matches('/')
        .to_string()
}

/// The `anthropic`/`openai` SDKs' `APIStatusError` text.
fn sdk_status_error(status: u16, text: &str) -> String {
    let text = pystr::strip(text);
    match serde_json::from_str::<Value>(text) {
        Ok(body) => format!("Error code: {status} - {}", pyjson::py_str(&body)),
        Err(_) if !text.is_empty() => text.to_string(),
        Err(_) => format!("Error code: {status}"),
    }
}

/// An SDK request with the SDKs' two retries (a dropped connection, 408,
/// 409, 429, 5xx); the failure as the SDK words it.
async fn sdk_send(build: impl Fn() -> reqwest::RequestBuilder) -> Result<reqwest::Response, String> {
    let mut retries = 0;
    loop {
        let retry = match build().send().await {
            Ok(r) if r.status().is_success() => return Ok(r),
            Ok(r) => {
                let status = r.status().as_u16();
                let header = r.headers().get("x-should-retry").and_then(|v| v.to_str().ok()).map(str::to_string);
                let should = match header.as_deref() {
                    Some("true") => true,
                    Some("false") => false,
                    _ => matches!(status, 408 | 409 | 429) || status >= 500,
                };
                if !should || retries == 2 {
                    let text = r.bytes().await.map(|b| String::from_utf8_lossy(&b).into_owned()).unwrap_or_default();
                    return Err(sdk_status_error(status, &text));
                }
                true
            }
            Err(e) if retries == 2 => {
                return Err(if e.is_timeout() { "Request timed out.".into() } else { "Connection error.".into() });
            }
            Err(_) => true,
        };
        if retry {
            tokio::time::sleep(Duration::from_millis(500 << retries)).await;
            retries += 1;
        }
    }
}

async fn discover_anthropic() -> Listing {
    let Some(key) = env("ANTHROPIC_API_KEY").filter(|k| !k.is_empty()) else {
        return Err(Fail::Skip("ANTHROPIC_API_KEY is not set".into()));
    };
    let failed = |why: String| Fail::Skip(format!("models.list failed: {why}"));
    let mut out = vec![];
    let mut after: Option<String> = None;
    loop {
        let mut url = Url::parse(&format!("{}/v1/models", anthropic_base())).map_err(|e| Fail::Skip(e.to_string()))?;
        url.query_pairs_mut().append_pair("limit", "1000");
        if let Some(a) = &after {
            url.query_pairs_mut().append_pair("after_id", a);
        }
        let resp = sdk_send(|| sdk_http().get(url.clone()).header("x-api-key", &key).header("anthropic-version", "2023-06-01"))
            .await
            .map_err(failed)?;
        let Value::Object(page) = json_body(resp).await? else { return malformed("models.list: not an object") };
        let data = match page.get("data") {
            Some(Value::Array(d)) => d.clone(),
            None | Some(Value::Null) => vec![],
            Some(_) => return malformed("models.list: data isn't a list"),
        };
        for m in &data {
            let Value::Object(m) = m else { return malformed("models.list: a model that isn't an object") };
            let Some(Value::String(id)) = m.get("id") else { return malformed("models.list: a model without an id") };
            out.push(Found {
                id: format!("anthropic:{id}"),
                label: or(opt_str(m, "display_name")?, || id.clone()),
                provider: "anthropic".into(),
                context_window: None,
                description: None,
                likely_chat: true,
            });
        }
        // `SyncPage.has_next_page`: only an explicit `false` ends it early.
        let more = !matches!(page.get("has_more"), Some(Value::Bool(false)));
        match page.get("last_id") {
            Some(Value::String(last)) if more && !data.is_empty() && !last.is_empty() => after = Some(last.clone()),
            _ => return Ok(out),
        }
    }
}

async fn discover_bedrock() -> Listing {
    let region = aws::region();
    let failed = |why: String| Fail::Skip(format!("ListFoundationModels failed ({region}): {why}"));
    let creds = match aws::credentials().await {
        Ok(c) => c,
        Err(aws::CredError::Failed(why)) => return Err(failed(why)),
        Err(aws::CredError::Unsupported(why)) => return Err(Fail::Skip(format!("AWS credentials: {why}"))),
    };
    let mut url = Url::parse(&format!("{}/foundation-models", aws::endpoint("bedrock", &region)))
        .map_err(|e| Fail::Skip(format!("the bedrock endpoint: {e}")))?;
    url.query_pairs_mut().append_pair("byOutputModality", "TEXT");
    let body = match aws::call(sdk_http(), &creds, &region, "bedrock", "ListFoundationModels", &url, None).await {
        Ok(b) => b,
        Err(aws::CallError::Failed(why)) => return Err(failed(why)),
        Err(aws::CallError::Unsupported(why)) => return Err(Fail::Skip(why)),
    };
    let summaries = match body.get("modelSummaries") {
        Some(Value::Array(s)) => s.clone(),
        None | Some(Value::Null) => vec![],
        Some(_) => return malformed("modelSummaries isn't a list"),
    };
    let mut out = vec![];
    for m in summaries {
        let Value::Object(m) = m else { return malformed("a model summary that isn't an object") };
        let Some(mid) = opt_str(&m, "modelId")?.filter(|s| !s.is_empty()) else { continue };
        let on_demand = match m.get("inferenceTypesSupported") {
            Some(Value::Array(t)) => t.iter().any(|v| v == "ON_DEMAND"),
            _ => false,
        };
        if !on_demand {
            continue;
        }
        let vendor = or(opt_str(&m, "providerName")?, || "Bedrock".into());
        out.push(Found {
            id: format!("bedrock:{mid}"),
            label: format!("{} ({vendor})", or(opt_str(&m, "modelName")?, || mid.clone())),
            provider: "bedrock".into(),
            context_window: None,
            description: None,
            likely_chat: true,
        });
    }
    Ok(out)
}

async fn discover_ollama() -> Listing {
    let mut host = env("OLLAMA_HOST").unwrap_or_else(|| "http://127.0.0.1:11434".into()).trim_end_matches('/').to_string();
    if !host.starts_with("http") {
        host = format!("http://{host}");
    }
    let resp = httpx_get(&format!("{host}/api/tags"), &[])
        .await
        .map_err(|e| e.map_or_else(|f| f, |why| Fail::Skip(format!("could not reach ollama at {host}: {why}"))))?;
    let Value::Object(body) = json_body(resp).await? else { return malformed("/api/tags: not an object") };
    let models = match body.get("models") {
        None => vec![],
        Some(Value::Array(m)) => m.clone(),
        Some(_) => return malformed("/api/tags: models isn't a list"),
    };
    let mut out = vec![];
    for m in models {
        let Value::Object(m) = m else { return malformed("/api/tags: a model that isn't an object") };
        let name = match m.get("name") {
            Some(Value::String(n)) if !n.is_empty() => n.clone(),
            Some(v) if pyjson::truthy(v) => return malformed(format!("a model name {v}")),
            _ => continue,
        };
        out.push(Found {
            id: format!("ollama:{name}"),
            label: format!("{name} (Ollama)"),
            provider: "ollama".into(),
            context_window: None,
            description: None,
            likely_chat: true,
        });
    }
    Ok(out)
}

fn openrouter_base() -> String {
    env("JARVIS_OPENROUTER_BASE_URL")
        .filter(|u| !u.is_empty())
        .unwrap_or_else(|| "https://openrouter.ai/api/v1".into())
        .trim_end_matches('/')
        .to_string()
}

async fn discover_openrouter() -> Listing {
    let resp = httpx_get(&format!("{}/models", openrouter_base()), &[])
        .await
        .map_err(|e| e.map_or_else(|f| f, |why| Fail::Skip(format!("could not reach OpenRouter: {why}"))))?;
    let Value::Object(body) = json_body(resp).await? else { return malformed("OpenRouter: not an object") };
    let data = match body.get("data") {
        None => vec![],
        Some(Value::Array(d)) => d.clone(),
        Some(_) => return malformed("OpenRouter: data isn't a list"),
    };
    let mut out = vec![];
    for m in data {
        let Value::Object(m) = m else { return malformed("OpenRouter: a model that isn't an object") };
        let mid = match m.get("id") {
            Some(Value::String(id)) if !id.is_empty() => id.clone(),
            Some(v) if pyjson::truthy(v) => return malformed(format!("an OpenRouter id {v}")),
            _ => continue,
        };
        let arch = match m.get("architecture") {
            Some(Value::Object(a)) => a.clone(),
            Some(v) if pyjson::truthy(v) => return malformed("architecture isn't an object"),
            _ => Default::default(),
        };
        let likely_chat = match arch.get("output_modalities") {
            Some(Value::Array(mods)) if !mods.is_empty() => mods.iter().any(|v| v == "text"),
            _ => looks_like_chat(&mid),
        };
        let top = || match m.get("top_provider") {
            Some(Value::Object(t)) => Ok(t.get("context_length").cloned()),
            Some(v) if pyjson::truthy(v) => malformed("top_provider isn't an object"),
            _ => Ok(None),
        };
        let stated = match m.get("context_length") {
            Some(v) if pyjson::truthy(v) => Some(v.clone()),
            _ => top()?,
        };
        // `int(window) if isinstance(window, (int, float)) and window > 0`.
        let context_window = match stated {
            Some(Value::Bool(true)) => Some(1),
            Some(Value::Number(n)) => match n.as_f64() {
                Some(f) if f > 0.0 => Some(window(if let Some(i) = n.as_i64() { i } else { f.trunc() as i64 })?),
                _ => None,
            },
            _ => None,
        };
        let description = match m.get("description") {
            Some(Value::String(d)) => Some(pystr::strip(d).to_string()).filter(|d| !d.is_empty()),
            Some(v) if pyjson::truthy(v) => return malformed("description isn't a string"),
            _ => None,
        };
        out.push(Found {
            id: format!("openrouter:{mid}"),
            label: or(opt_str(&m, "name")?, || mid.clone()),
            provider: "openrouter".into(),
            context_window,
            description,
            likely_chat,
        });
    }
    Ok(out)
}

async fn discover_endpoint(ep: &Endpoint) -> Listing {
    let headers: Vec<(&str, String)> = ep.api_key.iter().map(|k| ("Authorization", format!("Bearer {k}"))).collect();
    let failed = |why: String| Fail::Skip(format!("could not list {}'s models at {}: {why}", ep.name, ep.base_url));
    let resp = httpx_get(&format!("{}/models", ep.base_url), &headers).await.map_err(|e| e.map_or_else(|f| f, failed))?;
    // Inside Python's `try`: a body it can't read is a skip, worded by an
    // exception this side doesn't reproduce.
    let Value::Object(body) = json_body(resp).await? else { return malformed("/models: not an object") };
    let data = match body.get("data") {
        Some(Value::Array(d)) => d.clone(),
        _ => vec![],
    };
    let mut out = vec![];
    for m in data {
        let Value::Object(m) = m else { continue };
        let Some(Value::String(mid)) = m.get("id") else { continue };
        if mid.is_empty() {
            continue;
        }
        let stated = ["context_window", "context_length", "max_model_len"]
            .iter()
            .filter_map(|k| m.get(*k).and_then(Value::as_i64))
            .find(|w| *w > 0);
        out.push(Found {
            id: format!("{}:{mid}", ep.name),
            label: mid.clone(),
            provider: ep.name.clone(),
            context_window: stated.map(window).transpose()?,
            description: None,
            likely_chat: looks_like_chat(mid),
        });
    }
    Ok(out)
}

/// `discover(provider)`: an empty list is an answer, not a failure.
pub async fn discover(provider: &str, endpoints: &[Endpoint]) -> Listing {
    match provider {
        "google_genai" => discover_google().await,
        "anthropic" => discover_anthropic().await,
        "bedrock" => discover_bedrock().await,
        "ollama" => discover_ollama().await,
        "openrouter" => discover_openrouter().await,
        other => match endpoints.iter().find(|e| e.name == other) {
            Some(ep) => discover_endpoint(ep).await,
            None => Err(Fail::Skip(format!("discovery not implemented for provider {}", pyjson::repr_str(other)))),
        },
    }
}

// ── Entitlement probe ────────────────────────────────────────────────────────

/// `_first_line`: whitespace runs collapsed, cut at 160 characters.
fn first_line(text: &str) -> String {
    let line = text.split(pystr::is_space).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ");
    if pystr::len(&line) > 160 { format!("{}…", pystr::prefix(&line, 160)) } else { line }
}

/// `probe(model_id)`: can this credential call this model? A real
/// one-token call; `Err` is the reason it can't.
pub async fn probe(spec: &Spec, ends: &Endpoints) -> Result<(), String> {
    let name = spec.id.split_once(':').map_or("", |(_, n)| n);
    let said = |r: Result<(), String>| r.map_err(|e| first_line(&e));
    let hi_chat = |model: &str| json!({"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1});
    match spec.provider.as_str() {
        "google_genai" => said(probe_google(name, ends).await),
        "ollama" => said(probe_ollama(name, ends).await),
        "anthropic" => {
            let key = env("ANTHROPIC_API_KEY").unwrap_or_default();
            let url = format!("{}/v1/messages", anthropic_base());
            let body = hi_chat(name);
            let sent = sdk_send(|| {
                sdk_http().post(&url).header("x-api-key", &key).header("anthropic-version", "2023-06-01").json(&body)
            })
            .await;
            said(sent.map(drop))
        }
        "bedrock" => {
            let region = aws::region();
            let creds = match aws::credentials().await {
                Ok(c) => c,
                Err(aws::CredError::Failed(why)) => return said(Err(why)),
                Err(aws::CredError::Unsupported(why)) => return said(Err(format!("AWS credentials: {why}"))),
            };
            let url = format!("{}/model/{}/converse", aws::endpoint("bedrock-runtime", &region), aws::encode_label(name));
            let Ok(url) = Url::parse(&url) else { return said(Err(format!("not a valid bedrock-runtime endpoint: {url}"))) };
            let body = json!({"messages": [{"role": "user", "content": [{"text": "hi"}]}], "inferenceConfig": {"maxTokens": 1}});
            match aws::call(sdk_http(), &creds, &region, "bedrock", "Converse", &url, Some(&body)).await {
                Ok(_) => said(Ok(())),
                Err(aws::CallError::Failed(why) | aws::CallError::Unsupported(why)) => said(Err(why)),
            }
        }
        provider => {
            let (base, key) = if provider == "openrouter" {
                match &ends.openrouter_key {
                    Some(k) => (ends.openrouter_base.clone(), k.clone()),
                    None => return said(Err(format!("OPENROUTER_API_KEY is not set (required for '{}')", spec.id))),
                }
            } else {
                match ends.compatible.iter().find(|e| e.name == provider) {
                    // The OpenAI client won't start without a key; a local
                    // server is handed a placeholder it ignores.
                    Some(ep) => (ep.base_url.clone(), ep.api_key.clone().unwrap_or_else(|| "not-needed".into())),
                    None => return said(Err(format!("Unknown provider '{provider}' for model '{}'", spec.id))),
                }
            };
            let url = format!("{base}/chat/completions");
            let body = hi_chat(name);
            said(sdk_send(|| sdk_http().post(&url).bearer_auth(&key).json(&body)).await.map(drop))
        }
    }
}

/// google-genai's `APIError` text, as `ChatGoogleGenerativeAI` raises it.
async fn probe_google(name: &str, ends: &Endpoints) -> Result<(), String> {
    let key = ends.google_key.clone().unwrap_or_default();
    let url = format!("{}/v1beta/models/{name}:generateContent", ends.google_base);
    let body = json!({"contents": [{"role": "user", "parts": [{"text": "hi"}]}], "generationConfig": {"maxOutputTokens": 1}});
    let resp = sdk_http().post(&url).header("x-goog-api-key", key).json(&body).send().await.map_err(|e| e.to_string())?;
    let status = resp.status();
    if status.is_success() {
        return Ok(());
    }
    let text = resp.bytes().await.map(|b| String::from_utf8_lossy(&b).into_owned()).unwrap_or_default();
    let details = match serde_json::from_str::<Value>(&text) {
        Ok(Value::Array(mut one)) if one.len() == 1 => one.remove(0),
        Ok(v) => v,
        Err(_) => json!({"message": text, "status": status.canonical_reason().unwrap_or("")}),
    };
    let field = |k: &str| details.get(k).or_else(|| details.get("error").and_then(|e| e.get(k))).cloned();
    let state = field("status").map_or_else(|| "None".into(), |s| pyjson::py_str(&s));
    let err = format!("{} {state}. {}", status.as_u16(), pyjson::py_str(&details));
    Err(if status.is_client_error() { format!("Error calling model '{name}' ({state}): {err}") } else { err })
}

/// The `ollama` client's `ResponseError` text.
async fn probe_ollama(name: &str, ends: &Endpoints) -> Result<(), String> {
    const UNREACHABLE: &str =
        "Failed to connect to Ollama. Please check that Ollama is downloaded, running and accessible. https://ollama.com/download";
    let body = json!({"model": name, "messages": [{"role": "user", "content": "hi"}], "stream": false, "options": {"num_predict": 1}});
    let resp = match sdk_http().post(format!("{}/api/chat", ends.ollama_base)).json(&body).send().await {
        Ok(r) => r,
        Err(e) if e.is_connect() => return Err(UNREACHABLE.into()),
        Err(e) => return Err(httpx_error(&e).unwrap_or_else(|| e.to_string())),
    };
    let status = resp.status().as_u16();
    if resp.status().is_success() {
        return Ok(());
    }
    let text = resp.bytes().await.map(|b| String::from_utf8_lossy(&b).into_owned()).unwrap_or_default();
    let error = match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(o)) => o.get("error").map_or_else(|| text.clone(), pyjson::py_str),
        _ => text,
    };
    Err(format!("{error} (status code: {status})"))
}

// ── Drift report ─────────────────────────────────────────────────────────────

/// `SyncReport`, without `unreachable` (the caller probes).
#[derive(Debug, PartialEq)]
pub struct Report {
    pub missing: Vec<String>,
    pub new: Vec<Found>,
    /// (id, the provider's window)
    pub window_backfill: Vec<(String, i32)>,
    /// (id, the catalog's window, the provider's)
    pub window_drift: Vec<(String, i64, i32)>,
}

/// `build_report`: a provider's live models against the catalog.
pub fn build_report(provider: &str, catalog: &[Spec], found: &[Found]) -> Report {
    let ours: Vec<&Spec> = catalog.iter().filter(|s| s.provider == provider).collect();
    let mut live: HashMap<&str, &Found> = HashMap::new();
    for f in found {
        live.insert(&f.id, f);
    }
    let mut missing: Vec<String> = ours.iter().filter(|s| !live.contains_key(s.id.as_str())).map(|s| s.id.clone()).collect();
    missing.sort();
    let mut new: Vec<Found> = live.values().filter(|f| !ours.iter().any(|s| s.id == f.id)).map(|f| (*f).clone()).collect();
    new.sort_by(|a, b| a.id.cmp(&b.id));
    let mut window_backfill = vec![];
    let mut window_drift = vec![];
    for spec in &ours {
        let Some(theirs) = live.get(spec.id.as_str()).and_then(|f| f.context_window) else { continue };
        match spec.context_window {
            None => window_backfill.push((spec.id.clone(), theirs)),
            Some(w) if w != i64::from(theirs) => window_drift.push((spec.id.clone(), w, theirs)),
            Some(_) => {}
        }
    }
    window_backfill.sort();
    window_drift.sort();
    Report { missing, new, window_backfill, window_drift }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec(id: &str, window: Option<i64>) -> Spec {
        Spec { id: id.into(), label: id.into(), provider: id.split(':').next().unwrap().into(), context_window: window }
    }

    fn found(id: &str, window: Option<i32>) -> Found {
        Found {
            id: id.into(),
            label: id.into(),
            provider: id.split(':').next().unwrap().into(),
            context_window: window,
            description: None,
            likely_chat: true,
        }
    }

    #[test]
    fn diffs_the_catalog_against_the_listing() {
        let catalog = [spec("p:gone", None), spec("p:b", None), spec("p:a", Some(10)), spec("p:same", Some(5)), spec("q:x", None)];
        let listing = [found("p:z", Some(1)), found("p:a", Some(20)), found("p:b", Some(7)), found("p:same", Some(5)), found("p:new", None)];
        let r = build_report("p", &catalog, &listing);
        assert_eq!(r.missing, vec!["p:gone"]);
        assert_eq!(r.new.iter().map(|f| f.id.as_str()).collect::<Vec<_>>(), vec!["p:new", "p:z"]);
        assert_eq!(r.window_backfill, vec![("p:b".into(), 7)]);
        assert_eq!(r.window_drift, vec![("p:a".into(), 10, 20)]);
    }

    #[test]
    fn words_failures_as_python_does() {
        assert!(!looks_like_chat("gemini-2.5-flash-preview-TTS"));
        assert!(looks_like_chat("gemini-3.5-flash"));
        assert_eq!(first_line(" a\n\tb  c "), "a b c");
        assert_eq!(first_line(&"x".repeat(161)), format!("{}…", "x".repeat(160)));
        assert_eq!(sdk_status_error(404, r#" {"error": {"message": "no", "ok": true}} "#), "Error code: 404 - {'error': {'message': 'no', 'ok': True}}");
        assert_eq!(sdk_status_error(502, "bad gateway"), "bad gateway");
        assert_eq!(sdk_status_error(500, "  "), "Error code: 500");
    }
}
