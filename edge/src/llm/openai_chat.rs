//! OpenAI's Chat Completions format, streamed — what OpenRouter speaks.
//!
//! What it sends follows what LangChain's `ChatOpenAI` sent for the same
//! history, tool-call arguments in `json.dumps` spacing included (a cached
//! prefix that a thread built in Python stays byte-identical here). These
//! differ on purpose (`tests/test_edge_llm.py` names each one):
//!
//! - an assistant's content is only its text, each part a text block —
//!   LangChain sent a stored bare string as-is, which isn't a content part;
//! - the reply keeps the model's `reasoning`, which LangChain dropped, and
//!   names its provider by catalog id (`openrouter`), not `openai`.

use serde_json::{Map, Value, json};

use super::lines::Lines;
use super::shape::Prompt;
use super::transcript::{Content, Media, Message, ModelRef, Part, Role, ToolCall, Typed, Usage};
use super::{Blobs, Delta, Error, Request, data_url, new_id};
use crate::pyjson;

pub async fn complete(
    http: &reqwest::Client,
    base: &str,
    key: &str,
    provider: &str,
    name: &str,
    req: &Request<'_>,
    on_delta: &mut (dyn FnMut(Delta) + Send),
) -> Result<Message, Error> {
    let body = render(name, req)?;
    let resp = http
        .post(format!("{base}/chat/completions"))
        .bearer_auth(key)
        .json(&body)
        .send()
        .await
        .map_err(Error::connection)?;
    if !resp.status().is_success() {
        return Err(Error::from_response(resp).await);
    }
    let mut lines = Lines::new(resp);
    let mut reply = Reply::new(provider, name);
    while let Some(data) = lines.next_event().await? {
        if data.trim() == "[DONE]" {
            break;
        }
        let chunk: Value =
            serde_json::from_str(&data).map_err(|e| Error::fatal(format!("unreadable stream event: {e}: {data}")))?;
        if let Some(err) = chunk.get("error") {
            return Err(Error::from_stream(err));
        }
        reply.absorb(&chunk, on_delta);
    }
    Ok(reply.finish())
}

// ── request ──────────────────────────────────────────────────────────────────

const EPHEMERAL: &str = "ephemeral";

fn text_block(text: &str, breakpoint: bool) -> Value {
    let mut b = json!({"type": "text", "text": text});
    if breakpoint {
        b["cache_control"] = json!({"type": EPHEMERAL});
    }
    b
}

