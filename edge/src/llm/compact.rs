//! Compaction — a port of `core/compaction.py`. Per-call compaction
//! (`apply_per_call_compaction`, with `elide_stale_tool_results` from
//! `core/messages.py`) is below; the pure half of summarizing compaction
//! (`maybe_compact`'s counting, grouping and eviction plan) follows it, and
//! `agent/summarize.rs` makes its calls.
//!
//! Old tool output is most of an agent loop's history and is rarely read
//! again once acted on, yet every call re-bills it. Nothing here is stored:
//! the thread keeps the full output, and each call derives the same view.
//!
//! Both boundaries move in steps rather than one turn at a time. Shortening a
//! message rewrites the cached prefix from that point on, so a boundary that
//! slid every call would miss the history cache on every call; a stepped one
//! keeps the prefix byte-identical between steps.
//!
//! Lengths count characters, as Python's `len(str)` does, not bytes.

use serde_json::Value;

use super::shape::carries_results;
use super::transcript::{Content, Media, Message, Part, Role, Typed};

/// Tool output older than this many assistant turns is clipped…
const KEEP_TURNS: usize = 4;
/// …in steps of this many turns…
const ELIDE_STEP: usize = 4;
/// …when it is longer than this, down to its first `ELIDE_HEAD` characters.
const ELIDE_MIN_CHARS: usize = 2500;
const ELIDE_HEAD: usize = 400;

/// The newest tool-call groups kept whole; older ones become one-line stubs,
/// in steps of `COLLAPSE_STEP` groups.
const KEEP_TOOL_GROUPS: usize = 4;
const COLLAPSE_STEP: usize = 4;
/// How much of each result a stub quotes, and of all of them together.
const STUB_RESULT_CHARS: usize = 300;
const STUB_CHARS: usize = 500;

/// What a model call sees of `messages`: stale tool output clipped, then old
/// tool-call groups collapsed.
pub fn per_call(messages: Vec<Message>) -> Vec<Message> {
    collapse_old_tool_groups(elide_stale_tool_results(messages))
}

/// The first `n` of the steps `stale` covers — a boundary that only moves
/// once `step` more have gone stale.
fn stepped(stale: usize, step: usize) -> usize {
    stale / step * step
}

