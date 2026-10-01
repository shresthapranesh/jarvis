//! Subscription event types and their coercion from raw run events —
//! `server/graphql/types/events.py`, `automation_events.py` and
//! `workflow_events.py`.
//!
//! A run's events arrive as raw `{"event": name, "data": <JSON text>}`
//! records (`runs.rs`). Which typed event a record becomes depends on the
//! subscription watching, not on the run — the same `done` is a `DoneEvent`
//! to `taskEvents` and an `AutomationDoneEvent` to `automationRunEvents` —
//! so each union has its own coercer, as in Python.
//!
//! The coercers keep Python's semantics where a producer could observe them:
//! `data.get(k, default)` (a key that's present but null is *not* defaulted),
//! `x or default` (truthiness), `str()`, `int()`, and `json.dumps` for the
//! fields that carry JSON text (`pyjson.rs`). A value no Python field could
//! have rendered — null in a non-null string, say — makes the whole event an
//! error, where Python would have errored that field.

use async_graphql::{Json, Result, SimpleObject, Union};
use serde_json::{Map, Value};

use crate::pyjson::{dumps, py_float, py_int, py_str, truthy};

// ── shared ───────────────────────────────────────────────────────────────────

#[derive(SimpleObject, Clone)]
pub struct TokenEvent {
    pub text: String,
    pub source: String,
}

#[derive(SimpleObject, Clone)]
pub struct ErrorEvent {
    pub error: String,
}

#[derive(SimpleObject, Clone)]
pub struct TodoItem {
    pub text: String,
    pub status: String,
}

// ── chat ─────────────────────────────────────────────────────────────────────

#[derive(SimpleObject, Clone)]
pub struct ThinkingTokenEvent {
    pub text: String,
    pub source: String,
}

#[derive(SimpleObject, Clone)]
pub struct StepEvent {
    pub node: String,
    pub source: String,
    pub subagent: Option<String>,
    pub data: String,
}

