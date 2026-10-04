//! Ollama over `/api/chat`, streamed as NDJSON.
//!
//! What it sends follows what `langchain_ollama` sent, with these differences
//! on purpose (`tests/test_edge_llm.py` names each one):
//!
//! - a tool result says which tool it answers (`tool_name`);
//! - a message's text parts are joined by newlines without LangChain's
//!   leading one;
//! - no empty `options`.
//!
//! And the model's thinking is kept in the reply, where LangChain dropped it.

use serde_json::{Map, Value, json};

use super::lines::Lines;
use super::transcript::{Content, Message, ModelRef, Part, Role, ToolCall, Typed, Usage};
use super::{Delta, Endpoints, Error, Request, media_base64, new_id};

pub const PROVIDER: &str = "ollama";

/// `OLLAMA_HOST` read the way the `ollama` client reads it: scheme defaults
/// to http, port to 11434 when no scheme is given.
pub fn host(raw: Option<&str>) -> String {
    let raw = raw.unwrap_or("").trim().trim_end_matches('/');
    if raw.is_empty() {
        return "http://127.0.0.1:11434".into();
    }
    if raw.contains("://") {
        return raw.to_string();
    }
    let (hostport, path) = match raw.find('/') {
        Some(i) => (&raw[..i], &raw[i..]),
        None => (raw, ""),
    };
    let hostport = if let Some(port) = hostport.strip_prefix(':') {
        format!("127.0.0.1:{port}")
    } else if hostport.rsplit_once(':').is_some_and(|(_, p)| p.chars().all(|c| c.is_ascii_digit()) && !hostport.ends_with(']')) {
        hostport.to_string()
    } else {
        format!("{hostport}:11434")
    };
    format!("http://{hostport}{path}")
}

pub async fn complete(
    http: &reqwest::Client,
    ends: &Endpoints,
    name: &str,
    req: &Request<'_>,
    on_delta: &mut (dyn FnMut(Delta) + Send),
) -> Result<Message, Error> {
    let body = render(name, req)?;
    let resp = http.post(format!("{}/api/chat", ends.ollama_base)).json(&body).send().await.map_err(Error::connection)?;
    if !resp.status().is_success() {
        return Err(Error::from_response(resp).await);
    }
    let mut lines = Lines::new(resp);
    let mut reply = Reply::new(name);
    while let Some(line) = lines.next().await? {
        if line.trim().is_empty() {
            continue;
        }
        let chunk: Value =
            serde_json::from_str(&line).map_err(|e| Error::fatal(format!("unreadable stream line: {e}: {line}")))?;
        if let Some(err) = chunk.get("error") {
            return Err(Error::fatal(err.as_str().map_or_else(|| err.to_string(), str::to_string)));
        }
        reply.absorb(&chunk, on_delta);
    }
    Ok(reply.finish())
}

// ── request ──────────────────────────────────────────────────────────────────

pub fn render(name: &str, req: &Request<'_>) -> Result<Value, Error> {
    let system: Vec<&str> = req.prompt.system.iter().map(|b| b.text.as_str()).collect();
    let mut messages = vec![json!({"role": "system", "content": system.join("\n")})];
    for m in &req.prompt.messages {
        let (text, images) = flatten(&m.content, req)?;
        let mut out = match m.role {
            Role::System => json!({"role": "system", "content": text}),
            Role::User => json!({"role": "user", "content": text}),
            Role::Assistant => json!({"role": "assistant", "content": text}),
            Role::Tool => json!({"role": "tool", "content": text}),
        };
        if !images.is_empty() {
            out["images"] = images.into();
        }
        if m.role == Role::Assistant && !m.tool_calls.is_empty() {
            let calls: Vec<Value> =
                m.tool_calls.iter().map(|c| json!({"function": {"name": c.name, "arguments": c.args}})).collect();
            out["tool_calls"] = calls.into();
        }
        if m.role == Role::Tool
            && let Some(name) = m.name.as_deref().or_else(|| called(&req.prompt.messages, m.tool_call_id.as_deref()))
        {
            out["tool_name"] = name.into();
        }
        messages.push(out);
    }
    let mut body = json!({"model": name, "stream": true, "messages": messages});
    if !req.tools.is_empty() {
        let tools: Vec<Value> = req
            .tools
            .iter()
            .map(|t| {
                json!({"type": "function", "function": {
                    "name": t.name, "description": t.description, "parameters": parameters(&t.parameters)}})
            })
            .collect();
        body["tools"] = tools.into();
    }
    Ok(body)
}