fn head(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Clip a long string tool result older than the last `KEEP_TURNS` assistant
/// turns to its head and a note saying so. List content (images, structured
/// results) passes through.
fn elide_stale_tool_results(mut messages: Vec<Message>) -> Vec<Message> {
    let assistant: Vec<usize> =
        messages.iter().enumerate().filter(|(_, m)| m.role == Role::Assistant).map(|(i, _)| i).collect();
    let Some(stale) = assistant.len().checked_sub(KEEP_TURNS).filter(|&s| s > 0) else {
        return messages;
    };
    let cutoff = assistant[stepped(stale, ELIDE_STEP)];
    for m in &mut messages[..cutoff] {
        if m.role != Role::Tool {
            continue;
        }
        let Content::Text(text) = &m.content else { continue };
        let len = text.chars().count();
        if len <= ELIDE_MIN_CHARS {
            continue;
        }
        m.content = Content::Text(format!(
            "{}\n... [{} chars of stale tool output elided to save context — re-run the tool if you need it again]",
            head(text, ELIDE_HEAD),
            len - ELIDE_HEAD
        ));
    }
    messages
}

/// Where each tool-call group is: an assistant message with tool calls and
/// the results right after it, or results with no call before them.
/// Everything else is a group of one and never collapses.
fn tool_groups(messages: &[Message]) -> Vec<std::ops::Range<usize>> {
    let mut groups = vec![];
    let mut i = 0;
    while i < messages.len() {
        let m = &messages[i];
        let opens = (m.role == Role::Assistant && !m.tool_calls.is_empty()) || carries_results(m);
        let start = i;
        i += 1;
        if opens {
            while i < messages.len() && carries_results(&messages[i]) {
                i += 1;
            }
            groups.push(start..i);
        }
    }
    groups
}

/// Replace all but the newest `KEEP_TOOL_GROUPS` tool-call groups (rounded to
/// `COLLAPSE_STEP`) with one assistant message naming the tools and quoting
/// the start of each result.
fn collapse_old_tool_groups(messages: Vec<Message>) -> Vec<Message> {
    let groups = tool_groups(&messages);
    let count = stepped(groups.len().saturating_sub(KEEP_TOOL_GROUPS), COLLAPSE_STEP);
    if count == 0 {
        return messages;
    }
    let stubs: Vec<_> = groups[..count].iter().map(|g| (g.clone(), stub(&messages[g.clone()]))).collect();
    let mut stubs = stubs.into_iter().peekable();
    let mut out = Vec::with_capacity(messages.len());
    for (i, m) in messages.into_iter().enumerate() {
        match stubs.peek() {
            Some((g, _)) if g.contains(&i) => {
                if i + 1 == g.end {
                    out.push(stubs.next().expect("peeked").1);
                }
            }
            _ => out.push(m),
        }
    }
    out
}

/// `_summarize_tool_group_brief`, as an assistant message under the group's
/// first id.
///
/// A result with no text is left unquoted. Python quotes the repr of its
/// content instead — `[{'type': 'image_url', 'image_url': {'url': 'data:…`
/// — which is noise the model pays for.
fn stub(group: &[Message]) -> Message {
    let mut names: Vec<&str> = vec![];
    let mut results: Vec<String> = vec![];
    for m in group {
        if m.role == Role::Assistant {
            names.extend(m.tool_calls.iter().map(|c| c.name.as_str()));
        } else {
            let name = m.name.as_deref().filter(|n| !n.is_empty()).unwrap_or("tool");
            names.push(name);
            let text = head(&text(m), STUB_RESULT_CHARS);
            if !text.is_empty() {
                results.push(format!("{name}: {text}"));
            }
        }
    }
    if names.is_empty() {
        names.push("tool");
    }
    let mut unique: Vec<&str> = vec![];
    for n in names {
        if !unique.contains(&n) {
            unique.push(n);
        }
    }
    let unique = unique.join(", ");
    let summary = if results.is_empty() { unique.clone() } else { head(&results.join("; "), STUB_CHARS) };
    let mut m = Message::new(Role::Assistant, Content::Text(format!("[Previous tool activity: {unique} => {summary}]")));
    m.id = group[0].id.clone();
    m
}

/// `message_text`: string content, or each part's `text` (else its
/// `thinking`) as LangChain spells the part — `extras` spread beside the
/// fields. A bare string in a list is not read: Python's loop only looks at
/// dicts.
pub fn text(m: &Message) -> String {
    let parts = match &m.content {
        Content::Text(s) => return s.clone(),
        Content::Parts(p) => p,
    };
    let field = |p: &'_ Part, key: &str| -> Option<String> {
        let found = match p {
            Part::Str(_) => None,
            Part::Typed(Typed::Text { text, .. }) if key == "text" => Some(text.as_str()),
            Part::Typed(Typed::Thinking { thinking, .. }) if key == "thinking" => Some(thinking.as_str()),
            Part::Typed(Typed::Opaque { data, .. }) => data.get(key).and_then(Value::as_str),
            Part::Typed(
                Typed::Text { extras, .. }
                | Typed::Thinking { extras, .. }
                | Typed::RedactedThinking { extras, .. }
                | Typed::Image(Media { extras, .. })
                | Typed::File(Media { extras, .. }),
            ) => extras.get(key).and_then(Value::as_str),
        };
        found.filter(|s| !s.is_empty()).map(str::to_string)
    };
    parts.iter().filter_map(|p| field(p, "text").or_else(|| field(p, "thinking"))).collect()
}

// ── summarizing compaction (`maybe_compact`'s pure half) ─────────────────────

/// `KEEP_RECENT_GROUPS`: the newest groups a summarizing pass keeps verbatim.
pub const KEEP_RECENT_GROUPS: usize = 2;
/// `USAGE_SANITY_FLOOR`: a usage-derived count under this share of the
/// heuristic isn't believed — an undercount would keep compaction from firing.
pub const USAGE_SANITY_FLOOR: f64 = 0.5;

