//! OpenAI's Responses format, streamed — what Meta's Model API speaks
//! (`ChatMetaModel` defaults to it).
//!
//! What it sends follows LangChain's `_construct_responses_api_input` for the
//! same history: an assistant's text goes back as message items with the id
//! and `phase` the server gave them, a recorded `function_call` item goes back
//! as it was, and a call recorded only as a tool call gets a fresh item.
//!
//! The reply's reasoning becomes a `thinking` part (its summary, with the
//! item's id and any `encrypted_content` in extras) instead of LangChain's
//! opaque item, and the provider is the catalog's (`meta`), not `openai`.
//! Function calls are kept only as tool calls: their item ids aren't needed
//! to send them back.

use serde_json::{Map, Value, json};

use super::lines::Lines;
use super::transcript::{Content, Message, ModelRef, Part, Role, ToolCall, Typed, Usage};
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
    let resp =
        http.post(format!("{base}/responses")).bearer_auth(key).json(&body).send().await.map_err(Error::connection)?;
    if !resp.status().is_success() {
        return Err(Error::from_response(resp).await);
    }
    let mut lines = Lines::new(resp);
    let mut reply = Reply::new(provider, name);
    while let Some(data) = lines.next_event().await? {
        // OpenAI's Responses stream just ends; OpenRouter's says so first.
        if data.trim() == "[DONE]" {
            break;
        }
        let event: Value =
            serde_json::from_str(&data).map_err(|e| Error::fatal(format!("unreadable stream event: {e}: {data}")))?;
        match event.get("type").and_then(Value::as_str) {
            Some("error") => return Err(Error::from_stream(event.get("error").unwrap_or(&event))),
            Some("response.failed") => {
                return Err(Error::from_stream(event.pointer("/response/error").unwrap_or(&event)));
            }
            _ => reply.absorb(&event, on_delta),
        }
    }
    Ok(reply.finish())
}

// ── request ──────────────────────────────────────────────────────────────────

/// Recorded output items sent back as they are.
const REPLAYED: [&str; 14] = [
    "compaction",
    "web_search_call",
    "file_search_call",
    "function_call",
    "computer_call",
    "custom_tool_call",
    "code_interpreter_call",
    "mcp_call",
    "mcp_list_tools",
    "mcp_approval_request",
    "tool_search_call",
    "tool_search_output",
    "apply_patch_call",
    "apply_patch_call_output",
];

/// The prefix of an id LangChain made up rather than the server issued.
const LC_AUTO_PREFIX: &str = "lc_";

