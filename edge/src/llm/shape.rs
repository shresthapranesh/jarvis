//! What a thread's history goes through before it is sent — a port of
//! `core/messages.py` (`strip_historical_thinking`, `repair_orphan_tool_calls`,
//! `build_llm_messages`) and the cache layout in `core/context_cache.py`.
//!
//! The result is a [`Prompt`]: neutral, with breakpoints as flags. Each
//! provider spells them its own way (`cache_control` on a block, Bedrock's
//! `cachePoint`), and renders string content as a block list where a cached
//! prefix needs it to serialize the same marked or not — Python does that
//! with `normalize_history_content`, here it is the provider's job.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use super::transcript::{Content, Message, Part, Role, Typed};

/// One logical piece of the system prompt (`CacheSegment`).
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Segment {
    pub name: String,
    pub content: String,
    #[serde(default = "yes")]
    pub cacheable: bool,
}

fn yes() -> bool {
    true
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct SystemBlock {
    pub text: String,
    /// A cache breakpoint closes this block.
    pub breakpoint: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct Prompt {
    pub system: Vec<SystemBlock>,
    pub messages: Vec<Message>,
    /// The message carrying the rolling history breakpoint.
    pub history_breakpoint: Option<usize>,
    /// Laid out for a prefix cache: a provider renders a user or tool
    /// message's string content as one text block, so the message the
    /// breakpoint leaves on the next call serializes as it did when marked.
    #[serde(skip)]
    pub cached: bool,
}

// ── strip_historical_thinking ────────────────────────────────────────────────

/// Anthropic's `thinking`/`redacted_thinking` and LangChain v1's `reasoning`
/// (which reaches the transcript as an opaque part) — all three, or a block
/// one provider left behind crashes another.
const THINKING_TYPES: [&str; 3] = ["thinking", "redacted_thinking", "reasoning"];
/// `additional_kwargs` mirrors of the same content.
const THINKING_KWARGS: [&str; 2] = ["thinking", "reasoning"];

fn is_thinking(part: &Part) -> bool {
    match part {
        Part::Typed(Typed::Thinking { .. } | Typed::RedactedThinking { .. }) => true,
        _ => part.opaque_type().is_some_and(|t| THINKING_TYPES.contains(&t)),
    }
}

/// Drop every thinking block from every assistant message. The model thinks
/// afresh each turn, and a historical block's signature is what Bedrock and
/// Anthropic reject when it doesn't survive a round trip.
pub fn strip_historical_thinking(messages: Vec<Message>) -> Vec<Message> {
    messages
        .into_iter()
        .map(|mut m| {
            if m.role != Role::Assistant {
                return m;
            }
            if let Content::Parts(parts) = &mut m.content
                && parts.iter().any(is_thinking)
            {
                parts.retain(|p| !is_thinking(p));
                if parts.is_empty() {
                    parts.push(Part::Typed(Typed::Text { text: String::new(), signature: None, extras: Map::new() }));
                }
            }
            if let Some(Value::Object(kwargs)) = m.extras.get_mut("additional_kwargs") {
                for k in THINKING_KWARGS {
                    kwargs.remove(k);
                }
                // The encoder leaves an empty map out.
                if kwargs.is_empty() {
                    m.extras.remove("additional_kwargs");
                }
            }
            m
        })
        .collect()
}

/// What a call to `model` keeps of past thinking. A model that preserves
/// thinking (`llm::preserves_thinking`) is sent Anthropic's own blocks back
/// unchanged — the API drops another model's, unbilled — and loses only what
/// it can't read: another provider's reasoning, and a block with no
/// signature. Every other model is sent none (`strip_historical_thinking`).
pub fn strip_thinking_for(model: &str, messages: Vec<Message>) -> Vec<Message> {
    if !super::preserves_thinking(model) {
        return strip_historical_thinking(messages);
    }
    messages
        .into_iter()
        .map(|mut m| {
            if m.role != Role::Assistant {
                return m;
            }
            let native = m.model.as_ref().and_then(|r| r.provider.as_deref()) == Some("anthropic");
            if !native {
                return strip_historical_thinking(vec![m]).pop().expect("one in, one out");
            }
            // A Claude block that lost its signature can't be sent back.
            if let Content::Parts(parts) = &mut m.content
                && parts.iter().any(|p| matches!(p, Part::Typed(Typed::Thinking { signature: None, .. })))
            {
                parts.retain(|p| !matches!(p, Part::Typed(Typed::Thinking { signature: None, .. })));
                if parts.is_empty() {
                    parts.push(Part::Typed(Typed::Text { text: String::new(), signature: None, extras: Map::new() }));
                }
            }
            m
        })
        .collect()
}

// ── repair_orphan_tool_calls ─────────────────────────────────────────────────

pub const MISSING_RESULT: &str = "[Tool result missing — previous run was cancelled or interrupted.]";

/// The ids an assistant message needs results for: its `tool_calls`, and any
/// Anthropic `tool_use` part (a stream cut off mid-call leaves one there).
fn tool_use_ids(m: &Message) -> Vec<String> {
    let mut ids: Vec<String> = vec![];
    let from_calls = m.tool_calls.iter().filter_map(|c| c.id.clone());
    let from_parts = parts(m)
        .iter()
        .filter(|p| p.opaque_type() == Some("tool_use"))
        .filter_map(|p| opaque_str(p, "id"));
    for id in from_calls.chain(from_parts) {
        if !id.is_empty() && !ids.contains(&id) {
            ids.push(id);
        }
    }
    ids
}

fn parts(m: &Message) -> &[Part] {
    match &m.content {
        Content::Parts(p) => p,
        Content::Text(_) => &[],
    }
}

fn opaque_str(p: &Part, key: &str) -> Option<String> {
    match p {
        Part::Typed(Typed::Opaque { data, .. }) => data.get(key).and_then(Value::as_str).map(str::to_string),
        _ => None,
    }
}

/// A tool message, or a user message carrying Anthropic `tool_result` parts.
pub(super) fn carries_results(m: &Message) -> bool {
    match m.role {
        Role::Tool => true,
        Role::User => parts(m).iter().any(|p| p.opaque_type() == Some("tool_result")),
        _ => false,
    }
}

fn result_ids(m: &Message) -> Vec<String> {
    if m.role == Role::Tool {
        return m.tool_call_id.iter().filter(|id| !id.is_empty()).cloned().collect();
    }
    parts(m)
        .iter()
        .filter(|p| p.opaque_type() == Some("tool_result"))
        .filter_map(|p| opaque_str(p, "tool_use_id"))
        .filter(|id| !id.is_empty())
        .collect()
}

/// Give every tool call without a result in the messages right after it a
/// synthetic one. Anthropic and Bedrock reject a `tool_use` not answered in
/// the next turn, and a run cancelled between the model step and its tools
/// leaves exactly that.
pub fn repair_orphan_tool_calls(messages: Vec<Message>) -> Vec<Message> {
    let mut out = Vec::with_capacity(messages.len());
    let mut it = messages.into_iter().peekable();
    while let Some(m) = it.next() {
        let expected = if m.role == Role::Assistant { tool_use_ids(&m) } else { vec![] };
        out.push(m);
        if expected.is_empty() {
            continue;
        }
        let mut seen: Vec<String> = vec![];
        while let Some(next) = it.next_if(carries_results) {
            seen.extend(result_ids(&next));
            out.push(next);
        }
        for id in expected.into_iter().filter(|id| !seen.contains(id)) {
            let mut result = Message::new(Role::Tool, Content::Text(MISSING_RESULT.into()));
            result.tool_call_id = Some(id);
            result.status = Some("success".into());
            out.push(result);
        }
    }
    out
}

// ── build_llm_messages ───────────────────────────────────────────────────────

/// Volatile context rides at the end as a user message, so it says plainly
/// that the user didn't write it.
///
/// For a model that preserves thinking the history must only grow, so the
/// context isn't rebuilt at the end of every call: it is stored on the
/// message it followed (`TURN_CONTEXT_KEY` in that message's extras, by
/// `carry_turn_context`), and every call sends each stored copy where it
/// was. The model acts on the newest.
pub const TURN_CONTEXT_OPEN: &str = "<turn_context>\nContext the application attached for this step (memories retrieved for the current request, task list, project state). It is not a message from the user.\n\n";
pub const TURN_CONTEXT_CLOSE: &str = "\n</turn_context>";
/// Where a message keeps the turn context sent after it.
pub const TURN_CONTEXT_KEY: &str = "turn_context";

/// Providers whose breakpoint is a standalone block after the content.
const CACHE_POINT_PROVIDERS: [&str; 1] = ["bedrock"];
/// Providers whose tool messages can't carry a breakpoint (ChatOpenAI strips
/// `cache_control` from role=tool messages on OpenRouter).
const TOOL_MESSAGE_UNMARKABLE: [&str; 1] = ["openrouter"];

pub struct Layout<'a> {
    /// The static prompt.
    pub system: &'a str,
    /// The stable sections after it — the caller has already ordered them.
    pub segments: &'a [Segment],
    /// What may change on every call: retrieved memories, todos, …
    pub volatile: &'a str,
    /// Lay the request out for a prefix cache.
    pub cache: bool,
    /// Picks the breakpoint spelling and which messages can carry one.
    pub provider: &'a str,
    /// The history only grows (`llm::preserves_thinking`): the turn context
    /// goes where each copy was stored, not rebuilt at the end.
    pub append_only: bool,
}

/// The turn context for this call: the uncacheable segments and the
/// volatile tail, wrapped — `None` when there is none.
pub fn turn_context(layout: &Layout) -> Option<String> {
    let tail: Vec<&str> = layout
        .segments
        .iter()
        .filter(|s| !s.cacheable && !blank(&s.content))
        .map(|s| s.content.as_str())
        .chain(Some(layout.volatile.trim()).filter(|v| !v.is_empty()))
        .collect();
    (!tail.is_empty()).then(|| format!("{TURN_CONTEXT_OPEN}{}{TURN_CONTEXT_CLOSE}", tail.join("\n\n")))
}

/// Store `context` on the newest message when it differs from the newest
/// copy already stored — the append-only turn context. Returns the index of
/// the message it was stored on, which the caller writes back. Nothing is
/// stored after an assistant message (a call never continues one).
pub fn carry_turn_context(history: &mut [Message], context: Option<String>) -> Option<usize> {
    let context = context?;
    let stored = history.iter().rev().find_map(|m| m.extras.get(TURN_CONTEXT_KEY).and_then(Value::as_str));
    if stored == Some(context.as_str()) {
        return None;
    }
    let last = history.len().checked_sub(1)?;
    if history[last].role == Role::Assistant {
        return None;
    }
    history[last].extras.insert(TURN_CONTEXT_KEY.into(), Value::String(context));
    Some(last)
}

fn context_message(text: String) -> Message {
    Message::new(Role::User, Content::Parts(vec![Part::Typed(Typed::Text { text, signature: None, extras: Map::new() })]))
}

fn blank(s: &str) -> bool {
    s.trim().is_empty()
}

/// The request for `history`, with exactly one system prompt: a system
/// message inside the history (a compaction summary) is folded into it, as
/// Anthropic and Bedrock reject a system message after the conversation began.
///
/// With `cache`, most-stable-first — a prefix cache is invalidated from the
/// first changed byte:
///
/// ```text
/// system:  static ▸bp  cacheable segments + conversation summary ▸bp
/// history  rolling ▸bp on its newest markable message
/// tail:    one user message, <turn_context>…</turn_context>
/// ```
///
/// Without, everything (volatile and summaries included) is one system text
/// and the history passes through as it is.
pub fn build(layout: &Layout, history: Vec<Message>) -> Prompt {
    let mut summaries = vec![];
    let mut rest = vec![];
    for m in history {
        if m.role == Role::System {
            let text = system_text(&m);
            if !blank(&text) {
                summaries.push(text.trim().to_string());
            }
        } else {
            rest.push(m);
        }
    }
    if layout.cache || layout.append_only {
        cached(layout, rest, summaries)
    } else {
        uncached(layout, rest, summaries)
    }
}

/// `_system_text`: string content, or its text parts joined by newlines.
fn system_text(m: &Message) -> String {
    match &m.content {
        Content::Text(s) => s.clone(),
        Content::Parts(parts) => parts
            .iter()
            .filter_map(|p| match p {
                Part::Typed(Typed::Text { text, .. }) => Some(text.as_str()),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("\n"),
    }
}

fn uncached(layout: &Layout, rest: Vec<Message>, summaries: Vec<String>) -> Prompt {
    let volatile: Vec<&str> = std::iter::once(layout.volatile.trim())
        .chain(summaries.iter().map(String::as_str))
        .filter(|p| !p.is_empty())
        .collect();
    let volatile = volatile.join("\n\n");
    let text = if layout.segments.is_empty() {
        // `_make_system_message`
        if blank(&volatile) { layout.system.to_string() } else { format!("{}\n\n{volatile}", layout.system) }
    } else {
        // `build_cached_system_message`'s no-cache path
        std::iter::once(layout.system)
            .chain(layout.segments.iter().filter(|s| !blank(&s.content)).map(|s| s.content.as_str()))
            .chain(Some(volatile.as_str()).filter(|v| !blank(v)))
            .filter(|p| !blank(p))
            .collect::<Vec<_>>()
            .join("\n\n")
    };
    Prompt {
        system: vec![SystemBlock { text, breakpoint: false }],
        messages: rest,
        history_breakpoint: None,
        cached: false,
    }
}

fn cached(layout: &Layout, rest: Vec<Message>, summaries: Vec<String>) -> Prompt {
    let mut system = vec![];
    if !blank(layout.system) {
        system.push(SystemBlock { text: layout.system.to_string(), breakpoint: true });
    }
    let summary = (!summaries.is_empty()).then(|| summaries.join("\n\n"));
    let stable: Vec<&str> = layout
        .segments
        .iter()
        .filter(|s| s.cacheable)
        .map(|s| s.content.as_str())
        .chain(summary.as_deref())
        .filter(|c| !blank(c))
        .collect();
    let n = stable.len();
    for (i, text) in stable.into_iter().enumerate() {
        system.push(SystemBlock { text: text.to_string(), breakpoint: i + 1 == n });
    }
    if system.is_empty() {
        system.push(SystemBlock { text: layout.system.to_string(), breakpoint: false });
    }

    if layout.append_only {
        // Each stored copy right after the message it followed — a user
        // turn the provider merges with that message's own.
        let mut messages = Vec::with_capacity(rest.len());
        for m in rest {
            let carried = m.extras.get(TURN_CONTEXT_KEY).and_then(Value::as_str).map(str::to_string);
            messages.push(m);
            if let Some(text) = carried {
                messages.push(context_message(text));
            }
        }
        let history_breakpoint = messages.iter().rposition(|m| markable(m, layout.provider));
        return Prompt { system, messages, history_breakpoint, cached: true };
    }
    let history_breakpoint = rest.iter().rposition(|m| markable(m, layout.provider));
    let mut messages = rest;
    if let Some(text) = turn_context(layout) {
        messages.push(context_message(text));
    }
    Prompt { system, messages, history_breakpoint, cached: true }
}

/// Whether the history breakpoint can sit on `m` (`_markable`, over content
/// as `normalize_history_content` leaves it: a user or tool message's
/// non-blank string is one text block).
fn markable(m: &Message, provider: &str) -> bool {
    if !matches!(m.role, Role::User | Role::Tool) {
        // An assistant message may end in tool_use or thinking, which the
        // integrations disagree on marking.
        return false;
    }
    if TOOL_MESSAGE_UNMARKABLE.contains(&provider) && carries_results(m) {
        return false;
    }
    let last = match &m.content {
        Content::Text(s) => return !blank(s),
        Content::Parts(p) => match p.last() {
            Some(last) => last,
            None => return false,
        },
    };
    if CACHE_POINT_PROVIDERS.contains(&provider) {
        return true;
    }
    // A breakpoint sits on the block itself: only non-empty text or a
    // tool_result takes one on every Anthropic-shaped API.
    match last {
        Part::Str(s) => !blank(s),
        Part::Typed(Typed::Text { text, .. }) => !blank(text),
        p => p.opaque_type() == Some("tool_result"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::transcript::ToolCall;
    use serde_json::json;

    fn msg(v: Value) -> Message {
        serde_json::from_value(v).unwrap()
    }

    fn ai_calling(ids: &[&str]) -> Message {
        let mut m = Message::new(Role::Assistant, Content::Text(String::new()));
        m.tool_calls = ids
            .iter()
            .map(|id| ToolCall { id: Some(id.to_string()), name: "run_cell".into(), args: json!({}), signature: None })
            .collect();
        m
    }

    fn tool(id: &str) -> Message {
        msg(json!({"v": 1, "role": "tool", "content": "r", "tool_call_id": id, "status": "success"}))
    }

    #[test]
    fn strips_thinking_and_its_mirrors() {
        let m = msg(json!({"v": 1, "role": "assistant", "content": [
            {"type": "thinking", "thinking": "a"},
            {"type": "opaque", "data": {"type": "reasoning", "summary": []}},
            {"type": "text", "text": "kept"}],
            "extras": {"additional_kwargs": {"reasoning": {}, "thinking": "x", "other": 1}}}));
        let out = strip_historical_thinking(vec![m]);
        assert_eq!(
            serde_json::to_value(&out[0]).unwrap(),
            json!({"v": 1, "role": "assistant", "content": [{"type": "text", "text": "kept"}],
                   "extras": {"additional_kwargs": {"other": 1}}})
        );
        let only = msg(json!({"v": 1, "role": "assistant", "content": [{"type": "redacted_thinking", "data": "z"}]}));
        let out = strip_historical_thinking(vec![only]);
        assert_eq!(serde_json::to_value(&out[0].content).unwrap(), json!([{"type": "text", "text": ""}]));
        let user = msg(json!({"v": 1, "role": "user", "content": [{"type": "thinking", "thinking": "a"}]}));
        assert_eq!(strip_historical_thinking(vec![user.clone()])[0], user);
    }

    #[test]
    fn answers_orphaned_calls_in_place() {
        let user = msg(json!({"v": 1, "role": "user", "content": "go"}));
        let out = repair_orphan_tool_calls(vec![ai_calling(&["a", "b"]), tool("b"), user.clone()]);
        let roles: Vec<_> = out.iter().map(|m| (m.role, m.tool_call_id.clone())).collect();
        assert_eq!(
            roles,
            vec![
                (Role::Assistant, None),
                (Role::Tool, Some("b".into())),
                (Role::Tool, Some("a".into())),
                (Role::User, None)
            ]
        );
        assert_eq!(out[2].text(), MISSING_RESULT);
        // A tool_use left in content counts; a tool_result in a user message answers it.
        let ai = msg(json!({"v": 1, "role": "assistant", "content": [
            {"type": "opaque", "data": {"type": "tool_use", "id": "t1", "name": "x", "input": {}}}]}));
        let carried = msg(json!({"v": 1, "role": "user", "content": [
            {"type": "opaque", "data": {"type": "tool_result", "tool_use_id": "t1", "content": "r"}}]}));
        assert_eq!(repair_orphan_tool_calls(vec![ai.clone(), carried.clone()]).len(), 2);
        assert_eq!(repair_orphan_tool_calls(vec![ai]).len(), 2);
    }

    fn seg(name: &str, content: &str, cacheable: bool) -> Segment {
        Segment { name: name.into(), content: content.into(), cacheable }
    }

    #[test]
    fn uncached_is_one_system_text() {
        let summary = msg(json!({"v": 1, "role": "system", "content": " earlier "}));
        let user = msg(json!({"v": 1, "role": "user", "content": "hi"}));
        let layout = Layout { system: "S", segments: &[], volatile: " todos ", cache: false, provider: "google_genai", append_only: false };
        let p = build(&layout, vec![summary.clone(), user.clone()]);
        assert_eq!(p.system, vec![SystemBlock { text: "S\n\ntodos\n\nearlier".into(), breakpoint: false }]);
        assert_eq!(p.messages, vec![user.clone()]);
        let segs = [seg("skills", "K", true), seg("blank", "  ", true)];
        let layout = Layout { segments: &segs, ..layout };
        let p = build(&layout, vec![summary, user]);
        assert_eq!(p.system[0].text, "S\n\nK\n\ntodos\n\nearlier");
    }

    #[test]
    fn cached_layout_most_stable_first() {
        let summary = msg(json!({"v": 1, "role": "system", "content": "earlier"}));
        let user = msg(json!({"v": 1, "role": "user", "content": "hi"}));
        let segs = [seg("skills", "K", true), seg("project", "P", true), seg("memories", "M", false)];
        let layout = Layout { system: "S", segments: &segs, volatile: "todos", cache: true, provider: "anthropic", append_only: false };
        let p = build(&layout, vec![summary, user, ai_calling(&["a"]), tool("a")]);
        let sys: Vec<_> = p.system.iter().map(|b| (b.text.as_str(), b.breakpoint)).collect();
        assert_eq!(sys, vec![("S", true), ("K", false), ("P", false), ("earlier", true)]);
        assert_eq!(p.history_breakpoint, Some(2));
        let tail = p.messages.last().unwrap();
        assert_eq!(tail.text(), format!("{TURN_CONTEXT_OPEN}M\n\ntodos{TURN_CONTEXT_CLOSE}"));
        // OpenRouter can't mark a tool message, so the breakpoint falls back to the user's.
        let layout = Layout { provider: "openrouter", ..layout };
        let user = msg(json!({"v": 1, "role": "user", "content": "hi"}));
        let p = build(&layout, vec![user, ai_calling(&["a"]), tool("a")]);
        assert_eq!(p.history_breakpoint, Some(0));
    }

    fn claude(content: Value) -> Message {
        msg(json!({"v": 1, "role": "assistant", "content": content,
                   "model": {"provider": "anthropic", "name": "claude-opus-5-5"}}))
    }

    #[test]
    fn a_model_that_preserves_thinking_gets_claudes_own_back() {
        let signed = claude(json!([{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "t"}]));
        let unsigned = claude(json!([{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "t"}]));
        let other = msg(json!({"v": 1, "role": "assistant", "content": [
            {"type": "thinking", "thinking": "g", "signature": "s"}, {"type": "text", "text": "t"}],
            "model": {"provider": "google_genai", "name": "gemini"}}));
        let out = strip_thinking_for("anthropic:claude-opus-5-5", vec![signed.clone(), unsigned, other]);
        assert_eq!(out[0], signed);
        let text_only = json!([{"type": "text", "text": "t"}]);
        assert_eq!(serde_json::to_value(&out[1].content).unwrap(), text_only);
        assert_eq!(serde_json::to_value(&out[2].content).unwrap(), text_only);
        // Every other model is sent none, Claude's included.
        let out = strip_thinking_for("anthropic:claude-opus-4-7", vec![signed]);
        assert_eq!(serde_json::to_value(&out[0].content).unwrap(), text_only);
    }

    #[test]
    fn the_turn_context_is_stored_once_per_change() {
        let mut history = vec![msg(json!({"v": 1, "role": "user", "content": "go"}))];
        assert_eq!(carry_turn_context(&mut history, Some("A".into())), Some(0));
        assert_eq!(carry_turn_context(&mut history, Some("A".into())), None);
        assert_eq!(carry_turn_context(&mut history, None), None);
        history.push(ai_calling(&["a"]));
        // Never after an assistant message.
        assert_eq!(carry_turn_context(&mut history, Some("B".into())), None);
        history.push(tool("a"));
        assert_eq!(carry_turn_context(&mut history, Some("B".into())), Some(2));
        assert_eq!(history[2].extras[TURN_CONTEXT_KEY], "B");
        assert_eq!(history[0].extras[TURN_CONTEXT_KEY], "A");
    }

    #[test]
    fn append_only_sends_each_context_where_it_was_stored() {
        let layout = Layout { system: "S", segments: &[], volatile: "", cache: true, provider: "anthropic", append_only: true };
        let mut user = msg(json!({"v": 1, "role": "user", "content": "go"}));
        user.extras.insert(TURN_CONTEXT_KEY.into(), "<ctx A>".into());
        let mut result = tool("a");
        result.extras.insert(TURN_CONTEXT_KEY.into(), "<ctx B>".into());
        let p = build(&layout, vec![user, ai_calling(&["a"]), result]);
        let said: Vec<_> = p.messages.iter().map(|m| (m.role, m.text())).collect();
        assert_eq!(said, vec![
            (Role::User, "go".into()),
            (Role::User, "<ctx A>".into()),
            (Role::Assistant, String::new()),
            (Role::Tool, "r".into()),
            (Role::User, "<ctx B>".into()),
        ]);
        // Nothing is rebuilt at the end from the volatile tail.
        let layout = Layout { volatile: "todos", ..layout };
        assert_eq!(build(&layout, vec![msg(json!({"v": 1, "role": "user", "content": "go"}))]).messages.len(), 1);
    }

    #[test]
    fn markable_messages() {
        let m = |v| markable(&msg(v), "anthropic");
        assert!(!m(json!({"v": 1, "role": "user", "content": "  "})));
        assert!(!m(json!({"v": 1, "role": "user", "content": []})));
        assert!(!m(json!({"v": 1, "role": "user", "content": [{"type": "image", "blob": "sha256:a"}]})));
        assert!(markable(&msg(json!({"v": 1, "role": "user", "content": [{"type": "image", "blob": "sha256:a"}]})), "bedrock"));
        assert!(m(json!({"v": 1, "role": "user", "content": [{"type": "opaque", "data": {"type": "tool_result"}}]})));
        assert!(!m(json!({"v": 1, "role": "assistant", "content": "hi"})));
    }
}
