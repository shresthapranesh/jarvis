//! The tools the main agent is bound to, and the ones the edge runs itself.
//!
//! The schemas are Python's own (`tools.json`, exported from
//! `convert_to_openai_tool` and diffed against it by the tests), so the model
//! sees the same tool list whichever runtime calls it — and a cached prefix
//! stays byte-stable when a conversation moves between them.
//!
//! The edge runs `run_cell`, `write_artifact`, the todo tools, `remember`
//! and the `always` MCP servers' tools (whose schemas are converted as
//! `convert_to_openai_tool` converts them, `mcp::llm_tool`), a call a human
//! must approve once they have (`Step::Gated`). Any other call — workers, a
//! workflow — or a call whose arguments aren't plainly valid, is Python's: the
//! batch is handed over (`Plan::Python`) and Python runs it, validating and
//! gating as it always has.

use std::collections::HashMap;
use std::sync::LazyLock;
use std::time::Duration;

use serde_json::{Value, json};
use sqlx::SqlitePool;

use crate::llm::Tool;
use crate::llm::transcript::ToolCall;

static SCHEMAS: LazyLock<Vec<Tool>> = LazyLock::new(|| {
    serde_json::from_str(include_str!("tools.json")).expect("tools.json is a list of tool schemas")
});

fn schema(name: &str) -> Tool {
    SCHEMAS.iter().find(|t| t.name == name).cloned().unwrap_or_else(|| panic!("tools.json has no {name}"))
}

/// The policy a human set for a bound tool (`tools.policy`); absent entries
/// are enabled and ungated. A tool is keyed as `tool_key_for` keys it: an
/// MCP server's (`mcp:<server>/<name>`) if one lists that name, else
/// `bound:<name>`.
#[derive(Default)]
pub struct Policy {
    entries: serde_json::Map<String, Value>,
    /// `_mcp_owner_map`.
    owners: HashMap<String, String>,
}

impl Policy {
    pub async fn load(pool: &SqlitePool, mcp: &crate::mcp::Snapshot) -> Self {
        let raw = crate::catalog::setting(pool, "tools.policy").await.ok().flatten();
        let entries = match raw.as_deref().map(serde_json::from_str::<Value>) {
            Some(Ok(Value::Object(map))) => map,
            _ => Default::default(),
        };
        Policy { entries, owners: mcp.owners() }
    }

    /// `tool_key_for`.
    pub fn key_for(&self, name: &str) -> String {
        match self.owners.get(name) {
            Some(server) => format!("mcp:{server}/{name}"),
            None => format!("bound:{name}"),
        }
    }

    fn entry(&self, name: &str, field: &str, default: bool) -> bool {
        match self.entries.get(&self.key_for(name)) {
            Some(Value::Object(e)) => e.get(field).map_or(default, crate::pyjson::truthy),
            _ => default,
        }
    }

    pub fn enabled(&self, name: &str) -> bool {
        self.entry(name, "enabled", true)
    }

    pub fn needs_approval(&self, name: &str) -> bool {
        self.entry(name, "approval", false)
    }
}

/// What a turn is bound to: the schemas the model is sent, and which of
/// them are MCP tools (by name, the last one bound winning, as
/// `tools_by_name` has it).
#[derive(Default)]
pub struct Toolset {
    pub schemas: Vec<Tool>,
    mcp: HashMap<String, String>,
}

/// The main agent's tools in `_build_agent`'s order, without the ones a
/// human switched off. A board run (`board=True`) also gets `complete_task`
/// and `block_task`, before `remember`; with an embedder — which Python
/// always has — `remember` is bound too; then the `always` MCP servers'.
pub fn bound_for(policy: &Policy, board: bool, mcp: &crate::mcp::Snapshot) -> Toolset {
    let board_tools: &[&str] = if board { &["complete_task", "block_task"] } else { &[] };
    let mut set = Toolset {
        schemas: ["run_cell", "write_artifact", "write_todos", "set_todo_status", "spawn_workers", "run_workflow"]
            .iter()
            .chain(board_tools)
            .chain(&["remember"])
            .copied()
            .filter(|n| policy.enabled(n))
            .map(schema)
            .collect(),
        mcp: HashMap::new(),
    };
    for (server, tool) in mcp.bound() {
        if !policy.enabled(&tool.name) {
            continue;
        }
        match crate::mcp::llm_tool(tool) {
            Ok(t) => {
                set.schemas.push(t);
                set.mcp.insert(tool.name.clone(), server.to_string());
            }
            Err(e) => tracing::warn!("MCP tool {server}.{} has a schema that doesn't convert ({e}) — not binding it", tool.name),
        }
    }
    set
}

