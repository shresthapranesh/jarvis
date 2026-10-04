//! Per-tool approval gates — a port of `core/tool_gate.py` and
//! `core/approval.py`; a change to either is made in both.
//!
//! The `approvals` row is the rendezvous: a gated call records one and polls
//! it until a human answers (`resolveApproval`, from the chat prompt or the
//! inbox) or it times out. Nothing about the wait lives in memory, so the
//! answer can come through either server.

use std::time::Duration;

use serde_json::{Map, Value, json};
use sqlx::SqlitePool;

use crate::gql::codec::{new_id, now_stored};
use crate::pyjson;

/// `GATE_SOURCE`.
pub const GATE_SOURCE: &str = "tool";

/// `GATE_POLL_SECONDS`.
const POLL: Duration = Duration::from_millis(1500);

/// `gate_timeout_seconds`: `JARVIS_TOOL_GATE_TIMEOUT` (at least 10 s), else
/// 30 minutes.
pub fn gate_timeout() -> Duration {
    let secs = std::env::var("JARVIS_TOOL_GATE_TIMEOUT")
        .ok()
        .filter(|v| !v.is_empty())
        .and_then(|v| v.trim().parse::<f64>().ok())
        .map_or(1800.0, |s| s.max(10.0));
    Duration::from_secs_f64(secs)
}

/// `_truncate`: the first `limit` characters, and "…" if there were more.
fn truncate(text: &str, limit: usize) -> String {
    match text.char_indices().nth(limit) {
        Some((i, _)) => format!("{}…", &text[..i]),
        None => text.to_string(),
    }
}

/// `safe_args`: every argument as `str(value)`, cut to 500 characters.
pub fn safe_args(args: &Value) -> Map<String, Value> {
    let mut out = Map::new();
    if let Value::Object(map) = args {
        for (key, value) in map {
            out.insert(key.clone(), Value::String(truncate(&pyjson::py_str(value), 500)));
        }
    }
    out
}

/// `json.dumps(display, indent=2)` of the string-valued arguments.
fn dumps_indent2(display: &Map<String, Value>) -> String {
    if display.is_empty() {
        return "{}".into();
    }
    let items: Vec<String> = display
        .iter()
        .map(|(k, v)| format!("  {}: {}", pyjson::dumps(&Value::String(k.clone())), pyjson::dumps(v)))
        .collect();
    format!("{{\n{}\n}}", items.join(",\n"))
}

/// `describe_call`: the question the inbox and the chat prompt show.
fn describe_call(tool: &str, display: &Map<String, Value>) -> String {
    let mut question = format!("Run `{tool}`?");
    if !display.is_empty() {
        question.push_str(&format!("\nArgs: {}", truncate(&dumps_indent2(display), 1000)));
    }
    question + "\n\nReply 'approve' to run it, or 'deny' to skip it."
}

/// A pending gate, and the `approval_request` event that announces it.
pub struct Request {
    pub id: String,
    pub event: Value,
}

/// `create_gate_request`: the pending row for one call. Never deduplicated:
/// two identical calls are two operations.
pub async fn create(
    pool: &SqlitePool,
    tool_key: &str,
    tool: &str,
    args: &Value,
    conversation_id: Option<&str>,
    task_id: Option<&str>,
) -> sqlx::Result<Request> {
    let display = safe_args(args);
    let args_json: String = pyjson::dumps(&Value::Object(display.clone())).chars().take(8000).collect();
    let (id, now) = (new_id(), now_stored());
    sqlx::query(
        "INSERT INTO approvals (id, source, kind, status, question, label, tool, args_json, task_id, parent_id, \
         action_payload, requested_at, updated_at) VALUES (?, ?, 'approval', 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    )
    .bind(&id)
    .bind(GATE_SOURCE)
    .bind(describe_call(tool, &display))
    .bind(format!("Tool: {tool}"))
    .bind(tool)
    .bind(&args_json)
    .bind(task_id)
    .bind(conversation_id)
    .bind(pyjson::dumps(&json!({"tool_key": tool_key})))
    .bind(&now)
    .bind(&now)
    .execute(pool)
    .await?;
    // `announce_request` reads the args back from the row, a cut-off one as {}.
    let shown = match serde_json::from_str::<Value>(&args_json) {
        Ok(v @ Value::Object(_)) => v,
        _ => json!({}),
    };
    let event = json!({
        "tool": tool,
        "reason": "This tool requires human approval (Settings \u{2192} Tools).",
        "args": shown,
        "approval_id": id,
        "deferred": false,
    });
    Ok(Request { id, event })
}

/// `announce_resolved`'s event.
pub fn resolved_event(tool: &str, approved: bool, answer: &str) -> Value {
    json!({"tool": tool, "approved": approved, "answer": answer})
}

/// How a wait ended.
pub enum Outcome {
    Answered { approved: bool, answer: String },
    /// Nobody answered in time; the row is now `expired` and the call
    /// counts as denied ("timed out").
    TimedOut,
}

/// `wait_for_gate`: poll the row until it leaves `pending`, or expire it.
pub async fn wait(pool: &SqlitePool, id: &str, timeout: Duration) -> sqlx::Result<Outcome> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            let stamp = now_stored();
            sqlx::query(
                "UPDATE approvals SET status = 'expired', answer = NULL, resolved_at = ?, updated_at = ?, \
                 result = 'No answer before the approval timed out \u{2014} the call was not run.' \
                 WHERE id = ? AND status = 'pending'",
            )
            .bind(&stamp)
            .bind(&stamp)
            .bind(id)
            .execute(pool)
            .await?;
            return Ok(Outcome::TimedOut);
        }
        tokio::time::sleep(POLL.min(deadline - now)).await;
        let row: Option<(String, Option<String>)> =
            sqlx::query_as("SELECT status, answer FROM approvals WHERE id = ?").bind(id).fetch_optional(pool).await?;
        match row {
            Some((status, _)) if status == "pending" => {}
            Some((status, answer)) => {
                return Ok(Outcome::Answered { approved: status == "approved", answer: answer.unwrap_or_default() });
            }
            None => return Ok(Outcome::Answered { approved: false, answer: String::new() }),
        }
    }
}

