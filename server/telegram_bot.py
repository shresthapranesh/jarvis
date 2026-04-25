"""Telegram bot — optional, enabled by TELEGRAM_BOT_TOKEN env var."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from telegram import Bot, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from core.state import TaskState, _background_tasks, _tasks, stream_task_events
from db import async_session
from db.ops import add_message, get_default_model, get_or_create_conversation, get_setting
from server.routes_chat import _run_agent_task

logger = logging.getLogger(__name__)

_EDIT_INTERVAL = 1.0
_MAX_MSG_LEN = 4000
_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_LOADING_TEXT = "thinking..."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else None
    async with async_session() as session:
        raw = await get_setting(session, "telegram.allowed_users")
        model = await get_default_model(session)
    allowed = {int(x.strip()) for x in raw.split(",") if x.strip()} if raw else set()
    if user_id not in allowed:
        return

    chat_id = update.message.chat_id
    text = update.message.text
    conv_id = f"telegram_{chat_id}"

    sent = await context.bot.send_message(chat_id=chat_id, text="...")
    placeholder_id = sent.message_id

    async with async_session() as session:
        conv = await get_or_create_conversation(session, conv_id, model, text[:60])
        await add_message(session, conv.id, "user", text)
        task_msg = await add_message(
            session, conv.id, "assistant", "", model=model, status="running"
        )

    task_id = task_msg.id
    task_state = TaskState()
    _tasks[task_id] = task_state

    t = asyncio.create_task(_run_agent_task(task_id, text, model, conv_id))
    _background_tasks[task_id] = t
    t.add_done_callback(lambda _t: _background_tasks.pop(task_id, None))

    asyncio.create_task(_stream_to_telegram(context.bot, chat_id, placeholder_id, task_state))


async def _loading_animation(bot: Bot, chat_id: int, message_id: int) -> None:
    from telegram.constants import ChatAction
    frame = 0
    while True:
        spinner = _SPINNER[frame % len(_SPINNER)]
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"{spinner} {_LOADING_TEXT}",
            )
        except Exception:
            pass
        frame += 1
        await asyncio.sleep(0.7)


async def _stream_to_telegram(
    bot: Bot, chat_id: int, message_id: int, state: TaskState
) -> None:
    loading_task = asyncio.create_task(_loading_animation(bot, chat_id, message_id))
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
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