/// A call the edge runs itself, its arguments checked.
#[derive(Debug, PartialEq)]
pub enum Native {
    RunCell { code: String },
    WriteTodos { todos: Vec<String> },
    SetTodoStatus { index: i64, status: String },
    Remember { text: String, kind: String },
    WriteArtifact { title: String, content: Option<String>, file_path: Option<String>, artifact_id: Option<String> },
    CompleteTask { summary: String, metadata: Option<String> },
    BlockTask { reason: String, needs_input: bool },
    /// A bound MCP server's tool; the server checks the arguments.
    Mcp { server: String, tool: String, args: Value },
}

/// How a batch of calls will run.
#[derive(Debug, PartialEq)]
pub enum Plan {
    /// Every call is the edge's: unknown tools get ToolNode's error, the rest
    /// run here.
    Edge(Vec<Step>),
    /// Something in it is Python's; the batch goes over whole.
    Python(String),
}

#[derive(Debug, PartialEq)]
pub enum Step {
    Run(Native),
    /// A call its policy says a human approves first (`bound:<name>`).
    Gated(Native),
    /// A gated call a human said no to (or let time out): its answer.
    Denied(String),
    /// ToolNode's answer to a call naming no bound tool.
    Unknown(String),
}

/// `_UNKNOWN_TOOL`, naming each bound tool once.
pub fn unknown_tool(name: &str, bound: &[Tool]) -> String {
    let mut names: Vec<&str> = vec![];
    for t in bound {
        if !names.contains(&t.name.as_str()) {
            names.push(&t.name);
        }
    }
    format!("Error: {name} is not a valid tool, try one of [{}].", names.join(", "))
}

pub fn plan(calls: &[ToolCall], bound: &Toolset, policy: &Policy) -> Plan {
    let mut steps = vec![];
    for call in calls {
        if !bound.schemas.iter().any(|t| t.name == call.name) {
            steps.push(Step::Unknown(unknown_tool(&call.name, &bound.schemas)));
            continue;
        }
        let found = match bound.mcp.get(&call.name) {
            Some(server) => call.args.is_object().then(|| Native::Mcp {
                server: server.clone(),
                tool: call.name.clone(),
                args: call.args.clone(),
            }),
            None => native(&call.name, &call.args),
        };
        match found {
            Some(n) if policy.needs_approval(&call.name) => steps.push(Step::Gated(n)),
            Some(n) => steps.push(Step::Run(n)),
            None => return Plan::Python(format!("{} runs in Python", call.name)),
        }
    }
    Plan::Edge(steps)
}

/// The call as a native one, if its arguments are exactly what its schema
/// asks for. Anything Pydantic would coerce, default or reject is left to
/// Python, which says it the way the model has always been told.
fn native(name: &str, args: &Value) -> Option<Native> {
    let obj = args.as_object()?;
    let only = |keys: &[&str]| obj.keys().all(|k| keys.contains(&k.as_str()));
    match name {
        "run_cell" if only(&["code"]) => Some(Native::RunCell { code: obj.get("code")?.as_str()?.to_string() }),
        "write_todos" if only(&["todos"]) => {
            let todos = obj.get("todos")?.as_array()?.iter().map(|t| t.as_str().map(str::to_string)).collect::<Option<_>>()?;
            Some(Native::WriteTodos { todos })
        }
        "set_todo_status" if only(&["index", "status"]) => {
            let index = obj.get("index")?.as_i64()?;
            let status = obj.get("status")?.as_str()?;
            ["pending", "in_progress", "done"].contains(&status).then(|| Native::SetTodoStatus { index, status: status.into() })
        }
        "remember" if only(&["text", "kind"]) => {
            let text = obj.get("text")?.as_str()?.to_string();
            let kind = match obj.get("kind") {
                None => "fact".to_string(),
                Some(k) => k.as_str()?.to_string(),
            };
            Some(Native::Remember { text, kind })
        }
        "write_artifact" if only(&["title", "content", "file_path", "artifact_id"]) => {
            // Each optional one a string or null; anything else is Pydantic's to word.
            let opt = |key: &str| match obj.get(key) {
                None | Some(Value::Null) => Some(None),
                Some(Value::String(s)) => Some(Some(s.clone())),
                Some(_) => None,
            };
            Some(Native::WriteArtifact {
                title: obj.get("title")?.as_str()?.to_string(),
                content: opt("content")?,
                file_path: opt("file_path")?,
                artifact_id: opt("artifact_id")?,
            })
        }
        "complete_task" if only(&["summary", "metadata"]) => {
            let summary = obj.get("summary")?.as_str()?.to_string();
            let metadata = match obj.get("metadata") {
                None | Some(Value::Null) => None,
                // Not JSON at all: Python words that error from its parser.
                Some(m) => Some(m.as_str().filter(|m| serde_json::from_str::<Value>(m).is_ok())?.to_string()),
            };
            Some(Native::CompleteTask { summary, metadata })
        }
        "block_task" if only(&["reason", "needs_input"]) => {
            let reason = obj.get("reason")?.as_str()?.to_string();
            let needs_input = match obj.get("needs_input") {
                None => false,
                Some(b) => b.as_bool()?,
            };
            Some(Native::BlockTask { reason, needs_input })
        }
        _ => None,
    }
}

