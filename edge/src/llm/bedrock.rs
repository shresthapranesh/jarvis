//! Bedrock's ConverseStream — what `ChatBedrockConverse` (langchain-aws) sent
//! through boto3, signed here with `aws.rs`.
//!
//! The request follows `_messages_to_bedrock` and `_lc_content_to_bedrock`:
//! runs of user turns merged (`merge_message_runs`: two strings joined by a
//! newline), a tool message as a `toolResult` block in the user turn before
//! it, a blank text as `"."`, signed thinking as `reasoningContent`, unsigned
//! thinking left out, breakpoints as the `cachePoint` blocks the shaped
//! prompt marks, tool schemas with their null branches stripped
//! (`_strip_null_anyof`). A block LangChain refused (another provider's
//! reasoning or call items, a non-image file) fails the call here too.
//!
//! The reply comes back as AWS event-stream frames. These differ on purpose
//! (`tests/test_edge_llm.py` names each one):
//!
//! - the reasoning is recorded as thinking, so the next turn strips it as it
//!   strips Anthropic's (LangChain kept it in an opaque block that rode
//!   along); a reply LangChain recorded is sent back as LangChain sends it;
//! - the record names its provider by catalog id (`bedrock`, not
//!   `bedrock_converse`) and keeps the stop reason as its finish reason.

use futures_util::StreamExt;
use serde_json::{Map, Value, json};
use reqwest::Url;

use super::transcript::{Content, Media, Message, ModelRef, Part, Role, ToolCall, Typed, Usage};
use super::{Blobs, Delta, Error, Request, media_base64, new_id};
use crate::aws;

/// What boto3 sends for a blank text (`EMPTY_CONTENT`).
const EMPTY: &str = ".";

pub async fn complete(
    http: &reqwest::Client,
    name: &str,
    req: &Request<'_>,
    on_delta: &mut (dyn FnMut(Delta) + Send),
) -> Result<Message, Error> {
    let body = render(req)?;
    let creds = aws::credentials().await.map_err(|e| Error::fatal(e.message()))?;
    let region = aws::region();
    let raw = format!("{}/model/{}/converse-stream", aws::endpoint("bedrock-runtime", &region), aws::encode_label(name));
    let url = Url::parse(&raw).map_err(|e| Error::fatal(format!("{raw}: {e}")))?;
    let payload = serde_json::to_vec(&body).map_err(|e| Error::fatal(e.to_string()))?;
    let headers =
        aws::sign(&creds, &region, "bedrock", "POST", &url, &[("content-type", "application/json")], &payload, chrono::Utc::now());
    let mut post = http.post(url.clone()).body(payload);
    for (k, v) in &headers {
        if k != "host" {
            post = post.header(k, v);
        }
    }
    let resp = post.send().await.map_err(Error::connection)?;
    let status = resp.status().as_u16();
    if !(200..300).contains(&status) {
        let error_type = resp.headers().get("x-amzn-errortype").and_then(|v| v.to_str().ok()).map(str::to_string);
        let text = resp.bytes().await.map_err(Error::connection)?;
        return Err(match aws::error_parts(status, error_type.as_deref(), &text) {
            Ok((code, message)) => Error {
                transient: status == 429 || status >= 500 || code.contains("Throttl"),
                status: Some(status),
                message: format!("An error occurred ({code}) when calling the ConverseStream operation: {message}"),
            },
            Err(_) => Error { transient: status >= 500, status: Some(status), message: format!("{status}") },
        });
    }
    let mut frames = Frames::new(resp);
    let mut reply = Reply::new(name);
    while let Some(frame) = frames.next().await? {
        match frame.header(":message-type") {
            Some("event") => {
                let payload: Value = serde_json::from_slice(&frame.payload)
                    .map_err(|e| Error::fatal(format!("unreadable stream event: {e}")))?;
                reply.absorb(frame.header(":event-type").unwrap_or_default(), &payload, on_delta);
            }
            Some("exception") => {
                let kind = frame.header(":exception-type").unwrap_or("exception").to_string();
                let message = serde_json::from_slice::<Value>(&frame.payload)
                    .ok()
                    .and_then(|v| v["message"].as_str().map(str::to_string))
                    .unwrap_or_default();
                return Err(Error {
                    transient: matches!(
                        kind.as_str(),
                        "throttlingException" | "serviceUnavailableException" | "internalServerException"
                    ),
                    status: None,
                    message: format!("Received AWS exception {kind}: {message}"),
                });
            }
            _ => {
                let code = frame.header(":error-code").unwrap_or("error").to_string();
                return Err(Error::fatal(format!("{code}: {}", frame.header(":error-message").unwrap_or_default())));
            }
        }
    }
    Ok(reply.finish())
}

