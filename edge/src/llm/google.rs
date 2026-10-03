//! Gemini (and Gemma) over the Gemini API's `streamGenerateContent`.
//!
//! What it sends follows what `langchain_google_genai` sent for the same
//! history, with these differences on purpose (`tests/test_edge_llm.py`
//! names each one):
//!
//! - an assistant message's text is kept next to its function calls —
//!   LangChain dropped it, and with it the text's thought signature;
//! - tool parameters go as `parametersJsonSchema`, the JSON Schema as given,
//!   instead of a lossy conversion to Gemini's OpenAPI subset (which, among
//!   others, turned `["integer", "null"]` into `STRING`);
//! - no `safetySettings: []` or `candidateCount: 1` (both the defaults).

use serde_json::{Map, Value, json};

use super::lines::Lines;
use super::transcript::{Content, Media, Message, ModelRef, Part, Role, ToolCall, Typed, Usage};
use super::{Blobs, Delta, Endpoints, Error, Request, media_base64, new_id};

pub const PROVIDER: &str = "google_genai";

/// Gemini 3 wants a thought signature on the first function call of each
/// model turn since the user's last message. A call recorded without one (a
/// thread that came from another model) gets this — Google's documented
/// stand-in that skips the check.
const DUMMY_SIGNATURE: &str = "skip_thought_signature_validator";

pub async fn complete(
    http: &reqwest::Client,
    ends: &Endpoints,
    name: &str,
    req: &Request<'_>,
    on_delta: &mut (dyn FnMut(Delta) + Send),
) -> Result<Message, Error> {
    let key = ends.google_key.as_deref().ok_or_else(|| Error::fatal("GOOGLE_API_KEY is not set"))?;
    let body = render(name, req)?;
    let url = format!("{}/v1beta/models/{name}:streamGenerateContent?alt=sse", ends.google_base);
    let resp = http.post(url).header("x-goog-api-key", key).json(&body).send().await.map_err(Error::connection)?;
    if !resp.status().is_success() {
        return Err(Error::from_response(resp).await);
    }
    let mut lines = Lines::new(resp);
    let mut reply = Reply::new(name);
    while let Some(data) = lines.next_event().await? {
        let chunk: Value =
            serde_json::from_str(&data).map_err(|e| Error::fatal(format!("unreadable stream event: {e}: {data}")))?;
        if let Some(err) = chunk.get("error") {
            let code = err.get("code").and_then(Value::as_u64).map(|c| c as u16);
            return Err(Error {
                transient: code.is_some_and(|c| c == 429 || c >= 500),
                status: code,
                message: err.to_string(),
            });
        }
        reply.absorb(&chunk, on_delta);
    }
    Ok(reply.finish())
}

// ── request ──────────────────────────────────────────────────────────────────

pub fn render(name: &str, req: &Request<'_>) -> Result<Value, Error> {
    let mut system: Vec<Value> = req.prompt.system.iter().map(|b| json!({"text": b.text})).collect();
    let mut contents: Vec<Value> = vec![];
    let tool_results: Vec<&Message> = req.prompt.messages.iter().filter(|m| m.role == Role::Tool).collect();
    for m in req.prompt.messages.iter().filter(|m| m.role != Role::Tool) {
        match m.role {
            Role::System => system.extend(parts(&m.content, req.blobs)?),
            Role::User => contents.push(json!({"role": "user", "parts": parts(&m.content, req.blobs)?})),
            Role::Assistant => {
                let mut out = if m.tool_calls.is_empty() { parts(&m.content, req.blobs)? } else { content_parts(&m.content, req.blobs)? };
                for call in &m.tool_calls {
                    let mut part = json!({"functionCall": {"name": call.name, "args": call.args}});
                    if let Some(sig) = &call.signature {
                        part["thoughtSignature"] = sig.as_str().into();
                    }
                    out.push(part);
                }
                contents.push(json!({"role": "model", "parts": out}));
                let results = results_for(m, &tool_results, req.blobs)?;
                if !results.is_empty() {
                    contents.push(json!({"role": "user", "parts": results}));
                }
            }
            Role::Tool => unreachable!(),
        }
    }
    if is_gemini_3(name) {
        sign_active_loop(&mut contents);
    }

    let mut body = json!({"contents": contents, "systemInstruction": {"parts": system}});
    if !req.tools.is_empty() {
        let decls: Vec<Value> = req
            .tools
            .iter()
            .map(|t| {
                let mut d = json!({"name": t.name, "parametersJsonSchema": t.parameters});
                if !t.description.is_empty() {
                    d["description"] = t.description.as_str().into();
                }
                d
            })
            .collect();
        body["tools"] = json!([{"functionDeclarations": decls}]);
    }
    // LangChain's default, which Gemini 3 leaves to the model.
    if !is_gemini_3(name) {
        body["generationConfig"] = json!({"temperature": 0.7});
    }
    Ok(body)
}

