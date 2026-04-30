from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import Automation, AutomationRun, ConfigSetting, Conversation, Message, Step, Workflow, WorkflowRun


async def create_conversation(session: AsyncSession, model: str, title: str | None) -> Conversation:
    conv = Conversation(id=str(uuid4()), model=model, title=title)
    session.add(conv)
    await session.commit()
    return conv


async def get_or_create_conversation(
    session: AsyncSession, conversation_id: str | None, model: str, title: str | None
) -> Conversation:
    if conversation_id:
        result = await session.get(Conversation, conversation_id)
        if result:
            return result
        conv = Conversation(id=conversation_id, model=model, title=title)
        session.add(conv)
        await session.commit()
        return conv
    return await create_conversation(session, model, title)


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


async def add_step(
    session: AsyncSession,
    message_id: str,
    conv_id: str,
    node: str,
    source: str,
    data: str | None,
    seq: int,
) -> Step:
    step = Step(
        id=str(uuid4()),
        message_id=message_id,
        conversation_id=conv_id,
        node=node,
        source=source,
        data=data,
        seq=seq,
    )
    session.add(step)
    await session.commit()
    return step


async def list_conversations(session: AsyncSession) -> list[dict]:
    msg_count = (
        select(func.count(Message.id))
        .where(Message.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )
    result = await session.execute(
        select(Conversation, msg_count.label("message_count"))
        .order_by(Conversation.created_at.desc())
    )
    rows = result.all()
    return [
        {
            "id": conv.id,
            "title": conv.title,
            "model": conv.model,
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


async def delete_conversation(session: AsyncSession, conv_id: str) -> None:
    conv = await session.get(Conversation, conv_id)
    if conv:
        await session.delete(conv)
        await session.commit()


async def get_conversation(session: AsyncSession, conv_id: str) -> Conversation | None:
    result = await session.execute(
        select(Conversation)
        .where(Conversation.id == conv_id)
        .options(
            selectinload(Conversation.messages).selectinload(Message.steps)
        )
    )
    return result.scalar_one_or_none()


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
    )
    session.add(auto)
    await session.commit()
    return auto


async def get_automation(session: AsyncSession, automation_id: str) -> Automation | None:
    return await session.get(Automation, automation_id)


async def list_automations(session: AsyncSession) -> list[Automation]:
    result = await session.execute(select(Automation).order_by(Automation.created_at.desc()))
    return list(result.scalars().all())


async def update_automation(session: AsyncSession, automation_id: str, **kwargs: Any) -> Automation | None:
    auto = await session.get(Automation, automation_id)
    if auto is None:
        return None
    for key, value in kwargs.items():
        setattr(auto, key, value)
    auto.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return auto


async def delete_automation(session: AsyncSession, automation_id: str) -> bool:
    auto = await session.get(Automation, automation_id)
    if auto is None:
        return False
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
    session: AsyncSession, automation_id: str, triggered_by: str
) -> AutomationRun:
    run = AutomationRun(
        id=str(uuid4()),
        automation_id=automation_id,
        triggered_by=triggered_by,
        status="running",
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
) -> WorkflowRun:
    run = WorkflowRun(
        id=str(uuid4()),
        workflow_id=workflow_id,
        status="running",
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