// ── event-stream frames ──────────────────────────────────────────────────────

/// `zlib.crc32`, as the frames carry it.
fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc ^= u32::from(b);
        for _ in 0..8 {
            crc = if crc & 1 == 1 { (crc >> 1) ^ 0xEDB8_8320 } else { crc >> 1 };
        }
    }
    !crc
}

struct Frame {
    headers: Vec<(String, String)>,
    payload: Vec<u8>,
}

impl Frame {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }
}

/// `application/vnd.amazon.eventstream`: a prelude (total length, headers
/// length, their CRC), the headers, the payload, and the whole frame's CRC.
struct Frames {
    body: futures_util::stream::BoxStream<'static, reqwest::Result<bytes::Bytes>>,
    buf: Vec<u8>,
}

impl Frames {
    fn new(resp: reqwest::Response) -> Self {
        Frames { body: resp.bytes_stream().boxed(), buf: vec![] }
    }

    async fn next(&mut self) -> Result<Option<Frame>, Error> {
        loop {
            if self.buf.len() >= 12 {
                let total = u32::from_be_bytes(self.buf[0..4].try_into().expect("4 bytes")) as usize;
                if total < 16 {
                    return Err(Error::fatal(format!("an event-stream frame of {total} bytes")));
                }
                if self.buf.len() >= total {
                    let raw: Vec<u8> = self.buf.drain(..total).collect();
                    return parse_frame(&raw).map(Some);
                }
            }
            match self.body.next().await {
                Some(Ok(chunk)) => self.buf.extend_from_slice(&chunk),
                Some(Err(e)) => return Err(Error::connection(e)),
                None if self.buf.is_empty() => return Ok(None),
                None => return Err(Error { transient: true, status: None, message: "the event stream ended mid-frame".into() }),
            }
        }
    }
}

fn parse_frame(raw: &[u8]) -> Result<Frame, Error> {
    let bad = |what: &str| Error::fatal(format!("a corrupt event-stream frame: {what}"));
    let total = raw.len();
    let headers_len = u32::from_be_bytes(raw[4..8].try_into().expect("4 bytes")) as usize;
    if u32::from_be_bytes(raw[8..12].try_into().expect("4 bytes")) != crc32(&raw[..8]) {
        return Err(bad("prelude checksum"));
    }
    if u32::from_be_bytes(raw[total - 4..].try_into().expect("4 bytes")) != crc32(&raw[..total - 4]) {
        return Err(bad("message checksum"));
    }
    if 12 + headers_len > total - 4 {
        return Err(bad("headers length"));
    }
    let mut headers = vec![];
    let mut h = &raw[12..12 + headers_len];
    let take = |h: &mut &[u8], n: usize| -> Result<Vec<u8>, Error> {
        if h.len() < n {
            return Err(bad("a header"));
        }
        let (a, b) = h.split_at(n);
        *h = b;
        Ok(a.to_vec())
    };
    while !h.is_empty() {
        let n = take(&mut h, 1)?[0] as usize;
        let name = String::from_utf8_lossy(&take(&mut h, n)?).into_owned();
        let kind = take(&mut h, 1)?[0];
        let value = match kind {
            0 => "true".into(),
            1 => "false".into(),
            2 => take(&mut h, 1)?[0].to_string(),
            3 => i16::from_be_bytes(take(&mut h, 2)?.try_into().expect("2 bytes")).to_string(),
            4 => i32::from_be_bytes(take(&mut h, 4)?.try_into().expect("4 bytes")).to_string(),
            5 | 8 => i64::from_be_bytes(take(&mut h, 8)?.try_into().expect("8 bytes")).to_string(),
            6 | 7 => {
                let n = u16::from_be_bytes(take(&mut h, 2)?.try_into().expect("2 bytes")) as usize;
                String::from_utf8_lossy(&take(&mut h, n)?).into_owned()
            }
            9 => hex::encode(take(&mut h, 16)?),
            _ => return Err(bad("a header type")),
        };
        headers.push((name, value));
    }
    Ok(Frame { headers, payload: raw[12 + headers_len..total - 4].to_vec() })
}

