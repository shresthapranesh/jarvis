//! `resolveApproval` (`server/graphql/mutations/approval.py`, `core/approvals.py:resolve`)
//! and `requestToolApproval` (`server/graphql/mutations/tool.py`) — a change
//! to either is made in both.
//!
//! Every answer lives in a row: a tool gate (the waiter polls the row), a
//! board task's question, a paused workflow node, and a deferred action — a
//! denial, or an approved delete of a workflow, automation or skill, or MCP
//! call (`ACTIONS`' executors). `gate_action` records the deferred ones.

use std::sync::Arc;

use async_graphql::{Context, Object, Result, SimpleObject};
use serde_json::{Value, json};
use sqlx::SqlitePool;

use super::codec::now_stored;
use super::router::Caller;
use super::{EdgeData, RequestFrom};
use crate::approvals;
use crate::runs::{Registry, Run};

#[derive(SimpleObject)]
pub struct ResolveApprovalPayload {
    id: String,
    status: String,
    /// What resolving it produced.
    result: Option<String>,
}

/// The durable request a gated SDK call blocks on.
#[derive(SimpleObject)]
pub struct ToolApprovalRequest {
    id: String,
    status: String,
}

#[derive(sqlx::FromRow)]
struct Row {
    status: String,
    kind: String,
    source: String,
    action: Option<String>,
    action_payload: Option<String>,
    board_task_id: Option<String>,
    task_id: Option<String>,
    interrupt_id: Option<String>,
    parent_id: Option<String>,
    tool: Option<String>,
    result: Option<String>,
}

async fn row(pool: &SqlitePool, id: &str) -> sqlx::Result<Option<Row>> {
    sqlx::query_as(
        "SELECT status, kind, source, action, action_payload, board_task_id, task_id, interrupt_id, parent_id, tool, result FROM approvals WHERE id = ?",
    )
    .bind(id)
    .fetch_optional(pool)
    .await
}

/// `live_task_id`: the first unfinished run whose parent is the conversation.
pub(super) fn live_run(registry: &Registry, conversation: Option<&str>) -> Option<Arc<Run>> {
    let conversation = conversation?;
    registry.all().into_iter().find(|run| !run.fields().done && run.meta.parent_id.as_deref() == Some(conversation))
}

/// `_emit_to_run`'s target: the row's run if it names one, else the
/// conversation's live run.
fn run_of(registry: &Registry, task_id: Option<&str>, parent_id: Option<&str>) -> Option<Arc<Run>> {
    match task_id.filter(|t| !t.is_empty()) {
        Some(task) => registry.get(task),
        None => live_run(registry, parent_id),
    }
}

/// `_emit_to_run`: onto a live run's stream.
fn emit(run: Option<&Arc<Run>>, event: &str, data: &Value) {
    if let Some(run) = run.filter(|r| !r.fields().done) {
        run.emit_local(event, data);
    }
}

// ── deferred actions ────────────────────────────────────────────────────────

/// `core/approvals.py:ACTIONS`' gated names, label and question.
const ACTIONS: &[&str] = &["delete_workflow", "delete_automation", "delete_skill", "call_mcp_tool"];

fn action_label(action: &str) -> &'static str {
    match action {
        "delete_workflow" => "Delete workflow",
        "delete_automation" => "Delete automation",
        "delete_skill" => "Delete skill",
        _ => "Call MCP tool",
    }
}

fn describe_action(action: &str, payload: &Value) -> String {
    let get = |key: &str| payload.get(key).filter(|v| crate::pyjson::truthy(v)).map(crate::pyjson::py_str);
    let named = |thing: &str, id_key: &str| {
        let name = get("name").or_else(|| get(id_key)).unwrap_or_else(|| "None".into());
        format!("Delete {thing} {name}? This cannot be undone.")
    };
    match action {
        "delete_workflow" => named("workflow", "workflow_id"),
        "delete_automation" => named("automation", "automation_id"),
        "delete_skill" => named("skill", "skill_id"),
        _ => {
            // `_describe_mcp_call`.
            let args = payload.get("args").filter(|a| crate::pyjson::truthy(a)).cloned().unwrap_or_else(|| json!({}));
            let mut rendered = crate::pyjson::dumps(&args);
            if rendered.chars().count() > 300 {
                rendered = rendered.chars().take(300).collect::<String>() + "…";
            }
            let field = |key: &str| payload.get(key).map_or_else(|| "None".into(), crate::pyjson::py_str);
            format!("Call MCP tool {}.{} with {rendered}?", field("server"), field("tool"))
        }
    }
}

/// `required_actions`: the ones `approval.required_actions` names — none
/// unless an operator opts in.
async fn required_actions(pool: &SqlitePool) -> sqlx::Result<Vec<&'static str>> {
    let Some(raw) = crate::catalog::setting(pool, "approval.required_actions").await? else { return Ok(vec![]) };
    let names: Vec<&str> = raw.split(',').map(crate::pystr::strip).filter(|p| !p.is_empty()).collect();
    if names.is_empty() || names.iter().all(|n| *n == "none") {
        return Ok(vec![]);
    }
    if names.iter().all(|n| *n == "all") {
        return Ok(ACTIONS.to_vec());
    }
    Ok(ACTIONS.iter().copied().filter(|a| names.contains(a)).collect())
}