#[derive(SimpleObject, Clone)]
pub struct BrowserStepEvent {
    pub url: String,
    pub phase: String,
    pub source: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkerStartEvent {
    pub idx: i64,
    pub role: String,
    pub task: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkerStepEvent {
    pub idx: i64,
    pub role: String,
    pub node: String,
    pub data: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkerTokenEvent {
    pub idx: i64,
    pub text: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkerDoneEvent {
    pub idx: i64,
    pub role: String,
    pub task: String,
    pub status: String,
    pub result: String,
}

#[derive(SimpleObject, Clone)]
pub struct ArtifactEvent {
    pub artifact_id: String,
    pub title: String,
    pub action: String,
    pub kind: String,
    pub preview: Option<String>,
}

#[derive(SimpleObject, Clone)]
pub struct TodosUpdatedEvent {
    pub todos: Vec<TodoItem>,
    pub source: String,
}

#[derive(SimpleObject, Clone)]
pub struct QueuedMessageEvent {
    pub message_id: String,
    pub text: String,
    pub position: i64,
}

#[derive(SimpleObject, Clone)]
pub struct QueuedWithdrawnEvent {
    pub message_id: String,
}

#[derive(SimpleObject, Clone)]
pub struct QueuedConsumedEvent {
    pub message_ids: Vec<String>,
}

#[derive(SimpleObject, Clone)]
pub struct InterruptEvent {
    pub interrupt_id: String,
    pub question: String,
}

#[derive(SimpleObject, Clone)]
pub struct InterruptResolvedEvent {
    pub interrupt_id: String,
}

#[derive(SimpleObject, Clone)]
pub struct ApprovalRequestEvent {
    pub tool: String,
    pub reason: String,
    pub args: String,
    pub approval_id: Option<String>,
    pub deferred: bool,
}

#[derive(SimpleObject, Clone)]
pub struct ApprovalResolvedEvent {
    pub tool: String,
    pub approved: bool,
    pub answer: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowToolEvent {
    pub parent_run_id: String,
    pub child_event: String,
    pub data: String,
}

#[derive(SimpleObject, Clone)]
pub struct BudgetExceededEvent {
    pub reason: String,
    pub snapshot: String,
}

#[derive(SimpleObject, Clone)]
pub struct BudgetUpdateEvent {
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub total_tokens: i64,
    pub llm_calls: i64,
    pub tool_calls: i64,
    pub snapshot: String,
}

#[derive(SimpleObject, Clone)]
pub struct PerfUpdateEvent {
    pub ttft_ms: Option<f64>,
    pub llm_ms: Option<f64>,
    pub prefill_tps: Option<f64>,
    pub eval_tps: Option<f64>,
    pub llm_calls: i64,
    pub snapshot: String,
}

#[derive(SimpleObject, Clone)]
pub struct DoneEvent {
    pub message: String,
    pub conversation_id: String,
}

#[derive(SimpleObject, Clone)]
pub struct StoppedEvent {
    pub message: String,
    pub conversation_id: String,
}

// Variant names are the GraphQL member type names.
#[allow(clippy::enum_variant_names)]
#[derive(Union, Clone)]
pub enum ChatEvent {
    TokenEvent(TokenEvent),
    ThinkingTokenEvent(ThinkingTokenEvent),
    StepEvent(StepEvent),
    BrowserStepEvent(BrowserStepEvent),
    WorkerStartEvent(WorkerStartEvent),
    WorkerStepEvent(WorkerStepEvent),
    WorkerTokenEvent(WorkerTokenEvent),
    WorkerDoneEvent(WorkerDoneEvent),
    ArtifactEvent(ArtifactEvent),
    TodosUpdatedEvent(TodosUpdatedEvent),
    QueuedMessageEvent(QueuedMessageEvent),
    QueuedWithdrawnEvent(QueuedWithdrawnEvent),
    QueuedConsumedEvent(QueuedConsumedEvent),
    InterruptEvent(InterruptEvent),
    InterruptResolvedEvent(InterruptResolvedEvent),
    ApprovalRequestEvent(ApprovalRequestEvent),
    ApprovalResolvedEvent(ApprovalResolvedEvent),
    WorkflowToolEvent(WorkflowToolEvent),
    BudgetExceededEvent(BudgetExceededEvent),
    BudgetUpdateEvent(BudgetUpdateEvent),
    PerfUpdateEvent(PerfUpdateEvent),
    DoneEvent(DoneEvent),
    StoppedEvent(StoppedEvent),
    ErrorEvent(ErrorEvent),
}

// ── automation (and board runs) ──────────────────────────────────────────────

#[derive(SimpleObject, Clone)]
pub struct AutomationDoneEvent {
    pub output: Option<String>,
    pub run_id: String,
}

#[derive(SimpleObject, Clone)]
pub struct AutomationStoppedEvent {
    pub output: Option<String>,
    pub run_id: String,
}

// Variant names are the GraphQL member type names.
#[allow(clippy::enum_variant_names)]
#[derive(Union, Clone)]
pub enum AutomationEvent {
    TokenEvent(TokenEvent),
    AutomationDoneEvent(AutomationDoneEvent),
    AutomationStoppedEvent(AutomationStoppedEvent),
    ErrorEvent(ErrorEvent),
}

// ── workflow ─────────────────────────────────────────────────────────────────

#[derive(SimpleObject, Clone)]
pub struct WorkflowNodeStartEvent {
    pub node_id: String,
    pub node_type: String,
    pub label: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowNodeTokenEvent {
    pub node_id: String,
    pub text: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowNodeConditionEvent {
    pub node_id: String,
    pub verdict: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowNodeDoneEvent {
    pub node_id: String,
    pub output: Json<Value>,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowNodeErrorEvent {
    pub node_id: String,
    pub error: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowMapStartEvent {
    pub node_id: String,
    pub total: i64,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowMapItemDoneEvent {
    pub node_id: String,
    pub index: i64,
    pub result: Json<Value>,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowDoneEvent {
    pub outputs: Json<Value>,
    pub run_id: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowErrorEvent {
    pub error: String,
    pub run_id: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowStoppedEvent {
    pub run_id: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowApprovalRequestEvent {
    pub tool: String,
    pub reason: String,
    pub args: String,
    pub node_id: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowApprovalResolvedEvent {
    pub tool: String,
    pub approved: bool,
    pub answer: String,
    pub node_id: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowInterruptEvent {
    pub interrupt_id: String,
    pub question: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowInterruptResolvedEvent {
    pub interrupt_id: String,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowBudgetExceededEvent {
    pub reason: String,
    pub snapshot: Option<String>,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowBudgetUpdateEvent {
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub total_tokens: i64,
    pub llm_calls: i64,
    pub tool_calls: i64,
    pub snapshot: Option<String>,
}

#[derive(SimpleObject, Clone)]
pub struct WorkflowNodeRetryEvent {
    pub node_id: String,
    pub attempt: i64,
    pub max_retries: i64,
    pub error: String,
}

// Variant names are the GraphQL member type names.
#[allow(clippy::enum_variant_names)]
#[derive(Union, Clone)]
pub enum WorkflowEvent {
    WorkflowNodeStartEvent(WorkflowNodeStartEvent),
    WorkflowNodeTokenEvent(WorkflowNodeTokenEvent),
    WorkflowNodeConditionEvent(WorkflowNodeConditionEvent),
    WorkflowNodeDoneEvent(WorkflowNodeDoneEvent),
    WorkflowNodeErrorEvent(WorkflowNodeErrorEvent),
    WorkflowMapStartEvent(WorkflowMapStartEvent),
    WorkflowMapItemDoneEvent(WorkflowMapItemDoneEvent),
    WorkflowApprovalRequestEvent(WorkflowApprovalRequestEvent),
    WorkflowApprovalResolvedEvent(WorkflowApprovalResolvedEvent),
    WorkflowInterruptEvent(WorkflowInterruptEvent),
    WorkflowInterruptResolvedEvent(WorkflowInterruptResolvedEvent),
    WorkflowBudgetExceededEvent(WorkflowBudgetExceededEvent),
    WorkflowBudgetUpdateEvent(WorkflowBudgetUpdateEvent),
    WorkflowNodeRetryEvent(WorkflowNodeRetryEvent),
    WorkflowDoneEvent(WorkflowDoneEvent),
    WorkflowErrorEvent(WorkflowErrorEvent),
    WorkflowStoppedEvent(WorkflowStoppedEvent),
}

// ── reading a raw event the way Python did ───────────────────────────────────

/// A raw event's name and decoded payload: `raw.get("event") or
/// raw.get("type")`, and `data` decoded if it's JSON text. Undecodable text
/// is `{}`, as Python's `except JSONDecodeError` makes it.
fn split(raw: &Value) -> Result<(String, Data)> {
    let name = [raw.get("event"), raw.get("type")]
        .into_iter()
        .flatten()
        .find(|v| truthy(v))
        .map(py_str)
        .unwrap_or_default();
    let data = match raw.get("data") {
        Some(Value::String(text)) => match serde_json::from_str::<Value>(text) {
            Ok(Value::Object(map)) => map,
            // Valid JSON that isn't an object: Python's `.get` would raise.
            Ok(_) => return Err("event data is not an object".into()),
            Err(_) => Map::new(),
        },
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    };
    Ok((name, Data(data)))
}

struct Data(Map<String, Value>);

impl Data {
    /// `data.get(key, default)`, missing → default, present → as is.
    fn get(&self, key: &str) -> Option<&Value> {
        self.0.get(key)
    }

    /// `data.get(key, default)` into a non-null `str` field.
    fn s(&self, key: &str, default: &str) -> Result<String> {
        match self.get(key) {
            None => Ok(default.to_string()),
            Some(v) => graphql_string(v)?.ok_or_else(|| format!("{key}: null for a non-null field").into()),
        }
    }

    /// `data.get(key)` into a `str | None` field.
    fn opt_s(&self, key: &str) -> Result<Option<String>> {
        self.get(key).map_or(Ok(None), graphql_string)
    }

    /// `_safe_int(data.get(key, 0))` — never fails.
    fn safe_int(&self, key: &str) -> i64 {
        safe_int(self.get(key).unwrap_or(&Value::from(0)))
    }

    /// `int(data.get(key, 0))` — raises in Python on anything unconvertible.
    fn int(&self, key: &str) -> Result<i64> {
        let v = self.get(key).cloned().unwrap_or(Value::from(0));
        py_int(&v).ok_or_else(|| format!("{key}: not an integer").into())
    }

    /// `_safe_float(data.get(key))`.
    fn safe_float(&self, key: &str) -> Option<f64> {
        self.get(key).and_then(py_float)
    }

    /// `_as_str(data.get(key, default))`.
    fn as_str(&self, key: &str, default: Value) -> String {
        as_str(self.get(key).unwrap_or(&default))
    }

    fn truthy(&self, key: &str) -> bool {
        self.get(key).is_some_and(truthy)
    }

    /// `data.get(key) or <fallback>` for a JSON field.
    fn or_empty_object(&self, key: &str) -> Value {
        match self.get(key) {
            Some(v) if truthy(v) => v.clone(),
            _ => Value::Object(Map::new()),
        }
    }
}

/// graphql-core's `String` serialization of a Python value: strings as is,
/// bools as `true`/`false`, numbers via `str()`; null is None. Lists and
/// dicts it refuses.
fn graphql_string(v: &Value) -> Result<Option<String>> {
    match v {
        Value::Null => Ok(None),
        Value::String(s) => Ok(Some(s.clone())),
        Value::Bool(b) => Ok(Some(if *b { "true" } else { "false" }.into())),
        Value::Number(_) => Ok(Some(py_str(v))),
        other => Err(format!("String cannot represent value: {}", dumps(other)).into()),
    }
}

/// `_as_str`: None → "", a string as is, anything else `json.dumps`ed.
fn as_str(v: &Value) -> String {
    match v {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        other => dumps(other),
    }
}

/// `_safe_int`: `int(value)`, else `int(str(value).strip() or 0)`, else 0.
fn safe_int(v: &Value) -> i64 {
    py_int(v).or_else(|| py_str(v).trim().parse().ok()).unwrap_or(0)
}

// ── the coercers ─────────────────────────────────────────────────────────────

/// `coerce_chat_event`. `Ok(None)` for an event name chat doesn't render.
pub fn chat(raw: &Value) -> Result<Option<ChatEvent>> {
    let (name, d) = split(raw)?;
    Ok(Some(match name.as_str() {
        "token" => ChatEvent::TokenEvent(TokenEvent { text: d.s("text", "")?, source: d.s("source", "main")? }),
        "thinking_token" => {
            ChatEvent::ThinkingTokenEvent(ThinkingTokenEvent { text: d.s("text", "")?, source: d.s("source", "main")? })
        }
        "step" => ChatEvent::StepEvent(StepEvent {
            node: d.s("node", "")?,
            source: d.s("source", "")?,
            subagent: d.opt_s("subagent")?,
            data: d.as_str("data", Value::Null),
        }),
        "browser_step" => ChatEvent::BrowserStepEvent(BrowserStepEvent {
            url: d.as_str("url", Value::Null),
            phase: d.s("phase", "start")?,
            source: d.s("source", "")?,
        }),
        "worker_start" => ChatEvent::WorkerStartEvent(WorkerStartEvent {
            idx: d.safe_int("idx"),
            role: d.s("role", "")?,
            task: d.s("task", "")?,
        }),
        "worker_step" => ChatEvent::WorkerStepEvent(WorkerStepEvent {
            idx: d.safe_int("idx"),
            role: d.s("role", "")?,
            node: d.s("node", "")?,
            data: d.as_str("data", Value::Null),
        }),
        "worker_token" => ChatEvent::WorkerTokenEvent(WorkerTokenEvent { idx: d.safe_int("idx"), text: d.s("text", "")? }),
        "worker_done" => ChatEvent::WorkerDoneEvent(WorkerDoneEvent {
            idx: d.safe_int("idx"),
            role: d.s("role", "")?,
            task: d.s("task", "")?,
            status: d.s("status", "done")?,
            result: d.s("result", "")?,
        }),
        "artifact" => ChatEvent::ArtifactEvent(ArtifactEvent {
            artifact_id: d.s("id", "")?,
            title: d.s("title", "")?,
            action: d.s("action", "")?,
            kind: match d.get("kind") {
                Some(v) if truthy(v) => graphql_string(v)?.unwrap_or_default(),
                _ => "markdown".into(),
            },
            preview: d.opt_s("preview")?,
        }),
        "queued_message" => ChatEvent::QueuedMessageEvent(QueuedMessageEvent {
            message_id: d.s("message_id", "")?,
            text: d.s("text", "")?,
            position: d.safe_int("position"),
        }),
        "queued_withdrawn" => ChatEvent::QueuedWithdrawnEvent(QueuedWithdrawnEvent { message_id: d.s("message_id", "")? }),
        "queued_consumed" => {
            let ids = match d.get("message_ids") {
                Some(Value::Array(ids)) => ids.iter().map(py_str).collect(),
                _ => Vec::new(),
            };
            ChatEvent::QueuedConsumedEvent(QueuedConsumedEvent { message_ids: ids })
        }
        "todos_updated" => {
            let mut todos = Vec::new();
            if let Some(Value::Array(items)) = d.get("todos") {
                for t in items {
                    if let (Value::Object(t), true) = (t, t.get("text").is_some()) {
                        todos.push(TodoItem {
                            text: py_str(&t["text"]),
                            status: py_str(t.get("status").unwrap_or(&Value::from("pending"))),
                        });
                    }
                }
            }
            ChatEvent::TodosUpdatedEvent(TodosUpdatedEvent { todos, source: d.s("source", "")? })
        }
        "interrupt" => ChatEvent::InterruptEvent(InterruptEvent {
            interrupt_id: d.s("interrupt_id", "")?,
            question: d.s("question", "")?,
        }),
        "interrupt_resolved" => {
            ChatEvent::InterruptResolvedEvent(InterruptResolvedEvent { interrupt_id: d.s("interrupt_id", "")? })
        }
        "approval_request" => ChatEvent::ApprovalRequestEvent(ApprovalRequestEvent {
            tool: d.s("tool", "")?,
            reason: d.s("reason", "")?,
            args: d.as_str("args", Value::Object(Map::new())),
            approval_id: match d.get("approval_id") {
                Some(v) if truthy(v) => graphql_string(v)?,
                _ => None,
            },
            deferred: d.truthy("deferred"),
        }),
        "approval_resolved" => ChatEvent::ApprovalResolvedEvent(ApprovalResolvedEvent {
            tool: d.s("tool", "")?,
            approved: d.truthy("approved"),
            answer: d.s("answer", "")?,
        }),
        "workflow_event" => {
            let rest: Map<String, Value> = d
                .0
                .iter()
                .filter(|(k, _)| *k != "parent_run_id" && *k != "child_event")
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();
            ChatEvent::WorkflowToolEvent(WorkflowToolEvent {
                parent_run_id: d.s("parent_run_id", "")?,
                child_event: d.s("child_event", "")?,
                data: as_str(&Value::Object(rest)),
            })
        }
        "budget_exceeded" => {
            let snapshot = match d.get("snapshot") {
                Some(v) if truthy(v) => as_str(v),
                _ => as_str(&Value::Object(
                    d.0.iter().filter(|(k, _)| *k != "reason").map(|(k, v)| (k.clone(), v.clone())).collect(),
                )),
            };
            ChatEvent::BudgetExceededEvent(BudgetExceededEvent { reason: d.s("reason", "")?, snapshot })
        }
        "budget_update" => ChatEvent::BudgetUpdateEvent(BudgetUpdateEvent {
            input_tokens: d.safe_int("input_tokens"),
            output_tokens: d.safe_int("output_tokens"),
            total_tokens: d.safe_int("total_tokens"),
            llm_calls: d.safe_int("llm_calls"),
            tool_calls: d.safe_int("tool_calls"),
            snapshot: snapshot_or_all(&d),
        }),
        "perf_update" => ChatEvent::PerfUpdateEvent(PerfUpdateEvent {
            ttft_ms: d.safe_float("ttft_ms"),
            llm_ms: d.safe_float("llm_ms"),
            prefill_tps: d.safe_float("prefill_tps"),
            eval_tps: d.safe_float("eval_tps"),
            llm_calls: d.safe_int("llm_calls"),
            snapshot: snapshot_or_all(&d),
        }),
        "done" => ChatEvent::DoneEvent(DoneEvent {
            message: d.s("message", "")?,
            conversation_id: d.s("conversation_id", "")?,
        }),
        "stopped" => ChatEvent::StoppedEvent(StoppedEvent {
            message: d.s("message", "")?,
            conversation_id: d.s("conversation_id", "")?,
        }),
        "error" => ChatEvent::ErrorEvent(ErrorEvent { error: d.s("error", "")? }),
        _ => return Ok(None),
    }))
}

/// `_as_str(data.get("snapshot") or data)`.
fn snapshot_or_all(d: &Data) -> String {
    match d.get("snapshot") {
        Some(v) if truthy(v) => as_str(v),
        _ => as_str(&Value::Object(d.0.clone())),
    }
}

/// `coerce_automation_event` — also what board-task runs stream.
pub fn automation(raw: &Value) -> Result<Option<AutomationEvent>> {
    let (name, d) = split(raw)?;
    Ok(Some(match name.as_str() {
        "token" => AutomationEvent::TokenEvent(TokenEvent { text: d.s("text", "")?, source: d.s("source", "main")? }),
        "done" => AutomationEvent::AutomationDoneEvent(AutomationDoneEvent {
            output: d.opt_s("output")?,
            run_id: d.s("run_id", "")?,
        }),
        "stopped" => AutomationEvent::AutomationStoppedEvent(AutomationStoppedEvent {
            output: d.opt_s("output")?,
            run_id: d.s("run_id", "")?,
        }),
        "error" => AutomationEvent::ErrorEvent(ErrorEvent { error: d.s("error", "")? }),
        _ => return Ok(None),
    }))
}

/// `coerce_workflow_event`.
pub fn workflow(raw: &Value) -> Result<Option<WorkflowEvent>> {
    let (name, d) = split(raw)?;
    // `data.get("node_id") or data.get("tool", "")`
    let node_or_tool = |d: &Data| -> Result<String> {
        match d.get("node_id") {
            Some(v) if truthy(v) => Ok(graphql_string(v)?.unwrap_or_default()),
            _ => d.s("tool", ""),
        }
    };
    Ok(Some(match name.as_str() {
        "node_start" => WorkflowEvent::WorkflowNodeStartEvent(WorkflowNodeStartEvent {
            node_id: d.s("node_id", "")?,
            node_type: d.s("node_type", "")?,
            label: d.s("label", "")?,
        }),
        "node_token" => WorkflowEvent::WorkflowNodeTokenEvent(WorkflowNodeTokenEvent {
            node_id: d.s("node_id", "")?,
            text: d.s("text", "")?,
        }),
        "node_condition" => WorkflowEvent::WorkflowNodeConditionEvent(WorkflowNodeConditionEvent {
            node_id: d.s("node_id", "")?,
            verdict: py_str(d.get("verdict").unwrap_or(&Value::from(""))),
        }),
        "node_done" => WorkflowEvent::WorkflowNodeDoneEvent(WorkflowNodeDoneEvent {
            node_id: d.s("node_id", "")?,
            output: Json(d.or_empty_object("output")),
        }),
        "node_error" => WorkflowEvent::WorkflowNodeErrorEvent(WorkflowNodeErrorEvent {
            node_id: d.s("node_id", "")?,
            error: d.s("error", "")?,
        }),
        "map_start" => {
            WorkflowEvent::WorkflowMapStartEvent(WorkflowMapStartEvent { node_id: d.s("node_id", "")?, total: d.int("total")? })
        }
        "map_item_done" => WorkflowEvent::WorkflowMapItemDoneEvent(WorkflowMapItemDoneEvent {
            node_id: d.s("node_id", "")?,
            index: d.int("index")?,
            result: Json(d.or_empty_object("result")),
        }),
        "workflow_done" => WorkflowEvent::WorkflowDoneEvent(WorkflowDoneEvent {
            outputs: Json(d.or_empty_object("outputs")),
            run_id: d.s("run_id", "")?,
        }),
        "workflow_error" => WorkflowEvent::WorkflowErrorEvent(WorkflowErrorEvent {
            error: d.s("error", "")?,
            run_id: d.s("run_id", "")?,
        }),
        "approval_request" => WorkflowEvent::WorkflowApprovalRequestEvent(WorkflowApprovalRequestEvent {
            tool: d.s("tool", "")?,
            reason: d.s("reason", "")?,
            args: match d.get("args") {
                Some(Value::String(s)) => s.clone(),
                other => dumps(other.unwrap_or(&Value::Object(Map::new()))),
            },
            node_id: node_or_tool(&d)?,
        }),
        "approval_resolved" => WorkflowEvent::WorkflowApprovalResolvedEvent(WorkflowApprovalResolvedEvent {
            tool: d.s("tool", "")?,
            approved: d.truthy("approved"),
            answer: d.s("answer", "")?,
            node_id: node_or_tool(&d)?,
        }),
        "interrupt" => WorkflowEvent::WorkflowInterruptEvent(WorkflowInterruptEvent {
            interrupt_id: d.s("interrupt_id", "")?,
            question: d.s("question", "")?,
        }),
        "interrupt_resolved" => WorkflowEvent::WorkflowInterruptResolvedEvent(WorkflowInterruptResolvedEvent {
            interrupt_id: d.s("interrupt_id", "")?,
        }),
        "budget_exceeded" => WorkflowEvent::WorkflowBudgetExceededEvent(WorkflowBudgetExceededEvent {
            reason: d.s("reason", "")?,
            snapshot: Some(snapshot_str_or_dumps(&d)),
        }),
        "budget_update" => WorkflowEvent::WorkflowBudgetUpdateEvent(WorkflowBudgetUpdateEvent {
            input_tokens: d.int("input_tokens")?,
            output_tokens: d.int("output_tokens")?,
            total_tokens: d.int("total_tokens")?,
            llm_calls: d.int("llm_calls")?,
            tool_calls: d.int("tool_calls")?,
            snapshot: Some(snapshot_str_or_dumps(&d)),
        }),
        "node_retry" => WorkflowEvent::WorkflowNodeRetryEvent(WorkflowNodeRetryEvent {
            node_id: d.s("node_id", "")?,
            attempt: d.int("attempt")?,
            max_retries: d.int("max_retries")?,
            error: d.s("error", "")?,
        }),
        "workflow_stopped" => WorkflowEvent::WorkflowStoppedEvent(WorkflowStoppedEvent { run_id: d.s("run_id", "")? }),
        _ => return Ok(None),
    }))
}

/// `snapshot if isinstance(snapshot, str) else json.dumps(snapshot or {})`.
fn snapshot_str_or_dumps(d: &Data) -> String {
    match d.get("snapshot") {
        Some(Value::String(s)) => s.clone(),
        Some(v) if truthy(v) => dumps(v),
        _ => "{}".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn raw(event: &str, data: Value) -> Value {
        json!({"event": event, "data": crate::pyjson::dumps(&data)})
    }

    #[test]
    fn step_data_is_python_json_text() {
        let Some(ChatEvent::StepEvent(e)) =
            chat(&raw("step", json!({"node": "tools", "data": {"x": 1.0, "y": "é"}}))).unwrap()
        else {
            panic!("not a step")
        };
        assert_eq!(e.data, concat!(r#"{"x": 1.0, "y": ""#, "\\", r#"u00e9"}"#));
        assert_eq!(e.source, "");
    }

    #[test]
    fn present_null_is_not_defaulted() {
        assert!(chat(&raw("token", json!({"text": null}))).is_err());
        let Some(ChatEvent::TokenEvent(t)) = chat(&raw("token", json!({}))).unwrap() else { panic!() };
        assert_eq!((t.text.as_str(), t.source.as_str()), ("", "main"));
    }

    #[test]
    fn unknown_events_are_skipped() {
        assert!(chat(&raw("node_start", json!({}))).unwrap().is_none());
        assert!(workflow(&raw("token", json!({}))).unwrap().is_none());
    }

    #[test]
    fn verdict_is_python_str() {
        let Some(WorkflowEvent::WorkflowNodeConditionEvent(e)) =
            workflow(&raw("node_condition", json!({"verdict": true}))).unwrap()
        else {
            panic!()
        };
        assert_eq!(e.verdict, "True");
        assert_eq!(crate::pyjson::float_repr(1.0), "1.0");
    }
}
