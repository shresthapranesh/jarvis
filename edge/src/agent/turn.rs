//! One chat turn — `server/chat_runtime.py:_run_agent_task` and the loop in
//! `core/agent_loop.py`, for the turns the edge serves.
//!
//! The prompt goes into the thread and the plan is reset; then model step,
//! tool batch, repeat, until the model answers without calling a tool. Every
//! message is written as it arrives — the reply before any of its tools run,
//! each result once it and the calls before it are done — so a turn handed to
//! Python, or re-claimed after a crash, goes on from the rows.
//!
//! A history that outgrows the model's threshold is summarized before the
//! call (`summarize.rs`). A step that needs Python (`prompt::NeedsPython`,
//! `tools::Plan::Python`) hands the turn over with what it carried
//! (`Outcome::HandOver`); Python runs the recorded calls and goes on.

use std::sync::Arc;

use serde_json::{Value, json};
use sqlx::SqlitePool;
use uuid::Uuid;

use super::automation::{self, Spec};
use super::board;
use super::events::Emitter;
use super::queue::Job;
use super::summarize::{self, Summarizer};
use super::thread::Thread;
use super::tools::{self, Native, Plan, Policy, Step};
use super::{Agent, Outcome, prompt};
use crate::budget::{Budget, Limits};
use crate::gql::codec::now_stored;
use crate::llm::perf::PerfTracker;
use crate::llm::shape::{self, Layout};
use crate::llm::transcript::{Content, Message, Part, Role, ToolCall, Typed};
use crate::llm::{self, Delta, Endpoints, Request};
use crate::pyjson;
use crate::runs::Run;

/// `recursion_limit` for a chat turn: steps, so about 50 model calls.
const RECURSION_LIMIT: u32 = 100;

/// `user_message_id`: the prompt's thread id, derived from the task so a
/// re-claimed turn replaces its prompt rather than adding a second.
pub fn user_message_id(task_id: &str) -> String {
    Uuid::new_v5(&Uuid::NAMESPACE_URL, format!("jarvis-turn:{task_id}").as_bytes()).to_string()
}

/// How a turn stopped short of an answer.
enum Stop {
    /// Stopped by a human, or by its budget.
    Cancelled,
    /// `RecursionLimitReached`.
    Limit,
    /// Something only Python can do next; the turn goes over.
    Python(String),
    /// The model call or a tool failed.
    Failed(String),
}

/// What a turn is for: where its prompt comes from and where its end goes.
pub enum Kind {
    /// A chat turn: the user's message, answered on its Message row.
    Chat,
    /// A prompt or monitor automation's run (`automation.rs`).
    Automation(Spec),
    /// A board task's run (`board.rs`), with the board tools bound.
    Board(board::Spec),
}

impl Kind {
    fn name(&self) -> &'static str {
        match self {
            Kind::Chat => "chat",
            Kind::Automation(_) => "automation",
            Kind::Board(_) => "board_task",
        }
    }
}

pub struct Turn<'a> {
    agent: &'a Agent,
    run: Arc<Run>,
    kind: Kind,
    task_id: String,
    /// The agent thread — and, for chat, the conversation.
    thread_id: String,
    /// The conversation `run_cell`'s SDK is scoped to: `ToolContext.conversation_id`,
    /// which only chat sets.
    conversation: Option<String>,
    model: String,
    query: String,
    project_id: Option<String>,
    ephemeral: bool,
    /// This user message's retrieved context, by the message's id — the
    /// steps of one request reuse it, as Python's retrieval cache does.
    retrieved: Option<(Option<String>, Vec<crate::llm::shape::Segment>)>,
    attachments: bool,
    cancel_requested: bool,
    started: chrono::DateTime<chrono::Utc>,
    events: Emitter,
    budget: Budget,
    perf: PerfTracker,
    /// The turn's streamed text: the message it finishes with.
    text: String,
    input_tokens: i64,
    output_tokens: i64,
    has_usage: bool,
    steps: u32,
}

impl<'a> Turn<'a> {
    pub fn chat(agent: &'a Agent, job: &Job, run: Arc<Run>) -> Option<Self> {
        let payload = &job.payload;
        let conversation_id = payload["conv_id"].as_str()?.to_string();
        let mut turn = Self::new(agent, job, run, Kind::Chat, conversation_id.clone(), payload["model"].as_str()?, payload["query"].as_str()?);
        turn.conversation = Some(conversation_id);
        turn.attachments = payload["attachments"].as_array().is_some_and(|a| !a.is_empty());
        Some(turn)
    }

    /// An automation's run: its own thread or its conversation's, steps
    /// announced but not written as rows, as `_execute_prompt_type` runs it.
    pub fn automation(agent: &'a Agent, job: &Job, run: Arc<Run>, spec: Spec) -> Self {
        let (thread, model, prompt) = (spec.thread_id(), spec.model.clone(), spec.prompt());
        let mut turn = Self::new(agent, job, run, Kind::Automation(spec), thread, &model, &prompt);
        turn.events = turn.events.without_rows();
        turn
    }