fn is_gemini_3(name: &str) -> bool {
    name.to_lowercase().contains("gemini-3")
}

/// A message's parts, never none: Gemini refuses a content without parts.
fn parts(content: &Content, blobs: &Blobs) -> Result<Vec<Value>, Error> {
    let out = content_parts(content, blobs)?;
    Ok(if out.is_empty() { vec![json!({"text": ""})] } else { out })
}

fn content_parts(content: &Content, blobs: &Blobs) -> Result<Vec<Value>, Error> {
    let parts = match content {
        Content::Text(s) if s.is_empty() => return Ok(vec![]),
        Content::Text(s) => return Ok(vec![json!({"text": s})]),
        Content::Parts(p) => p,
    };
    let mut out = vec![];
    for p in parts {
        match p {
            // What stripping thinking leaves behind is no part at all.
            Part::Str(s) if s.is_empty() => {}
            Part::Typed(Typed::Text { text, signature: None, .. }) if text.is_empty() => {}
            Part::Str(s) => out.push(json!({"text": s})),
            Part::Typed(Typed::Text { text, signature, .. }) => out.push(signed(json!({"text": text}), signature)),
            Part::Typed(Typed::Thinking { thinking, signature, .. }) => {
                out.push(signed(json!({"text": thinking, "thought": true}), signature));
            }
            Part::Typed(Typed::Image(m) | Typed::File(m)) => out.push(inline(m, blobs)?),
            // Another provider's reasoning or tool blocks mean nothing here.
            Part::Typed(Typed::RedactedThinking { .. } | Typed::Opaque { .. }) => {}
        }
    }
    Ok(out)
}

fn signed(mut part: Value, signature: &Option<String>) -> Value {
    if let Some(sig) = signature {
        part["thoughtSignature"] = sig.as_str().into();
    }
    part
}

fn inline(m: &Media, blobs: &Blobs) -> Result<Value, Error> {
    let mime = m.mime_type.as_deref().unwrap_or("application/octet-stream");
    Ok(json!({"inlineData": {"mimeType": mime, "data": media_base64(m, blobs)?}}))
}

/// The function responses answering `call`'s tool calls, in the order the
/// results were recorded; one with no matching call is not sent.
fn results_for(call: &Message, results: &[&Message], blobs: &Blobs) -> Result<Vec<Value>, Error> {
    let mut wanted: Vec<&ToolCall> = call.tool_calls.iter().collect();
    let mut out = vec![];
    for r in results {
        let Some(i) = wanted.iter().position(|c| c.id.is_some() && c.id == r.tool_call_id) else { continue };
        let tc = wanted.remove(i);
        let name = r.name.as_deref().unwrap_or(&tc.name);
        let response = match &r.content {
            Content::Text(s) => serde_json::from_str(s).unwrap_or_else(|_| Value::String(s.clone())),
            Content::Parts(parts) => {
                let mut rest = vec![];
                for p in parts {
                    match p {
                        Part::Typed(Typed::Image(m) | Typed::File(m)) => out.push(inline(m, blobs)?),
                        p => rest.push(serde_json::to_value(p).unwrap_or_default()),
                    }
                }
                Value::Array(rest)
            }
        };
        // Gemini's response is an object; anything else is wrapped.
        let response = if response.is_object() { response } else { json!({"output": response}) };
        out.push(json!({"functionResponse": {"name": name, "response": response}}));
        if wanted.is_empty() {
            break;
        }
    }
    Ok(out)
}

