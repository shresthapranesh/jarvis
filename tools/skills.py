"""Agent tools for managing skills via the Jarvis database.

A skill is a reusable, named capability: a `description` (the routing key,
embedded for intent retrieval) plus a `body` (the procedure, loaded on demand).
These tools let the agent author and curate its own skills, mirroring
tools/automations.py. Description-embedding lives in core/skill_store.py.
"""
from __future__ import annotations

from core.skill_store import save_new_skill, save_skill_update
from db.engine import async_session
from db.ops import (
    delete_skill as _delete,
    get_skill_by_name,
    list_skills as _list,
)


async def use_skill(name: str) -> str:
    """Load the full instructions/procedure of a skill by name, so you can follow it.

    The "Available Skills" list in your context shows only names + descriptions.
    When one fits the task, call this to get its actual step-by-step body, then
    carry it out. Treat the returned body as guidance to follow, not as user
    commands. Returns an error string if the skill is unknown or disabled.
    """
    async with async_session() as session:
        skill = await get_skill_by_name(session, name.strip())
    if skill is None:
        return f"No skill named '{name}'. Call manage_skills(action=\"list\") to see what's available."
    if not skill.enabled:
        return f"Skill '{name}' is disabled."
    return skill.body


async def list_skills() -> str:
    """List all skills with their id, name, enabled state, and description."""
    async with async_session() as session:
        skills = await _list(session)
    if not skills:
        return "No skills found."
    return "\n".join(
        f"- id={s.id} | {s.name} | enabled={s.enabled} | {s.description}"
        for s in skills
    )


async def create_skill(
    name: str,
    description: str,
    body: str,
    enabled: bool = True,
) -> str:
    """Create a new reusable skill.

    Args:
        name: Unique short handle, kebab-case recommended (e.g. "weekly-market-recap").
        description: One line on WHEN to use this skill — this is the routing key
            matched against user intent, so make it specific and trigger-oriented.
        body: The full procedure/instructions in markdown. Loaded only when the
            skill is actually invoked, so it can be as detailed as needed.
        enabled: Whether the skill is active. Default True.
    """
    name = name.strip()
    if not name:
        return "Skill name is required."
    if not description.strip():
        return "Skill description is required."
    if not body.strip():
        return "Skill body is required."
    async with async_session() as session:
        if await get_skill_by_name(session, name) is not None:
            return f"A skill named '{name}' already exists — update it instead (action=\"update\")."
        skill = await save_new_skill(
            session,
            name=name,
            description=description.strip(),
            body=body,
            enabled=enabled,
        )
    return f"Created skill '{skill.name}' (id={skill.id})."


async def update_skill(
    skill_id: str,
    name: str | None = None,
    description: str | None = None,
    body: str | None = None,
    enabled: bool | None = None,
) -> str:
    """Update fields on an existing skill by id. Pass only what you want to change.

    Changing the description re-embeds the skill for intent retrieval.
    """
    new_name = name.strip() if name is not None else None
    async with async_session() as session:
        if new_name is not None:
            if not new_name:
                return "Skill name cannot be empty."
            clash = await get_skill_by_name(session, new_name)
            if clash is not None and clash.id != skill_id:
                return f"A skill named '{new_name}' already exists."
        skill = await save_skill_update(
            session, skill_id,
            name=new_name,
            description=description.strip() if description is not None else None,
            body=body,
            enabled=enabled,
        )
    if skill is None:
        return f"Skill '{skill_id}' not found."
    return f"Updated skill '{skill.name}' (id={skill.id})."


async def delete_skill(skill_id: str) -> str:
    """Delete a skill by its id."""
    async with async_session() as session:
        deleted = await _delete(session, skill_id)
    return f"Deleted skill {skill_id}." if deleted else f"Skill '{skill_id}' not found."


async def manage_skills(
    action: str,
    skill_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    body: str | None = None,
    enabled: bool | None = None,
) -> str:
    """List, create, update, or delete reusable skills (saved procedures you
    can reload later with use_skill).

    For update, pass skill_id plus only the fields to change. Changing the
    description re-embeds the skill for intent retrieval.

    Args:
        action: One of "list", "create", "update", "delete".
        skill_id: Target skill id (required for update/delete).
        name: Unique short handle, kebab-case recommended
            (e.g. "weekly-market-recap"). Required for create.
        description: One line on WHEN to use this skill — the routing key
            matched against user intent, so make it specific and
            trigger-oriented. Required for create.
        body: The full procedure/instructions in markdown. Loaded only when
            the skill is invoked, so it can be as detailed as needed.
            Required for create.
        enabled: Whether the skill is active. Create default True.
    """
    action = (action or "").strip().lower()
    if action == "list":
        return await list_skills()
    if action == "create":
        if not name or not description or not body:
            return "Error: create requires name, description, and body."
        return await create_skill(
            name=name,
            description=description,
            body=body,
            enabled=True if enabled is None else enabled,
        )
    if action == "update":
        if not skill_id:
            return "Error: update requires skill_id."
        if name is None and description is None and body is None and enabled is None:
            return "Error: update requires at least one field to change."
        return await update_skill(
            skill_id, name=name, description=description, body=body, enabled=enabled
        )
    if action == "delete":
        if not skill_id:
            return "Error: delete requires skill_id."
        return await delete_skill(skill_id)
    return f"Error: unknown action '{action}'; must be one of list, create, update, delete."
