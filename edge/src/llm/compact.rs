//! Per-call compaction — a port of `apply_per_call_compaction`
//! (`core/compaction.py`, `elide_stale_tool_results` in `core/messages.py`).
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
fn text(m: &Message) -> String {
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
}
