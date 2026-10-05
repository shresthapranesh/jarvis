//! The HTTP transports: Streamable HTTP (`mcp.client.streamable_http`) and
//! the older HTTP+SSE one (`mcp.client.sse`).
//!
//! Streamable HTTP POSTs each message; a request's answer comes back as the
//! response body, JSON or an event stream. The session id the server hands
//! out on `initialize` goes on every later request, and closing the session
//! DELETEs it. The standalone GET stream Python opens for server-initiated
//! messages isn't: jarvis asks nothing that needs one.
//!
//! HTTP+SSE holds a GET event stream open; its first `endpoint` event names
//! where to POST, and every answer arrives on the stream.

use std::collections::VecDeque;
use std::time::Duration;

use futures_util::StreamExt;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use reqwest::{StatusCode, Url};
use serde_json::{Value, json};
use tokio::sync::mpsc;

use super::config::Connection;
use super::session::Incoming;
use crate::pyjson;

const JSON: &str = "application/json";
const SSE: &str = "text/event-stream";
/// How many times a stream that broke off before its answer is resumed.
const MAX_RECONNECTS: usize = 2;

/// `headers`, which Python passes to httpx as given.
fn headers(params: &Connection) -> Result<HeaderMap, String> {
    let mut out = HeaderMap::new();
    match params.get("headers") {
        None | Some(Value::Null) => {}
        Some(Value::Object(map)) => {
            for (k, v) in map {
                let name = HeaderName::from_bytes(k.as_bytes()).map_err(|_| format!("Invalid header name: {k:?}"))?;
                let value = HeaderValue::from_str(&pyjson::py_str(v)).map_err(|_| format!("Invalid header value for {k:?}"))?;
                out.insert(name, value);
            }
        }
        Some(other) => return Err(format!("headers must be a mapping, not {}", pyjson::py_type(other))),
    }
    Ok(out)
}

/// A number of seconds (`float | timedelta`, which JSON spells as a number).
fn seconds(params: &Connection, key: &str, default: f64) -> Result<Duration, String> {
    match params.get(key) {
        None | Some(Value::Null) => Ok(Duration::from_secs_f64(default)),
        Some(v) => pyjson::py_float(v)
            .filter(|s| s.is_finite() && *s > 0.0)
            .map(Duration::from_secs_f64)
            .ok_or_else(|| format!("{key} must be a number of seconds")),
    }
}

/// httpx's `Timeout(timeout, read=sse_read_timeout)`, redirects followed as
/// `create_mcp_http_client` follows them.
fn client(connect: Duration, read: Duration) -> Result<reqwest::Client, String> {
    reqwest::Client::builder().connect_timeout(connect).read_timeout(read).build().map_err(|e| e.to_string())
}

fn url(params: &Connection) -> Result<Url, String> {
    let raw = pyjson::py_str(&params["url"]);
    Url::parse(&raw).map_err(|e| format!("Invalid URL {raw:?}: {e}"))
}

/// `str(exc)` for a failed request, as httpx says it where that's known.
fn transport_error(e: &reqwest::Error) -> String {
    crate::discovery::httpx_error(e).unwrap_or_else(|| e.to_string())
}

// ── server-sent events ──────────────────────────────────────────────────────

#[derive(Default)]
struct Event {
    event: String,
    data: String,
    id: Option<String>,
    retry: Option<u64>,
}

/// An event stream, per the SSE spec's field rules.
struct Events {
    body: futures_util::stream::BoxStream<'static, reqwest::Result<bytes::Bytes>>,
    buf: Vec<u8>,
    done: bool,
}

impl Events {
    fn new(resp: reqwest::Response) -> Self {
        Events { body: resp.bytes_stream().boxed(), buf: vec![], done: false }
    }

    async fn line(&mut self) -> Result<Option<String>, String> {
        loop {
            if let Some(i) = self.buf.iter().position(|&b| b == b'\n') {
                let mut line: Vec<u8> = self.buf.drain(..=i).collect();
                line.pop();
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                return Ok(Some(String::from_utf8_lossy(&line).into_owned()));
            }
            if self.done {
                return Ok(None);
            }
            match self.body.next().await {
                Some(Ok(chunk)) => self.buf.extend_from_slice(&chunk),
                Some(Err(e)) => return Err(transport_error(&e)),
                None => self.done = true,
            }
        }
    }

