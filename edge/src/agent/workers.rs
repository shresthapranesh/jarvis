//! `spawn_workers` — a port of `tools/workers.py` and the worker roles of
//! `core/agents.py` (`_ROLE_PROMPTS`, `_ROLE_TOOLS`, `_make_role_factory`);
//! a change to either is made in both.
//!
//! Each task runs on a worker, all at once: the run's model, a role's prompt
//! and tools, a history of its own in memory, a kernel of its own. What a
//! worker does reaches the run as `Note`s — its `worker_*` events, which the
//! turn writes and announces in order as `core/streaming.py` does, and its
//! model and tool calls, which count against the run's budget and usage as
//! the parent's callbacks counted them in Python.
//!
//! A worker can't be handed to Python halfway, so it answers everything
//! itself: unknown tools, arguments Pydantic would reject (worded as
//! `invoke_tool` words them), gated calls.

use std::sync::Arc;

use serde_json::{Map, Value, json};
use tokio::sync::mpsc::UnboundedSender;

use super::tools::{Policy, Toolset};
use super::{Agent, artifacts, files};
use crate::llm::perf::CallPerf;
use crate::llm::shape::{self, Layout};
use crate::llm::transcript::{Content, Message, Part, Role, ToolCall, Typed};
use crate::llm::{self, Delta, Endpoints, Request, compact};
use crate::{pyjson, pystr};

const DEFAULT_ROLE: &str = "general";
/// A worker's `recursion_limit`: generous, but under the main agent's.
const RECURSION_LIMIT: u32 = 50;
/// `_TokenTail`'s `max_chars`.
const TAIL_CHARS: usize = 120;

/// `_ROLE_PROMPTS`.
const ROLES: [(&str, &str); 4] = [
    (
        "general",
        "You are a focused worker agent. Complete the task given to you using run_cell(code) — a stateful \
         Python/IPython session with full network/filesystem access, where variables and imports persist across \
         calls like notebook cells. Use read_file/write_file/list_files for filesystem access if needed. When you \
         have a complete answer, return it concisely as your final response.",
    ),
    (
        "researcher",
        "You are a research worker. Your job is to find and verify information. Work in run_cell(code): \
         search(query) returns [{title, url, snippet}] leads and read(url) returns a page's main text — never \
         conclude from snippets alone; read() the promising results. Use httpx for APIs and read_file when given \
         local source material. Cross-check claims that matter across independent sources and prefer primary ones. \
         Cite the URLs you actually read in your final answer. If you cannot find something, say so explicitly — do \
         not guess. Return your findings concisely.",
    ),
    (
        "coder",
        "You are a code worker. Your job is to write or modify code precisely. Read the existing code (read_file / \
         list_files) before changing it. Make minimal, focused edits. Use run_cell(code) to run, test, and verify. \
         When something fails, fix the underlying cause; do not paper over it. Return a short summary of what you \
         changed and any test output.",
    ),
    (
        "writer",
        "You are a writing worker. Your job is to produce final-quality prose. Read source material via read_file \
         before drafting. Match the requested length, tone, and audience. You do NOT run code — no shell, no \
         run_cell. Save drafts via write_file when asked. Return the final text.",
    ),
];

/// `_ROLE_TOOLS[role]`, and whether the `always` MCP servers' tools follow.
fn role_tools(role: &str) -> (&'static [&'static str], bool) {
    match role {
        "general" => (
            &["run_cell", "read_file", "write_file", "list_files", "write_artifact", "read_artifact", "list_artifacts"],
            true,
        ),
        "researcher" => (&["run_cell", "read_file", "read_artifact", "list_artifacts"], true),
        "coder" => (&["run_cell", "read_file", "write_file", "list_files"], false),
        _ => (&["read_file", "write_file", "write_artifact", "read_artifact", "list_artifacts"], false),
    }
}

