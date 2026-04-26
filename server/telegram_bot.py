"""Telegram bot — optional, enabled by TELEGRAM_BOT_TOKEN env var."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import time

from telegram import Bot, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from core.schemas import AttachmentIn
from core.state import TaskState, _background_tasks, _tasks, stream_task_events
from db import async_session
from db.ops import add_message, get_default_model, get_or_create_conversation, get_setting
from server.routes_chat import _run_agent_task

logger = logging.getLogger(__name__)

_EDIT_INTERVAL = 1.0
_MAX_MSG_LEN = 4000


async def _check_and_get_model(user_id: int | None, chat_id: int) -> str | None:
    """Return the model for this chat, or None if the user is not on the allowlist."""
    async with async_session() as session:
        raw = await get_setting(session, "telegram.allowed_users")
        allowed = {int(x.strip()) for x in raw.split(",") if x.strip()} if raw else set()
        if user_id not in allowed:
            return None
        return await get_default_model(session)


async def _dispatch(
    bot: Bot,
    chat_id: int,
    model: str,
    user_content: str,
    db_user_content: str,
    placeholder_text: str,
    attachments: list[AttachmentIn] | None = None,
) -> None:
    """Create DB records, start the agent task, and kick off streaming."""
    conv_id = f"telegram_{chat_id}"
    sent = await bot.send_message(chat_id=chat_id, text=placeholder_text)
    placeholder_id = sent.message_id

    async with async_session() as session:
        conv = await get_or_create_conversation(session, conv_id, model, db_user_content[:60])
        await add_message(session, conv.id, "user", db_user_content)
        task_msg = await add_message(session, conv.id, "assistant", "", model=model, status="running")

    task_id = task_msg.id
    task_state = TaskState()
    _tasks[task_id] = task_state

    t = asyncio.create_task(_run_agent_task(task_id, user_content, model, conv_id, attachments=attachments))
    _background_tasks[task_id] = t
    t.add_done_callback(lambda _t: _background_tasks.pop(task_id, None))

    asyncio.create_task(_stream_to_telegram(bot, chat_id, placeholder_id, task_state))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.message.chat_id
    model = await _check_and_get_model(user_id, chat_id)
    if model is None:
        return

    text = update.message.text
    await _dispatch(context.bot, chat_id, model, text, text, "...")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from server.routes_media import transcribe_bytes

    if not update.message:
        return
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.message.chat_id
    model = await _check_and_get_model(user_id, chat_id)
    if model is None:
        return

    sent = await context.bot.send_message(chat_id=chat_id, text="⏳ Transcribing...")
    placeholder_id = sent.message_id

    tg_file = await voice.get_file()
    buf = await tg_file.download_as_bytearray()
    suffix = ".ogg" if update.message.voice else ".mp3"
    text = await transcribe_bytes(bytes(buf), suffix=suffix)
    if not text:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=placeholder_id, text="(could not transcribe audio)"
        )
        return

    conv_id = f"telegram_{chat_id}"
    async with async_session() as session:
        conv = await get_or_create_conversation(session, conv_id, model, text[:60])
        await add_message(session, conv.id, "user", f"[Voice] {text}")
        task_msg = await add_message(session, conv.id, "assistant", "", model=model, status="running")

    task_id = task_msg.id
    task_state = TaskState()
    _tasks[task_id] = task_state

    t = asyncio.create_task(_run_agent_task(task_id, text, model, conv_id))
    _background_tasks[task_id] = t
    t.add_done_callback(lambda _t: _background_tasks.pop(task_id, None))

    asyncio.create_task(_stream_to_telegram(context.bot, chat_id, placeholder_id, task_state))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.message.chat_id
    model = await _check_and_get_model(user_id, chat_id)
    if model is None:
        return

    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    buf = await tg_file.download_as_bytearray()
    b64 = base64.b64encode(bytes(buf)).decode()
    attachment = AttachmentIn(
        type="image", name="photo.jpg", mime_type="image/jpeg",
        data=b64, size=len(buf),
    )
    query = update.message.caption or "What's in this image?"

    await _dispatch(
        context.bot, chat_id, model,
        user_content=query,
        db_user_content=f"[Photo] {query}",
        placeholder_text="...",
        attachments=[attachment],
    )


async def _loading_animation(bot: Bot, chat_id: int) -> None:
    from telegram.constants import ChatAction
    while True:
        with contextlib.suppress(Exception):
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(4.0)


async def _stream_to_telegram(
    bot: Bot, chat_id: int, message_id: int, state: TaskState
) -> None:
    loading_task = asyncio.create_task(_loading_animation(bot, chat_id))
    accumulated = ""
    last_edit = 0.0

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
                            await bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=message_id,
                                text=accumulated[:_MAX_MSG_LEN],
                            )
                            last_edit = now
                        except Exception as exc:
                            logger.debug("edit_message_text: %s", exc)
    finally:
        if not loading_task.done():
            loading_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loading_task

    final = accumulated[:_MAX_MSG_LEN] if accumulated else "(no response)"
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=final)
    except Exception as exc:
        logger.debug("final edit_message_text: %s", exc)


def build_application(token: str) -> Application:
    builder = Application.builder().token(token)
    proxy = (
        os.environ.get("TELEGRAM_PROXY_URL")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("ALL_PROXY")
    )
    if proxy:
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
    app = builder.build()
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