    /// The next dispatched event, or `None` when the stream ends.
    async fn next(&mut self) -> Result<Option<Event>, String> {
        let mut ev = Event::default();
        let mut has_data = false;
        let mut any = false;
        while let Some(line) = self.line().await? {
            if line.is_empty() {
                if any {
                    if has_data && ev.event.is_empty() {
                        ev.event = "message".into();
                    }
                    return Ok(Some(ev));
                }
                continue;
            }
            if line.starts_with(':') {
                continue;
            }
            let (field, value) = match line.split_once(':') {
                Some((f, v)) => (f, v.strip_prefix(' ').unwrap_or(v)),
                None => (line.as_str(), ""),
            };
            any = true;
            match field {
                "event" => ev.event = value.into(),
                "data" => {
                    if has_data {
                        ev.data.push('\n');
                    }
                    ev.data.push_str(value);
                    has_data = true;
                }
                "id" if !value.contains('\0') => ev.id = Some(value.into()),
                "retry" => ev.retry = value.parse().ok(),
                _ => {}
            }
        }
        Ok(None)
    }
}

// ── Streamable HTTP ─────────────────────────────────────────────────────────

pub struct Http {
    client: reqwest::Client,
    url: Url,
    headers: HeaderMap,
    session_id: Option<String>,
    /// Set from the `initialize` answer; sent on every request after it.
    pub protocol_version: Option<String>,
    terminate_on_close: bool,
    /// Messages the last POST brought back, not yet read.
    inbox: VecDeque<Value>,
}

impl Http {
    pub fn open(params: &Connection) -> Result<Self, String> {
        let connect = seconds(params, "timeout", 30.0)?;
        let read = seconds(params, "sse_read_timeout", 300.0)?;
        Ok(Http {
            client: client(connect, read)?,
            url: url(params)?,
            headers: headers(params)?,
            session_id: None,
            protocol_version: None,
            terminate_on_close: params.get("terminate_on_close").is_none_or(pyjson::truthy),
            inbox: VecDeque::new(),
        })
    }

    fn request(&self, method: reqwest::Method) -> reqwest::RequestBuilder {
        let mut req = self.client.request(method, self.url.clone()).headers(self.headers.clone());
        if let Some(id) = &self.session_id {
            req = req.header("mcp-session-id", id);
        }
        if let Some(v) = &self.protocol_version {
            req = req.header("mcp-protocol-version", v);
        }
        req
    }

    pub async fn send(&mut self, msg: &Value) -> Result<(), String> {
        let id = msg.get("method").and(msg.get("id")).cloned();
        let initializing = msg.get("method").is_some_and(|m| m == "initialize");
        let body = serde_json::to_vec(msg).map_err(|e| e.to_string())?;
        let resp = self
            .request(reqwest::Method::POST)
            .header("accept", format!("{JSON}, {SSE}"))
            .header("content-type", JSON)
            .body(body)
            .send()
            .await
            .map_err(|e| transport_error(&e))?;
        if resp.status() == StatusCode::ACCEPTED {
            return Ok(());
        }
        if resp.status() == StatusCode::NOT_FOUND {
            if let Some(id) = id {
                self.inbox.push_back(json!({"jsonrpc": "2.0", "id": id, "error": {"code": 32600, "message": "Session terminated"}}));
            }
            return Ok(());
        }
        if !resp.status().is_success() {
            return Err(crate::discovery::httpx_status_error(&resp));
        }
        if initializing {
            if let Some(sid) = resp.headers().get("mcp-session-id").and_then(|v| v.to_str().ok()).filter(|s| !s.is_empty()) {
                self.session_id = Some(sid.to_string());
            }
        }
        // A notification or a response gets no answer back.
        let Some(id) = id else { return Ok(()) };
        let content_type = resp.headers().get("content-type").and_then(|v| v.to_str().ok()).unwrap_or("").to_lowercase();
        if content_type.starts_with(JSON) {
            let bytes = resp.bytes().await.map_err(|e| transport_error(&e))?;
            let msg: Value = serde_json::from_slice(&bytes).map_err(|e| format!("Error parsing JSON response: {e}"))?;
            self.inbox.push_back(msg);
            return Ok(());
        }
        if !content_type.starts_with(SSE) {
            return Err(format!("Unexpected content type: {content_type}"));
        }
        self.read_stream(resp, &id).await
    }