/// `gate_action`: an agent's destructive call, recorded for a human instead
/// of performed when the action is gated. `Ok` means go ahead; otherwise the
/// error says what is now pending — an open duplicate's id rather than a new
/// row, so an agent retrying in a loop doesn't fill the inbox.
pub(super) async fn gate_action(ctx: &Context<'_>, action: &str, payload: Value) -> Result<()> {
    let pool = ctx.data::<SqlitePool>()?;
    if !required_actions(pool).await?.contains(&action) {
        return Ok(());
    }
    let open: Vec<(String, String, Option<String>)> = sqlx::query_as(
        "SELECT id, question, action_payload FROM approvals WHERE status = 'pending' AND action = ? \
         ORDER BY requested_at DESC LIMIT 200",
    )
    .bind(action)
    .fetch_all(pool)
    .await?;
    let duplicate = open.into_iter().find(|(_, _, p)| {
        p.as_deref().and_then(|p| serde_json::from_str::<Value>(p).ok()).is_some_and(|p| p == payload)
    });
    let (id, question) = match duplicate {
        Some((id, question, _)) => (id, question),
        None => {
            let parent = ctx.data::<RequestFrom>()?.conversation.clone();
            let (id, now, question) = (super::codec::new_id(), now_stored(), describe_action(action, &payload));
            let dumped = crate::pyjson::dumps(&payload);
            let args_json: String = dumped.chars().take(2000).collect();
            let label = action_label(action);
            sqlx::query(
                "INSERT INTO approvals (id, source, kind, status, question, label, tool, args_json, action, \
                 action_payload, parent_id, requested_at, updated_at) \
                 VALUES (?, 'chat', 'approval', 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            )
            .bind(&id)
            .bind(&question)
            .bind(label)
            .bind(action)
            .bind(&args_json)
            .bind(action)
            .bind(&dumped)
            .bind(&parent)
            .bind(&now)
            .bind(&now)
            .execute(pool)
            .await?;
            // `announce_request`: the conversation that asked is told it didn't happen.
            let shown = match serde_json::from_str::<Value>(&args_json) {
                Ok(v @ Value::Object(_)) => v,
                _ => json!({}),
            };
            let run = live_run(ctx.data::<Arc<Registry>>()?, parent.as_deref());
            emit(
                run.as_ref(),
                "approval_request",
                &json!({
                    "tool": action,
                    "reason": format!("{label} was recorded, not performed \u{2014} it runs only once you approve it."),
                    "args": shown,
                    "approval_id": id,
                    "deferred": true,
                }),
            );
            (id, question)
        }
    };
    Err(format!(
        "Approval required: {question} (approval id {id}). It is now pending in /approvals; the action runs only once a human approves it."
    )
    .into())
}

/// `resolve_approval_row`: out of `pending`, unless it was answered a moment
/// ago from elsewhere.
async fn close(pool: &SqlitePool, id: &str, status: &str, answer: &str, result: &str) -> Result<()> {
    let now = now_stored();
    let done = sqlx::query(
        "UPDATE approvals SET status = ?, answer = ?, result = ?, resolved_at = ?, updated_at = ? \
         WHERE id = ? AND status = 'pending'",
    )
    .bind(status)
    .bind(answer)
    .bind(result)
    .bind(&now)
    .bind(&now)
    .bind(id)
    .execute(pool)
    .await?;
    if done.rows_affected() == 0 {
        let now_status = row(pool, id).await?.map_or_else(|| "gone".into(), |r| r.status);
        return Err(format!("approval already {now_status}").into());
    }
    Ok(())
}

/// `core/approvals.py:ACTIONS[action].execute` for an approved deferred
/// action — run before the row is closed, so a failure leaves it pending
/// and answerable.
async fn execute(ctx: &Context<'_>, action: &str, payload: Option<&str>) -> Result<String> {
    let payload: Value = serde_json::from_str(payload.unwrap_or("{}"))
        .map_err(|e| format!("the approval's payload is not valid JSON: {e}"))?;
    let id = |key: &str| -> Result<String> {
        payload.get(key).and_then(Value::as_str).map(str::to_string).ok_or_else(|| format!("the approval's payload has no {key}").into())
    };
    let pool: &SqlitePool = ctx.data()?;
    let (deleted, gone) = match action {
        "delete_workflow" => (super::workflow::delete_workflow(pool, &id("workflow_id")?).await?, "Workflow"),
        "delete_automation" => {
            (super::automation::delete_automation(pool, &id("automation_id")?, ctx.data()?).await?, "Automation")
        }
        "delete_skill" => (super::settings_lists::delete_skill(pool, &id("skill_id")?).await?, "Skill"),
        "call_mcp_tool" => return super::mcp::execute_approved(&ctx.data::<EdgeData>()?.mcp, &payload).await,
        other => {
            return Err(format!("approval references unknown action {}", crate::pyjson::repr_str(other)).into());
        }
    };
    Ok(if deleted { "Deleted.".into() } else { format!("{gone} no longer exists.") })
}

#[derive(Default)]
pub struct ApprovalMutation;

#[Object]
impl ApprovalMutation {
    // Answer a pending approval. For an approve/deny gate, anything that
    // isn't clearly a yes is a no: a reply matching no keyword is usually a
    // question.
    async fn resolve_approval(&self, ctx: &Context<'_>, id: String, answer: String) -> Result<ResolveApprovalPayload> {
        let answer = answer.trim();
        if answer.is_empty() {
            return Err("answer must not be empty".into());
        }
        let (pool, registry) = (ctx.data::<SqlitePool>()?, ctx.data::<Arc<Registry>>()?);
        let row = row(pool, &id).await?.ok_or("approval not found")?;
        if row.status != "pending" {
            return Err(format!("approval already {}", row.status).into());
        }
        let approved = row.kind != "approval" || approvals::is_affirmative(answer) == Some(true);

        if let Some(action) = &row.action {
            let result = if approved {
                execute(ctx, action, row.action_payload.as_deref()).await?
            } else {
                "Not executed.".into()
            };
            let status = if approved { "approved" } else { "denied" };
            close(pool, &id, status, answer, &result).await?;
            return Ok(ResolveApprovalPayload { id, status: status.into(), result: Some(result) });
        }
        if row.source == approvals::GATE_SOURCE {
            let run = run_of(registry, row.task_id.as_deref(), row.parent_id.as_deref());
            let (status, result) =
                if approved { ("approved", "Released the waiting call.") } else { ("denied", "The call was not run.") };
            close(pool, &id, status, answer, result).await?;
            // The answer usually comes from the inbox; the chat showing the
            // prompt is told to stop showing it.
            emit(run.as_ref(), "approval_resolved", &approvals::resolved_event(row.tool.as_deref().unwrap_or(""), approved, answer));
            return Ok(ResolveApprovalPayload { id, status: status.into(), result: Some(result.into()) });
        }
        if let Some(task) = &row.board_task_id {
            super::board::answer_task(pool, ctx.data::<EdgeData>()?, task, answer).await?;
            let after = self::row(pool, &id).await?.ok_or("approval not found")?;
            return Ok(ResolveApprovalPayload { id, status: after.status, result: after.result });
        }
        // A workflow run: its paused node reads the answer off the row.
        if let Some(run) = row.task_id.as_deref().and_then(|t| registry.get(t)).filter(|r| !r.fields().done) {
            let status = if row.kind == "input" { "answered" } else if approved { "approved" } else { "denied" };
            close(pool, &id, status, answer, "Delivered to the run.").await?;
            run.emit_local("interrupt_resolved", &json!({"interrupt_id": row.interrupt_id}));
            return Ok(ResolveApprovalPayload { id, status: status.into(), result: Some("Delivered to the run.".into()) });
        }
        // The run is gone (restart, crash, or it moved on): say so instead of
        // reporting success for an answer nobody received.
        close(pool, &id, "expired", answer, "The run was no longer waiting; the answer was not delivered.").await?;
        Err("the run this approval belongs to is no longer waiting".into())
    }

    // The agent's side of a gate: the `jarvis` SDK, in a kernel, records the
    // request here and then polls the row. A human's click is an approval,
    // not a request for one.
    async fn request_tool_approval(
        &self,
        ctx: &Context<'_>,
        tool_key: String,
        tool: String,
        #[graphql(default_with = "\"{}\".to_string()")] args_json: String,
        conversation_id: Option<String>,
    ) -> Result<ToolApprovalRequest> {
        let from = ctx.data::<RequestFrom>()?;
        if from.caller != Caller::Agent {
            return Err("requestToolApproval is only for agent-initiated calls".into());
        }
        match tool_key.split_once(':') {
            Some(("bound" | "sdk" | "mcp", rest)) if !rest.is_empty() => {}
            _ => return Err(format!("unknown tool key {}", crate::pyjson::repr_str(&tool_key)).into()),
        }
        let args = if args_json.is_empty() {
            json!({})
        } else {
            serde_json::from_str::<Value>(&args_json).map_err(|e| format!("args_json is not valid JSON: {e}"))?
        };
        if !args.is_object() {
            return Err("args_json must be a JSON object".into());
        }
        let conversation = conversation_id.filter(|c| !c.is_empty()).or_else(|| from.conversation.clone());
        let registry = ctx.data::<Arc<Registry>>()?;
        let run = live_run(registry, conversation.as_deref());
        let request = approvals::create(
            ctx.data()?,
            &tool_key,
            &tool,
            &args,
            conversation.as_deref(),
            run.as_ref().map(|r| r.id.as_str()),
        )
        .await?;
        emit(run.as_ref(), "approval_request", &request.event);
        Ok(ToolApprovalRequest { id: request.id, status: "pending".into() })
    }
}
