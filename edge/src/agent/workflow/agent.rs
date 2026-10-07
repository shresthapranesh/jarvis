//! `workflow/nodes.py:_run_agent_text`: the main agent on one prompt, for an
//! agent, refine, sequential, parallel or loop node — a change is made in both.
//!
//! The agent is the chat agent — its system prompt and retrieval, its tools
//! and their policy — on a history of its own in memory and a kernel of its
//! own (`configurable.thread_id` is a fresh uuid in Python, so its kernel
//! key is too; here the kernel is also shut down when the agent is done).
//! Only its streamed text leaves it, as `node_token`; everything else it
//! emits went nowhere in Python and goes nowhere here. Its model and tool
//! calls count against the workflow's budget. Nothing hands over to
//! Python: a call whose arguments the edge doesn't take is answered with
//! an error the model can correct.

use serde_json::{Value, json};
use uuid::Uuid;

use super::engine::Env;
use crate::agent::tools::{self, Native, Plan, Policy, Step, Toolset};
use crate::agent::{artifacts, prompt, turn, workers};
use crate::llm::shape::{self, Layout, Segment};
use crate::llm::transcript::{Content, Message, Role, ToolCall};
use crate::llm::{self, Delta, Endpoints, Request, compact};
use crate::pyjson;

/// `_run_agent_text`'s `recursion_limit`.
const RECURSION_LIMIT: u32 = 100;

/// The agent's streamed text over every step, as `node_token`s under
/// `node_id` — or why it failed.
pub async fn run_text(env: &Env<'_>, node_id: &str, model: &str, query: String) -> Result<String, String> {
    let mut node = NodeAgent::new(env, node_id, model, &query).await?;
    let kernel = workers::Kernel::new(env.agent.kernels.clone(), node.thread_id.clone());
    let answer = node.run(query).await.map(|_| std::mem::take(&mut node.text));
    kernel.shutdown().await;
    answer
}

/// The agent's last reply — `main.py run`'s `agent.ainvoke(...)["messages"][-1]`:
/// the same agent, on a history and kernel of its own, with no conversation.
pub async fn run_reply(env: &Env<'_>, model: &str, query: String) -> Result<Message, String> {
    let mut node = NodeAgent::new(env, "", model, &query).await?;
    let kernel = workers::Kernel::new(env.agent.kernels.clone(), node.thread_id.clone());
    let answer = node.run(query).await;
    kernel.shutdown().await;
    answer
}

struct NodeAgent<'a, 'e> {
    env: &'a Env<'e>,
    node_id: &'a str,
    model: &'a str,
    query: String,
    policy: Policy,
    mcp: std::sync::Arc<crate::mcp::Snapshot>,
    bound: Toolset,
    ends: Endpoints,
    retrieved: Vec<Segment>,
    thread_id: String,
    todos: Vec<Value>,
    text: String,
    steps: u32,
}

impl<'a, 'e> NodeAgent<'a, 'e> {
    async fn new(env: &'a Env<'e>, node_id: &'a str, model: &'a str, query: &str) -> Result<Self, String> {
        let agent = env.agent;
        let pool = &agent.pool;
        let mcp = agent.mcp.snapshot().await;
        let policy = Policy::load(pool, &mcp).await;
        let bound = tools::bound_for(&policy, false, &mcp);
        let thread_id = Uuid::new_v4().to_string();
        let retrieved = prompt::retrieved(pool, &agent.http, query, &thread_id, &mcp)
            .await
            .map_err(|prompt::Unbuilt(why)| format!("the agent's prompt couldn't be built: {why}"))?;
        let ends = Endpoints { compatible: crate::catalog::endpoints(pool).await.unwrap_or_default(), ..Endpoints::from_env() };
        Ok(NodeAgent {
            env,
            node_id,
            model,
            query: query.to_string(),
            policy,
            mcp,
            bound,
            ends,
            retrieved,
            thread_id,
            todos: vec![],
            text: String::new(),
            steps: 0,
        })
    }

    /// The loop, to the reply with no tool calls.
    async fn run(&mut self, query: String) -> Result<Message, String> {
        let mut history = vec![Message::new(Role::User, Content::Text(query))];
        loop {
            self.take_step()?;
            let reply = self.model_step(&history).await?;
            history.push(reply.clone());
            if reply.tool_calls.is_empty() {
                return Ok(reply);
            }
            self.take_step()?;
            let results = self.tool_step(&reply.tool_calls).await?;
            history.extend(results);
        }
    }