/// One task, as far as Python reads it before the worker starts.
#[derive(Debug, PartialEq)]
pub struct Task {
    /// `None`: the spec has no `task`, which fails its worker (`KeyError`).
    pub task: Option<String>,
    /// `spec.get("role") or "general"` — perhaps no role at all.
    pub role: String,
    /// `str()` of a truthy `context`.
    pub context: Option<String>,
}

/// `spawn_workers(tasks: list[dict])`'s argument, if every spec is one
/// Python reads without raising outside a worker; anything odder is
/// Python's to run.
pub fn tasks(raw: &Value) -> Option<Vec<Task>> {
    raw.as_array()?
        .iter()
        .map(|spec| {
            let spec = spec.as_object()?;
            let task = match spec.get("task") {
                None => None,
                Some(Value::String(s)) => Some(s.clone()),
                Some(_) => return None,
            };
            let role = match spec.get("role") {
                Some(Value::String(s)) if !s.is_empty() => s.clone(),
                Some(v) if pyjson::truthy(v) => return None,
                _ => DEFAULT_ROLE.to_string(),
            };
            let context = spec.get("context").filter(|v| pyjson::truthy(v)).map(pyjson::py_str);
            Some(Task { task, role, context })
        })
        .collect()
}

/// What a worker tells the run.
pub enum Note {
    /// An event for the run's stream: `worker_*`, or one a worker's tool
    /// announces (`artifact`, a gate's request).
    Event(&'static str, Value),
    /// A finished model call.
    Llm { input: Option<i64>, output: Option<i64>, perf: CallPerf },
    /// A tool call starting.
    Tool,
}

/// What the workers share with the run that spawned them: its model and
/// tool policy, and the ids its tools are scoped by.
pub struct Ctx<'a> {
    pub agent: &'a Agent,
    pub policy: &'a Policy,
    pub mcp: &'a crate::mcp::Snapshot,
    pub model: String,
    pub ends: Endpoints,
    /// `should_use_cache(model)`.
    pub cache: bool,
    pub conversation: Option<String>,
    pub project_id: Option<String>,
    /// The chat reply an artifact sits under.
    pub message_id: Option<String>,
    /// The run a gate's request is shown on (a chat's).
    pub announce_to: Option<String>,
    pub notes: UnboundedSender<Note>,
}

impl Ctx<'_> {
    fn note(&self, note: Note) {
        // The receiver lives as long as the call; a send after it is gone
        // has no one left to tell.
        let _ = self.notes.send(note);
    }

    fn event(&self, event: &'static str, data: Value) {
        self.note(Note::Event(event, data));
    }
}

/// Run every task at once; the answers in task order.
pub async fn spawn(ctx: &Ctx<'_>, tasks: Vec<Task>) -> String {
    let runs = tasks.into_iter().enumerate().map(|(i, task)| one(ctx, task, i + 1));
    futures_util::future::join_all(runs).await.join("\n\n---\n\n")
}