// ── request ──────────────────────────────────────────────────────────────────

fn cache_point() -> Value {
    json!({"cachePoint": {"type": "default"}})
}

pub fn render(req: &Request<'_>) -> Result<Value, Error> {
    let p = req.prompt;
    let mut system = vec![];
    if p.cached {
        for b in &p.system {
            system.push(text(&b.text));
            if b.breakpoint {
                system.push(cache_point());
            }
        }
    } else {
        let joined = p.system.iter().map(|b| b.text.as_str()).collect::<Vec<_>>().join("\n\n");
        system.push(text(&joined));
    }

    // The breakpoint is a `cachePoint` block closing its message's content;
    // a cached prompt's non-blank user or tool string is a text block first
    // (`normalize_history_content`). Then `merge_message_runs`: consecutive
    // user or assistant messages become one, two strings joined by a
    // newline, the calls one after another; tool messages are never merged.
    let mut merged: Vec<(Role, Content, Vec<ToolCall>, usize)> = vec![];
    for (i, m) in p.messages.iter().enumerate() {
        let mut content = m.content.clone();
        if let Content::Text(s) = &content
            && p.cached
            && matches!(m.role, Role::User | Role::Tool)
            && !s.trim().is_empty()
        {
            content = Content::Parts(vec![text_part(s)]);
        }
        if p.history_breakpoint == Some(i) {
            let mut parts = match content {
                Content::Text(s) => vec![text_part(&s)],
                Content::Parts(parts) => parts,
            };
            parts.push(Part::Typed(Typed::Opaque { data: cache_point(), extras: Map::new() }));
            content = Content::Parts(parts);
        }
        if let Some((role, last, calls, _)) = merged.last_mut()
            && *role == m.role
            && matches!(m.role, Role::User | Role::Assistant)
        {
            *last = join(std::mem::replace(last, Content::Text(String::new())), content);
            calls.extend(m.tool_calls.iter().cloned());
            continue;
        }
        merged.push((m.role, content, m.tool_calls.clone(), i));
    }

    let mut messages: Vec<Value> = vec![];
    for (role, content, calls, i) in merged {
        let blocks = content_blocks(&content, req.blobs)?;
        let turn = match role {
            Role::Assistant => {
                let mut blocks = blocks;
                upsert_calls(&mut blocks, &calls);
                messages.push(json!({"role": "assistant", "content": blocks}));
                continue;
            }
            Role::Tool => {
                let m = &p.messages[i];
                let (points, result): (Vec<Value>, Vec<Value>) = blocks.into_iter().partition(|b| b.get("cachePoint").is_some());
                let mut out = vec![json!({"toolResult": {
                    "content": result,
                    "toolUseId": m.tool_call_id,
                    "status": m.status.as_deref().unwrap_or("success"),
                }})];
                out.extend(points);
                out
            }
            Role::User | Role::System => blocks,
        };
        match messages.last_mut() {
            Some(last) if last["role"] == "user" => last["content"].as_array_mut().expect("blocks").extend(turn),
            _ => messages.push(json!({"role": "user", "content": turn})),
        }
    }
    if messages.is_empty() {
        messages.push(json!({"role": "user", "content": [text("")]}));
    }

    let mut body = json!({"messages": messages, "system": system, "inferenceConfig": {}});
    if !req.tools.is_empty() {
        let tools: Vec<Value> = req
            .tools
            .iter()
            .map(|t| {
                let description = if t.description.is_empty() { t.name.clone() } else { t.description.clone() };
                json!({"toolSpec": {"name": t.name, "description": description,
                                    "inputSchema": {"json": strip_null_anyof(&t.parameters)}}})
            })
            .collect();
        body["toolConfig"] = json!({"tools": tools});
    }
    Ok(body)
}