/// A tool's parameters as the `ollama` client's `Tool` model keeps them —
/// what Ollama's API defines: per argument only `type`, `items`,
/// `description` and `enum`, so an optional one (`anyOf` with null) is an
/// empty schema and a default is dropped. The server reads no more, and the
/// same bytes keep a conversation's KV prefix when it moves between runtimes.
fn parameters(schema: &Value) -> Value {
    let keep = |from: &Value, keys: &[&str]| -> serde_json::Map<String, Value> {
        keys.iter().filter_map(|k| Some((k.to_string(), from.get(*k).filter(|v| !v.is_null())?.clone()))).collect()
    };
    let mut out = keep(schema, &["type", "$defs", "items", "required"]);
    out.entry("type").or_insert_with(|| "object".into());
    if let Some(Value::Object(props)) = schema.get("properties") {
        let props: serde_json::Map<String, Value> = props
            .iter()
            .map(|(name, p)| (name.clone(), Value::Object(keep(p, &["type", "items", "description", "enum"]))))
            .collect();
        out.insert("properties".into(), Value::Object(props));
    }
    Value::Object(out)
}

/// The name of the tool call `id` answers.
fn called<'a>(messages: &'a [Message], id: Option<&str>) -> Option<&'a str> {
    let id = id?;
    messages.iter().flat_map(|m| &m.tool_calls).find(|c| c.id.as_deref() == Some(id)).map(|c| c.name.as_str())
}

/// Text parts joined by newlines, and images as base64. Ollama takes no
/// other media.
fn flatten(content: &Content, req: &Request<'_>) -> Result<(String, Vec<String>), Error> {
    let parts = match content {
        Content::Text(s) => return Ok((s.clone(), vec![])),
        Content::Parts(p) => p,
    };
    let mut text = vec![];
    let mut images = vec![];
    for p in parts {
        match p {
            Part::Str(s) => text.push(s.as_str()),
            Part::Typed(Typed::Text { text: t, .. }) => text.push(t),
            Part::Typed(Typed::Image(m)) => images.push(media_base64(m, req.blobs)?.to_string()),
            _ => {}
        }
    }
    Ok((text.join("\n"), images))
}

// ── reply ────────────────────────────────────────────────────────────────────

struct Reply {
    thinking: String,
    text: String,
    calls: Vec<ToolCall>,
    usage: Option<Usage>,
    finish_reason: Option<String>,
    model: String,
}

impl Reply {
    fn new(model: &str) -> Self {
        Reply {
            thinking: String::new(),
            text: String::new(),
            calls: vec![],
            usage: None,
            finish_reason: None,
            model: model.to_string(),
        }
    }