    /// A request's event stream, up to its answer; resumed from the last
    /// event id if it breaks off first.
    async fn read_stream(&mut self, resp: reqwest::Response, id: &Value) -> Result<(), String> {
        let mut resp = resp;
        let mut last_id: Option<String> = None;
        let mut retry: Option<u64> = None;
        for attempt in 0..=MAX_RECONNECTS {
            let mut events = Events::new(resp);
            loop {
                let ev = match events.next().await {
                    Ok(Some(ev)) => ev,
                    Ok(None) | Err(_) => break,
                };
                if ev.id.is_some() {
                    last_id = ev.id.clone();
                }
                if ev.retry.is_some() {
                    retry = ev.retry;
                }
                if ev.event != "message" || ev.data.is_empty() {
                    continue;
                }
                let Ok(msg) = serde_json::from_str::<Value>(&ev.data) else { continue };
                let answered = msg.get("method").is_none() && msg.get("id") == Some(id);
                self.inbox.push_back(msg);
                if answered {
                    return Ok(());
                }
            }
            let Some(last) = last_id.clone() else { break };
            if attempt == MAX_RECONNECTS {
                break;
            }
            tokio::time::sleep(Duration::from_millis(retry.unwrap_or(1000))).await;
            resp = match self.request(reqwest::Method::GET).header("accept", SSE).header("last-event-id", last).send().await {
                Ok(r) if r.status().is_success() => r,
                _ => break,
            };
        }
        Ok(())
    }

    pub fn recv(&mut self) -> Incoming {
        match self.inbox.pop_front() {
            Some(msg) => Incoming::Message(msg),
            None => Incoming::Closed("Connection closed".into()),
        }
    }

    pub async fn close(self) {
        if !self.terminate_on_close || self.session_id.is_none() {
            return;
        }
        match self.request(reqwest::Method::DELETE).send().await {
            Ok(r) if r.status() == StatusCode::METHOD_NOT_ALLOWED || r.status().is_success() => {}
            Ok(r) => tracing::warn!("Session termination failed: {}", r.status().as_u16()),
            Err(e) => tracing::warn!("Session termination failed: {e}"),
        }
    }
}

// ── HTTP+SSE ────────────────────────────────────────────────────────────────

pub struct Sse {
    client: reqwest::Client,
    headers: HeaderMap,
    endpoint: Url,
    rx: mpsc::Receiver<Incoming>,
    reader: tokio::task::JoinHandle<()>,
}

impl Sse {
    pub async fn open(params: &Connection) -> Result<Self, String> {
        let connect = seconds(params, "timeout", 5.0)?;
        let read = seconds(params, "sse_read_timeout", 300.0)?;
        let client = client(connect, read)?;
        let url = url(params)?;
        let headers = headers(params)?;
        let resp = client
            .get(url.clone())
            .headers(headers.clone())
            .header("accept", SSE)
            .header("cache-control", "no-store")
            .send()
            .await
            .map_err(|e| transport_error(&e))?;
        if !resp.status().is_success() {
            return Err(crate::discovery::httpx_status_error(&resp));
        }
        let mut events = Events::new(resp);
        let endpoint = loop {
            match events.next().await? {
                Some(ev) if ev.event == "endpoint" => {
                    let endpoint = url.join(&ev.data).map_err(|e| format!("Invalid endpoint URL {:?}: {e}", ev.data))?;
                    if endpoint.scheme() != url.scheme() || endpoint.host_str() != url.host_str() || endpoint.port() != url.port() {
                        return Err(format!("Endpoint origin does not match connection origin: {endpoint}"));
                    }
                    break endpoint;
                }
                Some(_) => continue,
                None => return Err("Connection closed".into()),
            }
        };
        let (tx, rx) = mpsc::channel(16);
        let reader = tokio::spawn(async move {
            loop {
                match events.next().await {
                    Ok(Some(ev)) if ev.event == "message" => {
                        let Ok(msg) = serde_json::from_str::<Value>(&ev.data) else { continue };
                        if tx.send(Incoming::Message(msg)).await.is_err() {
                            return;
                        }
                    }
                    Ok(Some(_)) => {}
                    Ok(None) => break,
                    Err(e) => {
                        let _ = tx.send(Incoming::Closed(e)).await;
                        return;
                    }
                }
            }
            let _ = tx.send(Incoming::Closed("Connection closed".into())).await;
        });
        Ok(Sse { client, headers, endpoint, rx, reader })
    }

    pub async fn send(&mut self, msg: &Value) -> Result<(), String> {
        let body = serde_json::to_vec(msg).map_err(|e| e.to_string())?;
        let resp = self
            .client
            .post(self.endpoint.clone())
            .headers(self.headers.clone())
            .header("content-type", JSON)
            .body(body)
            .send()
            .await
            .map_err(|e| transport_error(&e))?;
        if !resp.status().is_success() {
            return Err(crate::discovery::httpx_status_error(&resp));
        }
        Ok(())
    }

    pub async fn recv(&mut self) -> Incoming {
        self.rx.recv().await.unwrap_or_else(|| Incoming::Closed("Connection closed".into()))
    }

    pub fn close(self) {}
}

impl Drop for Sse {
    /// The GET stream ends with the session, however it ends.
    fn drop(&mut self) {
        self.reader.abort();
    }
}