    /// A board task's run: its conversation's thread, which `run_cell`'s SDK
    /// is scoped to too; steps announced, not written.
    pub fn board(agent: &'a Agent, job: &Job, run: Arc<Run>, spec: board::Spec) -> Self {
        let (thread, model, prompt) = (board::conversation_id(&spec.task_id), spec.model.clone(), spec.prompt.clone());
        let mut turn = Self::new(agent, job, run, Kind::Board(spec), thread.clone(), &model, &prompt);
        turn.conversation = Some(thread);
        turn.events = turn.events.without_rows();
        turn
    }

    fn new(agent: &'a Agent, job: &Job, run: Arc<Run>, kind: Kind, thread_id: String, model: &str, query: &str) -> Self {
        Turn {
            agent,
            events: Emitter::new(run.clone(), agent.pool.clone(), &job.id, &thread_id),
            run,
            budget: Budget::new(Limits::for_kind(kind.name())),
            kind,
            task_id: job.id.clone(),
            model: model.to_string(),
            query: query.to_string(),
            thread_id,
            conversation: None,
            project_id: None,
            ephemeral: false,
            retrieved: None,
            attachments: false,
            cancel_requested: job.cancel_requested,
            started: parse_stamp(&job.created_at),
            perf: PerfTracker::default(),
            text: String::new(),
            input_tokens: 0,
            output_tokens: 0,
            has_usage: false,
            steps: 0,
        }
    }

    fn pool(&self) -> &SqlitePool {
        &self.agent.pool
    }

    pub async fn run(mut self) -> Outcome {
        if self.attachments {
            return Outcome::HandOver(None);
        }
        if matches!(self.kind, Kind::Chat) {
            let (project_id, ephemeral) = self.scope().await;
            self.project_id = project_id;
            self.ephemeral = ephemeral;
        }
        let mut thread = match Thread::load(self.pool(), &self.thread_id).await {
            Ok(t) => t,
            Err(e) => {
                tracing::warn!("agent: {e}; handing the turn to Python");
                return Outcome::HandOver(None);
            }
        };
        // A conversation older than the transcript tables is converted from
        // checkpoints.db by Python first.
        if !thread.exists && self.has_history().await {
            return Outcome::HandOver(None);
        }
        if self.cancel_requested {
            self.run.update(|st| st.fields.cancelled = true);
        }

        // The prompt — and for chat, the plan reset, live subscribers first,
        // as Python. An automation's thread keeps its plan between runs.
        // The prompt's id is derived from the run, so a re-claimed run
        // replaces it (Python gives an automation's a fresh one).
        let prompt = Message { id: Some(user_message_id(&self.task_id)), ..Message::new(Role::User, Content::Text(self.query.clone())) };
        let wrote = match &self.kind {
            Kind::Chat => {
                self.events.emit("todos_updated", &json!({"todos": [], "source": "main"}));
                let wrote = async {
                    thread.set_todos(self.pool(), vec![]).await?;
                    thread.apply(self.pool(), vec![prompt]).await
                };
                wrote.await
            }
            Kind::Automation(spec) => {
                let wrote = async {
                    if spec.stateful {
                        automation::begin_conversation(self.pool(), spec).await.map_err(|e| e.to_string())?;
                    }
                    thread.apply(self.pool(), vec![prompt]).await
                };
                wrote.await
            }
            Kind::Board(spec) => {
                let wrote = async {
                    board::begin_conversation(self.pool(), spec).await.map_err(|e| e.to_string())?;
                    thread.apply(self.pool(), vec![prompt]).await
                };
                wrote.await
            }
        };
        let result = match wrote {
            Ok(()) => self.turn(&mut thread).await,
            Err(e) => Err(Stop::Failed(e)),
        };
        if let Err(Stop::Python(why)) = &result {
            tracing::info!("agent: run {} goes to Python: {why}", self.task_id);
            return self.hand_over();
        }
        match self.kind {
            Kind::Automation(_) => return self.finish_automation(result).await,
            Kind::Board(_) => return self.finish_board(result).await,
            Kind::Chat => {}
        }
        match result {
            Ok(()) => self.finish_done(false).await,
            Err(Stop::Limit) => self.finish_done(true).await,
            Err(Stop::Cancelled) => self.finish_stopped().await,
            Err(Stop::Python(_)) => unreachable!("handed over above"),
            Err(Stop::Failed(e)) => self.finish_failed(e).await,
        }
    }