/// `_normalise_todos`: `[{text, status}]`, legacy strings and odd statuses
/// made pending.
pub fn normalise_todos(raw: &[Value]) -> Vec<Value> {
    raw.iter()
        .filter_map(|item| match item {
            Value::String(s) => Some(json!({"text": s, "status": "pending"})),
            Value::Object(o) if o.contains_key("text") => {
                let status = o.get("status").and_then(Value::as_str).filter(|s| ["pending", "in_progress", "done"].contains(s));
                Some(json!({"text": crate::pyjson::py_str(&o["text"]), "status": status.unwrap_or("pending")}))
            }
            _ => None,
        })
        .collect()
}

/// `reduce_todos`: a list of the same length merges index by index, keeping
/// the more advanced status; anything else replaces the list.
pub fn reduce_todos(current: &[Value], update: &[Value]) -> Vec<Value> {
    let (cur, upd) = (normalise_todos(current), normalise_todos(update));
    if cur.is_empty() || upd.is_empty() || cur.len() != upd.len() {
        return upd;
    }
    let rank = |t: &Value| match t["status"].as_str() {
        Some("in_progress") => 1,
        Some("done") => 2,
        _ => 0,
    };
    cur.into_iter().zip(upd).map(|(c, u)| if rank(&u) >= rank(&c) { u } else { c }).collect()
}

/// `write_todos`' answer.
pub fn todos_written(n: usize) -> String {
    format!("Updated todo list ({n} item{}).", if n == 1 { "" } else { "s" })
}

/// `set_todo_status`: the new list, or the error the model gets.
pub fn set_status(todos: &[Value], index: i64, status: &str) -> Result<(Vec<Value>, String), String> {
    let mut todos = normalise_todos(todos);
    let Some(slot) = usize::try_from(index).ok().filter(|&i| i < todos.len()) else {
        return Err(format!("Error: index {index} out of range (have {} todos).", todos.len()));
    };
    todos[slot] = json!({"text": todos[slot]["text"], "status": status});
    Ok((todos, format!("Set todo {index} to {}.", py_repr_str(status))))
}

/// `repr()` of a status: the values are plain words, so single-quoted.
fn py_repr_str(s: &str) -> String {
    format!("'{s}'")
}

/// `DEFAULT_CELL_TIMEOUT`.
pub const CELL_TIMEOUT: Duration = Duration::from_secs(60);

#[cfg(test)]
mod tests {
    use super::*;

    fn call(name: &str, args: Value) -> ToolCall {
        ToolCall { id: Some("c".into()), name: name.into(), args, signature: None }
    }

    fn no_mcp() -> crate::mcp::Snapshot {
        crate::mcp::Snapshot { default_mode: crate::mcp::config::ALWAYS, ..Default::default() }
    }

