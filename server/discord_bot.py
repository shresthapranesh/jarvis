"""Discord bot — optional, enabled by DISCORD_BOT_TOKEN env var."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from discord.abc import MessageableChannel

from core.safety import gate_input, gate_output
from core.schemas import AttachmentIn
from core.state import (
    TaskState,
    _background_tasks,
    _tasks,
    log_task_created,
    log_task_received,
    stream_task_events,
)
from db import async_session
from db.ops import add_message, get_default_model, get_or_create_conversation, get_setting
from server.routes_chat import _run_agent_task

logger = logging.getLogger(__name__)

_EDIT_INTERVAL = 1.0
_MAX_MSG_LEN = 1900  # Discord hard limit is 2000; leave headroom.


async def _check_and_get_model(user_id: int) -> str | None:
    """Return the model for this user, or None if not on the allowlist."""
    async with async_session() as session:
        raw = await get_setting(session, "discord.allowed_users")
        allowed = {int(x.strip()) for x in raw.split(",") if x.strip()} if raw else set()
        if user_id not in allowed:
            return None
        return await get_default_model(session)


def _is_reply_to_bot(message: discord.Message, bot_user: discord.ClientUser | None) -> bool:
    if not message.reference or bot_user is None:
        return False
    resolved = message.reference.resolved
    if isinstance(resolved, discord.Message):
        return resolved.author.id == bot_user.id
    return False


def _should_respond(message: discord.Message, bot_user: discord.ClientUser | None) -> bool:
    """DMs always; guild messages only when @mentioned or replied-to."""
    if message.guild is None:
        return True
    if bot_user is not None and bot_user in message.mentions:
        return True
    return _is_reply_to_bot(message, bot_user)


def _strip_bot_mention(content: str, bot_user: discord.ClientUser | None) -> str:
    if bot_user is None:
        return content.strip()
    for token in (f"<@{bot_user.id}>", f"<@!{bot_user.id}>"):
        content = content.replace(token, "")
    return content.strip()


async def _dispatch(
    channel: "MessageableChannel",
    model: str,
    user_content: str,
    db_user_content: str,
    attachments: list[AttachmentIn] | None = None,
) -> None:
    """Create DB records, start the agent task, and kick off streaming."""
    rejection = await gate_input(user_content, model)
    if rejection:
        with contextlib.suppress(Exception):
            await channel.send(rejection[:_MAX_MSG_LEN])
        return

    conv_id = f"discord_{channel.id}"
    log_task_received("chat", conv_id, "discord")

    async with async_session() as session:
        conv = await get_or_create_conversation(session, conv_id, model, db_user_content[:60])
        await add_message(session, conv.id, "user", db_user_content)
        task_msg = await add_message(session, conv.id, "assistant", "", model=model, status="running")

    task_id = task_msg.id
    task_state = TaskState(parent_id=conv_id)
    _tasks[task_id] = task_state
    log_task_created(task_id, task_state, model)

    t = asyncio.create_task(_run_agent_task(task_id, user_content, model, conv_id, attachments=attachments))
    _background_tasks[task_id] = t
    t.add_done_callback(lambda _t: _background_tasks.pop(task_id, None))

    asyncio.create_task(_stream_to_discord(channel, None, task_state, model))


async def _loading_animation(channel: discord.abc.Messageable) -> None:
    """Show the typing indicator until cancelled."""
    with contextlib.suppress(Exception):
        async with channel.typing():
            await asyncio.sleep(86400)


async def _stream_to_discord(
    channel: "MessageableChannel",
    placeholder: discord.Message | None,
    state: TaskState,
    model: str,
) -> None:
    loading_task = asyncio.create_task(_loading_animation(channel))
    accumulated = ""
    last_edit = 0.0
    message: discord.Message | None = placeholder

    try:
        async for event in stream_task_events(state):
            if event["event"] == "token":
                data = json.loads(event["data"])
                if data.get("source") == "main":
                    if not loading_task.done():
                        loading_task.cancel()
                    accumulated += data.get("text", "")
                    now = time.monotonic()
                    if accumulated and now - last_edit >= _EDIT_INTERVAL:
                        try:
                            if message is None:
                                message = await channel.send(accumulated[:_MAX_MSG_LEN])
                            else:
                                await message.edit(content=accumulated[:_MAX_MSG_LEN])
                            last_edit = now
                        except Exception as exc:
                            logger.debug("discord edit: %s", exc)
    finally:
        if not loading_task.done():
            loading_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loading_task

    final = accumulated[:_MAX_MSG_LEN] if accumulated else "(no response)"
    final, _output_verdict = await gate_output(final, model)
    final = final[:_MAX_MSG_LEN]
    try:
        if message is None:
            await channel.send(final)
        else:
            await message.edit(content=final)
    except Exception as exc:
        logger.debug("discord final edit: %s", exc)


async def _handle_voice(
    message: discord.Message, attachment: discord.Attachment, model: str,
) -> None:
    from server.routes_media import transcribe_bytes

    placeholder = await message.channel.send("⏳ Transcribing...")
    buf = await attachment.read()
    ctype = (attachment.content_type or "").lower()
    suffix = ".ogg" if "ogg" in ctype else (".mp3" if "mp" in ctype else ".ogg")
    transcribed = await transcribe_bytes(bytes(buf), suffix=suffix)
    if not transcribed:
        with contextlib.suppress(Exception):
            await placeholder.edit(content="(could not transcribe audio)")
        return

    rejection = await gate_input(transcribed, model)
    if rejection:
        with contextlib.suppress(Exception):
            await placeholder.edit(content=rejection[:_MAX_MSG_LEN])
        return

    conv_id = f"discord_{message.channel.id}"
    log_task_received("chat", conv_id, "discord")
    async with async_session() as session:
        conv = await get_or_create_conversation(session, conv_id, model, transcribed[:60])
        await add_message(session, conv.id, "user", f"[Voice] {transcribed}")
        task_msg = await add_message(session, conv.id, "assistant", "", model=model, status="running")

    task_id = task_msg.id
    task_state = TaskState(parent_id=conv_id)
    _tasks[task_id] = task_state
    log_task_created(task_id, task_state, model)

    t = asyncio.create_task(_run_agent_task(task_id, transcribed, model, conv_id))
    _background_tasks[task_id] = t
    t.add_done_callback(lambda _t: _background_tasks.pop(task_id, None))

    asyncio.create_task(_stream_to_discord(message.channel, placeholder, task_state, model))


async def _handle_message(client: discord.Client, message: discord.Message) -> None:
    if message.author.bot:
        return
    if not _should_respond(message, client.user):
        return

    model = await _check_and_get_model(message.author.id)
    if model is None:
        return

    text = _strip_bot_mention(message.content, client.user)

    voice_attachment: discord.Attachment | None = None
    image_attachments: list[discord.Attachment] = []
    for att in message.attachments:
        ctype = (att.content_type or "").lower()
        is_voice_fn = getattr(att, "is_voice_message", None)
        if (callable(is_voice_fn) and is_voice_fn()) or ctype.startswith("audio/"):
            voice_attachment = att
            break
        if ctype.startswith("image/"):
            image_attachments.append(att)

    if voice_attachment is not None:
        await _handle_voice(message, voice_attachment, model)
        return

    if image_attachments:
        attachments_in: list[AttachmentIn] = []
        for att in image_attachments:
            buf = await att.read()
            b64 = base64.b64encode(bytes(buf)).decode()
            attachments_in.append(AttachmentIn(
                type="image",
                name=att.filename,
                mime_type=att.content_type or "image/jpeg",
                data=b64,
                size=len(buf),
            ))
        query = text or "What's in this image?"
        await _dispatch(
            message.channel, model,
            user_content=query,
            db_user_content=f"[Image] {query}",
            attachments=attachments_in,
        )
        return

    if not text:
        return
    await _dispatch(message.channel, model, text, text)


def build_client() -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_message(message: discord.Message) -> None:
        try:
            await _handle_message(client, message)
        except Exception:
            logger.exception("discord on_message error")

    return client