    /// The job goes to Python, carrying what the run did here.
    fn hand_over(&mut self) -> Outcome {
        self.events.flush();
        Outcome::HandOver(Some(json!({
            "text": self.text,
            "step_seq": self.events.step_seq,
            "steps": self.steps,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "llm_calls": self.budget.llm_calls,
                "tool_calls": self.budget.tool_calls,
            },
        })))
    }

    // ── the loop ────────────────────────────────────────────────────────────

    async fn turn(&mut self, thread: &mut Thread) -> Result<(), Stop> {
        let policy = Policy::load(self.pool()).await;
        let bound = tools::bound_for(&policy, matches!(self.kind, Kind::Board(_)));
        loop {
            self.take_step()?;
            let reply = self.model_step(thread, &bound).await?;
            if reply.tool_calls.is_empty() {
                return Ok(());
            }
            // Nothing runs before its batch is known to be the edge's.
            let steps = match tools::plan(&reply.tool_calls, &bound, &policy) {
                Plan::Edge(steps) => steps,
                Plan::Python(why) => return Err(Stop::Python(why)),
            };
            self.take_step()?;
            self.tool_step(thread, &reply.tool_calls, steps).await?;
        }
    }

    fn take_step(&mut self) -> Result<(), Stop> {
        if self.cancelled() {
            return Err(Stop::Cancelled);
        }
        if self.steps >= RECURSION_LIMIT {
            return Err(Stop::Limit);
        }
        self.steps += 1;
        Ok(())
    }

    fn cancelled(&self) -> bool {
        self.run.fields().cancelled
    }


    async fn model_step(&mut self, thread: &mut Thread, bound: &[llm::Tool]) -> Result<Message, Stop> {
        // Mid-run messages are a chat's; nothing queues behind other runs.
        let queued = match self.kind {
            Kind::Chat => self.drain_queued().await.map_err(Stop::Failed)?,
            Kind::Automation(_) | Kind::Board(_) => vec![],
        };
        let mut history = thread.messages.clone();
        history.extend(queued.iter().cloned());
        let query = latest_user_text(&history);
        let message_id = history.iter().rev().find(|m| m.role == Role::User).and_then(|m| m.id.clone());

        let retrieved = match &self.retrieved {
            Some((id, parts)) if *id == message_id => Ok(parts.clone()),
            _ => prompt::retrieved(self.pool(), &self.agent.http, &query, &self.thread_id).await,
        };
        let context = match retrieved {
            Ok(parts) => {
                self.retrieved = Some((message_id, parts.clone()));
                prompt::build(self.pool(), &query, self.project_id.as_deref(), &thread.todos, &parts).await
            }
            Err(e) => Err(e),
        };
        let context = match context {
            Ok(c) => c,
            Err(prompt::NeedsPython(why)) => {
                // Delivered already: they go into the thread for Python to see.
                thread.apply(self.pool(), queued).await.map_err(Stop::Failed)?;
                return Err(Stop::Python(why.into()));
            }
        };

        let provider = self.model.split_once(':').map_or("", |(p, _)| p);
        let ends = Endpoints {
            compatible: crate::catalog::endpoints(self.pool()).await.unwrap_or_default(),
            ..Endpoints::from_env()
        };
        // The history counted from the last call's usage, less this estimate
        // of the rest of the request — except on Ollama, whose input count
        // leaves out a KV-cached prefix.
        let overhead = (provider != "ollama").then(|| {
            let chars = context.system.chars().count()
                + context.segments.iter().map(|s| s.content.chars().count()).sum::<usize>()
                + context.volatile.chars().count();
            summarize::schema_tokens(bound) + (chars / 4) as i64
        });
        let threshold = compact_threshold(self.pool(), &self.model).await;
        let summarizer = Summarizer { http: &self.agent.http, ends: &ends, model: &self.model, blobs: &thread.blobs };
        let compaction = tokio::select! {
            c = summarize::maybe_compact(&summarizer, &history, threshold, overhead) => c,
            () = until_stopped(self.run.clone()) => return Err(Stop::Cancelled),
        };
        if let Some((text, evicted)) = &compaction.episode {
            // Awaited, so it can't race the next turn's retrieval. Losing one
            // only loses detail the running summary still outlines.
            if let Err(e) = summarize::record_episode(self.pool(), &self.agent.http, &self.thread_id, text, evicted).await {
                tracing::warn!("episode recording failed: {e}");
            }
        }

        let layout = Layout {
            system: &context.system,
            segments: &context.segments,
            volatile: &context.volatile,
            cache: honors_cache_control(&self.model),
            provider,
        };
        let shaped = shape::repair_orphan_tool_calls(shape::strip_historical_thinking(compaction.messages));
        let prompt = shape::build(&layout, shaped);
        let req = Request { model: &self.model, prompt: &prompt, tools: bound, blobs: &thread.blobs };

        let events = &mut self.events;
        let text = &mut self.text;
        let mut on_delta = |d: Delta| match d {
            Delta::Text(t) => {
                text.push_str(t);
                events.token(t);
            }
            Delta::Thinking(t) => events.thinking(t),
            Delta::ToolCall | Delta::Timings(_) => {}
        };
        let call = llm::complete(&self.agent.http, &ends, &req, &mut on_delta);
        let reply = tokio::select! {
            r = call => r,
            () = until_stopped(self.run.clone()) => return Err(Stop::Cancelled),
        };
        let reply = reply.map_err(|e| Stop::Failed(e.message))?;

        let usage = reply.message.usage.clone().unwrap_or_default();
        if usage.input.is_some() || usage.output.is_some() {
            self.has_usage = true;
            self.input_tokens += usage.input.unwrap_or(0);
            self.output_tokens += usage.output.unwrap_or(0);
        }
        let mut emitted = self.budget.record_llm(usage.input, usage.output);
        // Only a chat run tracks throughput (`start_run_callbacks(with_perf=…)`).
        if matches!(self.kind, Kind::Chat) {
            emitted.push(("perf_update", self.perf.record(reply.perf)));
        }
        emitted.extend(self.budget.check());
        for (event, data) in emitted {
            self.events.emit(event, &data);
        }
        self.sync_fields();

        let message = reply.message;
        // The compaction lands with the reply, as Python's step returns it.
        let (removed, mut update) = match compaction.update {
            Some((removed, summary)) => (removed, vec![summary]),
            None => (vec![], vec![]),
        };
        update.extend(queued);
        update.push(message.clone());
        thread.update(self.pool(), &removed, update).await.map_err(Stop::Failed)?;
        if self.cancelled() {
            return Err(Stop::Cancelled);
        }
        self.events.step("model_request", model_step_data(&message)).await.map_err(Stop::Failed)?;
        Ok(message)
    }

    async fn tool_step(&mut self, thread: &mut Thread, calls: &[ToolCall], steps: Vec<Step>) -> Result<(), Stop> {
        // Every gate is answered, in call order, before anything runs.
        let mut ready = Vec::with_capacity(steps.len());
        for (call, step) in calls.iter().zip(steps) {
            ready.push(match step {
                Step::Gated(native) => self.gate(call, native).await?,
                other => other,
            });
        }
        let mut results = Vec::with_capacity(calls.len());
        for (call, step) in calls.iter().zip(ready) {
            if self.cancelled() {
                return Err(Stop::Cancelled);
            }
            let (content, status) = match step {
                Step::Unknown(error) | Step::Denied(error) => (error, "error"),
                Step::Gated(_) => unreachable!("gates are answered first"),
                Step::Run(native) => {
                    self.budget.record_tool(1).into_iter().for_each(|(e, d)| self.events.emit(e, &d));
                    self.sync_fields();
                    let run = self.run.clone();
                    let ran = tokio::select! {
                        r = self.run_native(thread, native) => r,
                        () = until_stopped(run) => return Err(Stop::Cancelled),
                    };
                    (ran.map_err(Stop::Failed)?, "success")
                }
            };
            let result = Message {
                name: Some(call.name.clone()),
                tool_call_id: call.id.clone(),
                status: Some(status.into()),
                ..Message::new(Role::Tool, Content::Text(content))
            };
            thread.apply(self.pool(), vec![result.clone()]).await.map_err(Stop::Failed)?;
            results.push(result);
        }
        if self.cancelled() {
            return Err(Stop::Cancelled);
        }
        self.events.step("tools", tools_step_data(&results)).await.map_err(Stop::Failed)?;
        Ok(())
    }

    /// `make_tool_gate`'s wait for one call: the request recorded (and shown
    /// in a chat), then the row polled until a human answers or it times
    /// out. A stop while waiting stops the run.
    async fn gate(&mut self, call: &ToolCall, native: Native) -> Result<Step, Stop> {
        // `live_task_id(conversation_id)`: only a chat run is its
        // conversation's, so only a chat's request names its run and is shown.
        let task_id = matches!(self.kind, Kind::Chat).then(|| self.task_id.clone());
        let request = crate::approvals::create(
            self.pool(),
            &format!("bound:{}", call.name),
            &call.name,
            &call.args,
            self.conversation.as_deref(),
            task_id.as_deref(),
        )
        .await
        .map_err(|e| Stop::Failed(e.to_string()))?;
        if task_id.is_some() {
            self.events.emit("approval_request", &request.event);
        }
        tracing::info!("tool gate: waiting on approval {} for {}", request.id, call.name);
        let run = self.run.clone();
        let outcome = tokio::select! {
            o = crate::approvals::wait(self.pool(), &request.id, crate::approvals::gate_timeout()) => o,
            () = until_stopped(run) => return Err(Stop::Cancelled),
        };
        let (approved, answer) = match outcome.map_err(|e| Stop::Failed(e.to_string()))? {
            crate::approvals::Outcome::Answered { approved, answer } => (approved, answer),
            crate::approvals::Outcome::TimedOut => {
                if task_id.is_some() {
                    self.events.emit("approval_resolved", &crate::approvals::resolved_event(&call.name, false, "timed out"));
                }
                (false, "timed out".to_string())
            }
        };
        tracing::info!("tool gate: {} {} ({})", call.name, if approved { "approved" } else { "denied" }, request.id);
        Ok(if approved { Step::Run(native) } else { Step::Denied(crate::approvals::denial_message(&call.name, &answer)) })
    }

    /// One of the edge's tools; an `Err` fails the run, as a tool that raises
    /// does in Python.
    async fn run_native(&mut self, thread: &mut Thread, native: Native) -> Result<String, String> {
        match native {
            Native::RunCell { code } => {
                let cell = crate::kernels::Cell {
                    code: &code,
                    timeout: tools::CELL_TIMEOUT,
                    conversation_id: self.conversation.as_deref(),
                    project_id: self.project_id.as_deref(),
                };
                self.agent.kernels.run(&self.thread_id, &cell).await
            }
            Native::WriteTodos { todos } => {
                let items: Vec<Value> = todos.iter().map(|t| json!({"text": t, "status": "pending"})).collect();
                self.events.emit("todos_updated", &json!({"todos": items, "source": "main"}));
                let merged = tools::reduce_todos(&thread.todos, &items);
                thread.set_todos(self.pool(), merged).await?;
                Ok(tools::todos_written(items.len()))
            }
            Native::Remember { text, kind } => Ok(self.remember(&text, &kind).await),
            Native::WriteArtifact { title, content, file_path, artifact_id } => {
                let cwd = crate::config::app_dir();
                let scope = super::artifacts::Scope {
                    pool: self.pool(),
                    dir: &self.agent.artifacts_dir,
                    cwd: &cwd,
                    conversation_id: self.conversation.as_deref(),
                    // `message_id == task_id`: the chat reply the card sits under.
                    message_id: matches!(self.kind, Kind::Chat).then_some(self.task_id.as_str()),
                };
                let written = super::artifacts::write(
                    &scope,
                    &title,
                    content.as_deref(),
                    file_path.as_deref(),
                    artifact_id.as_deref(),
                )
                .await?;
                if let Some(event) = written.event {
                    self.events.emit("artifact", &event);
                }
                Ok(written.answer)
            }
            Native::CompleteTask { summary, metadata } => match &self.kind {
                Kind::Board(spec) => board::complete(self.pool(), spec, &summary, metadata.as_deref()).await,
                _ => Ok("Error: complete_task is only available while executing a board task.".into()),
            },
            Native::BlockTask { reason, needs_input } => match &self.kind {
                Kind::Board(spec) => board::block(self.pool(), spec, &reason, needs_input).await,
                _ => Ok("Error: block_task is only available while executing a board task.".into()),
            },
            Native::SetTodoStatus { index, status } => match tools::set_status(&thread.todos, index, &status) {
                Ok((todos, answer)) => {
                    self.events.emit("todos_updated", &json!({"todos": todos, "source": "main"}));
                    let merged = tools::reduce_todos(&thread.todos, &todos);
                    thread.set_todos(self.pool(), merged).await?;
                    Ok(answer)
                }
                Err(answer) => Ok(answer),
            },
        }
    }

    /// `remember`: a durable fact, merged into a near-duplicate. Its answer
    /// is the model's, failures included.
    async fn remember(&self, text: &str, kind: &str) -> String {
        if text.trim().is_empty() {
            return "Nothing to remember (empty text).".into();
        }
        if self.ephemeral {
            return "Skipped: this is an incognito chat, nothing is saved to long-term memory.".into();
        }
        let kind = if kind == "core" || kind == "fact" { kind } else { "fact" };
        match super::retrieve::upsert_memory(self.pool(), &self.agent.http, text, kind).await {
            Ok(_) => format!("Remembered ({kind})."),
            Err(e) => {
                tracing::warn!("remember failed: {e}");
                format!("Could not save memory: {e}")
            }
        }
    }

    /// The mid-run queue: messages the user sent while this run was going,
    /// delivered before the next model call (`_drain_queued_input`).
    async fn drain_queued(&mut self) -> Result<Vec<Message>, String> {
        let rows: Vec<(String, String)> = sqlx::query_as(
            "SELECT id, content FROM messages WHERE conversation_id = ? AND role = 'user' AND status = 'queued' \
             ORDER BY created_at ASC",
        )
        .bind(&self.thread_id)
        .fetch_all(self.pool())
        .await
        .map_err(|e| e.to_string())?;
        if rows.is_empty() {
            return Ok(vec![]);
        }
        for (id, _) in &rows {
            if let Err(e) = sqlx::query("UPDATE messages SET status = 'delivered' WHERE id = ?").bind(id).execute(self.pool()).await {
                // The message is going to the model regardless; left queued,
                // the next run would deliver it again — recoverable.
                tracing::warn!("queued message status flip failed: {e}");
            }
        }
        let ids: Vec<&str> = rows.iter().map(|(id, _)| id.as_str()).collect();
        self.events.emit("queued_consumed", &json!({"message_ids": ids}));
        tracing::info!("delivered {} queued message(s) to run {}", rows.len(), self.task_id);
        Ok(rows
            .into_iter()
            .map(|(id, text)| Message { id: Some(id), ..Message::new(Role::User, Content::Text(text)) })
            .collect())
    }

    fn sync_fields(&self) {
        let b = &self.budget;
        let (input, output, llm_calls, tool_calls) = (b.input_tokens, b.output_tokens, b.llm_calls, b.tool_calls);
        let exceeded = b.exceeded().map(str::to_string);
        self.run.update(|st| {
            let f = &mut st.fields;
            f.input_tokens = input;
            f.output_tokens = output;
            f.total_tokens = input + output;
            f.llm_calls = llm_calls;
            f.tool_calls = tool_calls;
            if let Some(reason) = exceeded {
                f.budget_exceeded = true;
                f.budget_reason = Some(reason);
                f.cancelled = true;
            }
        });
    }

    // ── context ─────────────────────────────────────────────────────────────

    /// `_resolve_conv_scope`: the project and incognito flag, read at the start.
    async fn scope(&self) -> (Option<String>, bool) {
        let row: Option<(Option<String>, Option<bool>)> =
            sqlx::query_as("SELECT project_id, ephemeral FROM conversations WHERE id = ?")
                .bind(&self.thread_id)
                .fetch_optional(self.pool())
                .await
                .ok()
                .flatten();
        row.map_or((None, false), |(p, e)| (p, e.unwrap_or(false)))
    }

    async fn has_history(&self) -> bool {
        let earlier: Option<i64> = sqlx::query_scalar(
            "SELECT 1 FROM messages WHERE conversation_id = ? AND role = 'assistant' AND id != ? LIMIT 1",
        )
        .bind(&self.thread_id)
        .bind(&self.task_id)
        .fetch_optional(self.pool())
        .await
        .ok()
        .flatten();
        earlier.is_some()
    }

    // ── the end ─────────────────────────────────────────────────────────────

    async fn finish_done(mut self, limit: bool) -> Outcome {
        self.events.flush();
        let mut message = self.text.clone();
        if limit && message.is_empty() {
            message = "(agent reached iteration limit)".into();
        }
        if self.budget.exceeded().is_some() {
            return self.finish_stopped().await;
        }
        self.finalize(&message, "done").await;
        self.events.emit("done", &json!({"message": message, "conversation_id": self.thread_id}));
        if !limit {
            crate::gql::start::redispatch_queued(self.pool(), &self.agent.runs, &self.thread_id, &self.model).await;
        }
        self.finished("done");
        Outcome::Finished
    }

    async fn finish_stopped(mut self) -> Outcome {
        self.events.flush();
        let mut message = self.text.clone();
        if let Some(reason) = self.budget.exceeded().map(str::to_string) {
            message = if message.trim().is_empty() {
                format!("Stopped: budget exceeded ({reason})")
            } else {
                format!("{message}\n\n[Stopped: budget exceeded ({reason})]")
            };
            self.finalize(&message, "stopped").await;
            self.events.emit(
                "budget_exceeded",
                &json!({"reason": reason, "message": message, "conversation_id": self.thread_id}),
            );
        } else {
            self.finalize(&message, "stopped").await;
        }
        self.events.emit("stopped", &json!({"message": message, "conversation_id": self.thread_id}));
        self.finished("stopped");
        Outcome::Finished
    }

    async fn finish_failed(mut self, error: String) -> Outcome {
        self.events.flush();
        tracing::warn!("agent: run {} failed: {error}", self.task_id);
        let message =
            if self.text.is_empty() { format!("The run failed before completing: {error}") } else { self.text.clone() };
        self.events.emit("error", &json!({"error": error}));
        self.finalize(&message, "error").await;
        self.finished("error");
        Outcome::Finished
    }

    /// `_run_automation_inner` after the agent: the run's row, its reply in a
    /// stateful conversation, notifications, and the closing event. A failed
    /// run (the step limit included — Python raises it) records the error; a
    /// spent budget is an error too; a stop keeps what was said; a monitor
    /// that saw nothing new finishes `no_change`, silently.
    async fn finish_automation(mut self, result: Result<(), Stop>) -> Outcome {
        self.events.flush();
        let Kind::Automation(spec) = &self.kind else { unreachable!("an automation's turn") };
        let output = self.text.clone();
        let end = match result {
            Err(Stop::Failed(e)) => automation::End::Failed(e),
            Err(Stop::Limit) => automation::End::Failed(format!("agent 'main' reached its limit of {RECURSION_LIMIT} steps")),
            _ => match self.budget.exceeded() {
                Some(reason) => automation::End::Budget { output, reason: reason.to_string() },
                None if self.cancelled() => automation::End::Stopped(Some(output)),
                None => automation::End::Done(output),
            },
        };
        if let automation::End::Failed(e) = &end {
            tracing::warn!("agent: run {} failed: {e}", self.task_id);
        }
        let status = automation::finish(&self.agent.pool, &self.run, spec, end).await;
        self.finished(status);
        Outcome::Finished
    }

    /// `_run_board_task_inner` after the agent, then the dispatch pass a
    /// finished task may unblock children for.
    async fn finish_board(mut self, result: Result<(), Stop>) -> Outcome {
        self.events.flush();
        let Kind::Board(spec) = &self.kind else { unreachable!("a board task's turn") };
        let output = self.text.clone();
        let end = match result {
            Err(Stop::Failed(e)) => automation::End::Failed(e),
            Err(Stop::Limit) => automation::End::Failed(format!("agent 'main' reached its limit of {RECURSION_LIMIT} steps")),
            _ => match self.budget.exceeded() {
                Some(reason) => automation::End::Budget { output, reason: reason.to_string() },
                None if self.cancelled() => automation::End::Stopped(Some(output)),
                None => automation::End::Done(output),
            },
        };
        if let automation::End::Failed(e) = &end {
            tracing::warn!("agent: run {} failed: {e}", self.task_id);
        }
        let (status, done) = board::finish(&self.agent.pool, &self.run, spec, end).await;
        self.finished(&status);
        if done {
            self.agent.dispatch();
        }
        Outcome::Finished
    }

    /// `_finalize_message`: the assistant row's content, status and usage,
    /// and any approval still open on the run closed as unanswerable.
    async fn finalize(&self, content: &str, status: &str) {
        let elapsed_ms = (chrono::Utc::now() - self.started).num_microseconds().unwrap_or(0) as f64 / 1000.0;
        let duration_ms = (elapsed_ms * 10.0).round() / 10.0;
        let perf = self.perf.message_perf().unwrap_or(Value::Null);
        let (input, output) = if self.has_usage { (Some(self.input_tokens), Some(self.output_tokens)) } else { (None, None) };
        let now = now_stored();
        let written = async {
            let mut tx = crate::db::write_tx(self.pool()).await?;
            sqlx::query(
                "UPDATE messages SET content = ?, status = ?, input_tokens = ?, output_tokens = ?, duration_ms = ?, \
                 ttft_ms = COALESCE(?, ttft_ms), llm_ms = COALESCE(?, llm_ms), prefill_tps = COALESCE(?, prefill_tps), \
                 eval_tps = COALESCE(?, eval_tps) WHERE id = ?",
            )
            .bind(content)
            .bind(status)
            .bind(input)
            .bind(output)
            .bind(duration_ms)
            .bind(perf["ttft_ms"].as_f64())
            .bind(perf["llm_ms"].as_f64())
            .bind(perf["prefill_tps"].as_f64())
            .bind(perf["eval_tps"].as_f64())
            .bind(&self.task_id)
            .execute(&mut *tx)
            .await?;
            sqlx::query(
                "UPDATE approvals SET status = 'expired', resolved_at = ?, result = ?, updated_at = ? \
                 WHERE status = 'pending' AND task_id = ?",
            )
            .bind(&now)
            .bind(format!("The run finished ({status}) before this was answered."))
            .bind(&now)
            .bind(&self.task_id)
            .execute(&mut *tx)
            .await?;
            tx.commit().await
        };
        if let Err(e) = written.await {
            tracing::error!("agent: finalizing run {}: {e}", self.task_id);
        }
    }

    /// `finish_task_state`: done in the mirror; it leaves a little later.
    fn finished(&self, status: &str) {
        tracing::info!(
            "task complete: kind={} task={} parent={} status={status} duration_ms={}",
            self.kind.name(),
            self.task_id,
            self.thread_id,
            (chrono::Utc::now() - self.started).num_milliseconds()
        );
        self.run.update(|st| st.fields.done = true);
        self.agent.runs.retire(&self.task_id);
    }
}