/// `run_one`: its events, then its part of the answer.
async fn one(ctx: &Ctx<'_>, spec: Task, idx: usize) -> String {
    let label = pystr::prefix(spec.task.as_deref().unwrap_or(""), 80).to_string();
    let prefix = format!("Task ({}): {label}", spec.role);
    let Some(&(role, prompt)) = ROLES.iter().find(|(r, _)| *r == spec.role) else {
        let err = format!("Unknown role '{}' (available: coder, general, researcher, writer)", spec.role);
        let result = format!("ERROR: {err}");
        ctx.event("worker_done", json!({"idx": idx, "role": spec.role, "task": label, "status": "error", "result": result}));
        return format!("{prefix}\n{err}");
    };
    ctx.event("worker_start", json!({"idx": idx, "role": role, "task": label}));
    // A kernel of its own, so concurrent workers' cells don't collide on the
    // conversation's; freed as soon as the worker is done.
    let key = format!("{}::w{idx}::{}", ctx.conversation.as_deref().unwrap_or("worker"), &uuid::Uuid::new_v4().simple().to_string()[..8]);
    let kernel = Kernel::new(ctx.agent.kernels.clone(), key.clone());
    let outcome = match spec.task {
        None => Err("'task'".to_string()),
        Some(task) => {
            let query = match &spec.context {
                Some(c) => format!("Context: {c}\n\nTask: {task}"),
                None => task,
            };
            Worker::new(ctx, idx, role, prompt, key).run(query).await
        }
    };
    kernel.shutdown().await;
    match outcome {
        Ok(answer) => {
            tracing::info!("worker {idx} ({role}) done ({} chars): {label}", pystr::len(&answer));
            let data = json!({"idx": idx, "role": role, "task": label, "status": "done", "result": answer});
            ctx.event("worker_done", data);
            format!("{prefix}\n{answer}")
        }
        Err(e) => {
            tracing::warn!("worker {idx} ({role}) failed: {label} — {e}");
            let data = json!({"idx": idx, "role": role, "task": label, "status": "error", "result": format!("ERROR: {e}")});
            ctx.event("worker_done", data);
            format!("{prefix}\nERROR: {e}")
        }
    }
}

/// A worker's kernel, shut down when it's done — or, if the run is stopped
/// mid-worker, when the worker is dropped.
pub(super) struct Kernel {
    kernels: Arc<crate::kernels::Kernels>,
    key: Option<String>,
}

impl Kernel {
    pub(super) fn new(kernels: Arc<crate::kernels::Kernels>, key: String) -> Self {
        Kernel { kernels, key: Some(key) }
    }

    pub(super) async fn shutdown(mut self) {
        if let Some(key) = self.key.take() {
            self.kernels.shutdown(&key).await;
        }
    }
}

impl Drop for Kernel {
    fn drop(&mut self) {
        if let Some(key) = self.key.take() {
            let kernels = self.kernels.clone();
            tokio::spawn(async move { kernels.shutdown(&key).await });
        }
    }
}

/// A worker's tool call, its arguments checked.
#[derive(Debug, PartialEq)]
enum Call {
    RunCell { code: String },
    ReadFile { filepath: String },
    WriteFile { filepath: String, content: String },
    ListFiles { directory: String },
    WriteArtifact { title: String, content: Option<String>, file_path: Option<String>, artifact_id: Option<String> },
    ReadArtifact { artifact_id: String, version: Option<i64> },
    ListArtifacts { all_conversations: bool },
    Mcp { server: String, tool: String, args: Value },
}

/// How one call of a batch goes.
enum Planned {
    /// ToolNode's answer for a tool the worker isn't bound to.
    Unknown(String),
    /// A bound tool: whether a human approves it first, and the call —
    /// or `invoke_tool`'s answer to its arguments.
    Bound { gated: bool, call: Result<Call, String> },
}

struct Worker<'c, 'a> {
    ctx: &'c Ctx<'a>,
    idx: usize,
    role: &'static str,
    prompt: &'static str,
    kernel: String,
    tools: Toolset,
    tail: Tail,
    steps: u32,
}

impl<'c, 'a> Worker<'c, 'a> {
    fn new(ctx: &'c Ctx<'a>, idx: usize, role: &'static str, prompt: &'static str, kernel: String) -> Self {
        let (names, with_mcp) = role_tools(role);
        // The main agent's policy: a tool a human switched off doesn't come
        // back through a worker, and a gated one still asks.
        let tools = super::tools::bound_named(ctx.policy, names, with_mcp.then_some(ctx.mcp));
        Worker { ctx, idx, role, prompt, kernel, tools, tail: Tail { idx, text: String::new(), chars: 0 }, steps: 0 }
    }