    fn take_step(&mut self) -> Result<(), String> {
        if self.steps >= RECURSION_LIMIT {
            return Err(format!("agent 'main' reached its limit of {RECURSION_LIMIT} steps"));
        }
        self.steps += 1;
        Ok(())
    }

    async fn model_step(&mut self, history: &[Message]) -> Result<Message, String> {
        let pool = &self.env.agent.pool;
        let context = prompt::build(pool, &self.query, None, &self.todos, &self.retrieved)
            .await
            .map_err(|prompt::Unbuilt(why)| format!("the agent's prompt couldn't be built: {why}"))?;
        let provider = self.model.split_once(':').map_or("", |(p, _)| p);
        let layout = Layout {
            system: &context.system,
            segments: &context.segments,
            volatile: &context.volatile,
            cache: turn::honors_cache_control(self.model),
            provider,
        };
        let shaped = shape::repair_orphan_tool_calls(shape::strip_historical_thinking(compact::per_call(history.to_vec())));
        let prompt = shape::build(&layout, shaped);
        let blobs = Default::default();
        let req = Request { model: self.model, prompt: &prompt, tools: &self.bound.schemas, blobs: &blobs };
        let (env, node_id, text) = (self.env, self.node_id, &mut self.text);
        let mut on_delta = |d: Delta| {
            if let Delta::Text(t) = d {
                if !t.is_empty() {
                    text.push_str(t);
                    env.emit("node_token", json!({"node_id": node_id, "text": t}));
                }
            }
        };
        let reply = llm::complete(&self.env.agent.http, &self.ends, &req, &mut on_delta).await.map_err(|e| e.message)?;
        let usage = reply.message.usage.clone().unwrap_or_default();
        if let Some(meter) = &self.env.meter {
            meter.llm(usage.input, usage.output);
        }
        Ok(reply.message)
    }

    fn count_tool(&self) {
        if let Some(meter) = &self.env.meter {
            meter.tool();
        }
    }

