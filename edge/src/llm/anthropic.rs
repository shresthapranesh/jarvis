//! Anthropic's Messages API, streamed — what `ChatAnthropic`
//! (langchain-anthropic) sent, built from the same shaped `Prompt`.
//!
//! The request follows `_get_request_payload` and `_format_messages`: runs of
//! user and tool messages merged into one user turn (a tool message is a
//! `tool_result` block), blank text blocks dropped, tool-call ids Anthropic
//! wouldn't accept rewritten the same way, `max_tokens` from the model
//! profiles langchain-anthropic ships. These differ on purpose
//! (`tests/test_edge_llm.py` names each one):
//!
//! - a stored PDF goes as a `document` block; LangChain sent its own `media`
//!   block, which Anthropic rejects;
//! - the reply keeps its stop reason as the finish reason (LangChain left it
//!   in the response metadata).

use std::sync::OnceLock;

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use super::lines::Lines;
use super::transcript::{Content, Media, Message, ModelRef, Part, Role, ToolCall, Typed, Usage};
use super::{Blobs, Delta, Error, Request, media_base64, new_id};

/// The API version every request names.
const VERSION: &str = "2023-06-01";

pub async fn complete(
    http: &reqwest::Client,
    base: &str,
    key: &str,
    name: &str,
    req: &Request<'_>,
    on_delta: &mut (dyn FnMut(Delta) + Send),
) -> Result<Message, Error> {
    let body = render(name, req)?;
    let resp = http
        .post(format!("{base}/v1/messages"))
        .header("x-api-key", key)
        .header("anthropic-version", VERSION)
        .json(&body)
        .send()
        .await
        .map_err(Error::connection)?;
    if !resp.status().is_success() {
        return Err(Error::from_response(resp).await);
    }
    let mut lines = Lines::new(resp);
    let mut reply = Reply::new(name);
    while let Some(data) = lines.next_event().await? {
        let event: Value =
            serde_json::from_str(&data).map_err(|e| Error::fatal(format!("unreadable stream event: {e}: {data}")))?;
        if event["type"] == "error" {
            return Err(stream_error(&event["error"]));
        }
        reply.absorb(&event, on_delta);
    }
    Ok(reply.finish())
}

/// An `error` event: overloaded, a server error or a rate limit is worth a
/// retry, as the SDK's status codes for them (529, 500, 429) would be.
fn stream_error(err: &Value) -> Error {
    let kind = err["type"].as_str().unwrap_or_default();
    Error {
        transient: matches!(kind, "overloaded_error" | "api_error" | "rate_limit_error"),
        status: None,
        message: err.to_string(),
    }
}

// ── request ──────────────────────────────────────────────────────────────────

/// `max_output_tokens` of langchain-anthropic's model profiles, by exact id —
/// what `ChatAnthropic` sends as `max_tokens` when none is set.
const MAX_OUTPUT_TOKENS: &[(&str, i64)] = &[
    ("claude-fable-5", 128_000),
    ("claude-haiku-4-5", 64_000),
    ("claude-haiku-4-5-20251001", 64_000),
    ("claude-opus-4-1", 32_000),
    ("claude-opus-4-1-20250805", 32_000),
    ("claude-opus-4-5", 64_000),
    ("claude-opus-4-5-20251101", 64_000),
    ("claude-opus-4-6", 128_000),
    ("claude-opus-4-7", 128_000),
    ("claude-opus-4-8", 128_000),
    ("claude-opus-5", 128_000),
    ("claude-sonnet-4-5", 64_000),
    ("claude-sonnet-4-5-20250929", 64_000),
    ("claude-sonnet-4-6", 128_000),
    ("claude-sonnet-5", 128_000),
];

/// `_FALLBACK_MAX_OUTPUT_TOKENS`: a model the profiles don't list.
const FALLBACK_MAX_TOKENS: i64 = 4096;

pub fn max_tokens(name: &str) -> i64 {
    MAX_OUTPUT_TOKENS.iter().find(|(id, _)| *id == name).map_or(FALLBACK_MAX_TOKENS, |(_, n)| *n)
}

/// `ContextCacheConfig.cache_control()` for Anthropic: `ttl` only when
/// `JARVIS_CACHE_TTL` asks for the hour (`resolve_cache_ttl`).
fn cache_control() -> Value {
    static TTL: OnceLock<bool> = OnceLock::new();
    let hour = *TTL.get_or_init(|| std::env::var("JARVIS_CACHE_TTL").is_ok_and(|v| v.trim() == "1h"));
    if hour { json!({"type": "ephemeral", "ttl": "1h"}) } else { json!({"type": "ephemeral"}) }
}