    async fn run(&mut self, query: String) -> Result<String, String> {
        let mut history = vec![Message::new(Role::User, Content::Text(query))];
        loop {
            self.take_step()?;
            let reply = self.model_step(&history).await?;
            history.push(reply.clone());
            self.step("model_request", super::turn::model_step_data(&reply));
            if reply.tool_calls.is_empty() {
                return Ok(answer(&reply));
            }
            self.take_step()?;
            let results = self.tool_step(&reply.tool_calls).await?;
            self.step("tools", super::turn::tools_step_data(&results));
            history.extend(results);
        }
    }

    fn take_step(&mut self) -> Result<(), String> {
        if self.steps >= RECURSION_LIMIT {
            return Err(format!("agent '{}' reached its limit of {RECURSION_LIMIT} steps", self.role));
        }
        self.steps += 1;
        Ok(())
    }

    /// A finished step, after the text that streamed before it.
    fn step(&mut self, node: &str, data: String) {
        self.tail.flush(self.ctx);
        self.ctx.event("worker_step", json!({"idx": self.idx, "role": self.role, "node": node, "data": data}));
    }

    /// `role_model`: the role's prompt, and the history trimmed and repaired
    /// as every model call's is.
    async fn model_step(&mut self, history: &[Message]) -> Result<Message, String> {
        let ctx = self.ctx;
        let provider = ctx.model.split_once(':').map_or("", |(p, _)| p);
        let shaped = shape::repair_orphan_tool_calls(shape::strip_historical_thinking(compact::per_call(history.to_vec())));
        let layout = Layout { system: self.prompt, segments: &[], volatile: "", cache: ctx.cache, provider };
        let prompt = shape::build(&layout, shaped);
        let blobs = Default::default();
        let req = Request { model: &ctx.model, prompt: &prompt, tools: &self.tools.schemas, blobs: &blobs };
        let tail = &mut self.tail;
        let mut on_delta = |d: Delta| {
            // Text only: a worker's thinking stays out of its tail.
            if let Delta::Text(t) = d {
                tail.add(ctx, t);
            }
        };
        let reply = llm::complete(&ctx.agent.http, &ctx.ends, &req, &mut on_delta).await.map_err(|e| e.message)?;
        let usage = reply.message.usage.clone().unwrap_or_default();
        ctx.note(Note::Llm { input: usage.input, output: usage.output, perf: reply.perf });
        Ok(reply.message)
    }

    /// One batch: every gate answered in call order, then the calls, each
    /// result in order.
    async fn tool_step(&mut self, calls: &[ToolCall]) -> Result<Vec<Message>, String> {
        let mut planned: Vec<Planned> = calls.iter().map(|c| self.plan(c)).collect();
        for (call, plan) in calls.iter().zip(planned.iter_mut()) {
            if let Planned::Bound { gated: true, .. } = plan {
                *plan = match self.gate(call).await? {
                    None => match std::mem::replace(plan, Planned::Unknown(String::new())) {
                        Planned::Bound { call, .. } => Planned::Bound { gated: false, call },
                        unknown => unknown,
                    },
                    Some(denied) => Planned::Unknown(denied),
                };
            }
        }
        let mut results = Vec::with_capacity(calls.len());
        for (call, plan) in calls.iter().zip(planned) {
            let (content, status, artifact) = match plan {
                Planned::Unknown(answer) => (Content::Text(answer), "error", None),
                Planned::Bound { call: Err(answer), .. } => {
                    self.ctx.note(Note::Tool);
                    (Content::Text(answer), "error", None)
                }
                Planned::Bound { call: Ok(Call::Mcp { server, tool, args }), .. } => {
                    self.ctx.note(Note::Tool);
                    let r = self.ctx.agent.mcp.call(&server, &tool, &args, None).await?;
                    (super::turn::mcp_content(&r.blocks), if r.is_error { "error" } else { "success" }, r.artifact)
                }
                Planned::Bound { call: Ok(native), .. } => {
                    self.ctx.note(Note::Tool);
                    (Content::Text(self.run_call(native).await?), "success", None)
                }
            };
            results.push(Message {
                name: Some(call.name.clone()),
                tool_call_id: call.id.clone(),
                status: Some(status.into()),
                artifact,
                ..Message::new(Role::Tool, content)
            });
        }
        Ok(results)
    }