pub fn render(name: &str, req: &Request<'_>) -> Result<Value, Error> {
    let system: Vec<&str> = req.prompt.system.iter().map(|b| b.text.as_str()).collect();
    let mut input = vec![json!({"content": system.join("\n\n"), "role": "system", "type": "message"})];
    for m in &req.prompt.messages {
        match m.role {
            Role::System | Role::User => {
                let role = if m.role == Role::User { "user" } else { "system" };
                let content = match &m.content {
                    Content::Text(s) => Value::String(s.clone()),
                    Content::Parts(parts) => {
                        let blocks = input_blocks(parts, req.blobs)?;
                        if blocks.is_empty() {
                            continue;
                        }
                        blocks.into()
                    }
                };
                input.push(json!({"content": content, "role": role, "type": "message"}));
            }
            Role::Tool => {
                let output = match &m.content {
                    Content::Text(s) => Value::String(s.clone()),
                    Content::Parts(parts) => input_blocks(parts, req.blobs)?.into(),
                };
                input.push(json!({"type": "function_call_output", "output": output, "call_id": m.tool_call_id}));
            }
            Role::Assistant => assistant(m, &mut input),
        }
    }
    let mut body = json!({"input": input, "model": name, "stream": true});
    if !req.tools.is_empty() {
        let tools: Vec<Value> = req
            .tools
            .iter()
            .map(|t| json!({"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}))
            .collect();
        body["tools"] = tools.into();
    }
    Ok(body)
}

fn input_blocks(parts: &[Part], blobs: &Blobs) -> Result<Vec<Value>, Error> {
    let mut out = vec![];
    for p in parts {
        match p {
            Part::Str(s) | Part::Typed(Typed::Text { text: s, .. }) => out.push(json!({"type": "input_text", "text": s})),
            Part::Typed(Typed::Image(m) | Typed::File(m)) => {
                out.push(json!({"type": "input_image", "image_url": data_url(m, blobs)?}));
            }
            _ => {}
        }
    }
    Ok(out)
}

fn assistant(m: &Message, input: &mut Vec<Value>) {
    match &m.content {
        Content::Text(s) if !s.is_empty() => input.push(output_message(s, None, None)),
        Content::Text(_) => {}
        Content::Parts(parts) => {
            for p in parts {
                match p {
                    Part::Str(s) => input.push(output_message(s, None, None)),
                    Part::Typed(Typed::Text { text, extras, .. }) => {
                        let id = extras.get("id").and_then(Value::as_str);
                        let phase = extras.get("phase").cloned();
                        let block = json!({"type": "output_text", "text": text, "annotations": []});
                        // Text parts of one server item go back as one item.
                        let same = id.and_then(|id| input.iter_mut().find(|item| item.get("id").and_then(Value::as_str) == Some(id)));
                        match same {
                            Some(item) => {
                                if let Some(c) = item["content"].as_array_mut() {
                                    c.push(block);
                                }
                                if let Some(phase) = phase {
                                    item["phase"] = phase;
                                }
                            }
                            None => input.push(output_message(text, id.filter(|i| !i.starts_with(LC_AUTO_PREFIX)), phase)),
                        }
                    }
                    Part::Typed(Typed::Opaque { data, .. })
                        if data.get("type").and_then(Value::as_str).is_some_and(|t| REPLAYED.contains(&t)) =>
                    {
                        let mut item = data.clone();
                        if let Some(obj) = item.as_object_mut() {
                            obj.remove("index");
                        }
                        input.push(item);
                    }
                    _ => {}
                }
            }
        }
    }
    let present: Vec<String> = input
        .iter()
        .filter(|i| matches!(i.get("type").and_then(Value::as_str), Some("function_call" | "custom_tool_call")))
        .filter_map(|i| i.get("call_id").and_then(Value::as_str).map(str::to_string))
        .collect();
    for c in &m.tool_calls {
        if c.id.as_ref().is_some_and(|id| present.contains(id)) {
            continue;
        }
        input.push(json!({"type": "function_call", "name": c.name, "arguments": pyjson::dumps_unicode(&c.args), "call_id": c.id}));
    }
}

fn output_message(text: &str, id: Option<&str>, phase: Option<Value>) -> Value {
    let mut item = json!({"type": "message", "content": [{"type": "output_text", "text": text, "annotations": []}], "role": "assistant"});
    if let Some(id) = id {
        item["id"] = id.into();
    }
    if let Some(phase) = phase {
        item["phase"] = phase;
    }
    item
}

// ── reply ────────────────────────────────────────────────────────────────────

struct Reply {
    id: Option<String>,
    /// Text and thinking parts in output order, each tagged with its item id.
    parts: Vec<(String, Typed)>,
    calls: Vec<ToolCall>,
    invalid: Vec<Value>,
    usage: Option<Usage>,
    status: Option<String>,
    provider: String,
    model: String,
}

impl Reply {
    fn new(provider: &str, model: &str) -> Self {
        Reply {
            id: None,
            parts: vec![],
            calls: vec![],
            invalid: vec![],
            usage: None,
            status: None,
            provider: provider.into(),
            model: model.into(),
        }
    }

    fn part(&mut self, item_id: &str, thought: bool) -> &mut Typed {
        let i = self.parts.iter().position(|(id, p)| id == item_id && matches!(p, Typed::Thinking { .. }) == thought);
        let i = i.unwrap_or_else(|| {
            let mut extras = Map::new();
            if !item_id.is_empty() {
                extras.insert("id".into(), item_id.into());
            }
            let p = if thought {
                Typed::Thinking { thinking: String::new(), signature: None, extras }
            } else {
                Typed::Text { text: String::new(), signature: None, extras }
            };
            self.parts.push((item_id.to_string(), p));
            self.parts.len() - 1
        });
        &mut self.parts[i].1
    }

    fn absorb(&mut self, ev: &Value, on_delta: &mut (dyn FnMut(Delta) + Send)) {
        let s = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).unwrap_or_default().to_string();
        match ev.get("type").and_then(Value::as_str).unwrap_or_default() {
            "response.created" | "response.in_progress" | "response.completed" | "response.incomplete" => {
                let r = &ev["response"];
                if let Some(id) = r.get("id").and_then(Value::as_str) {
                    self.id = Some(id.into());
                }
                if let Some(m) = r.get("model").and_then(Value::as_str) {
                    self.model = m.into();
                }
                if let Some(u) = r.get("usage").filter(|u| u.is_object()) {
                    self.usage = Some(usage(u));
                }
                self.status = r
                    .pointer("/incomplete_details/reason")
                    .and_then(Value::as_str)
                    .or_else(|| r.get("status").and_then(Value::as_str))
                    .map(str::to_string);
            }
            "response.output_item.added" => {
                let item = &ev["item"];
                if item["type"] == "message" {
                    let phase = item.get("phase").cloned();
                    if let (Typed::Text { extras, .. }, Some(phase)) = (self.part(&s(item, "id"), false), phase) {
                        extras.insert("phase".into(), phase);
                    }
                }
            }
            "response.output_text.delta" => {
                let delta = s(ev, "delta");
                if let Typed::Text { text, .. } = self.part(&s(ev, "item_id"), false) {
                    text.push_str(&delta);
                }
                if !delta.is_empty() {
                    on_delta(Delta::Text(&delta));
                }
            }
            "response.reasoning_summary_text.delta" | "response.reasoning_text.delta" => {
                let delta = s(ev, "delta");
                if let Typed::Thinking { thinking, .. } = self.part(&s(ev, "item_id"), true) {
                    thinking.push_str(&delta);
                }
                if !delta.is_empty() {
                    on_delta(Delta::Thinking(&delta));
                }
            }
            "response.reasoning_summary_part.added" => {
                // Summary parts read as paragraphs.
                if ev.get("summary_index").and_then(Value::as_u64).unwrap_or(0) > 0
                    && let Typed::Thinking { thinking, .. } = self.part(&s(ev, "item_id"), true)
                {
                    thinking.push_str("\n\n");
                }
            }
            "response.output_item.done" => {
                let item = &ev["item"];
                match item.get("type").and_then(Value::as_str) {
                    Some("function_call") => {
                        let (id, name, args) = (s(item, "call_id"), s(item, "name"), s(item, "arguments"));
                        match super::parse_args(&args) {
                            Ok(a) => self.calls.push(ToolCall { id: Some(id), name, args: a, signature: None }),
                            Err(error) => self.invalid.push(json!({"id": id, "name": name, "args": args, "error": error})),
                        }
                    }
                    Some("reasoning") => {
                        if let Some(enc) = item.get("encrypted_content").filter(|e| e.is_string())
                            && let Typed::Thinking { extras, .. } = self.part(&s(item, "id"), true)
                        {
                            extras.insert("encrypted_content".into(), enc.clone());
                        }
                    }
                    _ => {}
                }
            }
            _ => {}
        }
    }

    fn finish(self) -> Message {
        let parts: Vec<Part> = self
            .parts
            .into_iter()
            .filter(|(_, p)| match p {
                Typed::Text { text, .. } => !text.is_empty(),
                Typed::Thinking { thinking, extras, .. } => !thinking.is_empty() || extras.contains_key("encrypted_content"),
                _ => true,
            })
            .map(|(_, p)| Part::Typed(p))
            .collect();
        let mut m = Message::new(Role::Assistant, if parts.is_empty() { Content::Text(String::new()) } else { Content::Parts(parts) });
        m.id = Some(self.id.unwrap_or_else(new_id));
        m.tool_calls = self.calls;
        m.invalid_tool_calls = self.invalid;
        m.usage = self.usage;
        m.model = Some(ModelRef { provider: Some(self.provider), name: Some(self.model) });
        m.finish_reason = self.status.map(Value::String);
        m
    }
}

/// Responses usage as LangChain counted it.
fn usage(u: &Value) -> Usage {
    let n = |v: &Value, k: &str| v.get(k).and_then(Value::as_i64);
    let input = n(u, "input_tokens").unwrap_or(0);
    let output = n(u, "output_tokens").unwrap_or(0);
    let detail = |src: &str, from: &str, to: &str| {
        u.get(src).and_then(|d| n(d, from)).map(|v| (to.to_string(), Value::from(v))).into_iter().collect::<Map<_, _>>()
    };
    Usage {
        input: Some(input),
        output: Some(output),
        total: Some(n(u, "total_tokens").unwrap_or(input + output)),
        input_details: Some(detail("input_tokens_details", "cached_tokens", "cache_read")),
        output_details: Some(detail("output_tokens_details", "reasoning_tokens", "reasoning")),
        extras: Map::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::shape::{Prompt, SystemBlock};

    #[test]
    fn replays_recorded_items() {
        let msgs: Vec<Message> = serde_json::from_value(json!([
            {"v": 1, "role": "user", "content": "q"},
            {"v": 1, "role": "assistant", "content": [
                {"type": "text", "text": "I'll check.", "extras": {"phase": "commentary", "index": 1, "id": "msg_1"}},
                {"type": "text", "text": " More.", "extras": {"id": "msg_1"}},
                {"type": "opaque", "data": {"type": "function_call", "name": "f", "arguments": "{}", "call_id": "c1", "id": "fc_1", "index": 2}}],
             "tool_calls": [{"id": "c1", "name": "f", "args": {}}, {"id": "c2", "name": "g", "args": {"x": "é"}}]},
            {"v": 1, "role": "tool", "content": "r", "tool_call_id": "c1", "status": "success"},
            {"v": 1, "role": "assistant", "content": [{"type": "text", "text": "done", "extras": {"id": "lc_run--x"}}]},
        ]))
        .unwrap();
        let p = Prompt { system: vec![SystemBlock { text: "S".into(), breakpoint: false }], messages: msgs, history_breakpoint: None, cached: false };
        let b = render("m", &Request { model: "", prompt: &p, tools: &[], blobs: &Blobs::new() }).unwrap();
        assert_eq!(
            b["input"],
            json!([
                {"content": "S", "role": "system", "type": "message"},
                {"content": "q", "role": "user", "type": "message"},
                {"type": "message", "role": "assistant", "id": "msg_1", "phase": "commentary", "content": [
                    {"type": "output_text", "text": "I'll check.", "annotations": []},
                    {"type": "output_text", "text": " More.", "annotations": []}]},
                {"type": "function_call", "name": "f", "arguments": "{}", "call_id": "c1", "id": "fc_1"},
                {"type": "function_call", "name": "g", "arguments": "{\"x\": \"é\"}", "call_id": "c2"},
                {"type": "function_call_output", "output": "r", "call_id": "c1"},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done", "annotations": []}]},
            ])
        );
    }

    #[test]
    fn reply_from_events() {
        let evs = [
            json!({"type": "response.created", "response": {"id": "resp_1", "model": "muse", "status": "in_progress"}}),
            json!({"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1"}}),
            json!({"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "delta": "Think."}),
            json!({"type": "response.output_item.done", "item": {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC"}}),
            json!({"type": "response.output_item.added", "item": {"type": "message", "id": "msg_1", "phase": "commentary"}}),
            json!({"type": "response.output_text.delta", "item_id": "msg_1", "delta": "Let me"}),
            json!({"type": "response.output_text.delta", "item_id": "msg_1", "delta": " check."}),
            json!({"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "run_cell", "arguments": "{\"code\": \"x\"}"}}),
            json!({"type": "response.completed", "response": {"id": "resp_1", "model": "muse", "status": "completed",
                   "usage": {"input_tokens": 9, "output_tokens": 3, "total_tokens": 12, "input_tokens_details": {"cached_tokens": 4},
                             "output_tokens_details": {"reasoning_tokens": 1}}}}),
        ];
        let mut r = Reply::new("meta", "x");
        let mut seen = vec![];
        for e in &evs {
            r.absorb(e, &mut |d| seen.push(format!("{d:?}")));
        }
        assert_eq!(
            serde_json::to_value(r.finish()).unwrap(),
            json!({"v": 1, "role": "assistant", "id": "resp_1", "content": [
                {"type": "thinking", "thinking": "Think.", "extras": {"id": "rs_1", "encrypted_content": "ENC"}},
                {"type": "text", "text": "Let me check.", "extras": {"id": "msg_1", "phase": "commentary"}}],
             "tool_calls": [{"id": "call_1", "name": "run_cell", "args": {"code": "x"}}],
             "usage": {"input": 9, "output": 3, "total": 12, "input_details": {"cache_read": 4}, "output_details": {"reasoning": 1}},
             "model": {"provider": "meta", "name": "muse"},
             "finish_reason": "completed"})
        );
        assert_eq!(seen, ["Thinking(\"Think.\")", "Text(\"Let me\")", "Text(\" check.\")"]);
    }
}