pub fn render(name: &str, req: &Request<'_>) -> Result<Value, Error> {
    let p = req.prompt;
    let system = if p.cached {
        Value::Array(
            p.system
                .iter()
                .map(|b| {
                    let mut block = json!({"type": "text", "text": b.text});
                    if b.breakpoint {
                        block["cache_control"] = cache_control();
                    }
                    block
                })
                .collect(),
        )
    } else {
        Value::String(p.system.iter().map(|b| b.text.as_str()).collect::<Vec<_>>().join("\n\n"))
    };

    // `_merge_messages`: a tool message is a user turn holding its
    // `tool_result`, and consecutive user turns become one.
    let mut turns: Vec<(&'static str, Value)> = vec![];
    for (i, m) in p.messages.iter().enumerate() {
        let marked = p.history_breakpoint == Some(i);
        let (role, content) = match m.role {
            Role::Assistant => ("assistant", assistant_content(m, marked, req.blobs)?),
            Role::Tool => ("user", Value::Array(vec![tool_result(m, p.cached, marked, req.blobs)?])),
            Role::User | Role::System => ("user", user_content(&m.content, p.cached, marked, req.blobs)?),
        };
        match turns.last_mut() {
            Some((last_role, last)) if *last_role == "user" && role == "user" => {
                let mut merged = match std::mem::take(last) {
                    Value::String(s) => vec![json!({"type": "text", "text": s})],
                    Value::Array(blocks) => blocks,
                    _ => vec![],
                };
                match content {
                    Value::String(s) => merged.push(json!({"type": "text", "text": s})),
                    Value::Array(blocks) => merged.extend(blocks),
                    _ => {}
                }
                *last = Value::Array(merged);
            }
            _ => turns.push((role, content)),
        }
    }

    let n = turns.len();
    let mut messages = vec![];
    for (i, (role, mut content)) in turns.into_iter().enumerate() {
        if role == "assistant" {
            if i + 1 == n {
                // The final assistant turn's trailing whitespace is refused.
                match &mut content {
                    Value::String(s) => *s = s.trim_end().to_string(),
                    Value::Array(blocks) => {
                        if let Some(last) = blocks.last_mut().filter(|b| b["type"] == "text") {
                            last["text"] = last["text"].as_str().unwrap_or_default().trim_end().into();
                        }
                    }
                    _ => {}
                }
            } else if content.as_str() == Some("") || content.as_array().is_some_and(Vec::is_empty) {
                // Every message but a final assistant one needs content.
                continue;
            }
        }
        messages.push(json!({"role": role, "content": content}));
    }

    let mut body = json!({
        "model": name,
        "max_tokens": max_tokens(name),
        "messages": messages,
        "system": system,
        "stream": true,
    });
    if !req.tools.is_empty() {
        let tools: Vec<Value> = req
            .tools
            .iter()
            .map(|t| json!({"name": t.name, "input_schema": t.parameters, "description": t.description}))
            .collect();
        body["tools"] = tools.into();
    }
    Ok(body)
}

/// `_normalize_tool_call_id`: an id outside `[a-zA-Z0-9_-]+` (another
/// provider's) becomes a stable `toolu_` hash, the same for the call and its
/// result.
pub fn tool_id(id: &str) -> String {
    if id.is_empty() || id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-') {
        return id.to_string();
    }
    format!("toolu_{}", &hex::encode(Sha256::digest(id.as_bytes()))[..24])
}

fn text_block(text: &str, extras: &Map<String, Value>) -> Value {
    let mut b = json!({"type": "text", "text": text});
    // `_format_text_block` keeps these of what the part carried.
    for key in ["cache_control", "citations"] {
        if let Some(v) = extras.get(key) {
            b[key] = v.clone();
        }
    }
    b
}