/// `merge_content` for two user messages' content.
fn join(last: Content, next: Content) -> Content {
    match (last, next) {
        (Content::Text(a), Content::Text(b)) if !a.is_empty() && !b.is_empty() => Content::Text(format!("{a}\n{b}")),
        (Content::Text(a), Content::Text(b)) => Content::Text(a + &b),
        (Content::Text(a), Content::Parts(b)) => Content::Parts(std::iter::once(Part::Str(a)).chain(b).collect()),
        (Content::Parts(mut a), Content::Text(b)) => {
            match a.last_mut() {
                Some(Part::Str(s)) => s.push_str(&b),
                _ => a.push(Part::Str(b)),
            }
            Content::Parts(a)
        }
        (Content::Parts(mut a), Content::Parts(b)) => {
            a.extend(b);
            Content::Parts(a)
        }
    }
}

fn text_part(s: &str) -> Part {
    Part::Typed(Typed::Text { text: s.to_string(), signature: None, extras: Map::new() })
}

fn text(s: &str) -> Value {
    json!({"text": if s.trim().is_empty() { EMPTY } else { s }})
}

/// `_lc_content_to_bedrock`, then its last step: an empty text dropped.
fn content_blocks(content: &Content, blobs: &Blobs) -> Result<Vec<Value>, Error> {
    let blocks = match content {
        Content::Text(s) => vec![text(s)],
        Content::Parts(parts) if parts.is_empty() => vec![text("")],
        Content::Parts(parts) => {
            let mut out = vec![];
            for p in parts {
                if let Some(b) = block(p, blobs)? {
                    out.push(b);
                }
            }
            out
        }
    };
    Ok(blocks.into_iter().filter(|b| b.get("text").is_none_or(|t| t.as_str() != Some(""))).collect())
}

fn unsupported(what: &str) -> Error {
    Error::fatal(format!("Unsupported content block type: {what}"))
}

fn block(part: &Part, blobs: &Blobs) -> Result<Option<Value>, Error> {
    Ok(Some(match part {
        // A bare string is kept as it is (only an empty one is dropped).
        Part::Str(s) => json!({"text": s}),
        Part::Typed(Typed::Text { text: t, .. }) => text(t),
        Part::Typed(Typed::Thinking { thinking, signature, .. }) => match signature.as_deref().filter(|s| !s.is_empty()) {
            Some(sig) => json!({"reasoningContent": {"reasoningText": {"text": thinking, "signature": sig}}}),
            None => return Ok(None),
        },
        Part::Typed(Typed::RedactedThinking { .. }) => return Err(unsupported("redacted_thinking")),
        Part::Typed(Typed::Image(m) | Typed::File(m)) => image(m, blobs)?,
        Part::Typed(Typed::Opaque { data, .. }) => return opaque(data),
    }))
}

/// `_format_openai_image_url`: a base64 image, its format the MIME subtype.
fn image(m: &Media, blobs: &Blobs) -> Result<Value, Error> {
    let mime = m.mime_type.as_deref().unwrap_or_default();
    let Some(format) = mime.strip_prefix("image/").filter(|_| m.data.is_some() || m.blob.is_some()) else {
        return Err(Error::fatal(
            "The image URL provided is not supported. Expected image URL format is base64-encoded images.",
        ));
    };
    Ok(json!({"image": {"format": format, "source": {"bytes": media_base64(m, blobs)?}}}))
}

/// A provider's own block, as `_lc_content_to_bedrock` takes it.
fn opaque(data: &Value) -> Result<Option<Value>, Error> {
    let Some(kind) = data.get("type").and_then(Value::as_str) else {
        // Already in Bedrock's shape (a `cachePoint`).
        return Ok(Some(data.clone()));
    };
    let s = |k: &str| data.get(k).cloned().unwrap_or(Value::Null);
    Ok(Some(match kind {
        "text" => text(data["text"].as_str().unwrap_or_default()),
        "tool_use" | "server_tool_use" => {
            let input = match data.get("input") {
                Some(Value::String(raw)) if raw.is_empty() => json!({}),
                Some(Value::String(raw)) => serde_json::from_str(raw).unwrap_or_else(|_| json!({})),
                Some(v) => v.clone(),
                None => json!({}),
            };
            json!({"toolUse": {"toolUseId": s("id"), "input": input, "name": s("name")}})
        }
        "thinking" => match data["signature"].as_str().filter(|s| !s.is_empty()) {
            Some(sig) => json!({"reasoningContent": {"reasoningText": {"text": data["thinking"].as_str().unwrap_or_default(), "signature": sig}}}),
            None => return Ok(None),
        },
        "reasoning_content" => {
            let rc = data.get("reasoning_content").or_else(|| data.get("reasoningContent")).cloned().unwrap_or_default();
            match rc["signature"].as_str().filter(|s| !s.is_empty()) {
                Some(sig) => json!({"reasoningContent": {"reasoningText": {"text": rc["text"].as_str().unwrap_or_default(), "signature": sig}}}),
                None => return Ok(None),
            }
        }
        other => return Err(unsupported(other)),
    }))
}

