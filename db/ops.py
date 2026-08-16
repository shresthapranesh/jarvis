from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import case, func, select, update
from sqlalchemy import text as sa_text  # `text` is a param name in several ops below
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import get_config
from db.models import Artifact, Automation, AutomationRun, BoardTask, BoardTaskLink, ConfigSetting, Conversation, Document, Job, Memory, Message, NotificationChannel, Project, Skill, Step, Workflow, WorkflowRun

logger = logging.getLogger(__name__)


async def create_conversation(
    session: AsyncSession, model: str, title: str | None, surface: str = "web",
    project_id: str | None = None, ephemeral: bool = False,
) -> Conversation:
    conv = Conversation(
        id=str(uuid4()), model=model, title=title, surface=surface,
        project_id=project_id, ephemeral=ephemeral,
    )
    session.add(conv)
    await session.commit()
    return conv


async def get_or_create_conversation(
    session: AsyncSession,
    conversation_id: str | None,
    model: str,
    title: str | None,
    surface: str = "web",
    project_id: str | None = None,
    ephemeral: bool = False,
) -> Conversation:
    if conversation_id:
        result = await session.get(Conversation, conversation_id)
        if result:
            if result.model != model:
                result.model = model
                await session.commit()
            return result
        conv = Conversation(
            id=conversation_id, model=model, title=title, surface=surface,
            project_id=project_id, ephemeral=ephemeral,
        )
        session.add(conv)
        await session.commit()
        return conv
    return await create_conversation(session, model, title, surface, project_id, ephemeral)


async def add_message(
    session: AsyncSession,
    conv_id: str,
    role: str,
    content: str,
    model: str | None = None,
    status: str = "done",
) -> Message:
    msg = Message(id=str(uuid4()), conversation_id=conv_id, role=role, content=content, model=model, status=status)
    session.add(msg)
    await session.commit()
    return msg


async def update_message_content(session: AsyncSession, message_id: str, content: str) -> None:
    msg = await session.get(Message, message_id)
    if msg:
        msg.content = content
        await session.commit()


async def update_message_status(session: AsyncSession, message_id: str, status: str) -> None:
    msg = await session.get(Message, message_id)
    if msg:
        msg.status = status
        await session.commit()


async def update_message_usage(
    session: AsyncSession,
    message_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    perf: dict[str, float | None] | None = None,
) -> None:
    """Record token usage and, when measured, throughput for one message.

    `perf` carries the keys produced by `PerfTracker.message_perf()`. Usage and
    throughput are written together because they come from the same run and a
    second round trip would just be a second commit on the same row.
    """
    msg = await session.get(Message, message_id)
    if msg:
        msg.input_tokens = input_tokens
        msg.output_tokens = output_tokens
        if perf:
            msg.ttft_ms = perf.get("ttft_ms")
            msg.llm_ms = perf.get("llm_ms")
            msg.prefill_tps = perf.get("prefill_tps")
            msg.eval_tps = perf.get("eval_tps")
        await session.commit()


async def add_step(
    session: AsyncSession,
    message_id: str,
    conv_id: str,
    node: str,
    source: str,
    data: str | None,
    seq: int,
    subagent: str | None = None,
) -> Step:
    step = Step(
        id=str(uuid4()),
        message_id=message_id,
        conversation_id=conv_id,
        node=node,
        source=source,
        subagent=subagent,
        data=data,
        seq=seq,
    )
    session.add(step)
    await session.commit()
    return step


async def add_steps(
    session: AsyncSession,
    message_id: str,
    conv_id: str,
    records: list[tuple[str, str, str | None, int, str | None]],
) -> None:
    """Insert several Step rows in one transaction.

    `records` is (node, source, data, seq, subagent). Same rows `add_step`
    writes, but one commit for the batch instead of one per row — a spawn_workers
    burst emits a dozen steps per stream chunk, and a commit each was the bulk of
    the run's DB write traffic. Durability stays chunk-grained, which is what the
    activity sidebar rebuilds from.
    """
    if not records:
        return
    for node, source, data, seq, subagent in records:
        session.add(Step(
            id=str(uuid4()),
            message_id=message_id,
            conversation_id=conv_id,
            node=node,
            source=source,
            subagent=subagent,
            data=data,
            seq=seq,
        ))
    await session.commit()