/// One stored part as a content block, or `None` for what isn't sent.
fn block(part: &Part, assistant: Option<&Message>, blobs: &Blobs) -> Result<Option<Value>, Error> {
    Ok(Some(match part {
        // A bare string is a text block, blank or not.
        Part::Str(s) => json!({"type": "text", "text": s}),
        Part::Typed(Typed::Text { text, extras, .. }) => {
            if text.trim().is_empty() {
                return Ok(None);
            }
            text_block(text, extras)
        }
        Part::Typed(Typed::Thinking { thinking, signature, extras }) => {
            let mut b = json!({"type": "thinking", "thinking": thinking});
            if let Some(sig) = signature {
                b["signature"] = sig.as_str().into();
            }
            if let Some(cc) = extras.get("cache_control") {
                b["cache_control"] = cc.clone();
            }
            b
        }
        Part::Typed(Typed::RedactedThinking { data, .. }) => json!({"type": "redacted_thinking", "data": data}),
        Part::Typed(Typed::Image(m) | Typed::File(m)) => media(m, blobs)?,
        Part::Typed(Typed::Opaque { data, .. }) => return Ok(opaque(data, assistant)),
    }))
}

/// A stored image (`_format_image`) or, on purpose, a document.
fn media(m: &Media, blobs: &Blobs) -> Result<Value, Error> {
    if let Some(url) = m.url.as_deref().filter(|_| m.data.is_none() && m.blob.is_none()) {
        if url.starts_with("http://") || url.starts_with("https://") {
            return Ok(json!({"type": "image", "source": {"type": "url", "url": url}}));
        }
    }
    let mime = m.mime_type.as_deref().unwrap_or("application/octet-stream");
    let data = media_base64(m, blobs)?;
    if mime.starts_with("image/") {
        return Ok(json!({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}));
    }
    if mime == "application/pdf" {
        return Ok(json!({"type": "document", "source": {"type": "base64", "media_type": mime, "data": data}}));
    }
    Err(Error::fatal(format!("a {mime} part can't go to Anthropic")))
}

/// A provider's own block, as `_format_messages` passes it on: an Anthropic
/// tool call (`tool_use`) the message's own call replaces, another
/// provider's reasoning or call items left out.
fn opaque(data: &Value, assistant: Option<&Message>) -> Option<Value> {
    let kind = data["type"].as_str().unwrap_or_default();
    let from_anthropic = assistant
        .and_then(|m| m.model.as_ref())
        .and_then(|r| r.provider.as_deref())
        .is_some_and(|p| p == "anthropic");
    match kind {
        "reasoning" | "function_call" if !from_anthropic => None,
        // Answered from `tool_calls`, in `assistant_content`.
        "tool_use" => Some(data.clone()),
        "server_tool_use" | "mcp_tool_use" => {
            let mut out = Map::new();
            for key in ["type", "id", "input", "name", "server_name", "cache_control"] {
                if let Some(v) = data.get(key) {
                    out.insert(key.into(), v.clone());
                }
            }
            if data.get("input") == Some(&json!({}))
                && let Some(Ok(input)) = data.get("partial_json").and_then(Value::as_str).map(serde_json::from_str::<Value>)
                && input.as_object().is_some_and(|o| !o.is_empty())
            {
                out.insert("input".into(), input);
            }
            Some(Value::Object(out))
        }
        _ => Some(data.clone()),
    }
}

/// `tool_use` from a recorded call.
fn tool_use(call: &ToolCall) -> Value {
    json!({"type": "tool_use", "name": call.name, "input": call.args, "id": tool_id(call.id.as_deref().unwrap_or_default())})
}

fn mark(blocks: &mut [Value]) {
    if let Some(last) = blocks.last_mut() {
        last["cache_control"] = cache_control();
    }
}

fn assistant_content(m: &Message, marked: bool, blobs: &Blobs) -> Result<Value, Error> {
    let mut blocks = match &m.content {
        Content::Text(s) if m.tool_calls.is_empty() && !marked => return Ok(Value::String(s.clone())),
        Content::Text(s) if s.is_empty() => vec![],
        Content::Text(s) => vec![json!({"type": "text", "text": s})],
        Content::Parts(parts) => {
            let mut out = vec![];
            for p in parts {
                // A recorded `tool_use` is sent as the message's own call
                // with that id; one without a call keeps what it recorded.
                if p.opaque_type() == Some("tool_use") {
                    let Part::Typed(Typed::Opaque { data, .. }) = p else { unreachable!() };
                    let id = data["id"].as_str().unwrap_or_default();
                    match m.tool_calls.iter().find(|c| c.id.as_deref() == Some(id)).filter(|_| data.get("caller").is_none()) {
                        Some(call) => out.push(tool_use(call)),
                        None => out.push(recorded_tool_use(data)),
                    }
                    continue;
                }
                if let Some(b) = block(p, Some(m), blobs)? {
                    out.push(b);
                }
            }
            out
        }
    };
    if marked {
        mark(&mut blocks);
    }
    // Every call has its `tool_use`.
    let sent: Vec<String> =
        blocks.iter().filter(|b| b["type"] == "tool_use").filter_map(|b| b["id"].as_str().map(str::to_string)).collect();
    for call in &m.tool_calls {
        if !sent.contains(&tool_id(call.id.as_deref().unwrap_or_default())) {
            blocks.push(tool_use(call));
        }
    }
    Ok(Value::Array(blocks))
}