    #[test]
    fn a_batch_is_the_edges_only_if_every_call_is() {
        let policy = Policy::default();
        let tools = bound_for(&policy, false, &no_mcp());
        let plan = plan(
            &[call("run_cell", json!({"code": "1"})), call("nope", json!({})), call("write_todos", json!({"todos": ["a"]}))],
            &tools,
            &policy,
        );
        let Plan::Edge(steps) = plan else { panic!("{plan:?}") };
        assert_eq!(steps[0], Step::Run(Native::RunCell { code: "1".into() }));
        assert_eq!(
            steps[1],
            Step::Unknown(
                "Error: nope is not a valid tool, try one of [run_cell, write_artifact, write_todos, set_todo_status, \
                 spawn_workers, run_workflow, remember]."
                    .into()
            )
        );
        for python in [
            call("spawn_workers", json!({"tasks": []})),
            call("set_todo_status", json!({"index": "first", "status": "done"})),
            call("set_todo_status", json!({"index": 0, "status": "finished"})),
            call("run_cell", json!({"code": "1", "extra": true})),
        ] {
            assert!(matches!(plan_one(python, &tools, &policy), Plan::Python(_)));
        }
    }

    fn plan_one(c: ToolCall, tools: &Toolset, policy: &Policy) -> Plan {
        plan(&[c], tools, policy)
    }

    #[test]
    fn policy_unbinds_and_gates() {
        let policy = Policy {
            entries: serde_json::from_value(json!({
                "bound:remember": {"enabled": false},
                "bound:run_cell": {"approval": true},
            }))
            .unwrap(),
            ..Default::default()
        };
        let tools = bound_for(&policy, false, &no_mcp());
        assert!(!tools.schemas.iter().any(|t| t.name == "remember"));
        let gated = plan_one(call("run_cell", json!({"code": "1"})), &tools, &policy);
        assert_eq!(gated, Plan::Edge(vec![Step::Gated(Native::RunCell { code: "1".into() })]));
    }

    #[test]
    fn always_servers_are_bound_and_keyed_as_mcp() {
        let mut mcp = no_mcp();
        let tool = |name: &str| crate::mcp::Tool { name: name.into(), description: "d".into(), input_schema: json!({"type": "object"}) };
        let lazy: serde_json::Map<String, Value> = serde_json::from_value(json!({"x-jarvis-load": "lazy"})).unwrap();
        mcp.connections.insert("gh".into(), Default::default());
        mcp.connections.insert("off".into(), lazy);
        mcp.tools.insert("gh".into(), vec![tool("issue"), tool("pr")]);
        mcp.tools.insert("off".into(), vec![tool("hidden")]);
        let mut policy = Policy {
            entries: serde_json::from_value(json!({"mcp:gh/pr": {"enabled": false}, "mcp:gh/issue": {"approval": true}})).unwrap(),
            ..Default::default()
        };
        policy.owners = mcp.owners();
        let tools = bound_for(&policy, false, &mcp);
        let names: Vec<&str> = tools.schemas.iter().map(|t| t.name.as_str()).collect();
        assert_eq!(names.last(), Some(&"issue"));
        assert!(!names.contains(&"pr") && !names.contains(&"hidden"));
        assert_eq!(policy.key_for("issue"), "mcp:gh/issue");
        assert_eq!(policy.key_for("run_cell"), "bound:run_cell");
        let args = json!({"title": "t"});
        assert_eq!(
            plan_one(call("issue", args.clone()), &tools, &policy),
            Plan::Edge(vec![Step::Gated(Native::Mcp { server: "gh".into(), tool: "issue".into(), args })])
        );
        assert!(matches!(plan_one(call("issue", json!("x")), &tools, &policy), Plan::Python(_)));
    }

    #[test]
    fn todos_reduce_like_python() {
        let cur = vec![json!({"text": "a", "status": "done"}), json!("b")];
        let upd = vec![json!({"text": "a", "status": "pending"}), json!({"text": "b", "status": "in_progress"})];
        assert_eq!(
            reduce_todos(&cur, &upd),
            vec![json!({"text": "a", "status": "done"}), json!({"text": "b", "status": "in_progress"})]
        );
        assert_eq!(reduce_todos(&cur, &[]), Vec::<Value>::new());
        assert_eq!(set_status(&cur, 1, "done").unwrap().1, "Set todo 1 to 'done'.");
        assert_eq!(set_status(&cur, 2, "done").unwrap_err(), "Error: index 2 out of range (have 2 todos).");
        assert_eq!(todos_written(1), "Updated todo list (1 item).");
    }
}