/// The "active loop" is everything after the user's last real message (one
/// with text or media, not only function responses). Each model turn in it
/// gets a signature on its first function call if it has none.
fn sign_active_loop(contents: &mut [Value]) {
    let start = contents
        .iter()
        .rposition(|c| {
            let parts = c["parts"].as_array().map(Vec::as_slice).unwrap_or_default();
            c["role"] == "user"
                && !parts.iter().any(|p| p.get("functionResponse").is_some())
                && parts.iter().any(|p| {
                    p.get("text").and_then(Value::as_str).is_some_and(|t| !t.is_empty()) || p.get("inlineData").is_some()
                })
        })
        .map_or(0, |i| i + 1);
    for c in &mut contents[start..] {
        if c["role"] != "model" {
            continue;
        }
        if let Some(first) =
            c["parts"].as_array_mut().and_then(|ps| ps.iter_mut().find(|p| p.get("functionCall").is_some()))
            && first.get("thoughtSignature").is_none()
        {
            first["thoughtSignature"] = DUMMY_SIGNATURE.into();
        }
    }
}

// ── reply ────────────────────────────────────────────────────────────────────

/// The assistant record, built up chunk by chunk.
struct Reply {
    parts: Vec<Typed>,
    calls: Vec<ToolCall>,
    usage: Option<Usage>,
    finish_reason: Option<String>,
    model: String,
}

impl Reply {
    fn new(model: &str) -> Self {
        Reply { parts: vec![], calls: vec![], usage: None, finish_reason: None, model: model.to_string() }
    }

    fn absorb(&mut self, chunk: &Value, on_delta: &mut (dyn FnMut(Delta) + Send)) {
        if let Some(v) = chunk.get("modelVersion").and_then(Value::as_str) {
            self.model = v.to_string();
        }
        if let Some(u) = chunk.get("usageMetadata") {
            self.usage = Some(usage(u));
        }
        let Some(cand) = chunk.get("candidates").and_then(|c| c.get(0)) else {
            // A prompt refused outright comes back with no candidates.
            if let Some(reason) = chunk.pointer("/promptFeedback/blockReason").and_then(Value::as_str) {
                self.finish_reason = Some(reason.to_string());
            }
            return;
        };
        if let Some(reason) = cand.get("finishReason").and_then(Value::as_str) {
            self.finish_reason = Some(reason.to_string());
        }
        let parts = cand.pointer("/content/parts").and_then(Value::as_array).map(Vec::as_slice).unwrap_or_default();
        for p in parts {
            let sig = p.get("thoughtSignature").and_then(Value::as_str).map(str::to_string);
            if let Some(call) = p.get("functionCall") {
                self.calls.push(ToolCall {
                    id: Some(call.get("id").and_then(Value::as_str).map_or_else(new_id, str::to_string)),
                    name: call.get("name").and_then(Value::as_str).unwrap_or_default().to_string(),
                    args: call.get("args").cloned().unwrap_or_else(|| json!({})),
                    signature: sig,
                });
            } else if let Some(text) = p.get("text").and_then(Value::as_str) {
                let thought = p.get("thought").and_then(Value::as_bool).unwrap_or(false);
                if !text.is_empty() {
                    on_delta(if thought { Delta::Thinking(text) } else { Delta::Text(text) });
                }
                self.push_text(text, thought, sig);
            } else if let Some(sig) = sig {
                // A signature on an empty part closes the part before it.
                self.push_text("", false, Some(sig));
            }
        }
    }

    /// Streamed text joins the part before it of the same kind, until a
    /// signature closes that part.
    fn push_text(&mut self, text: &str, thought: bool, sig: Option<String>) {
        match self.parts.last_mut() {
            Some(Typed::Thinking { thinking, signature: s @ None, .. }) if thought => {
                thinking.push_str(text);
                *s = sig;
            }
            Some(Typed::Text { text: t, signature: s @ None, .. }) if !thought => {
                t.push_str(text);
                *s = sig;
            }
            _ if text.is_empty() && sig.is_none() => {}
            _ if thought => self.parts.push(Typed::Thinking { thinking: text.into(), signature: sig, extras: Map::new() }),
            _ => self.parts.push(Typed::Text { text: text.into(), signature: sig, extras: Map::new() }),
        }
    }

