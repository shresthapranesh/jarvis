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
//!
//! In a workflow's run each agent has a conversation of its own (`Seat`:
//! `surface = 'workflow'`, so out of the sidebar; `spawned_by` the run): its
//! prompt, its reply — followed live there, as a chat turn's is — and its
//! history as the conversation's thread, so it can be opened from the run
//! and gone on with. A child workflow's agents and `main.py run`'s have none.

use std::sync::Arc;

use serde_json::{Value, json};
use uuid::Uuid;

use super::engine::Env;
use crate::agent::tools::{self, Native, Policy, Step, Toolset};
use crate::agent::thread::Thread;
use crate::agent::{artifacts, prompt, turn, workers};
use crate::gql::codec::{new_id, now_stored};
use crate::llm::shape::{self, Layout, Segment};
use crate::llm::transcript::{Content, Message, Role, ToolCall};
use crate::llm::{self, Delta, Endpoints, Request, compact};
use crate::runs::{Meta, Registry, Run};

/// `_run_agent_text`'s `recursion_limit`.
const RECURSION_LIMIT: u32 = 100;

/// The agent's streamed text over every step, as `node_token`s under
/// `node_id` — or why it failed.
pub async fn run_text(env: &Env<'_>, node_id: &str, model: &str, query: String) -> Result<String, String> {
    let mut node = NodeAgent::new(env, node_id, model, &query).await?;
    let kernel = workers::Kernel::new(env.agent.kernels.clone(), node.thread_id.clone());
    node.seat = Seat::open(env, node_id, model, &node.thread_id, &query).await;
    let answer = node.run(query).await;
    if let Some(seat) = node.seat.take() {
        seat.close(std::mem::take(&mut node.history), model, answer.as_ref().err()).await;
    }
    kernel.shutdown().await;
    answer.map(|_| std::mem::take(&mut node.text))
}

/// A node agent's conversation, and the reply in it the agent writes into.
/// The reply has a run of its own (`Registry::register_unqueued`, a `chat`
/// run on the conversation), so the conversation's page follows it as it
/// follows a chat turn: its text and steps as they come, a stop, a message
/// sent meanwhile.
struct Seat {
    run: Arc<Run>,
    runs: Arc<Registry>,
    pool: sqlx::SqlitePool,
    conversation_id: String,
    message_id: String,
    /// The next `steps.seq`.
    seq: i64,
    /// What the agent has said so far.
    said: String,
    closed: bool,
}

impl Seat {
    /// The conversation, the prompt as its user's message, and the reply,
    /// running. None outside a workflow's own run, or if it can't be written
    /// — the agent runs all the same.
    async fn open(env: &Env<'_>, node_id: &str, model: &str, thread_id: &str, query: &str) -> Option<Seat> {
        let workflow = env.run.as_ref()?;
        let pool = &env.agent.pool;
        let message_id = new_id();
        let started = now_stored();
        let opened = async {
            let mut tx = crate::db::write_tx(pool).await?;
            sqlx::query(
                "INSERT INTO conversations (id, title, model, created_at, surface, pinned, project_id, ephemeral, spawned_by) \
                 VALUES (?, ?, ?, ?, ?, 0, NULL, 0, ?)",
            )
            .bind(thread_id)
            .bind(format!("{} · {node_id}", workflow.meta.label))
            .bind(model)
            .bind(&started)
            .bind(SURFACE)
            .bind(&workflow.id)
            .execute(&mut *tx)
            .await?;
            crate::gql::start::insert_message(&mut *tx, thread_id, "user", query, None, "done").await?;
            sqlx::query(
                "INSERT INTO messages (id, conversation_id, role, content, model, created_at, status) \
                 VALUES (?, ?, 'assistant', '', ?, ?, 'running')",
            )
            .bind(&message_id)
            .bind(thread_id)
            .bind(model)
            .bind(now_stored())
            .execute(&mut *tx)
            .await?;
            // Registered before the reply can be seen, as a trigger's run is.
            let meta = Meta {
                kind: "chat".into(),
                label: crate::gql::start::first_chars(query, 60),
                parent_id: Some(thread_id.into()),
                started_at: crate::gql::codec::iso_from_db(&started).utc().0,
            };
            let run = env.agent.runs.register_unqueued(&message_id, meta);
            match tx.commit().await {
                Ok(()) => Ok(run),
                Err(e) => {
                    env.agent.runs.discard_pending(&message_id);
                    Err(e)
                }
            }
        };
        match opened.await {
            Ok(run) => Some(Seat {
                run,
                runs: env.agent.runs.clone(),
                pool: pool.clone(),
                conversation_id: thread_id.into(),
                message_id,
                seq: 0,
                said: String::new(),
                closed: false,
            }),
            Err(e) => {
                tracing::warn!("workflow run {}: node {node_id}'s conversation: {e}", workflow.id);
                None
            }
        }
    }

