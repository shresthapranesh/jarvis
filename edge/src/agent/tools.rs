//! The tools the main agent is bound to, and the ones the edge runs itself.
//!
//! The schemas are Python's own (`tools.json`, exported from
//! `convert_to_openai_tool` and diffed against it by the tests), so the model
//! sees the same tool list whichever runtime calls it — and a cached prefix
//! stays byte-stable when a conversation moves between them.
//!
//! The bound tools are `run_cell`, `write_artifact`, the todo tools,
//! `remember`, `spawn_workers` (`workers.rs`, whose roles' tools are in
//! `tools.json` too), `run_workflow` (`workflow/`), the board tools on a
//! board run, and the `always` MCP servers' tools (whose schemas are
//! converted as `convert_to_openai_tool` converts them, `mcp::llm_tool`). A
//! call a human must approve waits for them (`Step::Gated`). A call naming no
//! bound tool, or with arguments its signature won't take (`Args`), is
//! answered with an error the model can fix (`Step::Refused`).

use std::collections::HashMap;
use std::sync::LazyLock;
use std::time::Duration;

use serde_json::{Map, Value, json};
use sqlx::SqlitePool;

use crate::llm::Tool;
use crate::llm::transcript::ToolCall;
use crate::pyjson;

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
    let names: Vec<&str> = ["run_cell", "write_artifact", "write_todos", "set_todo_status", "spawn_workers", "run_workflow"]
        .iter()
        .chain(board_tools)
        .chain(&["remember"])
        .copied()
        .collect();
    bound_named(policy, &names, Some(mcp))
}