pub fn render(name: &str, req: &Request<'_>) -> Result<Value, Error> {
    let p = req.prompt;
    let system = if p.cached {
        Value::Array(p.system.iter().map(|b| text_block(&b.text, b.breakpoint)).collect())
    } else {
        Value::String(p.system.iter().map(|b| b.text.as_str()).collect::<Vec<_>>().join("\n\n"))
    };
    let mut messages = vec![json!({"content": system, "role": "system"})];
    for (i, m) in p.messages.iter().enumerate() {
        messages.push(message(m, p, p.history_breakpoint == Some(i), req.blobs)?);
    }
    let mut body = json!({"messages": messages, "model": name, "stream": true, "stream_options": {"include_usage": true}});
    if !req.tools.is_empty() {
        let tools: Vec<Value> = req
            .tools
            .iter()
            .map(|t| json!({"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}))
            .collect();
        body["tools"] = tools.into();
    }
    Ok(body)
}

fn message(m: &Message, p: &Prompt, breakpoint: bool, blobs: &Blobs) -> Result<Value, Error> {
    let mut out = match m.role {
        Role::User | Role::Tool | Role::System => {
            let role = match m.role {
                Role::User => "user",
                Role::Tool => "tool",
                _ => "system",
            };
            json!({"content": input_content(&m.content, p.cached && m.role != Role::System, breakpoint, blobs)?, "role": role})
        }
        Role::Assistant => {
            let mut out = json!({"content": assistant_content(&m.content), "role": "assistant"});
            let calls: Vec<Value> = m
                .tool_calls
                .iter()
                .map(|c| json!({"type": "function", "id": c.id, "function": {"name": c.name, "arguments": pyjson::dumps_unicode(&c.args)}}))
                .chain(m.invalid_tool_calls.iter().map(|c| {
                    json!({"type": "function", "id": c.get("id"), "function": {"name": c.get("name"), "arguments": c.get("args")}})
                }))
                .collect();
            if !calls.is_empty() {
                // With calls, empty content is null, not "".
                if out["content"].as_str() == Some("") || out["content"].as_array().is_some_and(Vec::is_empty) {
                    out["content"] = Value::Null;
                }
                out["tool_calls"] = calls.into();
            }
            out
        }
    };
    if m.role == Role::Tool {
        out["tool_call_id"] = m.tool_call_id.as_deref().into();
    } else if let Some(name) = &m.name {
        out["name"] = name.as_str().into();
    }
    Ok(out)
}

/// A user, tool or system message's content: a string, or blocks — always
/// blocks in a cached prompt when it has text, so marking it changes nothing
/// but the breakpoint.
fn input_content(content: &Content, cached: bool, breakpoint: bool, blobs: &Blobs) -> Result<Value, Error> {
    let mut blocks = match content {
        Content::Text(s) if !(cached || breakpoint) || s.trim().is_empty() => return Ok(Value::String(s.clone())),
        Content::Text(s) => vec![text_block(s, false)],
        Content::Parts(parts) => {
            let mut out = vec![];
            for p in parts {
                match p {
                    Part::Str(s) | Part::Typed(Typed::Text { text: s, .. }) => out.push(text_block(s, false)),
                    // jarvis sends non-image media to these models as an image
                    // part too (`streaming._build_message_content`).
                    Part::Typed(Typed::Image(m) | Typed::File(m)) => out.push(image(m, blobs)?),
                    // Anthropic tool results ride as they were recorded.
                    Part::Typed(Typed::Opaque { data, .. }) if data.get("type").is_some() => out.push(data.clone()),
                    _ => {}
                }
            }
            out
        }
    };
    if breakpoint && let Some(last) = blocks.last_mut() {
        last["cache_control"] = json!({"type": EPHEMERAL});
    }
    Ok(Value::Array(blocks))
}

fn image(m: &Media, blobs: &Blobs) -> Result<Value, Error> {
    Ok(json!({"type": "image_url", "image_url": {"url": data_url(m, blobs)?}}))
}

/// Only the text a model said; thinking, and another provider's tool or
/// reasoning items, are not Chat Completions content.
fn assistant_content(content: &Content) -> Value {
    match content {
        Content::Text(s) => Value::String(s.clone()),
        Content::Parts(parts) => Value::Array(
            parts
                .iter()
                .filter_map(|p| match p {
                    Part::Str(s) | Part::Typed(Typed::Text { text: s, .. }) => Some(text_block(s, false)),
                    _ => None,
                })
                .collect(),
        ),
    }
}

// ── reply ────────────────────────────────────────────────────────────────────

#[derive(Default)]
struct PendingCall {
    id: Option<String>,
    name: String,
    args: String,
}

struct Reply {
    id: Option<String>,
    thinking: String,
    text: String,
    calls: Vec<PendingCall>,
    usage: Option<Usage>,
    finish_reason: Option<String>,
    provider: String,
    model: String,
}

impl Reply {
    fn new(provider: &str, model: &str) -> Self {
        Reply {
            id: None,
            thinking: String::new(),
            text: String::new(),
            calls: vec![],
            usage: None,
            finish_reason: None,
            provider: provider.to_string(),
            model: model.to_string(),
        }
    }

    fn absorb(&mut self, chunk: &Value, on_delta: &mut (dyn FnMut(Delta) + Send)) {
        if self.id.is_none() {
            self.id = chunk.get("id").and_then(Value::as_str).map(str::to_string);
        }
        if let Some(m) = chunk.get("model").and_then(Value::as_str) {
            self.model = m.to_string();
        }
        if let Some(u) = chunk.get("usage").filter(|u| u.is_object()) {
            self.usage = Some(usage(u));
        }
        let Some(choice) = chunk.get("choices").and_then(|c| c.get(0)) else { return };
        if let Some(r) = choice.get("finish_reason").and_then(Value::as_str) {
            self.finish_reason = Some(r.to_string());
        }
        let Some(delta) = choice.get("delta") else { return };
        // OpenRouter says `reasoning`; DeepSeek-style servers `reasoning_content`.
        for key in ["reasoning", "reasoning_content"] {
            if let Some(t) = delta.get(key).and_then(Value::as_str).filter(|t| !t.is_empty()) {
                on_delta(Delta::Thinking(t));
                self.thinking.push_str(t);
            }
        }
        if let Some(t) = delta.get("content").and_then(Value::as_str).filter(|t| !t.is_empty()) {
            on_delta(Delta::Text(t));
            self.text.push_str(t);
        }
        for tc in delta.get("tool_calls").and_then(Value::as_array).map(Vec::as_slice).unwrap_or_default() {
            let index = tc.get("index").and_then(Value::as_u64).unwrap_or(self.calls.len() as u64) as usize;
            while self.calls.len() <= index {
                self.calls.push(PendingCall::default());
            }
            let call = &mut self.calls[index];
            if let Some(id) = tc.get("id").and_then(Value::as_str) {
                call.id = Some(id.to_string());
            }
            if let Some(f) = tc.get("function") {
                if let Some(n) = f.get("name").and_then(Value::as_str) {
                    call.name.push_str(n);
                }
                if let Some(a) = f.get("arguments").and_then(Value::as_str) {
                    call.args.push_str(a);
                }
            }
        }
    }

    fn finish(self) -> Message {
        let content = if self.thinking.is_empty() {
            Content::Text(self.text)
        } else {
            let mut parts = vec![Part::Typed(Typed::Thinking { thinking: self.thinking, signature: None, extras: Map::new() })];
            if !self.text.is_empty() {
                parts.push(Part::Typed(Typed::Text { text: self.text, signature: None, extras: Map::new() }));
            }
            Content::Parts(parts)
        };
        let mut m = Message::new(Role::Assistant, content);
        m.id = Some(self.id.unwrap_or_else(new_id));
        for c in self.calls {
            let id = c.id.unwrap_or_else(new_id);
            match super::parse_args(&c.args) {
                Ok(args) => m.tool_calls.push(ToolCall { id: Some(id), name: c.name, args, signature: None }),
                Err(error) => m.invalid_tool_calls.push(json!({"id": id, "name": c.name, "args": c.args, "error": error})),
            }
        }
        m.usage = self.usage;
        m.model = Some(ModelRef { provider: Some(self.provider), name: Some(self.model) });
        m.finish_reason = self.finish_reason.map(Value::String);
        m
    }
}

/// Chat Completions usage as LangChain counted it.
fn usage(u: &Value) -> Usage {
    let n = |v: &Value, k: &str| v.get(k).and_then(Value::as_i64);
    let input = n(u, "prompt_tokens").unwrap_or(0);
    let output = n(u, "completion_tokens").unwrap_or(0);
    let details = |src: &str, keys: &[(&str, &str)]| {
        let d = u.get(src).cloned().unwrap_or_default();
        keys.iter().filter_map(|(from, to)| n(&d, from).map(|v| (to.to_string(), v.into()))).collect::<Map<_, _>>()
    };
    Usage {
        input: Some(input),
        output: Some(output),
        total: Some(n(u, "total_tokens").unwrap_or(input + output)),
        input_details: Some(details(
            "prompt_tokens_details",
            &[("audio_tokens", "audio"), ("cached_tokens", "cache_read"), ("cache_write_tokens", "cache_creation")],
        )),
        output_details: Some(details(
            "completion_tokens_details",
            &[("audio_tokens", "audio"), ("reasoning_tokens", "reasoning")],
        )),
        extras: Map::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::shape::{Layout, build};

    fn msgs(v: Value) -> Vec<Message> {
        serde_json::from_value(v).unwrap()
    }

    #[test]
    fn cached_request_marks_blocks() {
        let history = msgs(json!([
            {"v": 1, "role": "user", "content": "hi"},
            {"v": 1, "role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "f", "args": {"q": "é"}}]},
            {"v": 1, "role": "tool", "content": "r", "tool_call_id": "c1", "status": "success"},
        ]));
        let layout = Layout { system: "S", segments: &[], volatile: "todo", cache: true, provider: "openrouter" };
        let p = build(&layout, history);
        let b = render("anthropic/claude", &Request { model: "", prompt: &p, tools: &[], blobs: &Blobs::new() }).unwrap();
        assert_eq!(
            b["messages"],
            json!([
                {"content": [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}], "role": "system"},
                {"content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}], "role": "user"},
                {"content": null, "role": "assistant", "tool_calls": [
                    {"type": "function", "id": "c1", "function": {"name": "f", "arguments": "{\"q\": \"é\"}"}}]},
                {"content": [{"type": "text", "text": "r"}], "role": "tool", "tool_call_id": "c1"},
                {"content": [{"type": "text", "text": format!("{}todo{}", crate::llm::shape::TURN_CONTEXT_OPEN, crate::llm::shape::TURN_CONTEXT_CLOSE)}], "role": "user"},
            ])
        );
        assert_eq!(b["stream_options"], json!({"include_usage": true}));
    }

    #[test]
    fn reply_from_chunks() {
        let chunks = [
            json!({"id": "gen-1", "model": "m", "choices": [{"delta": {"role": "assistant", "content": "", "reasoning": "Hm."}}]}),
            json!({"id": "gen-1", "choices": [{"delta": {"content": "Hi", "tool_calls": [
                {"index": 0, "id": "call_1", "function": {"name": "run_cell", "arguments": "{\"code\": "}}]}}]}),
            json!({"id": "gen-1", "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"x\"}"}},
                {"index": 1, "id": "call_2", "function": {"name": "bad", "arguments": "{oops"}}]}, "finish_reason": "tool_calls"}]}),
            json!({"id": "gen-1", "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12,
                   "prompt_tokens_details": {"cached_tokens": 4}}}),
        ];
        let mut r = Reply::new("openrouter", "x");
        let mut seen = vec![];
        for c in &chunks {
            r.absorb(c, &mut |d| seen.push(format!("{d:?}")));
        }
        let m = serde_json::to_value(r.finish()).unwrap();
        assert_eq!(m["id"], "gen-1");
        assert_eq!(m["content"], json!([{"type": "thinking", "thinking": "Hm."}, {"type": "text", "text": "Hi"}]));
        assert_eq!(m["tool_calls"], json!([{"id": "call_1", "name": "run_cell", "args": {"code": "x"}}]));
        assert_eq!(m["invalid_tool_calls"][0]["args"], "{oops");
        assert_eq!(
            m["usage"],
            json!({"input": 9, "output": 3, "total": 12, "input_details": {"cache_read": 4}, "output_details": {}})
        );
        assert_eq!(m["model"], json!({"provider": "openrouter", "name": "m"}));
        assert_eq!(m["finish_reason"], "tool_calls");
        assert_eq!(seen, ["Thinking(\"Hm.\")", "Text(\"Hi\")"]);
    }
}