    fn token(&mut self, text: &str) {
        self.said.push_str(text);
        self.run.emit_local("token", &json!({"text": text, "source": "main"}));
    }

    /// A finished step: its row, then its event, as a chat turn's.
    async fn step(&mut self, node: &str, data: String) {
        let written = sqlx::query(
            "INSERT INTO steps (id, message_id, conversation_id, node, source, subagent, data, seq, created_at) \
             VALUES (?, ?, ?, ?, 'main', NULL, ?, ?, ?)",
        )
        .bind(new_id())
        .bind(&self.message_id)
        .bind(&self.conversation_id)
        .bind(node)
        .bind(&data)
        .bind(self.seq)
        .bind(now_stored())
        .execute(&self.pool)
        .await;
        match written {
            Ok(_) => {
                self.seq += 1;
                self.run.emit_local("step", &json!({"node": node, "source": "main", "subagent": null, "data": data}));
            }
            Err(e) => tracing::warn!("workflow agent {}: writing a step: {e}", self.conversation_id),
        }
    }

    /// Whether the reply was stopped from its conversation.
    fn stopped(&self) -> bool {
        self.run.fields().cancelled
    }

    /// The reply as it ended, and the agent's history as the conversation's
    /// thread: a message sent there later — or meanwhile, which becomes the
    /// next turn now — goes on from all of it.
    async fn close(mut self, history: Vec<Message>, model: &str, error: Option<&String>) {
        self.closed = true;
        let (content, status) = match error {
            _ if self.stopped() => (self.said.clone(), "stopped"),
            Some(error) if self.said.is_empty() => (format!("The run failed before completing: {error}"), "error"),
            Some(_) => (self.said.clone(), "error"),
            None => (self.said.clone(), "done"),
        };
        let closed = async {
            sqlx::query("UPDATE messages SET content = ?, status = ? WHERE id = ?")
                .bind(&content)
                .bind(status)
                .bind(&self.message_id)
                .execute(&self.pool)
                .await
                .map_err(|e| e.to_string())?;
            Thread::load(&self.pool, &self.conversation_id).await?.apply(&self.pool, history).await
        };
        if let Err(e) = closed.await {
            tracing::warn!("workflow agent {}: writing its conversation: {e}", self.conversation_id);
        }
        match (status, error) {
            ("error", Some(error)) => self.run.emit_local("error", &json!({"error": error})),
            _ => self.run.emit_local(status, &json!({"message": content, "conversation_id": self.conversation_id})),
        }
        self.end();
        if status == "done" {
            crate::gql::start::redispatch_queued(&self.pool, &self.runs, &self.conversation_id, model).await;
        }
    }

    fn end(&self) {
        self.run.update(|st| st.fields.done = true);
        self.runs.retire(&self.message_id);
    }
}

/// A stopped workflow drops its agents where they stand: the reply ends
/// stopped, with what it had said.
impl Drop for Seat {
    fn drop(&mut self) {
        if self.closed {
            return;
        }
        self.run.emit_local("stopped", &json!({"message": self.said, "conversation_id": self.conversation_id}));
        self.end();
        let (pool, id, said) = (self.pool.clone(), self.message_id.clone(), std::mem::take(&mut self.said));
        tokio::spawn(async move {
            let written = sqlx::query("UPDATE messages SET content = ?, status = 'stopped' WHERE id = ? AND status = 'running'")
                .bind(said)
                .bind(&id)
                .execute(&pool)
                .await;
            if let Err(e) = written {
                tracing::warn!("workflow agent's reply {id}: recording its stop: {e}");
            }
        });
    }
}