/// A `tool_use` block with no call beside it: its input, or what streamed.
fn recorded_tool_use(data: &Value) -> Value {
    let input = match data.get("input").filter(|i| i.as_object().is_some_and(|o| !o.is_empty())) {
        Some(input) => input.clone(),
        None => data
            .get("partial_json")
            .and_then(Value::as_str)
            .map(|raw| serde_json::from_str(if raw.is_empty() { "{}" } else { raw }).unwrap_or_else(|_| json!({})))
            .unwrap_or_else(|| json!({})),
    };
    let mut b = json!({"type": "tool_use", "name": data["name"], "input": input, "id": tool_id(data["id"].as_str().unwrap_or_default())});
    if let Some(caller) = data.get("caller") {
        b["caller"] = caller.clone();
    }
    b
}

/// A user message's content: a string, or blocks — a cached prompt's
/// non-blank string as one text block (`normalize_history_content`).
fn user_content(content: &Content, cached: bool, marked: bool, blobs: &Blobs) -> Result<Value, Error> {
    let mut blocks = match content {
        Content::Text(s) if !cached || s.trim().is_empty() => return Ok(Value::String(s.clone())),
        Content::Text(s) => vec![json!({"type": "text", "text": s})],
        Content::Parts(parts) => {
            let mut out = vec![];
            for p in parts {
                if let Some(b) = block(p, None, blobs)? {
                    out.push(b);
                }
            }
            out
        }
    };
    if marked {
        mark(&mut blocks);
    }
    Ok(Value::Array(blocks))
}

/// A tool message as its `tool_result`, the breakpoint hoisted onto it.
fn tool_result(m: &Message, cached: bool, marked: bool, blobs: &Blobs) -> Result<Value, Error> {
    let mut content = user_content(&m.content, cached, false, blobs)?;
    let mut out = json!({
        "type": "tool_result",
        "tool_use_id": tool_id(m.tool_call_id.as_deref().unwrap_or_default()),
        "is_error": m.status.as_deref() == Some("error"),
    });
    if marked {
        out["cache_control"] = cache_control();
    }
    // A block's own breakpoint can't stay inside a tool_result.
    if let Some(blocks) = content.as_array_mut() {
        for b in blocks {
            if let Some(cc) = b.as_object_mut().and_then(|o| o.remove("cache_control")) {
                out["cache_control"] = cc;
            }
        }
    }
    out["content"] = content;
    Ok(out)
}

// ── reply ────────────────────────────────────────────────────────────────────

enum Block {
    Text(String),
    Thinking { thinking: String, signature: String },
    Redacted(String),
    Call { id: String, name: String, args: String },
    /// Server-side blocks and anything newer: not kept.
    Other,
}

struct Reply {
    id: Option<String>,
    model: String,
    blocks: Vec<Block>,
    usage: Option<Value>,
    stop_reason: Option<String>,
}

impl Reply {
    fn new(model: &str) -> Self {
        Reply { id: None, model: model.to_string(), blocks: vec![], usage: None, stop_reason: None }
    }

    fn block(&mut self, index: &Value) -> Option<&mut Block> {
        self.blocks.get_mut(index.as_u64()? as usize)
    }