    fn absorb(&mut self, chunk: &Value, on_delta: &mut (dyn FnMut(Delta) + Send)) {
        if let Some(model) = chunk.get("model").and_then(Value::as_str) {
            self.model = model.to_string();
        }
        if let Some(msg) = chunk.get("message") {
            if let Some(t) = msg.get("thinking").and_then(Value::as_str).filter(|t| !t.is_empty()) {
                on_delta(Delta::Thinking(t));
                self.thinking.push_str(t);
            }
            if let Some(t) = msg.get("content").and_then(Value::as_str).filter(|t| !t.is_empty()) {
                on_delta(Delta::Text(t));
                self.text.push_str(t);
            }
            for call in msg.get("tool_calls").and_then(Value::as_array).map(Vec::as_slice).unwrap_or_default() {
                on_delta(Delta::ToolCall);
                let f = &call["function"];
                self.calls.push(ToolCall {
                    id: Some(call.get("id").and_then(Value::as_str).map_or_else(new_id, str::to_string)),
                    name: f.get("name").and_then(Value::as_str).unwrap_or_default().to_string(),
                    args: f.get("arguments").cloned().unwrap_or_else(|| json!({})),
                    signature: None,
                });
            }
        }
        if chunk.get("done").and_then(Value::as_bool) == Some(true) {
            let n = |k: &str| chunk.get(k).and_then(Value::as_i64);
            let (input, output) = (n("prompt_eval_count"), n("eval_count"));
            self.usage = Some(Usage {
                input,
                output,
                total: (input.is_some() || output.is_some()).then(|| input.unwrap_or(0) + output.unwrap_or(0)),
                ..Default::default()
            });
            self.finish_reason = chunk.get("done_reason").and_then(Value::as_str).map(str::to_string);
            // Nanoseconds, measured server-side; both spans or neither.
            let (prefill, decode) = (n("prompt_eval_duration").unwrap_or(0), n("eval_duration").unwrap_or(0));
            if prefill > 0 && decode > 0 {
                on_delta(Delta::Timings(super::perf::ServerTimings {
                    prefill_tokens: input.unwrap_or(0),
                    prefill_seconds: prefill as f64 / 1e9,
                    output_tokens: output.unwrap_or(0),
                    decode_seconds: decode as f64 / 1e9,
                }));
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
        m.id = Some(new_id());
        m.tool_calls = self.calls;
        m.usage = self.usage;
        m.model = Some(ModelRef { provider: Some(PROVIDER.into()), name: Some(self.model) });
        m.finish_reason = self.finish_reason.map(Value::String);
        m
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::Blobs;
    use crate::llm::shape::{Prompt, SystemBlock};

    #[test]
    fn hosts() {
        let h = |s| host(Some(s));
        assert_eq!(host(None), "http://127.0.0.1:11434");
        assert_eq!(h("1.2.3.4"), "http://1.2.3.4:11434");
        assert_eq!(h(":56789"), "http://127.0.0.1:56789");
        assert_eq!(h("example.com:56789/"), "http://example.com:56789");
        assert_eq!(h("example.com/path"), "http://example.com:11434/path");
        assert_eq!(h("https://example.com"), "https://example.com");
        assert_eq!(h("http://127.0.0.1:9"), "http://127.0.0.1:9");
    }

    #[test]
    fn request() {
        let msgs: Vec<Message> = serde_json::from_value(json!([
            {"v": 1, "role": "user", "content": [{"type": "text", "text": "look"}, "more",
                                                 {"type": "image", "mime_type": "image/png", "data": "iVBO"}]},
            {"v": 1, "role": "assistant", "content": "ok", "tool_calls": [{"id": "c1", "name": "run_cell", "args": {"code": "x"}}]},
            {"v": 1, "role": "tool", "content": "result", "tool_call_id": "c1", "status": "success"},
        ]))
        .unwrap();
        let p = Prompt { system: vec![SystemBlock { text: "S".into(), breakpoint: false }], messages: msgs, history_breakpoint: None, cached: false };
        let b = render("gemma4:26b", &Request { model: "", prompt: &p, tools: &[], blobs: &Blobs::new() }).unwrap();
        assert_eq!(
            b,
            json!({"model": "gemma4:26b", "stream": true, "messages": [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "look\nmore", "images": ["iVBO"]},
                {"role": "assistant", "content": "ok", "tool_calls": [{"function": {"name": "run_cell", "arguments": {"code": "x"}}}]},
                {"role": "tool", "content": "result", "tool_name": "run_cell"},
            ]})
        );
    }

    #[test]
    fn reply() {
        let lines = [
            json!({"model": "gemma4:26b", "message": {"role": "assistant", "content": "", "thinking": "hmm"}, "done": false}),
            json!({"model": "gemma4:26b", "message": {"role": "assistant", "content": "Hel"}, "done": false}),
            json!({"model": "gemma4:26b", "message": {"role": "assistant", "content": "lo",
                   "tool_calls": [{"function": {"name": "run_cell", "arguments": {"code": "1+1"}}}]}, "done": false}),
            json!({"model": "gemma4:26b", "message": {"role": "assistant", "content": ""}, "done": true,
                   "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 7,
                   "prompt_eval_duration": 500_000_000, "eval_duration": 2_000_000_000}),
        ];
        let mut r = Reply::new("x");
        let mut deltas = vec![];
        for l in &lines {
            r.absorb(l, &mut |d| deltas.push(format!("{d:?}")));
        }
        let mut m = serde_json::to_value(r.finish()).unwrap();
        m.as_object_mut().unwrap().remove("id");
        m["tool_calls"][0].as_object_mut().unwrap().remove("id");
        assert_eq!(
            m,
            json!({"v": 1, "role": "assistant",
                   "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Hello"}],
                   "tool_calls": [{"name": "run_cell", "args": {"code": "1+1"}}],
                   "usage": {"input": 50, "output": 7, "total": 57},
                   "model": {"provider": "ollama", "name": "gemma4:26b"},
                   "finish_reason": "stop"})
        );
        let timings = "Timings(ServerTimings { prefill_tokens: 50, prefill_seconds: 0.5, output_tokens: 7, decode_seconds: 2.0 })";
        assert_eq!(deltas, ["Thinking(\"hmm\")", "Text(\"Hel\")", "Text(\"lo\")", "ToolCall", timings]);
    }
}
