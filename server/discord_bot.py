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

from core.schemas import AttachmentIn
from core.state import (
    TaskState,
    _tasks,
    stream_task_events,
)
from db import async_session
from db.ops import add_message, get_or_create_conversation, get_setting, resolve_model
from server.chat_runtime import enqueue_chat_task, route_to_live_run

logger = logging.getLogger(__name__)

_EDIT_INTERVAL = 1.0
_MAX_MSG_LEN = 1900  # Discord hard limit is 2000; leave headroom.


def _split_for_discord(text: str, limit: int = _MAX_MSG_LEN) -> list[str]:
    """Split text into Discord-sendable chunks at clean boundaries.
    Prefers paragraph > line > word breaks within the last quarter of the window
    so chunk seams stay stable as more tokens stream in."""
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window_start = limit - limit // 4
        cut = remaining.rfind("\n\n", window_start, limit)
        if cut == -1:
            cut = remaining.rfind("\n", window_start, limit)
        if cut == -1:
            cut = remaining.rfind(" ", window_start, limit)
        if cut == -1:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _check_and_get_model(user_id: int) -> str | None:
    """Return the model for this user, or None if not on the allowlist."""
    async with async_session() as session:
        raw = await get_setting(session, "discord.allowed_users")
        allowed = {int(x.strip()) for x in raw.split(",") if x.strip()} if raw else set()
        if user_id not in allowed:
            return None
        return await resolve_model(None, session)


def _is_reply_to_bot(message: discord.Message, bot_user: discord.ClientUser | None) -> bool:
    if not message.reference or bot_user is None:
        return False
    resolved = message.reference.resolved
    if isinstance(resolved, discord.Message):
        return resolved.author.id == bot_user.id
    return False


def _should_respond(message: discord.Message, bot_user: discord.ClientUser | None) -> bool:
    """DMs always; guild messages only when @mentioned, replied-to, or inside a bot-owned thread."""
    if message.guild is None:
        return True
    channel = message.channel
    if (
        isinstance(channel, discord.Thread)
        and bot_user is not None
        and channel.owner_id == bot_user.id
    ):
        return True
    if bot_user is not None and bot_user in message.mentions:
        return True
    return _is_reply_to_bot(message, bot_user)


async def _resolve_target_channel(
    message: discord.Message, prompt: str,
) -> "MessageableChannel":
    """In a guild text channel, create a thread off the user's message and return it.
    DMs and existing threads pass through unchanged."""
    channel = message.channel
    if message.guild is None or isinstance(channel, discord.Thread):
        return channel
    first_line = (prompt or "").strip().splitlines()[0] if prompt and prompt.strip() else ""
    name = first_line[:90] or "Conversation"
    try:
        return await message.create_thread(name=name, auto_archive_duration=1440)
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.debug("discord create_thread failed: %s", exc)
        return channel


async def _send_reply(
    channel: "MessageableChannel",
    content: str,
    reply_to: discord.Message | None,
) -> discord.Message:
    """Send into `channel`, using Discord's native reply feature when `reply_to`
    lives in the same channel. fail_if_not_exists=False so a deleted original doesn't error."""
    if reply_to is not None and reply_to.channel.id == channel.id:
        ref = discord.MessageReference(
            message_id=reply_to.id,
            channel_id=reply_to.channel.id,
            fail_if_not_exists=False,
        )
        return await channel.send(content, reference=ref, mention_author=False)
    return await channel.send(content)


def _strip_bot_mention(content: str, bot_user: discord.ClientUser | None) -> str:
    if bot_user is None:
        return content.strip()
    for token in (f"<@{bot_user.id}>", f"<@!{bot_user.id}>"):
        content = content.replace(token, "")
    return content.strip()


# Sent instead of a second stream when a message lands mid-run. A channel is
# one conversation, so the alternative is two runs racing the same LangGraph
# thread — and the reply already in flight will answer this too.
_QUEUED_NOTE = "📥 Added to what I'm working on — it'll be picked up in a moment."


async def _route_or_none(session, conv_id: str, text: str, attachments) -> str | None:
    """Queue onto a live run. Returns a note to send, or None to start a turn."""
    try:
        routed = await route_to_live_run(session, conv_id, text, attachments)
    except ValueError as exc:
        return str(exc)
    return None if routed is None else _QUEUED_NOTE