// ── helpers ─────────────────────────────────────────────────────────────────

/// Resolves once the run is stopped — by a human, or by its budget.
pub(super) async fn until_stopped(run: Arc<Run>) {
    let mut watch = run.subscribe();
    while !run.fields().cancelled {
        if watch.changed().await.is_err() {
            std::future::pending::<()>().await;
        }
    }
}

/// A stored timestamp (`YYYY-MM-DD HH:MM:SS[.ffffff]`, UTC) — now if odd.
fn parse_stamp(stored: &str) -> chrono::DateTime<chrono::Utc> {
    chrono::NaiveDateTime::parse_from_str(stored, "%Y-%m-%d %H:%M:%S%.f")
        .map(|t| t.and_utc())
        .unwrap_or_else(|_| chrono::Utc::now())
}

/// `_latest_user_text`: the newest user message's text, stripped.
fn latest_user_text(history: &[Message]) -> String {
    let Some(m) = history.iter().rev().find(|m| m.role == Role::User) else { return String::new() };
    let text = match &m.content {
        Content::Text(s) => s.clone(),
        Content::Parts(parts) => parts
            .iter()
            .filter_map(|p| match p {
                Part::Typed(Typed::Text { text, .. }) => Some(text.as_str()),
                Part::Typed(Typed::Thinking { thinking, .. }) => Some(thinking.as_str()),
                _ => None,
            })
            .collect(),
    };
    text.trim().to_string()
}