/// `_upsert_tool_calls_to_bedrock_content`: each call as a `toolUse`, a
/// recorded one updated in place.
fn upsert_calls(blocks: &mut Vec<Value>, calls: &[ToolCall]) {
    if !calls.is_empty() && *blocks == [json!({"text": EMPTY})] {
        blocks.clear();
    }
    for call in calls {
        let id = call.id.clone().map(Value::String).unwrap_or(Value::Null);
        match blocks.iter_mut().find(|b| b.get("toolUse").is_some_and(|t| t["toolUseId"] == id)) {
            Some(b) => {
                b["toolUse"]["input"] = call.args.clone();
                b["toolUse"]["name"] = call.name.as_str().into();
            }
            None => blocks.push(json!({"toolUse": {"toolUseId": id, "input": call.args, "name": call.name}})),
        }
    }
}

/// `_strip_null_anyof`: a nullable parameter as its concrete type.
pub fn strip_null_anyof(schema: &Value) -> Value {
    let Value::Object(map) = schema else { return schema.clone() };
    let mut out = Map::new();
    for (key, value) in map {
        match (key.as_str(), value) {
            ("anyOf", Value::Array(variants)) => {
                let kept: Vec<Value> =
                    variants.iter().filter(|v| v.get("type").and_then(Value::as_str) != Some("null")).map(strip_null_anyof).collect();
                if kept.len() == 1 {
                    if let Value::Object(only) = &kept[0] {
                        out.extend(only.clone());
                    }
                    continue;
                }
                out.insert(key.clone(), if kept.is_empty() { value.clone() } else { Value::Array(kept) });
            }
            ("type", Value::Array(types)) => {
                let kept: Vec<Value> = types.iter().filter(|t| t.as_str() != Some("null")).cloned().collect();
                out.insert(
                    key.clone(),
                    match kept.len() {
                        1 => kept[0].clone(),
                        0 => value.clone(),
                        _ => Value::Array(kept),
                    },
                );
            }
            (_, Value::Object(_)) => {
                out.insert(key.clone(), strip_null_anyof(value));
            }
            (_, Value::Array(items)) => {
                out.insert(key.clone(), Value::Array(items.iter().map(strip_null_anyof).collect()));
            }
            _ => {
                out.insert(key.clone(), value.clone());
            }
        }
    }
    Value::Object(out)
}

// ── reply ────────────────────────────────────────────────────────────────────

enum Block {
    Text(String),
    Reasoning { text: String, signature: String },
    Redacted(String),
    Call { id: String, name: String, args: String },
}

struct Reply {
    model: String,
    blocks: Vec<(u64, Block)>,
    usage: Option<Value>,
    stop_reason: Option<String>,
}

impl Reply {
    fn new(model: &str) -> Self {
        Reply { model: model.to_string(), blocks: vec![], usage: None, stop_reason: None }
    }

    /// The block at `index`, made with `make` if it isn't there yet.
    fn at(&mut self, index: u64, make: impl FnOnce() -> Block) -> &mut Block {
        let pos = match self.blocks.iter().position(|(i, _)| *i == index) {
            Some(pos) => pos,
            None => {
                self.blocks.push((index, make()));
                self.blocks.len() - 1
            }
        };
        &mut self.blocks[pos].1
    }