async def list_conversations(
    session: AsyncSession, surface: str | None = "web"
) -> list[dict]:
    """List conversations, filtered to one surface by default. surface=None lists all."""
    msg_count = (
        select(func.count(Message.id))
        .where(Message.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )
    stmt = (
        select(Conversation, msg_count.label("message_count"))
        # Incognito conversations never appear in history listings.
        .where(Conversation.ephemeral == False)  # noqa: E712
        .order_by(Conversation.pinned.desc(), Conversation.created_at.desc())
    )
    if surface is not None:
        stmt = stmt.where(Conversation.surface == surface)
    result = await session.execute(stmt)
    rows = result.all()
    return [
        {
            "id": conv.id,
            "title": conv.title,
            "model": conv.model,
            "surface": conv.surface,
            "pinned": conv.pinned,
            "project_id": conv.project_id,
            "ephemeral": conv.ephemeral,
            "created_at": conv.created_at.isoformat(),
            "message_count": count,
        }
        for conv, count in rows
    ]


async def update_conversation_title(session: AsyncSession, conv_id: str, title: str) -> None:
    conv = await session.get(Conversation, conv_id)
    if conv:
        conv.title = title
        await session.commit()


async def update_conversation(
    session: AsyncSession,
    conv_id: str,
    title: str | None = None,
    model: str | None = None,
    pinned: bool | None = None,
) -> Conversation | None:
    conv = await session.get(Conversation, conv_id)
    if conv is None:
        return None
    if title is not None:
        conv.title = title
    if model is not None:
        conv.model = model
    if pinned is not None:
        conv.pinned = pinned
    await session.commit()
    return conv


async def delete_conversation(session: AsyncSession, conv_id: str) -> None:
    conv = await session.get(Conversation, conv_id)
    if conv is None:
        return

    art_rows = (
        await session.execute(
            select(Artifact.id, Artifact.filename).where(Artifact.conversation_id == conv_id)
        )
    ).all()
    art_ids = [row[0] for row in art_rows]
    art_filenames = {row[0]: row[1] for row in art_rows}
    doc_paths = list(
        (await session.execute(
            select(Document.path).where(Document.conversation_id == conv_id)
        )).scalars()
    )
    # Artifact versions — collect their on-disk paths before cascade
    from db.models import ArtifactVersion
    version_paths: list[str] = []
    if art_ids:
        version_paths = list(
            (await session.execute(
                select(ArtifactVersion.filename).where(ArtifactVersion.artifact_id.in_(art_ids))
            )).scalars()
        )

    await session.delete(conv)  # cascades messages, steps, artifacts, documents via ORM
    await session.commit()

    cfg = get_config()
    for aid in art_ids:
        path = Path(art_filenames.get(aid) or (cfg.artifacts_dir / f"{aid}.md"))
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Failed to unlink artifact file %s: %s", path, e)
        # Also any versioned files not captured via DB (fallback glob, any extension)
        try:
            for vf in cfg.artifacts_dir.glob(f"{aid}_v*"):
                vf.unlink(missing_ok=True)
        except OSError:
            pass
    for vp in version_paths:
        try:
            Path(vp).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Failed to unlink artifact version file %s: %s", vp, e)

    for raw_path in doc_paths:
        try:
            Path(raw_path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Failed to unlink document file %s: %s", raw_path, e)

    try:
        from core.state import get_async_checkpointer
        await get_async_checkpointer().adelete_thread(conv_id)
    except RuntimeError:
        pass
    except Exception as e:
        logger.warning("Failed to delete checkpoint thread %s: %s", conv_id, e)

    try:
        from core.kernels import get_kernel_registry
        await get_kernel_registry().shutdown(conv_id)
    except Exception as e:
        logger.warning("Failed to shut down kernel for %s: %s", conv_id, e)


async def sweep_ephemeral_conversations(session: AsyncSession) -> int:
    """Delete any incognito conversations left behind by a crash or a client
    that never fired discardConversation. Called on startup. Each is torn down
    via delete_conversation so rows, on-disk files, and the checkpointer thread
    all go with it. Skips any that still have a pending/running job (mirrors the
    zombie-row sweep) so an in-flight incognito run isn't yanked out from under
    its worker."""
    ids = list(
        (await session.execute(
            select(Conversation.id).where(Conversation.ephemeral == True)  # noqa: E712
        )).scalars()
    )
    if not ids:
        return 0
    # A chat Job's id is the assistant Message id (job.id == task_id), and that
    # message belongs to the conversation — so an ephemeral conv is "in flight"
    # iff it owns a message whose id is a pending/running chat job.
    live_job_ids = select(Job.id).where(
        Job.kind == "chat", Job.status.in_(("pending", "running"))
    )
    live = set(
        (await session.execute(
            select(Message.conversation_id).where(
                Message.conversation_id.in_(ids),
                Message.id.in_(live_job_ids),
            )
        )).scalars()
    )
    swept = 0
    for cid in ids:
        if cid in live:
            continue
        await delete_conversation(session, cid)
        swept += 1
    return swept


async def get_conversation_meta(session: AsyncSession, conv_id: str) -> Conversation | None:
    return await session.get(Conversation, conv_id)


# ── Projects ──────────────────────────────────────────────────────────────────

async def create_project(
    session: AsyncSession,
    name: str,
    description: str | None = None,
    instructions: str = "",
) -> Project:
    proj = Project(
        id=str(uuid4()), name=name, description=description, instructions=instructions
    )
    session.add(proj)
    await session.commit()
    return proj


async def get_project(session: AsyncSession, project_id: str) -> Project | None:
    return await session.get(Project, project_id)


async def list_projects(session: AsyncSession) -> list[Project]:
    result = await session.execute(select(Project).order_by(Project.updated_at.desc()))
    return list(result.scalars())


async def update_project(session: AsyncSession, project_id: str, **kwargs: Any) -> Project | None:
    proj = await session.get(Project, project_id)
    if proj is None:
        return None
    for key, value in kwargs.items():
        setattr(proj, key, value)
    proj.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return proj


async def append_project_memory(session: AsyncSession, project_id: str, text: str) -> Project | None:
    proj = await session.get(Project, project_id)
    if proj is None:
        return None
    proj.memory = f"{proj.memory.rstrip()}\n\n{text.strip()}".strip()
    proj.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return proj


async def get_project_activity_since(
    session: AsyncSession, project_id: str, since: datetime | None
) -> tuple[int, datetime | None, datetime | None, int]:
    """(count, oldest_created_at, newest_created_at, total_chars) of new messages.

    Cheap enough to run for every project on every consolidation tick — the job
    uses `newest` to decide whether the project has gone quiet, `oldest` to
    decide how long material has been waiting, and the char total to decide
    whether an LLM call is worth spending, all before fetching any content.
    Incognito conversations are excluded here for the same reason
    `get_recent_messages` excludes them: they must never reach memory.
    """
    q = (
        select(
            func.count(Message.id),
            func.min(Message.created_at),
            func.max(Message.created_at),
            func.coalesce(func.sum(func.length(Message.content)), 0),
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.project_id == project_id)
        .where(Message.role.in_(["user", "assistant"]))
        .where(Conversation.ephemeral == False)  # noqa: E712
    )
    if since is not None:
        q = q.where(Message.created_at > since)
    count, oldest, newest, chars = (await session.execute(q)).one()
    return int(count or 0), oldest, newest, int(chars or 0)


async def get_project_messages_since(
    session: AsyncSession,
    project_id: str,
    since: datetime | None,
    limit: int = 400,
) -> list[dict]:
    """A project's user/assistant messages after `since`, **oldest first**.

    Oldest-first is load-bearing: the consolidation job walks these under a char
    budget and advances its watermark only as far as it actually consumed, so a
    backlog is picked up by the next run instead of being silently skipped (the
    bug `get_recent_messages` still has — it takes the newest N and advances the
    watermark past everything).
    """
    q = (
        select(
            Message.role,
            Message.content,
            Message.created_at,
            Conversation.title,
            Conversation.id.label("conversation_id"),
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.project_id == project_id)
        .where(Message.role.in_(["user", "assistant"]))
        .where(Conversation.ephemeral == False)  # noqa: E712
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    if since is not None:
        q = q.where(Message.created_at > since)
    rows = (await session.execute(q)).all()
    return [
        {
            "role": r.role,
            "content": r.content,
            "created_at": r.created_at,
            "title": r.title or "Untitled",
            "conversation_id": r.conversation_id,
        }
        for r in rows
    ]


async def delete_project(session: AsyncSession, project_id: str) -> bool:
    """Delete a project, keeping its conversations (their project_id is nulled).

    The explicit UPDATE matters: there is no ORM relationship to cascade, and
    SQLite FK enforcement is off, so a bare delete would leave dangling ids.
    """
    proj = await session.get(Project, project_id)
    if proj is None:
        return False
    await session.execute(
        update(Conversation)
        .where(Conversation.project_id == project_id)
        .values(project_id=None)
    )
    await session.delete(proj)
    await session.commit()
    return True


async def list_project_conversations(session: AsyncSession, project_id: str) -> list[Conversation]:
    result = await session.execute(
        select(Conversation)
        .where(Conversation.project_id == project_id)
        .order_by(Conversation.pinned.desc(), Conversation.created_at.desc())
    )
    return list(result.scalars())


async def count_project_conversations(session: AsyncSession, project_id: str) -> int:
    result = await session.execute(
        select(func.count(Conversation.id)).where(Conversation.project_id == project_id)
    )
    return result.scalar_one()


async def set_conversation_project(
    session: AsyncSession, conv_id: str, project_id: str | None
) -> Conversation | None:
    """Assign a conversation to a project (or remove it with project_id=None).

    Only web conversations may join projects — automation/board/bot threads
    have their own context regimes and must never pick up project injection.
    """
    conv = await session.get(Conversation, conv_id)
    if conv is None:
        return None
    if conv.surface != "web":
        raise ValueError("only web conversations can belong to a project")
    if project_id is not None and await session.get(Project, project_id) is None:
        raise ValueError(f"project not found: {project_id}")
    conv.project_id = project_id
    await session.commit()
    return conv


async def list_messages_connection(
    session: AsyncSession,
    conv_id: str,
    last: int,
    before_ts: datetime | None,
    before_id: str | None,
) -> tuple[list[Message], bool]:
    """Newest-N messages older than the (before_ts, before_id) cursor.

    Composite cursor (created_at, id) makes the order stable when two messages
    share a timestamp. Lookahead trick (last + 1) drives has_previous_page.
    Returned rows are oldest-first within the page (Connection convention).
    """
    stmt = (
        select(Message)
        .where(Message.conversation_id == conv_id)
        .options(selectinload(Message.steps))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(last + 1)
    )
    if before_ts is not None:
        stmt = stmt.where(
            (Message.created_at < before_ts)
            | ((Message.created_at == before_ts) & (Message.id < before_id))
        )
    rows = list((await session.execute(stmt)).scalars().all())
    has_previous = len(rows) > last
    if has_previous:
        rows = rows[:last]
    rows.reverse()
    return rows, has_previous


# ── Automation CRUD ────────────────────────────────────────────────────────────

async def create_automation(
    session: AsyncSession,
    name: str,
    description: str | None,
    input_type: str,
    prompt_text: str | None,
    model: str | None,
    code_text: str | None,
    webhook_url: str | None,
    webhook_method: str | None,
    webhook_headers: str | None,
    webhook_body: str | None,
    schedule: str | None,
    enabled: bool,
    notifications: str | None = None,
    stateful: bool = False,
) -> Automation:
    auto = Automation(
        id=str(uuid4()),
        name=name,
        description=description,
        input_type=input_type,
        prompt_text=prompt_text,
        model=model,
        code_text=code_text,
        webhook_url=webhook_url,
        webhook_method=webhook_method,
        webhook_headers=webhook_headers,
        webhook_body=webhook_body,
        schedule=schedule,
        enabled=enabled,
        notifications=notifications,
        stateful=stateful,
    )
    session.add(auto)
    await session.commit()
    return auto


async def get_automation(session: AsyncSession, automation_id: str) -> Automation | None:
    return await session.get(Automation, automation_id)


async def list_automations(session: AsyncSession) -> list[Automation]:
    result = await session.execute(select(Automation).order_by(Automation.created_at.desc()))
    return list(result.scalars().all())


async def list_automations_with_stats(
    session: AsyncSession,
) -> list[tuple[Automation, dict[str, Any]]]:
    """Return automations alongside aggregate stats from the last 7 days plus
    the latest run's status and timestamp (regardless of age)."""
    automations = await list_automations(session)
    if not automations:
        return []

    since = datetime.now(timezone.utc) - timedelta(days=7)

    agg_rows = (
        await session.execute(
            select(
                AutomationRun.automation_id,
                func.count(AutomationRun.id).label("total_7d"),
                func.sum(
                    case((AutomationRun.status.in_(["done", "no_change"]), 1), else_=0)
                ).label("success_7d"),
            )
            .where(AutomationRun.started_at >= since)
            .group_by(AutomationRun.automation_id)
        )
    ).all()
    agg_by_id = {r.automation_id: r for r in agg_rows}

    latest_subq = (
        select(
            AutomationRun.automation_id,
            func.max(AutomationRun.started_at).label("latest_at"),
        )
        .group_by(AutomationRun.automation_id)
        .subquery()
    )
    latest_rows = (
        await session.execute(
            select(AutomationRun.automation_id, AutomationRun.status, AutomationRun.started_at)
            .join(
                latest_subq,
                (AutomationRun.automation_id == latest_subq.c.automation_id)
                & (AutomationRun.started_at == latest_subq.c.latest_at),
            )
        )
    ).all()
    latest_by_id = {r.automation_id: r for r in latest_rows}

    out: list[tuple[Automation, dict[str, Any]]] = []
    for auto in automations:
        agg = agg_by_id.get(auto.id)
        latest = latest_by_id.get(auto.id)
        out.append(
            (
                auto,
                {
                    "total_count_7d": int(agg.total_7d) if agg else 0,
                    "success_count_7d": int(agg.success_7d or 0) if agg else 0,
                    "last_run_status": latest.status if latest else None,
                    "last_run_at": latest.started_at.isoformat() if latest else None,
                },
            )
        )
    return out


async def update_automation(session: AsyncSession, automation_id: str, **kwargs: Any) -> Automation | None:
    auto = await session.get(Automation, automation_id)
    if auto is None:
        return None
    for key, value in kwargs.items():
        setattr(auto, key, value)
    auto.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return auto


def automation_conversation_id(automation_id: str) -> str:
    """Deterministic conversation/thread id for a stateful automation's history."""
    return f"automation_{automation_id}"


async def delete_automation(session: AsyncSession, automation_id: str) -> bool:
    auto = await session.get(Automation, automation_id)
    if auto is None:
        return False
    # Stateful prompt automations own a conversation (messages + checkpointer
    # thread); delete_conversation no-ops when it was never created.
    await delete_conversation(session, automation_conversation_id(automation_id))
    await session.delete(auto)
    await session.commit()
    return True


async def list_enabled_scheduled_automations(session: AsyncSession) -> list[Automation]:
    result = await session.execute(
        select(Automation).where(Automation.enabled == True, Automation.schedule.isnot(None))  # noqa: E712
    )
    return list(result.scalars().all())


# ── AutomationRun CRUD ─────────────────────────────────────────────────────────

async def create_automation_run(
    session: AsyncSession,
    automation_id: str,
    triggered_by: str,
    *,
    run_id: str | None = None,
    status: str = "running",
) -> AutomationRun:
    """Create an AutomationRun. `run_id` lets the caller bind a specific UUID
    (used by the queue worker so job.id == AutomationRun.id); `status` defaults
    to "running" for the legacy asyncio.create_task path."""
    run = AutomationRun(
        id=run_id or str(uuid4()),
        automation_id=automation_id,
        triggered_by=triggered_by,
        status=status,
    )
    session.add(run)
    await session.commit()
    return run


async def finish_automation_run(
    session: AsyncSession,
    run_id: str,
    status: str,
    output: str | None,
    error: str | None,
) -> None:
    run = await session.get(AutomationRun, run_id)
    if run:
        run.status = status
        run.output = output
        run.error = error
        run.finished_at = datetime.now(timezone.utc)
        await session.commit()


async def list_automation_runs(
    session: AsyncSession, automation_id: str, limit: int = 50
) -> list[AutomationRun]:
    result = await session.execute(
        select(AutomationRun)
        .where(AutomationRun.automation_id == automation_id)
        .order_by(AutomationRun.started_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_automation_run(session: AsyncSession, run_id: str) -> AutomationRun | None:
    return await session.get(AutomationRun, run_id)


# ── Workflow CRUD ──────────────────────────────────────────────────────────────

async def create_workflow(
    session: AsyncSession,
    name: str,
    description: str | None,
    definition: str,
    notifications: str | None = None,
) -> Workflow:
    wf = Workflow(
        id=str(uuid4()),
        name=name,
        description=description,
        definition=definition,
        notifications=notifications,
    )
    session.add(wf)
    await session.commit()
    return wf


async def get_workflow(session: AsyncSession, workflow_id: str) -> Workflow | None:
    return await session.get(Workflow, workflow_id)


async def list_workflows(session: AsyncSession) -> list[Workflow]:
    result = await session.execute(select(Workflow).order_by(Workflow.created_at.desc()))
    return list(result.scalars().all())


async def update_workflow(session: AsyncSession, workflow_id: str, **kwargs: Any) -> Workflow | None:
    wf = await session.get(Workflow, workflow_id)
    if wf is None:
        return None
    for key, value in kwargs.items():
        setattr(wf, key, value)
    wf.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return wf


async def delete_workflow(session: AsyncSession, workflow_id: str) -> bool:
    wf = await session.get(Workflow, workflow_id)
    if wf is None:
        return False
    await session.delete(wf)
    await session.commit()
    return True


# ── WorkflowRun CRUD ───────────────────────────────────────────────────────────

async def create_workflow_run(
    session: AsyncSession,
    workflow_id: str,
    inputs: str | None,
    *,
    run_id: str | None = None,
    status: str = "running",
) -> WorkflowRun:
    """Create a WorkflowRun. `run_id` lets the caller bind a specific UUID
    (queue worker sets it to job.id so cancellation is a no-join lookup)."""
    run = WorkflowRun(
        id=run_id or str(uuid4()),
        workflow_id=workflow_id,
        status=status,
        inputs=inputs,
        node_results="[]",
    )
    session.add(run)
    await session.commit()
    return run


async def get_workflow_run(session: AsyncSession, run_id: str) -> WorkflowRun | None:
    return await session.get(WorkflowRun, run_id)


async def list_workflow_runs(
    session: AsyncSession, workflow_id: str, limit: int = 50
) -> list[WorkflowRun]:
    result = await session.execute(
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id == workflow_id)
        .order_by(WorkflowRun.started_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def finish_workflow_run(
    session: AsyncSession,
    run_id: str,
    status: str,
    outputs: str | None,
    node_results: str | None,
    error: str | None,
) -> None:
    run = await session.get(WorkflowRun, run_id)
    if run:
        run.status = status
        run.outputs = outputs
        run.node_results = node_results
        run.error = error
        run.finished_at = datetime.now(timezone.utc)
        await session.commit()


async def get_recent_messages(
    session: AsyncSession,
    since: datetime | None = None,
    limit: int = 200,
) -> list[dict]:
    """Fetch recent user/assistant messages across all conversations, oldest-first."""
    q = (
        select(Message.role, Message.content, Message.created_at, Conversation.title)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Message.role.in_(["user", "assistant"]))
        # Incognito conversations must never feed long-term memory consolidation.
        .where(Conversation.ephemeral == False)  # noqa: E712
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    if since is not None:
        q = q.where(Message.created_at >= since)
    rows = (await session.execute(q)).all()
    return [
        {
            "role": r.role,
            "content": r.content,
            "created_at": r.created_at.isoformat(),
            "title": r.title or "Untitled",
        }
        for r in reversed(rows)
    ]


# ── Config settings CRUD ──────────────────────────────────────────────────────

async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(ConfigSetting, key)
    return row.value if row else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(ConfigSetting, key)
    if row:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    else:
        session.add(ConfigSetting(key=key, value=value))
    await session.commit()


async def delete_setting(session: AsyncSession, key: str) -> bool:
    row = await session.get(ConfigSetting, key)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def list_settings(session: AsyncSession) -> list[ConfigSetting]:
    result = await session.execute(select(ConfigSetting).order_by(ConfigSetting.key))
    return list(result.scalars().all())


async def get_default_model(session: AsyncSession) -> str:
    from core.model_catalog import DEFAULT_MODEL as _CATALOG_DEFAULT
    value = await get_setting(session, "default.model")
    return value if value else _CATALOG_DEFAULT


async def resolve_model(explicit: str | None, session: AsyncSession | None = None) -> str:
    """The model a run should actually use: `explicit` if set, else the default.

    Call this instead of writing `explicit or DEFAULT_MODEL`. That constant is
    the compile-time catalog seed, not the operator's choice, so falling back to
    it routes the run to a model the user never selected — the failure is silent
    until that provider errors and names a model nobody picked.

    Pass `session` when one is already open; otherwise a short-lived one is
    used, so this is safe to call from sync-ish contexts like workflow nodes.
    """
    if explicit:
        return explicit
    if session is not None:
        return await get_default_model(session)
    from db import async_session

    async with async_session() as own_session:
        return await get_default_model(own_session)


# ── Custom (runtime-added) models ─────────────────────────────────────────────
#
# Persisted as a JSON list of {id, label, provider} under the config key
# `models.custom`. Merged with the built-in catalog by core.model_catalog.

_CUSTOM_MODELS_KEY = "models.custom"


async def get_custom_models(session: AsyncSession) -> list[dict]:
    """Return the runtime-added models as a list of {id, label, provider} dicts."""
    raw = await get_setting(session, _CUSTOM_MODELS_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


async def add_custom_model(
    session: AsyncSession, model_id: str, label: str, provider: str,
) -> None:
    """Add (or replace, by id) a custom model in the catalog."""
    models = [m for m in await get_custom_models(session) if m.get("id") != model_id]
    models.append({"id": model_id, "label": label, "provider": provider})
    await set_setting(session, _CUSTOM_MODELS_KEY, json.dumps(models))


async def remove_custom_model(session: AsyncSession, model_id: str) -> bool:
    """Remove a custom model by id. Returns False if it wasn't a custom model."""
    models = await get_custom_models(session)
    remaining = [m for m in models if m.get("id") != model_id]
    if len(remaining) == len(models):
        return False
    await set_setting(session, _CUSTOM_MODELS_KEY, json.dumps(remaining))
    return True


# ── Notification channels CRUD ────────────────────────────────────────────────

async def create_notification_channel(
    session: AsyncSession, name: str, type: str, target: str,
) -> NotificationChannel:
    ch = NotificationChannel(id=str(uuid4()), name=name, type=type, target=target)
    session.add(ch)
    await session.commit()
    return ch


async def get_notification_channel(
    session: AsyncSession, channel_id: str,
) -> NotificationChannel | None:
    return await session.get(NotificationChannel, channel_id)


async def list_notification_channels(session: AsyncSession) -> list[NotificationChannel]:
    result = await session.execute(
        select(NotificationChannel).order_by(NotificationChannel.created_at.asc())
    )
    return list(result.scalars().all())


async def get_notification_channels_by_ids(
    session: AsyncSession, ids: set[str],
) -> list[NotificationChannel]:
    if not ids:
        return []
    result = await session.execute(
        select(NotificationChannel).where(NotificationChannel.id.in_(ids))
    )
    return list(result.scalars().all())


async def update_notification_channel(
    session: AsyncSession, channel_id: str, **kwargs: Any,
) -> NotificationChannel | None:
    ch = await session.get(NotificationChannel, channel_id)
    if ch is None:
        return None
    for key, value in kwargs.items():
        if value is None:
            continue
        setattr(ch, key, value)
    ch.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return ch


async def delete_notification_channel(session: AsyncSession, channel_id: str) -> bool:
    ch = await session.get(NotificationChannel, channel_id)
    if ch is None:
        return False
    await session.delete(ch)
    await session.commit()
    return True


async def list_references_to_channel(
    session: AsyncSession, channel_id: str,
) -> list[dict]:
    """Return [{kind, id, name}] for automations/workflows whose notifications JSON
    contains a ref to this channel. Used by the delete guard."""
    refs: list[dict] = []
    autos = await session.execute(select(Automation.id, Automation.name, Automation.notifications))
    for aid, aname, raw in autos.all():
        if _notifications_ref(raw, channel_id):
            refs.append({"kind": "automation", "id": aid, "name": aname})
    wfs = await session.execute(select(Workflow.id, Workflow.name, Workflow.notifications))
    for wid, wname, raw in wfs.all():
        if _notifications_ref(raw, channel_id):
            refs.append({"kind": "workflow", "id": wid, "name": wname})
    return refs


def _notifications_ref(raw: str | None, channel_id: str) -> bool:
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, list):
        return False
    return any(isinstance(e, dict) and e.get("id") == channel_id for e in parsed)


# ── Artifact CRUD ─────────────────────────────────────────────────────────────

async def create_artifact(
    session: AsyncSession,
    title: str,
    filename: str,
    kind: str = "markdown",
    mime_type: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> Artifact:
    art = Artifact(
        id=str(uuid4()),
        title=title,
        filename=filename,
        kind=kind,
        mime_type=mime_type,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    session.add(art)
    await session.commit()
    return art


async def get_artifact(session: AsyncSession, artifact_id: str) -> Artifact | None:
    return await session.get(Artifact, artifact_id)


async def list_artifacts(
    session: AsyncSession, conversation_id: str | None = None
) -> list[Artifact]:
    q = select(Artifact).order_by(Artifact.updated_at.desc())
    if conversation_id is not None:
        q = q.where(Artifact.conversation_id == conversation_id)
    result = await session.execute(q)
    return list(result.scalars().all())


async def update_artifact(
    session: AsyncSession, artifact_id: str, **kwargs: Any
) -> Artifact | None:
    art = await session.get(Artifact, artifact_id)
    if art is None:
        return None
    for key, value in kwargs.items():
        setattr(art, key, value)
    art.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return art


async def delete_artifact(session: AsyncSession, artifact_id: str) -> bool:
    art = await session.get(Artifact, artifact_id)
    if art is None:
        return False
    # Collect version file paths before cascade delete
    from db.models import ArtifactVersion
    version_paths = list(
        (await session.execute(
            select(ArtifactVersion.filename).where(ArtifactVersion.artifact_id == artifact_id)
        )).scalars()
    )
    live_path = art.filename
    await session.delete(art)
    await session.commit()
    # Delete live file
    try:
        Path(live_path).unlink(missing_ok=True)
    except OSError:
        pass
    for vp in version_paths:
        try:
            Path(vp).unlink(missing_ok=True)
        except OSError:
            pass
    return True


# ── Artifact versioning (ADK ArtifactService analog) ─────────────────────────

async def create_artifact_version(
    session: AsyncSession,
    artifact_id: str,
    title: str,
    filename: str,
    version: int,
) -> Any:
    from db.models import ArtifactVersion
    ver = ArtifactVersion(
        artifact_id=artifact_id,
        title=title,
        filename=filename,
        version=version,
    )
    session.add(ver)
    await session.commit()
    return ver


async def list_artifact_versions(
    session: AsyncSession, artifact_id: str
) -> list[Any]:
    from db.models import ArtifactVersion
    result = await session.execute(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version.asc())
    )
    return list(result.scalars().all())


async def get_artifact_version(
    session: AsyncSession, artifact_id: str, version: int
) -> Any | None:
    from db.models import ArtifactVersion
    result = await session.execute(
        select(ArtifactVersion).where(
            ArtifactVersion.artifact_id == artifact_id,
            ArtifactVersion.version == version,
        )
    )
    return result.scalar_one_or_none()


async def get_latest_artifact_version_number(
    session: AsyncSession, artifact_id: str
) -> int:
    from db.models import ArtifactVersion
    result = await session.execute(
        select(func.max(ArtifactVersion.version)).where(
            ArtifactVersion.artifact_id == artifact_id
        )
    )
    max_v = result.scalar()
    return int(max_v) if max_v is not None else 0


# ── Document CRUD ─────────────────────────────────────────────────────────────

async def create_document(
    session: AsyncSession,
    conversation_id: str,
    message_id: str | None,
    filename: str,
    mime_type: str,
    size: int,
    path: str,
) -> Document:
    doc = Document(
        id=str(uuid4()),
        conversation_id=conversation_id,
        message_id=message_id,
        filename=filename,
        mime_type=mime_type,
        size=size,
        path=path,
    )
    session.add(doc)
    await session.commit()
    return doc


async def get_document(session: AsyncSession, document_id: str) -> Document | None:
    return await session.get(Document, document_id)


async def list_documents(session: AsyncSession, conversation_id: str) -> list[Document]:
    q = (
        select(Document)
        .where(Document.conversation_id == conversation_id)
        .order_by(Document.created_at.asc())
    )
    result = await session.execute(q)
    return list(result.scalars().all())


async def delete_document(session: AsyncSession, document_id: str) -> Document | None:
    doc = await session.get(Document, document_id)
    if doc is None:
        return None
    await session.delete(doc)
    await session.commit()
    return doc


# ── Memory CRUD ───────────────────────────────────────────────────────────────

async def list_memories(session: AsyncSession, kind: str | None = None) -> list[Memory]:
    q = select(Memory).order_by(Memory.updated_at.desc())
    if kind is not None:
        q = q.where(Memory.kind == kind)
    result = await session.execute(q)
    return list(result.scalars().all())


async def create_memory(
    session: AsyncSession, *, text: str, kind: str, embedding: bytes | None
) -> Memory:
    mem = Memory(id=str(uuid4()), text=text, kind=kind, embedding=embedding)
    session.add(mem)
    await session.commit()
    return mem


async def update_memory_item(
    session: AsyncSession,
    memory_id: str,
    *,
    text: str | None = None,
    kind: str | None = None,
    embedding: bytes | None = None,
) -> Memory | None:
    mem = await session.get(Memory, memory_id)
    if mem is None:
        return None
    if text is not None:
        mem.text = text
    if kind is not None:
        mem.kind = kind
    if embedding is not None:
        mem.embedding = embedding
    mem.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return mem


async def delete_memory(session: AsyncSession, memory_id: str) -> bool:
    mem = await session.get(Memory, memory_id)
    if mem is None:
        return False
    await session.delete(mem)
    await session.commit()
    return True


async def count_memories(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Memory))).scalar_one()


# ── Lexical (BM25) retrieval ──────────────────────────────────────────────────
# `match_expr` must come from core.retrieval.fts_match_expr — never raw user
# text, which FTS5 parses as a query language and rejects on bare punctuation.
# bm25() returns a *negative* relevance where lower is better, hence ORDER BY
# ASC; callers use rank order only, not the magnitude.

async def search_memories_lexical(
    session: AsyncSession, match_expr: str, *, kind: str = "fact", limit: int = 20
) -> list[str]:
    """Memory ids matching `match_expr`, best BM25 rank first. Empty on any
    FTS failure (index missing on an older DB, SQLite built without FTS5)."""
    try:
        rows = await session.execute(sa_text("""
            SELECT m.id AS id
            FROM memories_fts f
            JOIN memories m ON m.rowid = f.rowid
            WHERE memories_fts MATCH :q AND m.kind = :kind
            ORDER BY bm25(memories_fts)
            LIMIT :limit
        """), {"q": match_expr, "kind": kind, "limit": limit})
        return [r[0] for r in rows.all()]
    except Exception as exc:
        logger.warning("lexical memory search failed (%s) — dense-only this turn", exc)
        return []


async def search_chunks_lexical(
    session: AsyncSession, conversation_id: str, match_expr: str, *, limit: int = 20
) -> list[str]:
    """DocumentChunk ids in `conversation_id` matching `match_expr`, best first."""
    try:
        rows = await session.execute(sa_text("""
            SELECT c.id AS id
            FROM document_chunks_fts f
            JOIN document_chunks c ON c.rowid = f.rowid
            WHERE document_chunks_fts MATCH :q AND c.conversation_id = :cid
            ORDER BY bm25(document_chunks_fts)
            LIMIT :limit
        """), {"q": match_expr, "cid": conversation_id, "limit": limit})
        return [r[0] for r in rows.all()]
    except Exception as exc:
        logger.warning("lexical chunk search failed (%s) — dense-only this turn", exc)
        return []


# ── Memory activity (audit log for when memories were surfaced) ───────────────

async def log_memory_activities(
    session: AsyncSession, activities: list[dict]
) -> int:
    """Bulk insert memory activity rows. Each dict: memory_id, conversation_id, kind, score, query, source."""
    from db.models import MemoryActivity

    if not activities:
        return 0
    objs = []
    for act in activities:
        objs.append(
            MemoryActivity(
                id=str(uuid4()),
                memory_id=act["memory_id"],
                conversation_id=act.get("conversation_id"),
                kind=act.get("kind", "fact"),
                score=act.get("score"),
                query=(act.get("query") or "")[:500] if act.get("query") else None,
                source=act.get("source", "retrieval"),
            )
        )
    session.add_all(objs)
    await session.commit()
    return len(objs)


async def list_memory_activities(
    session: AsyncSession, memory_id: str, limit: int = 50
) -> list:
    from db.models import MemoryActivity

    q = (
        select(MemoryActivity)
        .where(MemoryActivity.memory_id == memory_id)
        .order_by(MemoryActivity.accessed_at.desc())
        .limit(limit)
    )
    result = await session.execute(q)
    return list(result.scalars().all())


async def get_memory_usage_map(
    session: AsyncSession, memory_ids: list[str] | None = None
) -> dict[str, dict]:
    """Return {memory_id: {last_used, count}} for given ids or all if None."""
    from db.models import MemoryActivity

    q = select(
        MemoryActivity.memory_id,
        func.max(MemoryActivity.accessed_at).label("last_used"),
        func.count().label("cnt"),
    ).group_by(MemoryActivity.memory_id)

    if memory_ids:
        q = q.where(MemoryActivity.memory_id.in_(memory_ids))

    rows = (await session.execute(q)).all()
    return {
        r.memory_id: {"last_used": r.last_used, "count": r.cnt}
        for r in rows
    }


async def prune_memory_activities(session: AsyncSession, older_than_days: int = 90) -> int:
    from datetime import timedelta

    from db.models import MemoryActivity

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    q = select(MemoryActivity).where(MemoryActivity.accessed_at < cutoff)
    rows = (await session.execute(q)).scalars().all()
    count = len(rows)
    for r in rows:
        await session.delete(r)
    if count:
        await session.commit()
    return count


# ── Skills CRUD ───────────────────────────────────────────────────────────────
# Pure persistence; the description-embedding lives one layer up in
# core/skill_store.py so both the GraphQL mutations and the agent tools share it.

async def create_skill(
    session: AsyncSession,
    *,
    name: str,
    description: str,
    body: str,
    embedding: bytes | None,
    enabled: bool = True,
) -> Skill:
    skill = Skill(
        id=str(uuid4()),
        name=name,
        description=description,
        body=body,
        embedding=embedding,
        enabled=enabled,
    )
    session.add(skill)
    await session.commit()
    return skill


async def get_skill(session: AsyncSession, skill_id: str) -> Skill | None:
    return await session.get(Skill, skill_id)


async def get_skill_by_name(session: AsyncSession, name: str) -> Skill | None:
    result = await session.execute(select(Skill).where(Skill.name == name))
    return result.scalar_one_or_none()


async def list_skills(
    session: AsyncSession, *, enabled_only: bool = False,
) -> list[Skill]:
    q = select(Skill).order_by(Skill.name.asc())
    if enabled_only:
        q = q.where(Skill.enabled.is_(True))
    result = await session.execute(q)
    return list(result.scalars().all())


async def update_skill(
    session: AsyncSession,
    skill_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    body: str | None = None,
    enabled: bool | None = None,
    embedding: bytes | None = None,
) -> Skill | None:
    skill = await session.get(Skill, skill_id)
    if skill is None:
        return None
    if name is not None:
        skill.name = name
    if description is not None:
        skill.description = description
    if body is not None:
        skill.body = body
    if enabled is not None:
        skill.enabled = enabled
    if embedding is not None:
        skill.embedding = embedding
    skill.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return skill


async def delete_skill(session: AsyncSession, skill_id: str) -> bool:
    skill = await session.get(Skill, skill_id)
    if skill is None:
        return False
    await session.delete(skill)
    await session.commit()
    return True


async def append_node_result(
    session: AsyncSession,
    run_id: str,
    node_entry: dict,
) -> None:
    """Append a node execution record to the run's node_results JSON array."""
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return
    existing: list = json.loads(run.node_results or "[]")
    existing.append(node_entry)
    run.node_results = json.dumps(existing)
    await session.commit()


# ── Board task (kanban) CRUD ───────────────────────────────────────────────────

def board_task_conversation_id(task_id: str) -> str:
    """Deterministic conversation/thread id for a board task's run history."""
    return f"boardtask_{task_id}"


async def create_board_task(
    session: AsyncSession,
    *,
    title: str,
    body: str | None = None,
    status: str = "ready",
    priority: int = 0,
    created_by: str = "user",
    model: str | None = None,
    skill: str | None = None,
    parent_ids: list[str] | None = None,
) -> BoardTask:
    """Create a board task, optionally linked under existing parents.

    Tasks with parents are forced to "todo" — the dispatcher promotes them to
    "ready" once every parent is done. Raises ValueError on a missing parent.
    """
    parent_ids = [p for p in (parent_ids or []) if p]
    if parent_ids:
        found = (await session.execute(
            select(BoardTask.id).where(BoardTask.id.in_(parent_ids))
        )).scalars().all()
        missing = set(parent_ids) - set(found)
        if missing:
            raise ValueError(f"parent task(s) not found: {', '.join(sorted(missing))}")
        status = "todo"
    task = BoardTask(
        id=str(uuid4()),
        title=title,
        body=body,
        status=status,
        priority=priority,
        created_by=created_by,
        model=model,
        skill=skill,
    )
    session.add(task)
    for pid in parent_ids:
        session.add(BoardTaskLink(id=str(uuid4()), parent_id=pid, child_id=task.id))
    await session.commit()
    return task


async def get_board_task(session: AsyncSession, task_id: str) -> BoardTask | None:
    return await session.get(BoardTask, task_id)


async def get_board_task_by_job(session: AsyncSession, job_id: str) -> BoardTask | None:
    result = await session.execute(select(BoardTask).where(BoardTask.job_id == job_id))
    return result.scalars().first()


async def list_board_tasks(
    session: AsyncSession, *, include_archived: bool = False,
) -> list[BoardTask]:
    stmt = select(BoardTask).order_by(BoardTask.priority.desc(), BoardTask.created_at.asc())
    if not include_archived:
        stmt = stmt.where(BoardTask.status != "archived")
    return list((await session.execute(stmt)).scalars().all())


async def list_board_task_links(session: AsyncSession) -> list[BoardTaskLink]:
    return list((await session.execute(select(BoardTaskLink))).scalars().all())


async def get_board_task_parents(session: AsyncSession, task_id: str) -> list[BoardTask]:
    """Parents of `task_id`, oldest link first — used to build handoff context."""
    result = await session.execute(
        select(BoardTask)
        .join(BoardTaskLink, BoardTaskLink.parent_id == BoardTask.id)
        .where(BoardTaskLink.child_id == task_id)
        .order_by(BoardTaskLink.created_at.asc())
    )
    return list(result.scalars().all())


async def list_board_task_descendants(session: AsyncSession, task_id: str) -> set[str]:
    """Ids of every task reachable from `task_id` via parent→child edges."""
    links = await list_board_task_links(session)
    children: dict[str, list[str]] = {}
    for link in links:
        children.setdefault(link.parent_id, []).append(link.child_id)
    seen: set[str] = set()
    frontier = [task_id]
    while frontier:
        node = frontier.pop()
        for child in children.get(node, []):
            if child not in seen:
                seen.add(child)
                frontier.append(child)
    return seen


async def replace_board_task_parents(
    session: AsyncSession, task_id: str, parent_ids: list[str],
) -> BoardTask | None:
    """Replace a task's parent links with `parent_ids`.

    Rejects missing parents, self-links, and cycles (a descendant of the task
    can't become its parent). If the task is waiting (todo/ready) its status is
    recomputed: unfinished parents park it in "todo"; otherwise it keeps its
    current column. Raises ValueError on invalid input.
    """
    task = await session.get(BoardTask, task_id)
    if task is None:
        return None
    parent_ids = [p for p in dict.fromkeys(parent_ids) if p]  # dedupe, keep order
    if task_id in parent_ids:
        raise ValueError("a task cannot depend on itself")
    if parent_ids:
        found = (await session.execute(
            select(BoardTask.id).where(BoardTask.id.in_(parent_ids))
        )).scalars().all()
        missing = set(parent_ids) - set(found)
        if missing:
            raise ValueError(f"parent task(s) not found: {', '.join(sorted(missing))}")
        descendants = await list_board_task_descendants(session, task_id)
        cyclic = descendants & set(parent_ids)
        if cyclic:
            raise ValueError("dependency cycle: a task's descendant cannot be its parent")
    for link in (await session.execute(
        select(BoardTaskLink).where(BoardTaskLink.child_id == task_id)
    )).scalars().all():
        await session.delete(link)
    for pid in parent_ids:
        session.add(BoardTaskLink(id=str(uuid4()), parent_id=pid, child_id=task_id))
    if task.status in ("todo", "ready") and parent_ids:
        parents = (await session.execute(
            select(BoardTask).where(BoardTask.id.in_(parent_ids))
        )).scalars().all()
        if any(p.status != "done" for p in parents):
            task.status = "todo"
    task.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return task


async def update_board_task(session: AsyncSession, task_id: str, **kwargs: Any) -> BoardTask | None:
    task = await session.get(BoardTask, task_id)
    if task is None:
        return None
    for key, value in kwargs.items():
        setattr(task, key, value)
    task.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return task


async def delete_board_task(session: AsyncSession, task_id: str) -> bool:
    task = await session.get(BoardTask, task_id)
    if task is None:
        return False
    # Deleting a task orphans nothing: drop both edge directions, then the
    # backing conversation (no-op if the task never ran).
    for link in (await session.execute(
        select(BoardTaskLink).where(
            (BoardTaskLink.parent_id == task_id) | (BoardTaskLink.child_id == task_id)
        )
    )).scalars().all():
        await session.delete(link)
    await delete_conversation(session, board_task_conversation_id(task_id))
    await session.delete(task)
    await session.commit()
    return True


async def promote_ready_board_tasks(session: AsyncSession) -> int:
    """Flip "todo" tasks whose parents are ALL done to "ready".

    Only tasks that actually have parents auto-promote; a parentless todo is a
    parked card that stays put until someone moves it. Does not commit — the
    dispatcher commits promotion + dispatch atomically.
    """
    linked_todo = (await session.execute(
        select(BoardTask)
        .join(BoardTaskLink, BoardTaskLink.child_id == BoardTask.id)
        .where(BoardTask.status == "todo")
        .distinct()
    )).scalars().all()
    promoted = 0
    for task in linked_todo:
        parents = await get_board_task_parents(session, task.id)
        if parents and all(p.status == "done" for p in parents):
            task.status = "ready"
            task.updated_at = datetime.now(timezone.utc)
            promoted += 1
    return promoted


# ── Startup zombie sweep ───────────────────────────────────────────────────────

async def cleanup_zombie_running_rows(session: AsyncSession) -> dict[str, int]:
    """Flip rows still marked 'running' from a prior process.

    Called once at lifespan startup. The in-memory _tasks registry is empty on
    a fresh boot, so any 'running' Message/AutomationRun/WorkflowRun in the DB
    is a zombie from a previous instance that crashed or was killed. For Jobs,
    a 'running' row means the worker that claimed it is gone — flip back to
    'pending' (and clear the lock) so a fresh worker can re-claim immediately
    instead of waiting for the reaper to notice the locked_until expiry.

    Returns a {table_name: rowcount} dict for logging.
    """
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}

    msg_res = await session.execute(
        update(Message).where(Message.status == "running").values(status="error")
    )
    counts["messages"] = msg_res.rowcount or 0  # type: ignore[attr-defined]

    auto_res = await session.execute(
        update(AutomationRun)
        .where(AutomationRun.status == "running")
        .values(status="error", error="interrupted by server restart", finished_at=now)
    )
    counts["automation_runs"] = auto_res.rowcount or 0  # type: ignore[attr-defined]

    wf_res = await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.status == "running")
        .values(status="error", error="interrupted by server restart", finished_at=now)
    )
    counts["workflow_runs"] = wf_res.rowcount or 0  # type: ignore[attr-defined]

    job_res = await session.execute(
        update(Job)
        .where(Job.status == "running")
        .values(status="pending", locked_by=None, locked_until=None, updated_at=now)
    )
    counts["jobs"] = job_res.rowcount or 0  # type: ignore[attr-defined]

    # Board tasks stuck "running" whose dispatch job is gone go back to
    # "ready" for a fresh dispatch. Tasks whose job survived as pending
    # (flipped just above) are left alone — that job will re-run them, and
    # flipping to ready too would let the dispatcher enqueue a duplicate.
    live_jobs = select(Job.id).where(Job.status.in_(["pending", "running"]))
    bt_res = await session.execute(
        update(BoardTask)
        .where(
            BoardTask.status == "running",
            (BoardTask.job_id.is_(None)) | (BoardTask.job_id.not_in(live_jobs)),
        )
        .values(status="ready", job_id=None, updated_at=now)
    )
    counts["board_tasks"] = bt_res.rowcount or 0  # type: ignore[attr-defined]

    await session.commit()
    return counts