    /// One batch: the gates answered in call order, then each call.
    async fn tool_step(&mut self, calls: &[ToolCall]) -> Result<Vec<Message>, String> {
        let mut steps = Vec::with_capacity(calls.len());
        for call in calls {
            let step = match tools::plan(std::slice::from_ref(call), &self.bound, &self.policy) {
                Plan::Edge(mut one) => one.pop().expect("one call, one step"),
                // In chat, Python words this; here the model gets a plainer
                // version of `invoke_tool`'s error.
                Plan::Python(_) => Step::Unknown(format!(
                    "Error invoking tool '{}' with kwargs {} with error:\n arguments don't match the tool's schema\n \
                     Please fix the error and try again.",
                    call.name,
                    pyjson::py_repr(&call.args)
                )),
            };
            steps.push(match step {
                Step::Gated(native) => self.gate(call, native).await?,
                other => other,
            });
        }
        let mut results = Vec::with_capacity(calls.len());
        for (call, step) in calls.iter().zip(steps) {
            let (content, status, artifact) = match step {
                Step::Unknown(error) | Step::Denied(error) => (Content::Text(error), "error", None),
                Step::Gated(_) => unreachable!("gates are answered first"),
                Step::Run(Native::Mcp { server, tool, args }) => {
                    self.count_tool();
                    let r = self.env.agent.mcp.call(&server, &tool, &args, None).await?;
                    (turn::mcp_content(&r.blocks), if r.is_error { "error" } else { "success" }, r.artifact)
                }
                Step::Run(native) => {
                    self.count_tool();
                    (Content::Text(self.run_native(native).await?), "success", None)
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

    /// `make_tool_gate` with no conversation: recorded for the inbox, not
    /// shown on any run.
    async fn gate(&self, call: &ToolCall, native: Native) -> Result<Step, String> {
        let pool = &self.env.agent.pool;
        let key = self.policy.key_for(&call.name);
        let request = crate::approvals::create(pool, &key, &call.name, &call.args, None, None).await.map_err(|e| e.to_string())?;
        tracing::info!("tool gate: waiting on approval {} for {} (workflow node {})", request.id, call.name, self.node_id);
        let outcome = crate::approvals::wait(pool, &request.id, crate::approvals::gate_timeout()).await.map_err(|e| e.to_string())?;
        Ok(match outcome {
            crate::approvals::Outcome::Answered { approved: true, .. } => Step::Run(native),
            crate::approvals::Outcome::Answered { answer, .. } => Step::Denied(crate::approvals::denial_message(&call.name, &answer)),
            crate::approvals::Outcome::TimedOut => Step::Denied(crate::approvals::denial_message(&call.name, "timed out")),
        })
    }

    /// One of the agent's tools; an `Err` fails the node, as a tool that
    /// raises does in Python.
    async fn run_native(&mut self, native: Native) -> Result<String, String> {
        let agent = self.env.agent;
        match native {
            Native::RunCell { code } => {
                let cell = crate::kernels::Cell { code: &code, timeout: tools::CELL_TIMEOUT, conversation_id: None, project_id: None };
                agent.kernels.run(&self.thread_id, &cell).await
            }
            Native::WriteTodos { todos } => {
                let items: Vec<Value> = todos.iter().map(|t| json!({"text": t, "status": "pending"})).collect();
                self.todos = tools::reduce_todos(&self.todos, &items);
                Ok(tools::todos_written(items.len()))
            }
            Native::SetTodoStatus { index, status } => match tools::set_status(&self.todos, index, &status) {
                Ok((todos, answer)) => {
                    self.todos = tools::reduce_todos(&self.todos, &todos);
                    Ok(answer)
                }
                Err(answer) => Ok(answer),
            },
            Native::Remember { text, kind } => {
                if text.trim().is_empty() {
                    return Ok("Nothing to remember (empty text).".into());
                }
                let kind = if kind == "core" || kind == "fact" { kind } else { "fact".into() };
                Ok(match crate::agent::retrieve::upsert_memory(&agent.pool, &agent.http, &text, &kind).await {
                    Ok(_) => format!("Remembered ({kind})."),
                    Err(e) => format!("Could not save memory: {e}"),
                })
            }
            Native::WriteArtifact { title, content, file_path, artifact_id } => {
                let cwd = crate::config::app_dir();
                let scope = artifacts::Scope {
                    pool: &agent.pool,
                    dir: &agent.artifacts_dir,
                    cwd: &cwd,
                    conversation_id: None,
                    message_id: None,
                };
                let written =
                    artifacts::write(&scope, &title, content.as_deref(), file_path.as_deref(), artifact_id.as_deref()).await?;
                Ok(written.answer)
            }
            Native::SpawnWorkers { tasks } => Ok(self.spawn_workers(tasks).await),
            Native::RunWorkflow { workflow_id, inputs_json } => {
                Ok(match super::Call::prepare(&agent.pool, self.env.depth, &workflow_id, inputs_json.as_deref()).await {
                    Ok(call) => {
                        let ran = call.run(agent, self.env.depth + 1).await;
                        call.answer(ran)
                    }
                    Err(answer) => answer,
                })
            }
            Native::CompleteTask { .. } => Ok("Error: complete_task is only available while executing a board task.".into()),
            Native::BlockTask { .. } => Ok("Error: block_task is only available while executing a board task.".into()),
            Native::Mcp { .. } => unreachable!("runs in tool_step"),
        }
    }

    /// `spawn_workers` inside a node: the workers' calls count against the
    /// workflow's budget; what they announce goes nowhere, as the node's own
    /// events do.
    async fn spawn_workers(&self, tasks: Vec<workers::Task>) -> String {
        let (notes, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let ctx = workers::Ctx {
            agent: self.env.agent,
            policy: &self.policy,
            mcp: &self.mcp,
            model: self.model.to_string(),
            ends: self.ends.clone(),
            cache: turn::honors_cache_control(self.model),
            conversation: None,
            project_id: None,
            message_id: None,
            announce_to: None,
            notes,
        };
        let meter = self.env.meter.clone();
        let note = move |n: workers::Note| match (n, &meter) {
            (workers::Note::Llm { input, output, .. }, Some(m)) => m.llm(input, output),
            (workers::Note::Tool, Some(m)) => m.tool(),
            _ => {}
        };
        let all = workers::spawn(&ctx, tasks);
        tokio::pin!(all);
        let answer = loop {
            tokio::select! {
                biased;
                Some(n) = rx.recv() => note(n),
                answer = &mut all => break answer,
            }
        };
        while let Ok(n) = rx.try_recv() {
            note(n);
        }
        answer
    }
}