    fn absorb(&mut self, event: &str, payload: &Value, on_delta: &mut (dyn FnMut(Delta) + Send)) {
        let index = payload["contentBlockIndex"].as_u64().unwrap_or(0);
        match event {
            "contentBlockStart" => {
                if let Some(t) = payload["start"].get("toolUse") {
                    on_delta(Delta::ToolCall);
                    let call = Block::Call {
                        id: t["toolUseId"].as_str().unwrap_or_default().into(),
                        name: t["name"].as_str().unwrap_or_default().into(),
                        args: String::new(),
                    };
                    self.at(index, || call);
                }
            }
            "contentBlockDelta" => {
                let d = &payload["delta"];
                if let Some(t) = d.get("text").and_then(Value::as_str) {
                    if let Block::Text(text) = self.at(index, || Block::Text(String::new())) {
                        text.push_str(t);
                    }
                    on_delta(Delta::Text(t));
                } else if let Some(r) = d.get("reasoningContent") {
                    if let Some(data) = r.get("redactedContent").and_then(Value::as_str) {
                        if let Block::Redacted(all) = self.at(index, || Block::Redacted(String::new())) {
                            all.push_str(data);
                        }
                        return;
                    }
                    let block = self.at(index, || Block::Reasoning { text: String::new(), signature: String::new() });
                    if let Block::Reasoning { text, signature } = block {
                        if let Some(t) = r.get("text").and_then(Value::as_str) {
                            text.push_str(t);
                            on_delta(Delta::Thinking(t));
                        }
                        if let Some(s) = r.get("signature").and_then(Value::as_str) {
                            signature.push_str(s);
                        }
                    }
                } else if let Some(input) = d.get("toolUse").and_then(|t| t["input"].as_str()) {
                    on_delta(Delta::ToolCall);
                    if let Block::Call { args, .. } = self.at(index, || Block::Call { id: String::new(), name: String::new(), args: String::new() }) {
                        args.push_str(input);
                    }
                }
            }
            "messageStop" => self.stop_reason = payload["stopReason"].as_str().map(str::to_string),
            "metadata" => self.usage = payload.get("usage").cloned(),
            _ => {}
        }
    }