    fn plan(&self, call: &ToolCall) -> Planned {
        if !self.tools.schemas.iter().any(|t| t.name == call.name) {
            return Planned::Unknown(super::tools::unknown_tool(&call.name, &self.tools.schemas));
        }
        let gated = self.ctx.policy.needs_approval(&call.name);
        let call = match self.tools.mcp_server(&call.name) {
            Some(server) => Ok(Call::Mcp { server: server.to_string(), tool: call.name.clone(), args: call.args.clone() }),
            None => check(&call.name, &call.args),
        };
        Planned::Bound { gated, call }
    }

    /// `make_tool_gate` for one call: `None` once approved, else the answer
    /// the model gets.
    async fn gate(&self, call: &ToolCall) -> Result<Option<String>, String> {
        let ctx = self.ctx;
        let pool = &ctx.agent.pool;
        let key = ctx.policy.key_for(&call.name);
        let request =
            crate::approvals::create(pool, &key, &call.name, &call.args, ctx.conversation.as_deref(), ctx.announce_to.as_deref())
                .await
                .map_err(|e| e.to_string())?;
        if ctx.announce_to.is_some() {
            ctx.event("approval_request", request.event);
        }
        tracing::info!("tool gate: waiting on approval {} for {} (worker {})", request.id, call.name, self.idx);
        let outcome = crate::approvals::wait(pool, &request.id, crate::approvals::gate_timeout()).await.map_err(|e| e.to_string())?;
        let (approved, answer) = match outcome {
            crate::approvals::Outcome::Answered { approved, answer } => (approved, answer),
            crate::approvals::Outcome::TimedOut => {
                if ctx.announce_to.is_some() {
                    ctx.event("approval_resolved", crate::approvals::resolved_event(&call.name, false, "timed out"));
                }
                (false, "timed out".to_string())
            }
        };
        tracing::info!("tool gate: {} {} ({})", call.name, if approved { "approved" } else { "denied" }, request.id);
        Ok((!approved).then(|| crate::approvals::denial_message(&call.name, &answer)))
    }

    /// One of the worker's tools; an `Err` is what Python raises, which
    /// fails the worker.
    async fn run_call(&mut self, call: Call) -> Result<String, String> {
        let ctx = self.ctx;
        let pool = &ctx.agent.pool;
        let cwd = crate::config::app_dir();
        match call {
            Call::RunCell { code } => {
                let cell = crate::kernels::Cell {
                    code: &code,
                    timeout: super::tools::CELL_TIMEOUT,
                    conversation_id: ctx.conversation.as_deref(),
                    project_id: ctx.project_id.as_deref(),
                };
                ctx.agent.kernels.run(&self.kernel, &cell).await
            }
            Call::ReadFile { filepath } => files::read_file(&cwd, &filepath),
            Call::WriteFile { filepath, content } => files::write_file(&cwd, &filepath, &content),
            Call::ListFiles { directory } => files::list_files(&cwd, &directory),
            Call::WriteArtifact { title, content, file_path, artifact_id } => {
                let scope = artifacts::Scope {
                    pool,
                    dir: &ctx.agent.artifacts_dir,
                    cwd: &cwd,
                    conversation_id: ctx.conversation.as_deref(),
                    message_id: ctx.message_id.as_deref(),
                };
                let written =
                    artifacts::write(&scope, &title, content.as_deref(), file_path.as_deref(), artifact_id.as_deref()).await?;
                if let Some(event) = written.event {
                    // Flushed first: a tool's event comes after the text before it.
                    self.tail.flush(ctx);
                    ctx.event("artifact", event);
                }
                Ok(written.answer)
            }
            Call::ReadArtifact { artifact_id, version } => {
                artifacts::read(pool, &ctx.agent.artifacts_dir, &cwd, &artifact_id, version).await
            }
            Call::ListArtifacts { all_conversations } => artifacts::list(pool, ctx.conversation.as_deref(), all_conversations).await,
            Call::Mcp { .. } => unreachable!("an MCP call runs in tool_step"),
        }
    }
}