    fn absorb(&mut self, event: &Value, on_delta: &mut (dyn FnMut(Delta) + Send)) {
        match event["type"].as_str().unwrap_or_default() {
            "message_start" => {
                let m = &event["message"];
                self.id = m["id"].as_str().map(str::to_string);
                if let Some(model) = m["model"].as_str() {
                    self.model = model.to_string();
                }
                if m["usage"].is_object() {
                    self.usage = Some(m["usage"].clone());
                }
            }
            "content_block_start" => {
                let b = &event["content_block"];
                let index = event["index"].as_u64().unwrap_or(self.blocks.len() as u64) as usize;
                while self.blocks.len() <= index {
                    self.blocks.push(Block::Other);
                }
                let s = |k: &str| b[k].as_str().unwrap_or_default().to_string();
                self.blocks[index] = match b["type"].as_str().unwrap_or_default() {
                    "text" => {
                        let text = s("text");
                        if !text.is_empty() {
                            on_delta(Delta::Text(&text));
                        }
                        Block::Text(text)
                    }
                    "thinking" => {
                        let thinking = s("thinking");
                        if !thinking.is_empty() {
                            on_delta(Delta::Thinking(&thinking));
                        }
                        Block::Thinking { thinking, signature: s("signature") }
                    }
                    "redacted_thinking" => Block::Redacted(s("data")),
                    "tool_use" => {
                        on_delta(Delta::ToolCall);
                        // Arguments can come whole, with no deltas after.
                        let args = match b.get("input").filter(|i| i.as_object().is_some_and(|o| !o.is_empty())) {
                            Some(input) => crate::pyjson::dumps(input),
                            None => String::new(),
                        };
                        Block::Call { id: s("id"), name: s("name"), args }
                    }
                    _ => Block::Other,
                };
            }
            "content_block_delta" => {
                let d = &event["delta"];
                let piece = |k: &str| d[k].as_str().unwrap_or_default();
                match (d["type"].as_str().unwrap_or_default(), self.block(&event["index"])) {
                    ("text_delta", Some(Block::Text(text))) => {
                        on_delta(Delta::Text(piece("text")));
                        text.push_str(piece("text"));
                    }
                    ("thinking_delta", Some(Block::Thinking { thinking, .. })) => {
                        on_delta(Delta::Thinking(piece("thinking")));
                        thinking.push_str(piece("thinking"));
                    }
                    ("signature_delta", Some(Block::Thinking { signature, .. })) => signature.push_str(piece("signature")),
                    ("input_json_delta", Some(Block::Call { args, .. })) => {
                        on_delta(Delta::ToolCall);
                        args.push_str(piece("partial_json"));
                    }
                    _ => {}
                }
            }
            "message_delta" => {
                if let Some(r) = event["delta"]["stop_reason"].as_str() {
                    self.stop_reason = Some(r.to_string());
                }
                // Cumulative: the final counts, cache reads and writes too.
                if let Some(u) = event["usage"].as_object() {
                    let mut merged = self.usage.take().and_then(|v| v.as_object().cloned()).unwrap_or_default();
                    for (k, v) in u {
                        if !v.is_null() {
                            merged.insert(k.clone(), v.clone());
                        }
                    }
                    self.usage = Some(Value::Object(merged));
                }
            }
            _ => {}
        }
    }

    fn finish(self) -> Message {
        let mut parts = vec![];
        let mut calls = vec![];
        let mut thought = false;
        for b in self.blocks {
            match b {
                Block::Text(text) if !text.is_empty() => {
                    parts.push(Part::Typed(Typed::Text { text, signature: None, extras: Map::new() }))
                }
                Block::Thinking { thinking, signature } => {
                    thought = true;
                    let signature = (!signature.is_empty()).then_some(signature);
                    parts.push(Part::Typed(Typed::Thinking { thinking, signature, extras: Map::new() }));
                }
                Block::Redacted(data) => {
                    thought = true;
                    parts.push(Part::Typed(Typed::RedactedThinking { data, extras: Map::new() }));
                }
                Block::Call { id, name, args } => calls.push((id, name, args)),
                Block::Text(_) | Block::Other => {}
            }
        }
        let content = if thought {
            Content::Parts(parts)
        } else {
            Content::Text(
                parts
                    .into_iter()
                    .filter_map(|p| match p {
                        Part::Typed(Typed::Text { text, .. }) => Some(text),
                        _ => None,
                    })
                    .collect(),
            )
        };
        let mut m = Message::new(Role::Assistant, content);
        m.id = Some(self.id.unwrap_or_else(new_id));
        for (id, name, args) in calls {
            let id = if id.is_empty() { new_id() } else { id };
            match super::parse_args(&args) {
                Ok(args) => m.tool_calls.push(ToolCall { id: Some(id), name, args, signature: None }),
                Err(error) => m.invalid_tool_calls.push(json!({"id": id, "name": name, "args": args, "error": error})),
            }
        }
        m.usage = self.usage.as_ref().map(usage);
        m.model = Some(ModelRef { provider: Some("anthropic".into()), name: Some(self.model) });
        m.finish_reason = self.stop_reason.map(Value::String);
        m
    }
}