    fn finish(mut self) -> Message {
        self.blocks.sort_by_key(|(i, _)| *i);
        let mut parts = vec![];
        let mut calls = vec![];
        let mut thought = false;
        for (_, b) in self.blocks {
            match b {
                Block::Text(text) if !text.is_empty() => {
                    parts.push(Part::Typed(Typed::Text { text, signature: None, extras: Map::new() }))
                }
                Block::Reasoning { text, signature } => {
                    thought = true;
                    let signature = (!signature.is_empty()).then_some(signature);
                    parts.push(Part::Typed(Typed::Thinking { thinking: text, signature, extras: Map::new() }));
                }
                Block::Redacted(data) => {
                    thought = true;
                    parts.push(Part::Typed(Typed::RedactedThinking { data, extras: Map::new() }));
                }
                Block::Call { id, name, args } => calls.push((id, name, args)),
                Block::Text(_) => {}
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
        m.id = Some(new_id());
        for (id, name, args) in calls {
            let id = if id.is_empty() { new_id() } else { id };
            match super::parse_args(&args) {
                Ok(args) => m.tool_calls.push(ToolCall { id: Some(id), name, args, signature: None }),
                Err(error) => m.invalid_tool_calls.push(json!({"id": id, "name": name, "args": args, "error": error})),
            }
        }
        m.usage = self.usage.as_ref().map(usage);
        m.model = Some(ModelRef { provider: Some("bedrock".into()), name: Some(self.model) });
        m.finish_reason = self.stop_reason.map(Value::String);
        m
    }
}

/// `_extract_usage_metadata`: cache reads and writes are input too.
fn usage(u: &Value) -> Usage {
    let n = |k: &str| u.get(k).and_then(Value::as_i64).unwrap_or(0);
    let (read, write) = (n("cacheReadInputTokens"), n("cacheWriteInputTokens"));
    let input = n("inputTokens") + read + write;
    let output = n("outputTokens");
    let total = u.get("totalTokens").and_then(Value::as_i64).unwrap_or(input + output);
    let mut details = Map::new();
    details.insert("cache_read".into(), read.into());
    details.insert("cache_creation".into(), write.into());
    if let Some(Value::Array(split)) = u.get("cacheDetails") {
        let sum = |ttl: &str| -> i64 {
            split.iter().filter(|d| d["ttl"] == ttl).map(|d| d["inputTokens"].as_i64().unwrap_or(0)).sum()
        };
        let (m5, h1) = (sum("5m"), sum("1h"));
        if m5 != 0 {
            details.insert("ephemeral_5m_input_tokens".into(), m5.into());
        }
        if h1 != 0 {
            details.insert("ephemeral_1h_input_tokens".into(), h1.into());
        }
        if m5 + h1 > 0 {
            details.insert("cache_creation".into(), 0.into());
        }
    }
    Usage {
        input: Some(input),
        output: Some(output),
        total: Some(total),
        input_details: Some(details),
        output_details: None,
        extras: Map::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(event: &str, payload: &Value) -> Vec<u8> {
        let mut headers = vec![];
        for (k, v) in [(":event-type", event), (":content-type", "application/json"), (":message-type", "event")] {
            headers.push(k.len() as u8);
            headers.extend_from_slice(k.as_bytes());
            headers.push(7);
            headers.extend_from_slice(&(v.len() as u16).to_be_bytes());
            headers.extend_from_slice(v.as_bytes());
        }
        let body = serde_json::to_vec(payload).unwrap();
        let total = (12 + headers.len() + body.len() + 4) as u32;
        let mut out = total.to_be_bytes().to_vec();
        out.extend_from_slice(&(headers.len() as u32).to_be_bytes());
        let prelude_crc = crc32(&out);
        out.extend_from_slice(&prelude_crc.to_be_bytes());
        out.extend(headers);
        out.extend(body);
        let crc = crc32(&out);
        out.extend_from_slice(&crc.to_be_bytes());
        out
    }

    #[test]
    fn crc_is_zlibs() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn frames_parse_and_checksums_hold() {
        let raw = frame("contentBlockDelta", &json!({"contentBlockIndex": 0, "delta": {"text": "hé"}}));
        let f = parse_frame(&raw).unwrap();
        assert_eq!(f.header(":event-type"), Some("contentBlockDelta"));
        assert_eq!(serde_json::from_slice::<Value>(&f.payload).unwrap()["delta"]["text"], "hé");
        let mut broken = raw.clone();
        let last = broken.len() - 5;
        broken[last] ^= 1;
        assert!(parse_frame(&broken).is_err());
    }

    #[test]
    fn nullable_parameters_lose_their_null() {
        let s = json!({"type": "object", "properties": {
            "a": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": null},
            "b": {"type": ["string", "null"]},
            "c": {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]}}});
        assert_eq!(strip_null_anyof(&s), json!({"type": "object", "properties": {
            "a": {"type": "integer", "default": null},
            "b": {"type": "string"},
            "c": {"anyOf": [{"type": "integer"}, {"type": "string"}]}}}));
    }

    #[test]
    fn reply_from_events() {
        let events = [
            ("messageStart", json!({"role": "assistant"})),
            ("contentBlockDelta", json!({"contentBlockIndex": 0, "delta": {"reasoningContent": {"text": "Hm."}}})),
            ("contentBlockDelta", json!({"contentBlockIndex": 0, "delta": {"reasoningContent": {"signature": "c2ln"}}})),
            ("contentBlockDelta", json!({"contentBlockIndex": 1, "delta": {"text": "Hi"}})),
            ("contentBlockStart", json!({"contentBlockIndex": 2, "start": {"toolUse": {"toolUseId": "t1", "name": "f"}}})),
            ("contentBlockDelta", json!({"contentBlockIndex": 2, "delta": {"toolUse": {"input": "{\"q\": 1}"}}})),
            ("messageStop", json!({"stopReason": "tool_use"})),
            ("metadata", json!({"usage": {"inputTokens": 5, "outputTokens": 9, "totalTokens": 14, "cacheReadInputTokens": 3}})),
        ];
        let mut r = Reply::new("us.anthropic.x");
        let mut seen = vec![];
        for (e, p) in &events {
            r.absorb(e, p, &mut |d| seen.push(format!("{d:?}")));
        }
        let m = serde_json::to_value(r.finish()).unwrap();
        assert_eq!(m["content"], json!([{"type": "thinking", "thinking": "Hm.", "signature": "c2ln"}, {"type": "text", "text": "Hi"}]));
        assert_eq!(m["tool_calls"], json!([{"id": "t1", "name": "f", "args": {"q": 1}}]));
        assert_eq!(m["usage"]["input"], 8);
        assert_eq!(m["usage"]["input_details"], json!({"cache_read": 3, "cache_creation": 0}));
        assert_eq!(m["model"], json!({"provider": "bedrock", "name": "us.anthropic.x"}));
        assert_eq!(m["finish_reason"], "tool_use");
        assert_eq!(seen, ["Thinking(\"Hm.\")", "Text(\"Hi\")", "ToolCall", "ToolCall"]);
    }
}