/// `_TokenTail`: a worker's streamed text as `worker_token` events, at
/// `TAIL_CHARS` or a step boundary.
struct Tail {
    idx: usize,
    text: String,
    chars: usize,
}

impl Tail {
    fn add(&mut self, ctx: &Ctx<'_>, text: &str) {
        if text.is_empty() {
            return;
        }
        self.text.push_str(text);
        self.chars += pystr::len(text);
        if self.chars >= TAIL_CHARS {
            self.flush(ctx);
        }
    }

    fn flush(&mut self, ctx: &Ctx<'_>) {
        if self.text.is_empty() {
            return;
        }
        self.chars = 0;
        ctx.event("worker_token", json!({"idx": self.idx, "text": std::mem::take(&mut self.text)}));
    }
}

/// The worker's answer: its last reply's text — the text blocks of a
/// reply in parts, or the parts themselves if none has text.
fn answer(reply: &Message) -> String {
    match &reply.content {
        Content::Text(s) => s.clone(),
        Content::Parts(parts) => {
            let texts: Vec<&str> = parts
                .iter()
                .filter_map(|p| match p {
                    Part::Typed(Typed::Text { text, .. }) => Some(text.as_str()),
                    _ => None,
                })
                .collect();
            let joined = pystr::strip(&texts.join(" ")).to_string();
            if joined.is_empty() { pyjson::py_repr(&super::turn::lc_blocks(parts)) } else { joined }
        }
    }
}

// ── arguments ───────────────────────────────────────────────────────────────

/// `_BAD_ARGS`.
fn bad_args(tool: &str, args: &Value, errors: &[String]) -> String {
    format!(
        "Error invoking tool '{tool}' with kwargs {} with error:\n {}\n Please fix the error and try again.",
        pyjson::py_repr(args),
        errors.join("\n")
    )
}

/// The call as its tool's signature takes it, or `invoke_tool`'s answer.
/// Pydantic's lax mode: extra keys ignored, an integer from a whole float
/// or a numeric string, a boolean from 0/1 or a word.
fn check(name: &str, args: &Value) -> Result<Call, String> {
    let empty = Map::new();
    let mut a = Args { args: args.as_object().unwrap_or(&empty), errors: vec![] };
    let call = match name {
        "run_cell" => Call::RunCell { code: a.str("code") },
        "read_file" => Call::ReadFile { filepath: a.str("filepath") },
        "write_file" => Call::WriteFile { filepath: a.str("filepath"), content: a.str("content") },
        "list_files" => Call::ListFiles { directory: a.str("directory") },
        "write_artifact" => Call::WriteArtifact {
            title: a.str("title"),
            content: a.opt_str("content"),
            file_path: a.opt_str("file_path"),
            artifact_id: a.opt_str("artifact_id"),
        },
        "read_artifact" => Call::ReadArtifact { artifact_id: a.str("artifact_id"), version: a.opt_int("version") },
        "list_artifacts" => Call::ListArtifacts { all_conversations: a.bool("all_conversations", false) },
        other => unreachable!("{other} is no worker tool"),
    };
    if a.errors.is_empty() { Ok(call) } else { Err(bad_args(name, args, &a.errors)) }
}

struct Args<'v> {
    args: &'v Map<String, Value>,
    errors: Vec<String>,
}