/// `_allowed(tools)`: the named tools, in order, then the `always` MCP
/// servers' (when given), without the switched-off ones.
pub fn bound_named(policy: &Policy, names: &[&str], mcp: Option<&crate::mcp::Snapshot>) -> Toolset {
    let mut set = Toolset {
        schemas: names.iter().copied().filter(|n| policy.enabled(n)).map(schema).collect(),
        mcp: HashMap::new(),
    };
    for (server, tool) in mcp.map(crate::mcp::Snapshot::bound).unwrap_or_default() {
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

impl Toolset {
    /// The server a bound MCP tool is called on.
    pub fn mcp_server(&self, name: &str) -> Option<&str> {
        self.mcp.get(name).map(String::as_str)
    }
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
    SpawnWorkers { tasks: Vec<super::workers::Task> },
    /// A saved workflow, run to its outputs (`workflow/mod.rs`).
    RunWorkflow { workflow_id: String, inputs_json: Option<String> },
    /// A bound MCP server's tool; the server checks the arguments.
    Mcp { server: String, tool: String, args: Value },
}

#[derive(Debug, PartialEq)]
pub enum Step {
    Run(Native),
    /// A call its policy says a human approves first (`bound:<name>`).
    Gated(Native),
    /// A gated call a human said no to (or let time out): its answer.
    Denied(String),
    /// Answered with an error without running: ToolNode's for a call naming
    /// no bound tool, `invoke_tool`'s for arguments that don't fit.
    Refused(String),
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

/// What each call of a batch comes to.
pub fn plan(calls: &[ToolCall], bound: &Toolset, policy: &Policy) -> Vec<Step> {
    calls
        .iter()
        .map(|call| {
            if !bound.schemas.iter().any(|t| t.name == call.name) {
                return Step::Refused(unknown_tool(&call.name, &bound.schemas));
            }
            let found = match bound.mcp.get(&call.name) {
                // The server checks the rest.
                Some(server) if call.args.is_object() => {
                    Ok(Native::Mcp { server: server.clone(), tool: call.name.clone(), args: call.args.clone() })
                }
                Some(_) => Err(bad_args(&call.name, &call.args, &["Input should be a valid dictionary".into()])),
                None => native(&call.name, &call.args),
            };
            match found {
                Ok(n) if policy.needs_approval(&call.name) => Step::Gated(n),
                Ok(n) => Step::Run(n),
                Err(error) => Step::Refused(error),
            }
        })
        .collect()
}

/// The call as its tool's signature takes it, or the error the model gets.
fn native(name: &str, args: &Value) -> Result<Native, String> {
    let Value::Object(obj) = args else {
        return Err(bad_args(name, args, &["Input should be a valid dictionary".into()]));
    };
    let mut a = Args::new(obj);
    let call = match name {
        "run_cell" => Native::RunCell { code: a.str("code") },
        "write_todos" => Native::WriteTodos { todos: a.str_list("todos") },
        "set_todo_status" => {
            Native::SetTodoStatus { index: a.int("index"), status: a.choice("status", &["pending", "in_progress", "done"]) }
        }
        "remember" => Native::Remember { text: a.str("text"), kind: a.str_or("kind", "fact") },
        "write_artifact" => Native::WriteArtifact {
            title: a.str("title"),
            content: a.opt_str("content"),
            file_path: a.opt_str("file_path"),
            artifact_id: a.opt_str("artifact_id"),
        },
        "complete_task" => Native::CompleteTask { summary: a.str("summary"), metadata: a.opt_str("metadata") },
        "block_task" => Native::BlockTask { reason: a.str("reason"), needs_input: a.bool("needs_input", false) },
        "spawn_workers" => {
            let raw = a.value("tasks");
            let tasks = raw.and_then(super::workers::tasks);
            if raw.is_some() && tasks.is_none() {
                a.fail("tasks", "Input should be a list of objects, each with a string `task` and an optional string `role`");
            }
            Native::SpawnWorkers { tasks: tasks.unwrap_or_default() }
        }
        "run_workflow" => Native::RunWorkflow { workflow_id: a.str("workflow_id"), inputs_json: a.opt_str("inputs_json") },
        other => return Err(format!("Error: {other} has no handler.")),
    };
    a.finish(name, args, call)
}

/// `_BAD_ARGS`.
fn bad_args(tool: &str, args: &Value, errors: &[String]) -> String {
    format!(
        "Error invoking tool '{tool}' with kwargs {} with error:\n {}\n Please fix the error and try again.",
        pyjson::py_repr(args),
        errors.join("\n")
    )
}

/// A call's arguments read as its tool's signature takes them — Pydantic's
/// lax mode: extra keys ignored, an integer from a whole float or a numeric
/// string, a boolean from 0/1 or a word. What doesn't fit is collected, and
/// the model is told all of it at once (`finish`).
pub(super) struct Args<'v> {
    args: &'v Map<String, Value>,
    errors: Vec<String>,
}

impl<'v> Args<'v> {
    pub fn new(args: &'v Map<String, Value>) -> Self {
        Args { args, errors: vec![] }
    }

    /// The call, or `invoke_tool`'s answer naming everything that was wrong.
    pub fn finish<T>(self, tool: &str, raw: &Value, call: T) -> Result<T, String> {
        if self.errors.is_empty() { Ok(call) } else { Err(bad_args(tool, raw, &self.errors)) }
    }

    pub fn fail(&mut self, key: &str, msg: &str) {
        self.errors.push(format!("{key}: {msg}"));
    }

    pub fn str(&mut self, key: &str) -> String {
        match self.args.get(key) {
            Some(Value::String(s)) => s.clone(),
            None => {
                self.fail(key, "Field required");
                String::new()
            }
            Some(_) => {
                self.fail(key, "Input should be a valid string");
                String::new()
            }
        }
    }

    pub fn opt_str(&mut self, key: &str) -> Option<String> {
        match self.args.get(key) {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => {
                self.fail(key, "Input should be a valid string");
                None
            }
        }
    }

    pub fn opt_int(&mut self, key: &str) -> Option<i64> {
        match self.args.get(key) {
            None | Some(Value::Null) => None,
            Some(v) => self.as_int(key, v),
        }
    }

    pub fn as_int(&mut self, key: &str, v: &Value) -> Option<i64> {
        let n = match v {
            Value::Bool(b) => Some(i64::from(*b)),
            Value::Number(n) => match (n.as_i64(), n.as_f64()) {
                (Some(i), _) => Some(i),
                (None, Some(f)) if f.fract() == 0.0 && f.abs() < 9.2e18 => Some(f as i64),
                (None, Some(_)) => {
                    self.fail(key, "Input should be a valid integer, got a number with a fractional part");
                    return None;
                }
                _ => None,
            },
            Value::String(s) => match s.trim().parse::<i64>() {
                Ok(i) => Some(i),
                Err(_) => {
                    self.fail(key, "Input should be a valid integer, unable to parse string as an integer");
                    return None;
                }
            },
            _ => None,
        };
        if n.is_none() {
            self.fail(key, "Input should be a valid integer");
        }
        n
    }

    pub fn bool(&mut self, key: &str, default: bool) -> bool {
        const UNREADABLE: &str = "Input should be a valid boolean, unable to interpret input";
        let b = match self.args.get(key) {
            None => return default,
            Some(Value::Bool(b)) => Some(*b),
            Some(Value::Number(n)) => match n.as_f64() {
                Some(0.0) => Some(false),
                Some(1.0) => Some(true),
                _ => {
                    self.fail(key, UNREADABLE);
                    return default;
                }
            },
            Some(Value::String(s)) => match s.to_lowercase().as_str() {
                "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
                "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
                _ => {
                    self.fail(key, UNREADABLE);
                    return default;
                }
            },
            Some(_) => None,
        };
        b.unwrap_or_else(|| {
            self.fail(key, "Input should be a valid boolean");
            default
        })
    }

    pub fn int(&mut self, key: &str) -> i64 {
        match self.args.get(key) {
            None => {
                self.fail(key, "Field required");
                0
            }
            Some(v) => self.as_int(key, v).unwrap_or_default(),
        }
    }

    /// A string that must be one of `choices`.
    pub fn choice(&mut self, key: &str, choices: &[&str]) -> String {
        let value = self.str(key);
        if self.args.get(key).is_some_and(Value::is_string) && !choices.contains(&value.as_str()) {
            let quoted: Vec<String> = choices.iter().map(|c| format!("'{c}'")).collect();
            let (last, rest) = quoted.split_last().expect("choices");
            self.fail(key, &format!("Input should be {} or {last}", rest.join(", ")));
        }
        value
    }

    /// A string, `default` when absent.
    pub fn str_or(&mut self, key: &str, default: &str) -> String {
        if self.args.contains_key(key) { self.str(key) } else { default.to_string() }
    }

    /// A list of strings.
    pub fn str_list(&mut self, key: &str) -> Vec<String> {
        match self.args.get(key) {
            None => {
                self.fail(key, "Field required");
                vec![]
            }
            Some(Value::Array(items)) => {
                let mut out = vec![];
                for (i, item) in items.iter().enumerate() {
                    match item {
                        Value::String(s) => out.push(s.clone()),
                        _ => self.fail(&format!("{key}.{i}"), "Input should be a valid string"),
                    }
                }
                out
            }
            Some(_) => {
                self.fail(key, "Input should be a valid list");
                vec![]
            }
        }
    }

    /// The raw value of a required key.
    pub fn value(&mut self, key: &str) -> Option<&'v Value> {
        let v = self.args.get(key);
        if v.is_none() {
            self.fail(key, "Field required");
        }
        v
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
    fn every_call_is_planned_on_its_own() {
        let policy = Policy::default();
        let tools = bound_for(&policy, false, &no_mcp());
        let steps = plan(
            &[call("run_cell", json!({"code": "1"})), call("nope", json!({})), call("write_todos", json!({"todos": ["a"]}))],
            &tools,
            &policy,
        );
        assert_eq!(steps[0], Step::Run(Native::RunCell { code: "1".into() }));
        assert_eq!(
            steps[1],
            Step::Refused(
                "Error: nope is not a valid tool, try one of [run_cell, write_artifact, write_todos, set_todo_status, \
                 spawn_workers, run_workflow, remember]."
                    .into()
            )
        );
        assert_eq!(steps[2], Step::Run(Native::WriteTodos { todos: vec!["a".into()] }));
        assert_eq!(plan_one(call("spawn_workers", json!({"tasks": []})), &tools, &policy), Step::Run(Native::SpawnWorkers { tasks: vec![] }));
        assert_eq!(
            plan_one(call("run_workflow", json!({"workflow_id": "w"})), &tools, &policy),
            Step::Run(Native::RunWorkflow { workflow_id: "w".into(), inputs_json: None })
        );
        // Pydantic's lax reading: extra keys ignored, a numeric string an integer.
        assert_eq!(plan_one(call("run_cell", json!({"code": "1", "extra": true})), &tools, &policy), Step::Run(Native::RunCell { code: "1".into() }));
        assert_eq!(
            plan_one(call("set_todo_status", json!({"index": "2", "status": "done"})), &tools, &policy),
            Step::Run(Native::SetTodoStatus { index: 2, status: "done".into() })
        );
    }

    #[test]
    fn bad_arguments_are_answered_for_the_model_to_fix() {
        let policy = Policy::default();
        let tools = bound_for(&policy, false, &no_mcp());
        let refused = |c: ToolCall| match plan_one(c, &tools, &policy) {
            Step::Refused(error) => error,
            other => panic!("{other:?}"),
        };
        assert_eq!(
            refused(call("set_todo_status", json!({"index": "first", "status": "finished"}))),
            "Error invoking tool 'set_todo_status' with kwargs {'index': 'first', 'status': 'finished'} with error:\n \
             index: Input should be a valid integer, unable to parse string as an integer\n\
             status: Input should be 'pending', 'in_progress' or 'done'\n Please fix the error and try again."
        );
        assert_eq!(
            refused(call("run_cell", json!({}))),
            "Error invoking tool 'run_cell' with kwargs {} with error:\n code: Field required\n Please fix the error and try again."
        );
        assert!(refused(call("run_workflow", json!({"workflow_id": "w", "inputs_json": {"a": 1}}))).contains("inputs_json: Input should be a valid string"));
        assert!(refused(call("spawn_workers", json!({"tasks": [{"task": 1}]}))).contains("tasks: Input should be a list of objects"));
        assert!(refused(call("write_todos", json!({"todos": ["a", 2]}))).contains("todos.1: Input should be a valid string"));
        assert!(refused(call("run_cell", json!("print(1)"))).contains("Input should be a valid dictionary"));
    }

    fn plan_one(c: ToolCall, tools: &Toolset, policy: &Policy) -> Step {
        plan(&[c], tools, policy).pop().expect("one call, one step")
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
        assert_eq!(gated, Step::Gated(Native::RunCell { code: "1".into() }));
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
            Step::Gated(Native::Mcp { server: "gh".into(), tool: "issue".into(), args })
        );
        assert!(matches!(plan_one(call("issue", json!("x")), &tools, &policy), Step::Refused(_)));
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