async def _dispatch(
    channel: "MessageableChannel",
    model: str,
    user_content: str,
    db_user_content: str,
    attachments: list[AttachmentIn] | None = None,
    reply_to: discord.Message | None = None,
) -> None:
    """Create DB records, start the agent task, and kick off streaming."""
    loading_task = asyncio.create_task(_loading_animation(channel))
    conv_id = f"discord_{channel.id}"
    async with async_session() as session:
        await get_or_create_conversation(session, conv_id, model, db_user_content[:60], surface="discord")
        note = await _route_or_none(session, conv_id, user_content, attachments)
        if note is not None:
            loading_task.cancel()
            with contextlib.suppress(Exception):
                await _send_reply(channel, note, reply_to)
            return
        await add_message(session, conv_id, "user", db_user_content)
        task_id = await enqueue_chat_task(
            session, user_content, model, conv_id,
            attachments=attachments, source="discord",
        )

    task_state = _tasks[task_id]
    asyncio.create_task(_stream_to_discord(channel, None, task_state, loading_task=loading_task, reply_to=reply_to))


async def _loading_animation(channel: discord.abc.Messageable) -> None:
    """Show the typing indicator until cancelled."""
    with contextlib.suppress(Exception):
        async with channel.typing():
            await asyncio.sleep(86400)


async def _stream_to_discord(
    channel: "MessageableChannel",
    placeholder: discord.Message | None,
    state: TaskState,
    loading_task: asyncio.Task | None = None,
    reply_to: discord.Message | None = None,
) -> None:
    if loading_task is None:
        loading_task = asyncio.create_task(_loading_animation(channel))
    accumulated = ""
    last_edit = 0.0
    messages: list[discord.Message] = [placeholder] if placeholder is not None else []

    async def render(text: str) -> None:
        chunks = _split_for_discord(text)
        for i, chunk in enumerate(chunks):
            if i < len(messages):
                if messages[i].content != chunk:
                    await messages[i].edit(content=chunk)
            else:
                reply = reply_to if i == 0 else None
                msg = await _send_reply(channel, chunk, reply)
                messages.append(msg)

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
                            await render(accumulated)
                            last_edit = now
                        except Exception as exc:
                            logger.debug("discord edit: %s", exc)
    finally:
        if not loading_task.done():
            loading_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loading_task

    final = accumulated if accumulated else "(no response)"
    try:
        await render(final)
    except Exception as exc:
        logger.debug("discord final edit: %s", exc)


async def _handle_voice(
    message: discord.Message, attachment: discord.Attachment, model: str,
) -> None:
    from server.routes_media import transcribe_bytes

    buf = await attachment.read()
    ctype = (attachment.content_type or "").lower()
    suffix = ".ogg" if "ogg" in ctype else (".mp3" if "mp" in ctype else ".ogg")
    transcribed = await transcribe_bytes(bytes(buf), suffix=suffix)
    if not transcribed:
        with contextlib.suppress(Exception):
            await message.channel.send("(could not transcribe audio)")
        return

    target = await _resolve_target_channel(message, transcribed)
    placeholder: discord.Message | None = None
    with contextlib.suppress(Exception):
        placeholder = await _send_reply(target, "⏳ Transcribing...", message)

    loading_task = asyncio.create_task(_loading_animation(target))
    conv_id = f"discord_{target.id}"
    async with async_session() as session:
        await get_or_create_conversation(session, conv_id, model, transcribed[:60], surface="discord")
        note = await _route_or_none(session, conv_id, transcribed, None)
        if note is not None:
            loading_task.cancel()
            with contextlib.suppress(Exception):
                if placeholder is not None:
                    await placeholder.edit(content=note)
                else:
                    await _send_reply(target, note, message)
            return
        await add_message(session, conv_id, "user", f"[Voice] {transcribed}")
        task_id = await enqueue_chat_task(
            session, transcribed, model, conv_id, source="discord",
        )

    task_state = _tasks[task_id]

    asyncio.create_task(_stream_to_discord(target, placeholder, task_state, loading_task=loading_task, reply_to=message))


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
        target = await _resolve_target_channel(message, query)
        await _dispatch(
            target, model,
            user_content=query,
            db_user_content=f"[Image] {query}",
            attachments=attachments_in,
            reply_to=message,
        )
        return

    if not text:
        return
    target = await _resolve_target_channel(message, text)
    await _dispatch(target, model, text, text, reply_to=message)


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