impl Args<'_> {
    fn fail(&mut self, key: &str, msg: &str) {
        self.errors.push(format!("{key}: {msg}"));
    }

    fn str(&mut self, key: &str) -> String {
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

    fn opt_str(&mut self, key: &str) -> Option<String> {
        match self.args.get(key) {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => {
                self.fail(key, "Input should be a valid string");
                None
            }
        }
    }

    fn opt_int(&mut self, key: &str) -> Option<i64> {
        match self.args.get(key) {
            None | Some(Value::Null) => None,
            Some(v) => self.as_int(key, v),
        }
    }

    fn as_int(&mut self, key: &str, v: &Value) -> Option<i64> {
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

    fn bool(&mut self, key: &str, default: bool) -> bool {
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
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn specs_python_reads_without_raising() {
        let got = tasks(&json!([
            {"task": "a"},
            {"task": "b", "role": "", "context": 0},
            {"task": "c", "role": "coder", "context": {"k": 1}},
            {"role": "writer"},
        ]))
        .unwrap();
        assert_eq!(got[0], Task { task: Some("a".into()), role: "general".into(), context: None });
        assert_eq!(got[1].role, "general");
        assert_eq!(got[1].context, None);
        assert_eq!(got[2].context.as_deref(), Some("{'k': 1}"));
        assert_eq!(got[3].task, None);
        for odd in [json!([{"task": 5}]), json!([{"task": "x", "role": 3}]), json!(["x"]), json!({"task": "x"})] {
            assert_eq!(tasks(&odd), None, "{odd}");
        }
    }

    #[test]
    fn bad_arguments_are_worded_as_python_words_them() {
        let err = |name: &str, args: Value| check(name, &args).unwrap_err();
        assert_eq!(
            err("read_file", json!({})),
            "Error invoking tool 'read_file' with kwargs {} with error:\n filepath: Field required\n Please fix the error \
             and try again."
        );
        assert!(err("read_file", json!({"filepath": null})).contains("filepath: Input should be a valid string"));
        assert!(
            err("read_artifact", json!({"artifact_id": "a", "version": "two"}))
                .contains("version: Input should be a valid integer, unable to parse string as an integer")
        );
        assert!(
            err("read_artifact", json!({"artifact_id": "a", "version": 1.5}))
                .contains("version: Input should be a valid integer, got a number with a fractional part")
        );
        assert!(err("read_artifact", json!({"artifact_id": "a", "version": [1]})).contains("version: Input should be a valid integer\n"));
        assert!(
            err("list_artifacts", json!({"all_conversations": 2}))
                .contains("all_conversations: Input should be a valid boolean, unable to interpret input")
        );
        assert!(err("list_artifacts", json!({"all_conversations": null})).contains("all_conversations: Input should be a valid boolean\n"));
        assert_eq!(
            err("write_file", json!({"filepath": "x"})),
            "Error invoking tool 'write_file' with kwargs {'filepath': 'x'} with error:\n content: Field required\n Please \
             fix the error and try again."
        );
        assert_eq!(check("read_file", &json!({"filepath": "x", "extra": 1})).unwrap(), Call::ReadFile { filepath: "x".into() });
        assert_eq!(
            check("read_artifact", &json!({"artifact_id": "a", "version": true})).unwrap(),
            Call::ReadArtifact { artifact_id: "a".into(), version: Some(1) }
        );
    }

    #[test]
    fn an_answer_is_the_last_replys_text() {
        let parts: Message = serde_json::from_value(json!({"v": 1, "role": "assistant", "content": [
            {"type": "thinking", "thinking": "hm"}, {"type": "text", "text": " a"}, {"type": "text", "text": "b "}]}))
        .unwrap();
        assert_eq!(answer(&parts), "a b");
        assert_eq!(answer(&Message::new(Role::Assistant, Content::Text("x".into()))), "x");
    }

    #[test]
    fn each_role_has_its_prompt_and_tools() {
        assert_eq!(ROLES.map(|(r, _)| r), ["general", "researcher", "coder", "writer"]);
        assert!(!role_tools("writer").0.contains(&"run_cell"));
    }
}