/// `conversations.surface` of a workflow agent's conversation.
pub const SURFACE: &str = "workflow";

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
    /// Its conversation, in a workflow's run.
    seat: Option<Seat>,
    /// What it and the model said, kept for its conversation's thread.
    history: Vec<Message>,
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
        let bound = tools::bound_for(&policy, false, false, &mcp);
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
            seat: None,
            history: vec![],
            todos: vec![],
            text: String::new(),
            steps: 0,
        })
    }

    /// The loop, to the reply with no tool calls.
    async fn run(&mut self, query: String) -> Result<Message, String> {
        let mut history = vec![Message::new(Role::User, Content::Text(query))];
        let answer = self.steps(&mut history).await;
        self.history = history;
        answer
    }

    async fn steps(&mut self, history: &mut Vec<Message>) -> Result<Message, String> {
        loop {
            self.take_step()?;
            let reply = self.model_step(history).await?;
            history.push(reply.clone());
            if let Some(seat) = &mut self.seat {
                seat.step("model_request", turn::model_step_data(&reply)).await;
            }
            if let Some(why) = llm::cut_short(&reply) {
                return Err(why);
            }
            if reply.tool_calls.is_empty() {
                return Ok(reply);
            }
            self.take_step()?;
            let results = self.tool_step(&reply.tool_calls).await?;
            if let Some(seat) = &mut self.seat {
                seat.step("tools", turn::tools_step_data(&results)).await;
            }
            history.extend(results);
        }
    }

    fn take_step(&mut self) -> Result<(), String> {
        if self.seat.as_ref().is_some_and(Seat::stopped) {
            return Err("the agent was stopped from its conversation".into());
        }
        if self.steps >= RECURSION_LIMIT {
            return Err(format!("agent 'main' reached its limit of {RECURSION_LIMIT} steps"));
        }
        self.steps += 1;
        Ok(())
    }

    async fn model_step(&mut self, history: &mut [Message]) -> Result<Message, String> {
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
            append_only: llm::preserves_thinking(self.model),
        };
        if layout.append_only {
            shape::carry_turn_context(history, shape::turn_context(&layout));
        }
        let history = compact::per_call_for(self.model, history.to_vec());
        let shaped = shape::repair_orphan_tool_calls(shape::strip_thinking_for(self.model, history));
        let prompt = shape::build(&layout, shaped);
        let blobs = Default::default();
        let req = Request {
            model: self.model,
            prompt: &prompt,
            tools: &self.bound.schemas,
            blobs: &blobs,
            effort: crate::agent::effort(pool, self.model, "workflow", None).await,
            cache_key: None,
        };
        let (env, node_id, text, seat) = (self.env, self.node_id, &mut self.text, &mut self.seat);
        let mut on_delta = |d: Delta| {
            if let Delta::Text(t) = d {
                if !t.is_empty() {
                    text.push_str(t);
                    env.emit("node_token", json!({"node_id": node_id, "text": t}));
                    if let Some(seat) = seat {
                        seat.token(t);
                    }
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
            let step = tools::plan(std::slice::from_ref(call), &self.bound, &self.policy).pop().expect("one call, one step");
            steps.push(match step {
                Step::Gated(native) => self.gate(call, native).await?,
                other => other,
            });
        }
        let mut results = Vec::with_capacity(calls.len());
        for (call, step) in calls.iter().zip(steps) {
            let (content, status, artifact) = match step {
                Step::Refused(error) | Step::Denied(error) => (Content::Text(error), "error", None),
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
                // With a conversation, the SDK is scoped to it.
                let conversation_id = self.seat.as_ref().map(|_| self.thread_id.as_str());
                let cell = crate::kernels::Cell { code: &code, timeout: tools::CELL_TIMEOUT, conversation_id, project_id: None };
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
            Native::Remember { text, kind, topic } => {
                if text.trim().is_empty() {
                    return Ok("Nothing to remember (empty text).".into());
                }
                let kind = if kind == "core" || kind == "fact" { kind } else { "fact".into() };
                // A workflow agent's thread is its own, not a conversation.
                let origin = crate::agent::retrieve::Origin { source: "remember", conversation_id: None };
                let topic = topic.as_deref().and_then(crate::agent::retrieve::topic_of);
                Ok(match crate::agent::retrieve::upsert_memory(&agent.pool, &agent.http, &text, &kind, topic, origin).await {
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
                    conversation_id: self.seat.as_ref().map(|_| self.thread_id.as_str()),
                    message_id: self.seat.as_ref().map(|s| s.message_id.as_str()),
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
            Native::AskUser { .. } => Ok(tools::ASK_NOWHERE.into()),
            Native::Mcp { .. } => unreachable!("runs in tool_step"),
            Native::RunAgents { .. } | Native::CheckAgents { .. } => unreachable!("a turn's tools, never bound here"),
            Native::ReadFile { .. }
            | Native::WriteFile { .. }
            | Native::ListFiles { .. }
            | Native::ReadArtifact { .. }
            | Native::ListArtifacts { .. } => unreachable!("a sub-agent role's tool, never bound here"),
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
