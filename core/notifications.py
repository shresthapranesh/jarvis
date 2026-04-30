"""Outbound notifications for automation/workflow run completion.

Currently only Telegram is supported. The schema is designed to be extensible:
configs are stored as a JSON array of `{"type": str, ...channel-specific keys}`.
Failures inside the dispatcher are caught and logged — they must never propagate,
so a broken notification setup cannot fail an otherwise successful run.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, cast

from core import state

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

_MAX_TELEGRAM_LEN = 3800  # leaves room for the [STATUS] title header within Telegram's 4096 limit


def parse_notifications(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("notifications config is not valid JSON; ignoring: %r", raw)
        return []
    if not isinstance(parsed, list):
        logger.warning("notifications config must be a list; got %s", type(parsed).__name__)
        return []
    return [c for c in parsed if isinstance(c, dict)]


def _matches(on: str, status: str) -> bool:
    if on == "both":
        return True
    return on == status


async def send_notifications(
    configs: list[dict],
    *,
    status: str,
    title: str,
    body: str,
) -> None:
    if not configs:
        return

    header = f"[{status.upper()}] {title}"
    truncated = body if len(body) <= _MAX_TELEGRAM_LEN else body[:_MAX_TELEGRAM_LEN] + "…"
    text = f"{header}\n\n{truncated}"

    for cfg in configs:
        ch_type = cfg.get("type")
        on = cfg.get("on", "both")
        if not _matches(on, status):
            continue
        try:
            if ch_type == "telegram":
                chat_id = cfg.get("chat_id")
                if not chat_id:
                    logger.warning("telegram notification missing chat_id; skipping")
                    continue
                await _send_telegram(str(chat_id), text)
            else:
                logger.warning("unknown notification type %r; skipping", ch_type)
        except Exception as exc:
            logger.warning("notification dispatch failed (%s): %s", ch_type, exc)


async def _send_telegram(chat_id: str, text: str) -> None:
    bot = cast("Bot | None", state.get_telegram_bot())
    if bot is None:
        logger.warning("telegram bot not configured; skipping notification to %s", chat_id)
        return
    await bot.send_message(chat_id=int(chat_id), text=text)