/// `_create_usage_metadata`: `input_tokens` excludes the cache, so reads and
/// writes are added back; the TTL split replaces the generic write count.
fn usage(u: &Value) -> Usage {
    let n = |v: &Value, k: &str| v.get(k).and_then(Value::as_i64);
    let mut details = Map::new();
    let read = n(u, "cache_read_input_tokens");
    let mut creation = n(u, "cache_creation_input_tokens");
    let mut specific = 0;
    if let Some(split) = u.get("cache_creation").filter(|c| c.is_object()) {
        for key in ["ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"] {
            let v = n(split, key);
            specific += v.unwrap_or(0);
            if let Some(v) = v {
                details.insert(key.into(), v.into());
            }
        }
        if specific > 0 {
            creation = Some(0);
        }
    }
    if let Some(r) = read {
        details.insert("cache_read".into(), r.into());
    }
    if let Some(c) = creation {
        details.insert("cache_creation".into(), c.into());
    }
    let written = if specific > 0 { specific } else { creation.unwrap_or(0) };
    let input = n(u, "input_tokens").unwrap_or(0) + read.unwrap_or(0) + written;
    let output = n(u, "output_tokens").unwrap_or(0);
    Usage {
        input: Some(input),
        output: Some(output),
        total: Some(input + output),
        input_details: Some(details),
        output_details: None,
        extras: Map::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_another_provider_minted_are_hashed() {
        assert_eq!(tool_id("toolu_01Ab-x_9"), "toolu_01Ab-x_9");
        assert_eq!(tool_id(""), "");
        let hashed = tool_id("functions.write_todos:0");
        assert!(hashed.starts_with("toolu_") && hashed.len() == 30);
        assert_eq!(hashed, tool_id("functions.write_todos:0"));
    }

    #[test]
    fn max_tokens_follow_the_profiles() {
        assert_eq!(max_tokens("claude-sonnet-4-6"), 128_000);
        assert_eq!(max_tokens("claude-haiku-4-5-20251001"), 64_000);
        assert_eq!(max_tokens("claude-opus-5-5"), 4096);
    }

    #[test]
    fn usage_adds_the_cache_back() {
        let u = usage(&json!({"input_tokens": 112, "cache_read_input_tokens": 700, "cache_creation_input_tokens": 50,
                              "output_tokens": 40}));
        assert_eq!((u.input, u.output, u.total), (Some(862), Some(40), Some(902)));
        let split = usage(&json!({"input_tokens": 1, "cache_creation_input_tokens": 30, "output_tokens": 2,
                                  "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 20}}));
        assert_eq!(split.input, Some(31));
        assert_eq!(
            Value::Object(split.input_details.unwrap()),
            json!({"cache_creation": 0, "ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 20})
        );
    }

    #[test]
    fn reply_from_events() {
        let events = [
            json!({"type": "message_start", "message": {"id": "msg_1", "model": "claude-x", "usage": {"input_tokens": 5}}}),
            json!({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
            json!({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Hm."}}),
            json!({"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "c2ln"}}),
            json!({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
            json!({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hi"}}),
            json!({"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {}}}),
            json!({"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"q\": 1}"}}),
            json!({"type": "content_block_start", "index": 3, "content_block": {"type": "tool_use", "id": "toolu_2", "name": "g", "input": {"whole": true}}}),
            json!({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"input_tokens": 5, "output_tokens": 9}}),
        ];
        let mut r = Reply::new("asked");
        let mut seen = vec![];
        for e in &events {
            r.absorb(e, &mut |d| seen.push(format!("{d:?}")));
        }
        let m = serde_json::to_value(r.finish()).unwrap();
        assert_eq!(m["id"], "msg_1");
        assert_eq!(m["content"], json!([{"type": "thinking", "thinking": "Hm.", "signature": "c2ln"}, {"type": "text", "text": "Hi"}]));
        assert_eq!(m["tool_calls"], json!([{"id": "toolu_1", "name": "f", "args": {"q": 1}}, {"id": "toolu_2", "name": "g", "args": {"whole": true}}]));
        assert_eq!(m["usage"]["input"], 5);
        assert_eq!(m["model"], json!({"provider": "anthropic", "name": "claude-x"}));
        assert_eq!(m["finish_reason"], "tool_use");
        assert_eq!(seen, ["Thinking(\"Hm.\")", "Text(\"Hi\")", "ToolCall", "ToolCall", "ToolCall"]);
    }
}