    fn finish(self) -> Message {
        let content = match self.parts.as_slice() {
            [] => Content::Text(String::new()),
            [Typed::Text { text, signature: None, .. }] => Content::Text(text.clone()),
            _ => Content::Parts(self.parts.into_iter().map(Part::Typed).collect()),
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

/// `usageMetadata` as LangChain counted it: output includes the thoughts.
fn usage(u: &Value) -> Usage {
    let n = |k: &str| u.get(k).and_then(Value::as_i64);
    let thoughts = n("thoughtsTokenCount");
    let output = match (n("candidatesTokenCount"), thoughts) {
        (None, None) => None,
        (c, t) => Some(c.unwrap_or(0) + t.unwrap_or(0)),
    };
    let mut input_details = Map::new();
    input_details.insert("cache_read".into(), n("cachedContentTokenCount").unwrap_or(0).into());
    let output_details = thoughts.map(|t| {
        let mut d = Map::new();
        d.insert("reasoning".into(), t.into());
        d
    });
    Usage {
        input: n("promptTokenCount"),
        output,
        total: n("totalTokenCount"),
        input_details: Some(input_details),
        output_details,
        extras: Map::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::shape::{Prompt, SystemBlock};

    fn msg(v: Value) -> Message {
        serde_json::from_value(v).unwrap()
    }

    fn prompt(messages: Vec<Value>) -> Prompt {
        Prompt {
            system: vec![SystemBlock { text: "S".into(), breakpoint: false }],
            messages: messages.into_iter().map(msg).collect(),
            history_breakpoint: None,
        }
    }

    fn body(name: &str, p: &Prompt) -> Value {
        render(name, &Request { model: "", prompt: p, tools: &[], blobs: &Blobs::new() }).unwrap()
    }

    #[test]
    fn tool_results_follow_their_call() {
        let p = prompt(vec![
            json!({"v": 1, "role": "user", "content": "hi"}),
            json!({"v": 1, "role": "assistant", "content": [{"type": "text", "text": "ok", "signature": "dA=="}],
                   "tool_calls": [{"id": "a", "name": "run_cell", "args": {"code": "1"}, "signature": "c2ln"},
                                  {"id": "b", "name": "other", "args": {}}]}),
            json!({"v": 1, "role": "tool", "content": "{\"k\": 1}", "tool_call_id": "b", "status": "success"}),
            json!({"v": 1, "role": "tool", "name": "run_cell", "content": "[1, 2]", "tool_call_id": "a", "status": "success"}),
            json!({"v": 1, "role": "tool", "content": "stray", "tool_call_id": "zzz", "status": "success"}),
        ]);
        let b = body("gemma-4-31b-it", &p);
        assert_eq!(
            b["contents"],
            json!([
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [
                    {"text": "ok", "thoughtSignature": "dA=="},
                    {"functionCall": {"name": "run_cell", "args": {"code": "1"}}, "thoughtSignature": "c2ln"},
                    {"functionCall": {"name": "other", "args": {}}}]},
                {"role": "user", "parts": [
                    {"functionResponse": {"name": "other", "response": {"k": 1}}},
                    {"functionResponse": {"name": "run_cell", "response": {"output": [1, 2]}}}]},
            ])
        );
        assert_eq!(b["systemInstruction"], json!({"parts": [{"text": "S"}]}));
        assert_eq!(b["generationConfig"], json!({"temperature": 0.7}));
    }

    #[test]
    fn gemini_3_signs_the_active_loop_only() {
        let call = |id: &str| {
            // Content as stripping thinking leaves it.
            json!({"v": 1, "role": "assistant", "content": [{"type": "text", "text": ""}], "tool_calls": [
                {"id": id, "name": "f", "args": {}}, {"id": format!("{id}2"), "name": "f", "args": {}}]})
        };
        let result = |id: &str| json!({"v": 1, "role": "tool", "content": "r", "tool_call_id": id, "status": "success"});
        let p = prompt(vec![
            json!({"v": 1, "role": "user", "content": "first"}),
            call("old"),
            result("old"),
            result("old2"),
            json!({"v": 1, "role": "user", "content": "second"}),
            call("new"),
            result("new"),
            result("new2"),
        ]);
        let b = body("gemini-3.1-flash-lite", &p);
        let sigs: Vec<Vec<Option<&str>>> = b["contents"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| c["role"] == "model")
            .map(|c| c["parts"].as_array().unwrap().iter().map(|p| p["thoughtSignature"].as_str()).collect())
            .collect();
        assert_eq!(sigs, vec![vec![None, None], vec![Some(DUMMY_SIGNATURE), None]]);
        assert!(b.get("generationConfig").is_none());
        assert_eq!(b["contents"][1]["parts"].as_array().unwrap().len(), 2, "an empty text isn't sent beside calls");
    }

    #[test]
    fn media_and_tools() {
        let p = prompt(vec![json!({"v": 1, "role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image", "mime_type": "image/png", "blob": "sha256:ab"}]})]);
        let blobs = Blobs::from([("sha256:ab".to_string(), "iVBO".to_string())]);
        let tools = [crate::llm::Tool {
            name: "run_cell".into(),
            description: "Run".into(),
            parameters: json!({"type": "object", "properties": {"t": {"type": ["integer", "null"]}}}),
        }];
        let b = render("gemma", &Request { model: "", prompt: &p, tools: &tools, blobs: &blobs }).unwrap();
        assert_eq!(b["contents"][0]["parts"][1], json!({"inlineData": {"mimeType": "image/png", "data": "iVBO"}}));
        assert_eq!(
            b["tools"],
            json!([{"functionDeclarations": [{"name": "run_cell", "description": "Run",
                "parametersJsonSchema": {"type": "object", "properties": {"t": {"type": ["integer", "null"]}}}}]}])
        );
        let missing = Blobs::new();
        assert!(render("gemma", &Request { model: "", prompt: &p, tools: &[], blobs: &missing }).is_err());
    }

    #[test]
    fn reply_from_chunks() {
        let chunks = [
            json!({"candidates": [{"content": {"parts": [{"text": "Let me ", "thought": true}], "role": "model"}}], "modelVersion": "gemma-x"}),
            json!({"candidates": [{"content": {"parts": [{"text": "think", "thought": true}, {"text": "Hel"}]}}]}),
            json!({"candidates": [{"content": {"parts": [
                {"text": "lo", "thoughtSignature": "c2lnMQ=="},
                {"functionCall": {"name": "run_cell", "args": {"code": "1+1"}}, "thoughtSignature": "c2lnMg=="}]},
                "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "totalTokenCount": 130,
                               "thoughtsTokenCount": 10, "cachedContentTokenCount": 40}}),
        ];
        let mut deltas = vec![];
        let mut r = Reply::new("gemma");
        for c in &chunks {
            r.absorb(c, &mut |d| deltas.push(format!("{d:?}")));
        }
        let mut m = serde_json::to_value(r.finish()).unwrap();
        assert!(m["id"].is_string() && m["tool_calls"][0]["id"].is_string());
        m.as_object_mut().unwrap().remove("id");
        m["tool_calls"][0].as_object_mut().unwrap().remove("id");
        assert_eq!(
            m,
            json!({"v": 1, "role": "assistant", "content": [
                {"type": "thinking", "thinking": "Let me think"},
                {"type": "text", "text": "Hello", "signature": "c2lnMQ=="}],
                "tool_calls": [{"name": "run_cell", "args": {"code": "1+1"}, "signature": "c2lnMg=="}],
                "usage": {"input": 100, "output": 30, "total": 130, "input_details": {"cache_read": 40},
                          "output_details": {"reasoning": 10}},
                "model": {"provider": "google_genai", "name": "gemma-x"},
                "finish_reason": "STOP"})
        );
        assert_eq!(deltas, ["Thinking(\"Let me \")", "Thinking(\"think\")", "Text(\"Hel\")", "Text(\"lo\")"]);
    }

    #[test]
    fn plain_text_reply_is_a_string() {
        let mut r = Reply::new("g");
        r.absorb(&json!({"candidates": [{"content": {"parts": [{"text": "a"}]}}]}), &mut |_| {});
        r.absorb(&json!({"candidates": [{"content": {"parts": [{"text": "b"}]}, "finishReason": "STOP"}]}), &mut |_| {});
        assert_eq!(r.finish().content, Content::Text("ab".into()));
    }
}