/// `honors_cache_control` with the default providers: of the edge's, only an
/// OpenRouter route to Anthropic.
fn honors_cache_control(model: &str) -> bool {
    match model.split_once(':') {
        Some(("openrouter", name)) => name.to_lowercase().trim_start_matches('~').starts_with("anthropic/"),
        _ => false,
    }
}

/// `compact_threshold`: `JARVIS_COMPACT_TOKEN_THRESHOLD`, else 40% of the
/// model's window clamped to [12k, 200k], else 80k.
async fn compact_threshold(pool: &SqlitePool, model: &str) -> i64 {
    for var in ["JARVIS_COMPACT_TOKEN_THRESHOLD", "JARVIS_SUMMARIZE_TOKEN_THRESHOLD"] {
        if let Some(n) = std::env::var(var).ok().filter(|v| !v.is_empty()) {
            if let Ok(n) = n.trim().parse() {
                return n;
            }
            break;
        }
    }
    let window = match crate::catalog::catalog(pool).await {
        Ok(Ok((_, specs))) => specs.into_iter().find(|s| s.id == model).and_then(|s| s.context_window),
        _ => None,
    };
    match window {
        Some(w) if w > 0 => ((w as f64 * 0.4) as i64).clamp(12_000, 200_000),
        _ => 80_000,
    }
}