/// `estimate_tokens_heuristic`: four characters a token over `message_text`.
pub fn heuristic(messages: &[Message]) -> i64 {
    (messages.iter().map(|m| text(m).chars().count()).sum::<usize>() / 4) as i64
}

/// `history_tokens_from_usage`: the history's size from the provider's own
/// count on the previous call — its input less `overhead` (the request's
/// non-history part), plus that reply's output and the heuristic over what
/// came after it. `None` when the newest assistant message carries no input
/// count.
pub fn history_tokens_from_usage(messages: &[Message], overhead: i64) -> Option<i64> {
    let i = messages.iter().rposition(|m| m.role == Role::Assistant)?;
    let usage = messages[i].usage.as_ref();
    let input = usage.and_then(|u| u.input).unwrap_or(0);
    if input <= 0 {
        return None;
    }
    let output = usage.and_then(|u| u.output).unwrap_or(0);
    Some((input - overhead).max(0) + output + heuristic(&messages[i + 1..]))
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Kind {
    Summary,
    System,
    User,
    ToolCall,
    AssistantText,
}

#[derive(Debug)]
struct Group {
    kind: Kind,
    range: std::ops::Range<usize>,
}

fn is_summary(m: &Message) -> bool {
    if m.role != Role::System {
        return false;
    }
    let t = text(m).to_lowercase();
    t.contains("[conversation summary") || t.contains("[prior conversation summary")
}

fn is_user(m: &Message) -> bool {
    m.role == Role::User && !carries_results(m)
}

/// `group_messages`: semantic turns — a summary, a system message, a user
/// message, an assistant call with its results (or a run of results), or
/// anything else on its own.
fn group_messages(messages: &[Message]) -> Vec<Group> {
    let mut groups = vec![];
    let mut i = 0;
    while i < messages.len() {
        let m = &messages[i];
        let start = i;
        i += 1;
        let kind = if is_summary(m) {
            Kind::Summary
        } else if m.role == Role::System {
            Kind::System
        } else if is_user(m) {
            Kind::User
        } else if (m.role == Role::Assistant && !m.tool_calls.is_empty()) || carries_results(m) {
            while i < messages.len() && carries_results(&messages[i]) {
                i += 1;
            }
            Kind::ToolCall
        } else {
            Kind::AssistantText
        };
        groups.push(Group { kind, range: start..i });
    }
    groups
}

/// What one summarizing pass takes out of a history and keeps.
#[derive(Debug, PartialEq)]
pub struct Eviction {
    /// The evicted stretch as the summarizer reads it.
    pub for_summary: Vec<Message>,
    /// The summaries already in the history, joined — merged with the new one.
    pub old_summary: Option<String>,
    /// The kept groups' messages, in order.
    pub kept: Vec<Message>,
    /// Every message to evict: the summarized ones and the old summaries.
    pub removed: Vec<String>,
    /// Just the summarized ones — what the episode covers.
    pub evicted: Vec<String>,
}

/// `maybe_compact` steps 2–3: the newest `KEEP_RECENT_GROUPS` groups and the
/// newest user message stay, the kept window starts at a user message when
/// it can, and everything else that isn't a summary is summarized. `None`
/// when there is nothing to summarize.
pub fn eviction(messages: &[Message]) -> Option<Eviction> {
    let groups = group_messages(messages);
    if groups.len() <= KEEP_RECENT_GROUPS {
        return None;
    }
    let old: Vec<&Group> = groups.iter().filter(|g| g.kind == Kind::Summary).collect();
    let rest: Vec<&Group> = groups.iter().filter(|g| g.kind != Kind::Summary).collect();
    if rest.len() <= KEEP_RECENT_GROUPS {
        return None;
    }
    // Indices into `rest`, which is in order.
    let mut kept: Vec<usize> = (rest.len() - KEEP_RECENT_GROUPS..rest.len()).collect();
    if let Some(u) = rest.iter().rposition(|g| g.kind == Kind::User) {
        kept.push(u);
    }
    kept.sort_unstable();
    kept.dedup();
    let first = rest[kept[0]];
    if first.kind != Kind::User {
        if let Some(u) = rest.iter().rposition(|g| g.range.end <= first.range.start && g.kind == Kind::User) {
            kept.push(u);
            kept.sort_unstable();
            kept.dedup();
        }
    }
    let summarized: Vec<&Group> = rest.iter().enumerate().filter(|(i, _)| !kept.contains(i)).map(|(_, g)| *g).collect();
    if summarized.is_empty() {
        return None;
    }
    let for_summary = for_summary(messages, &summarized);
    if for_summary.is_empty() {
        tracing::warn!("compact: nothing left to summarize after conversion — skip");
        return None;
    }
    let ids = |gs: &[&Group]| -> Vec<String> {
        gs.iter().flat_map(|g| messages[g.range.clone()].iter().filter_map(|m| m.id.clone())).collect()
    };
    let evicted = ids(&summarized);
    let mut removed = evicted.clone();
    removed.extend(ids(&old));
    let old_summary = (!old.is_empty())
        .then(|| old.iter().flat_map(|g| messages[g.range.clone()].iter().map(text)).collect::<Vec<_>>().join("\n\n"));
    Some(Eviction {
        for_summary,
        old_summary,
        kept: kept.iter().flat_map(|&i| messages[rest[i].range.clone()].iter().cloned()).collect(),
        removed,
        evicted,
    })
}

/// `_build_safe_messages_for_summary`: user messages as they are; assistant
/// messages as their text and the tools they called; results as user text
/// quoting their start; other system messages as they are.
///
/// A result with no text is quoted as empty. Python quotes the repr of its
/// content instead, as `stub` above notes.
fn for_summary(messages: &[Message], groups: &[&Group]) -> Vec<Message> {
    let mut out = vec![];
    for m in groups.iter().flat_map(|g| &messages[g.range.clone()]) {
        if is_user(m) {
            out.push(m.clone());
        } else if m.role == Role::Assistant {
            let mut said = text(m);
            if !m.tool_calls.is_empty() {
                let calls: Vec<String> = m
                    .tool_calls
                    .iter()
                    .map(|c| {
                        let args: Vec<String> =
                            c.args.as_object().map(|a| a.keys().map(|k| format!("{k}=...")).collect()).unwrap_or_default();
                        format!("{}({})", c.name, args.join(", "))
                    })
                    .collect();
                let calls = calls.join(", ");
                said = if said.is_empty() { format!("[Called tools: {calls}]") } else { format!("{said}\n[Called tools: {calls}]") };
            }
            if !said.is_empty() {
                out.push(Message::new(Role::Assistant, Content::Text(said)));
            }
        } else if carries_results(m) {
            // `getattr(m, "name", "tool")`: the attribute exists, so an unset
            // name reads as Python's `None`.
            let name = m.name.as_deref().unwrap_or("None");
            let quoted = head(&text(m), 500);
            out.push(Message::new(Role::User, Content::Text(format!("[Tool result from {name}]: {quoted}"))));
        } else if m.role == Role::System && !is_summary(m) {
            out.push(m.clone());
        }
    }
    out
}

/// The summarizer's instructions, as `maybe_compact` words them.
pub const DELTA_PROMPT: &str = "Summarize the following conversation history concisely. Preserve key facts, decisions, \
tool outputs, file changes, and unresolved items. Keep it under 600 words.";
pub const MERGE_PROMPT: &str = "You have an existing conversation summary and a new chunk summary. Merge them into a \
single coherent summary under 800 words. Preserve all durable facts, decisions, tool outputs, and goals. Do not \
invent details. Prioritize newer information if conflict.";

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::transcript::ToolCall;
    use serde_json::json;

    fn msg(v: serde_json::Value) -> Message {
        serde_json::from_value(v).unwrap()
    }

    fn call(id: &str, name: &str) -> Message {
        let mut m = Message::new(Role::Assistant, Content::Text(String::new()));
        m.id = Some(format!("a-{id}"));
        m.tool_calls = vec![ToolCall { id: Some(id.into()), name: name.into(), args: json!({}), signature: None }];
        m
    }

    fn result(id: &str, name: &str, content: &str) -> Message {
        msg(json!({"v": 1, "role": "tool", "name": name, "content": content, "tool_call_id": id, "status": "success"}))
    }

    /// `n` call/result pairs after one user message.
    fn rounds(n: usize, output: &str) -> Vec<Message> {
        let mut out = vec![msg(json!({"v": 1, "role": "user", "content": "go"}))];
        for i in 0..n {
            out.push(call(&format!("c{i}"), "run_cell"));
            out.push(result(&format!("c{i}"), "run_cell", output));
        }
        out
    }

    #[test]
    fn elision_moves_in_steps() {
        let long = "é".repeat(3000);
        // Four turns are kept; a fifth is stale but the boundary hasn't stepped.
        assert_eq!(elide_stale_tool_results(rounds(5, &long)), rounds(5, &long));
        // Three stale turns are still short of a step.
        assert_eq!(elide_stale_tool_results(rounds(7, &long)), rounds(7, &long));
        // Eight: the boundary jumps to the fifth assistant turn.
        let eight = elide_stale_tool_results(rounds(8, &long));
        let clipped: Vec<bool> = eight.iter().filter(|m| m.role == Role::Tool).map(|m| m.text() != long).collect();
        assert_eq!(clipped, [true, true, true, true, false, false, false, false]);
        let stub = eight[2].text();
        assert!(stub.starts_with(&"é".repeat(400)));
        assert!(stub.contains("\n... [2600 chars of stale tool output elided"));
        // Short output and list content pass through.
        assert_eq!(elide_stale_tool_results(rounds(8, "short")), rounds(8, "short"));
    }

    #[test]
    fn old_groups_collapse_to_stubs() {
        let history = rounds(8, "4");
        // Eight groups: the oldest four collapse, each to a stub under the call's id.
        let out = collapse_old_tool_groups(history.clone());
        assert_eq!(out.len(), 1 + 4 + 8);
        assert_eq!(out[1].id.as_deref(), Some("a-c0"));
        assert_eq!(out[1].text(), "[Previous tool activity: run_cell => run_cell: 4]");
        assert_eq!(out[5..], history[9..]);
        // Seven: three stale, below a step.
        assert_eq!(collapse_old_tool_groups(rounds(7, "4")), rounds(7, "4"));
    }

    #[test]
    fn stubs_name_every_tool_once() {
        let mut ai = call("a", "run_cell");
        ai.tool_calls.push(ToolCall { id: Some("b".into()), name: "write_todos".into(), args: json!({}), signature: None });
        let image = msg(json!({"v": 1, "role": "tool", "content": [{"type": "image", "data": "AA=="}], "tool_call_id": "b"}));
        let s = stub(&[ai, result("a", "run_cell", &"x".repeat(400)), image]);
        assert_eq!(s.text(), format!("[Previous tool activity: run_cell, write_todos, tool => run_cell: {}]", "x".repeat(300)));
        // Results with no text name the tools instead.
        let s = stub(&[result("a", "", "")]);
        assert_eq!(s.text(), "[Previous tool activity: tool => tool]");
    }

    #[test]
    fn text_reads_parts_as_python_does() {
        let m = msg(json!({"v": 1, "role": "tool", "tool_call_id": "a", "content": [
            "bare", {"type": "text", "text": "t"}, {"type": "thinking", "thinking": "h"},
            {"type": "opaque", "data": {"type": "x", "text": "o"}}, {"type": "image", "data": "AA=="}]}));
        assert_eq!(text(&m), "tho");
    }

    // ── summarizing ─────────────────────────────────────────────────────────

    fn m(v: serde_json::Value) -> Message {
        let mut v = v;
        v["v"] = json!(1);
        msg(v)
    }

    fn said(e: &Eviction) -> Vec<(Role, String)> {
        e.for_summary.iter().map(|m| (m.role, text(m))).collect()
    }

    fn ids(ms: &[Message]) -> Vec<&str> {
        ms.iter().map(|m| m.id.as_deref().unwrap_or("-")).collect()
    }

    /// Expectations are Python's `maybe_compact` on the same messages.
    #[test]
    fn eviction_keeps_from_a_user_message() {
        let history = vec![
            m(json!({"role": "user", "id": "u1", "content": "first"})),
            m(json!({"role": "assistant", "id": "a1", "content": "",
                     "tool_calls": [{"id": "c1", "name": "run_cell", "args": {"code": "1", "x": 2}}]})),
            m(json!({"role": "tool", "id": "t1", "name": "run_cell", "content": "out", "tool_call_id": "c1"})),
            m(json!({"role": "assistant", "id": "a2", "content": "said"})),
            m(json!({"role": "user", "id": "u2", "content": "second"})),
        ];
        let e = eviction(&history).unwrap();
        assert_eq!(said(&e), vec![
            (Role::Assistant, "[Called tools: run_cell(code=..., x=...)]".into()),
            (Role::User, "[Tool result from run_cell]: out".into()),
        ]);
        assert_eq!(ids(&e.kept), ["u1", "a2", "u2"]);
        assert_eq!((e.removed.clone(), e.evicted.clone(), e.old_summary.clone()), (vec!["a1".into(), "t1".into()], vec!["a1".into(), "t1".into()], None));
    }

    #[test]
    fn eviction_merges_the_old_summary() {
        let history = vec![
            m(json!({"role": "user", "id": "u0", "content": "q0"})),
            m(json!({"role": "assistant", "id": "a0", "content": "a"})),
            m(json!({"role": "system", "id": "s0", "content": "[Conversation summary]\nold"})),
            m(json!({"role": "user", "id": "u1", "content": "q1"})),
            m(json!({"role": "assistant", "id": "a1", "content": "thinking aloud",
                     "tool_calls": [{"id": "c1", "name": "write_todos", "args": {"todos": ["x"]}}]})),
            m(json!({"role": "tool", "id": "t1", "content": "done", "tool_call_id": "c1"})),
            m(json!({"role": "assistant", "id": "a2", "content": "", "tool_calls": [{"id": "c2", "name": "run_cell", "args": {}}]})),
            m(json!({"role": "tool", "id": "t2", "name": "run_cell", "content": "r2", "tool_call_id": "c2"})),
        ];
        let e = eviction(&history).unwrap();
        assert_eq!(said(&e), vec![(Role::User, "q0".into()), (Role::Assistant, "a".into())]);
        assert_eq!(e.old_summary.as_deref(), Some("[Conversation summary]\nold"));
        assert_eq!(ids(&e.kept), ["u1", "a1", "t1", "a2", "t2"]);
        assert_eq!(e.removed, ["u0", "a0", "s0"]);
        assert_eq!(e.evicted, ["u0", "a0"]);
    }

    #[test]
    fn nothing_to_evict_in_a_short_history() {
        let history = vec![
            m(json!({"role": "user", "id": "u1", "content": "q"})),
            m(json!({"role": "assistant", "id": "a1", "content": "a"})),
            m(json!({"role": "user", "id": "u2", "content": "q2"})),
        ];
        assert_eq!(eviction(&history), None);
    }

    #[test]
    fn a_result_without_a_name_is_from_none() {
        let history = vec![
            m(json!({"role": "assistant", "id": "a1", "content": "", "tool_calls": [{"id": "c1", "name": "x", "args": {}}]})),
            m(json!({"role": "tool", "id": "t1", "content": "r", "tool_call_id": "c1"})),
            m(json!({"role": "user", "id": "u1", "content": "q"})),
            m(json!({"role": "assistant", "id": "a2", "content": "a"})),
        ];
        let e = eviction(&history).unwrap();
        assert_eq!(said(&e)[1].1, "[Tool result from None]: r");
    }

    #[test]
    fn history_from_usage() {
        let mut history = rounds(1, "abcdefgh");
        assert_eq!(history_tokens_from_usage(&history, 10), None);
        history[1].usage = Some(serde_json::from_value(json!({"input": 1000, "output": 30})).unwrap());
        // 1000 less 10 overhead, the 30 it wrote, and "abcdefgh" since.
        assert_eq!(history_tokens_from_usage(&history, 10), Some(990 + 30 + 2));
        assert_eq!(history_tokens_from_usage(&history, 5000), Some(32));
        history[1].usage = Some(serde_json::from_value(json!({"output": 30})).unwrap());
        assert_eq!(history_tokens_from_usage(&history, 10), None);
    }
}