/// `denial_message`: phrased so the model works around it rather than retrying.
pub fn denial_message(tool: &str, answer: &str) -> String {
    let reason = match answer.to_lowercase().as_str() {
        "" | "no" | "deny" | "denied" => String::new(),
        _ => format!(" ({answer})"),
    };
    format!(
        "Denied by a human{reason}: `{tool}` was not run. Do not retry it \u{2014} continue without it, or say what you need and why."
    )
}

// ── reading an answer ───────────────────────────────────────────────────────

const AFFIRMATIVE: &[&str] = &[
    "yes", "y", "approve", "approved", "ok", "okay", "proceed", "confirm", "confirmed", "allow", "allowed", "go",
    "go ahead", "do it", "sure", "aye",
];

const NEGATIVE: &[&str] = &[
    "no", "n", "deny", "denied", "cancel", "abort", "stop", "reject", "rejected", "block", "nope", "don't", "dont",
];

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// `re.findall(r"\b\w+\b", text)`.
fn words(text: &str) -> Vec<&str> {
    text.split(|c: char| !is_word(c)).filter(|w| !w.is_empty()).collect()
}

/// `is_affirmative_answer`: yes, no, or None when it's neither — which a
/// gate reads as no.
pub fn is_affirmative(text: &str) -> Option<bool> {
    let norm = text.trim().to_lowercase();
    if norm.is_empty() {
        return None;
    }
    if AFFIRMATIVE.contains(&norm.as_str()) {
        return Some(true);
    }
    if NEGATIVE.contains(&norm.as_str()) {
        return Some(false);
    }
    let tokens = words(&norm);
    let phrase_in_text = |phrase: &str| -> bool {
        if phrase.chars().count() == 1 {
            false
        } else if phrase.contains(' ') {
            norm.contains(phrase)
        } else if tokens.contains(&phrase) {
            true
        } else if phrase.contains('\'') || phrase == "dont" {
            norm.contains(phrase) || norm.contains(&phrase.replace('\'', ""))
        } else {
            false
        }
    };
    // `\bbut\b.*\b(yes|approve|ok|proceed)\b`, `.` stopping at a newline.
    let but_then_yes = norm.lines().any(|line| {
        let w = words(line);
        w.iter()
            .position(|t| *t == "but")
            .is_some_and(|i| w[i + 1..].iter().any(|t| ["yes", "approve", "ok", "proceed"].contains(t)))
    });
    let by_length = |list: &[&'static str]| {
        let mut v: Vec<&'static str> = list.to_vec();
        v.sort_by_key(|p| std::cmp::Reverse(p.chars().count()));
        v
    };
    for neg in by_length(NEGATIVE) {
        if neg.chars().count() == 1 {
            continue;
        }
        if phrase_in_text(neg) {
            if but_then_yes {
                break;
            }
            return Some(false);
        }
    }
    for aff in by_length(AFFIRMATIVE) {
        if aff.chars().count() > 1 && phrase_in_text(aff) {
            return Some(true);
        }
    }
    if norm.starts_with("yes") || norm.starts_with("approve") || norm.starts_with("ok") {
        return Some(true);
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn answers_read_as_python_reads_them() {
        for (text, want) in [
            ("yes", Some(true)),
            (" Y ", Some(true)),
            ("n", Some(false)),
            ("go ahead and do it", Some(true)),
            ("I don't think so", Some(false)),
            ("no, but yes actually", Some(true)),
            ("known issue?", None),
            ("okay then", Some(true)),
            ("yesss", Some(true)),
            ("what does it do", None),
        ] {
            assert_eq!(is_affirmative(text), want, "{text}");
        }
    }

    #[test]
    fn the_question_shows_the_arguments() {
        let display = safe_args(&json!({"code": "rm -rf /", "n": [1, "a"]}));
        assert_eq!(
            describe_call("run_cell", &display),
            "Run `run_cell`?\nArgs: {\n  \"code\": \"rm -rf /\",\n  \"n\": \"[1, 'a']\"\n}\n\nReply 'approve' to run it, or 'deny' to skip it."
        );
        assert_eq!(denial_message("x", "Denied"), "Denied by a human: `x` was not run. Do not retry it \u{2014} continue without it, or say what you need and why.");
    }
}