/// `_extract_step_data` for a model step: its calls, else its text.
fn model_step_data(m: &Message) -> String {
    if !m.tool_calls.is_empty() {
        let calls: Vec<Value> = m.tool_calls.iter().map(|c| json!({"name": c.name, "args": c.args})).collect();
        return pyjson::dumps(&json!({"tool_calls": calls}));
    }
    let text = match &m.content {
        Content::Text(s) => s.clone(),
        // Text blocks only: a bare string in a list isn't a block.
        Content::Parts(parts) => parts
            .iter()
            .filter_map(|p| match p {
                Part::Typed(Typed::Text { text, .. }) => Some(text.as_str()),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join(" ")
            .trim()
            .to_string(),
    };
    if text.is_empty() {
        // Python's is the `repr` of the step's messages, metadata and all;
        // this is its shape for a reply that carried none.
        let raw = "{'messages': [AIMessage(content='', additional_kwargs={}, response_metadata={}, tool_calls=[], \
                   invalid_tool_calls=[])]}";
        return pyjson::dumps(&json!({"raw": raw}));
    }
    pyjson::dumps(&json!({"text": text.chars().take(400).collect::<String>()}))
}

/// `_extract_step_data` for a tool batch: one result, or the list.
fn tools_step_data(results: &[Message]) -> String {
    let entries: Vec<Value> = results
        .iter()
        .map(|m| {
            let output = match &m.content {
                Content::Text(s) => s.chars().take(400).collect::<String>(),
                other => serde_json::to_string(other).unwrap_or_default().chars().take(400).collect(),
            };
            json!({"tool": m.name.clone().unwrap_or_default(), "output": output})
        })
        .collect();
    pyjson::dumps(&if entries.len() == 1 { entries[0].clone() } else { Value::Array(entries) })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn user_message_id_is_python_uuid5() {
        // str(uuid5(NAMESPACE_URL, "jarvis-turn:t1"))
        assert_eq!(user_message_id("t1"), "d9b23af6-71ae-5e66-bbe9-1a74999cd142");
    }

    #[test]
    fn step_data_matches_extract_step_data() {
        let reply: Message = serde_json::from_value(json!({"v": 1, "role": "assistant", "content": [
            {"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "Hé "}, {"type": "text", "text": "there"}]}))
        .unwrap();
        assert_eq!(model_step_data(&reply), "{\"text\": \"H\\u00e9  there\"}");
        let calls: Message = serde_json::from_value(json!({"v": 1, "role": "assistant", "content": "",
            "tool_calls": [{"id": "c", "name": "run_cell", "args": {"code": "1"}}]}))
        .unwrap();
        assert_eq!(model_step_data(&calls), r#"{"tool_calls": [{"name": "run_cell", "args": {"code": "1"}}]}"#);
        let result = |t: &str| Message { name: Some(t.into()), ..Message::new(Role::Tool, Content::Text("ok".into())) };
        assert_eq!(tools_step_data(&[result("a")]), r#"{"tool": "a", "output": "ok"}"#);
        assert_eq!(tools_step_data(&[result("a"), result("b")]), r#"[{"tool": "a", "output": "ok"}, {"tool": "b", "output": "ok"}]"#);
    }
}
