"""Outbound notifications for automation/workflow run completion.

Notifications on a run reference centrally-defined channels by id:
`[{"id": "<channel-uuid>", "on": "both"|"done"|"error"}, ...]`. The channel
record (`NotificationChannel`) carries `type` and `target`. The dispatcher
resolves refs → channel records → platform sender.

Failures inside the dispatcher are caught and logged — they must never propagate,
so a broken notification setup cannot fail an otherwise successful run.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, cast

from sqlalchemy.ext.asyncio import AsyncSession

from core import state
from db.ops import get_notification_channels_by_ids

if TYPE_CHECKING:
    import discord
    from telegram import Bot

logger = logging.getLogger(__name__)

_MAX_TELEGRAM_LEN = 3800  # leaves room for the [STATUS] title header within Telegram's 4096 limit
_MAX_DISCORD_LEN = 1900   # Discord hard limit is 2000; leave headroom


def parse_notifications(raw: str | None) -> list[dict]:
    """Parse the notifications JSON column. Returns `[{id, on}, ...]`; legacy
    entries (no `id` key) are silently dropped."""
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
    return [c for c in parsed if isinstance(c, dict) and isinstance(c.get("id"), str)]


def _matches(on: str, status: str) -> bool:
    if on == "both":
        return True
    return on == status


def _build_text(status: str, title: str, body: str) -> str:
    header = f"[{status.upper()}] {title}"
    truncated = body if len(body) <= _MAX_TELEGRAM_LEN else body[:_MAX_TELEGRAM_LEN] + "…"
    return f"{header}\n\n{truncated}"


async def send_notifications(
    session: AsyncSession,
    raw: str | None,
    *,
    status: str,
    title: str,
    body: str,
) -> None:
    refs = parse_notifications(raw)
    if not refs:
        return

    channels = await get_notification_channels_by_ids(session, {r["id"] for r in refs})
    by_id = {c.id: c for c in channels}

    text = _build_text(status, title, body)

    for ref in refs:
        ch = by_id.get(ref["id"])
        if ch is None:
            logger.warning("notification refs missing channel %s; skipping", ref["id"])
            continue
        if not _matches(ref.get("on", "both"), status):
            continue
        try:
            if ch.type == "telegram":
                await _send_telegram(ch.target, text)
            elif ch.type == "discord":
                await _send_discord(ch.target, text)
            else:
                logger.warning("unknown channel type %r; skipping", ch.type)
        except Exception as exc:
            logger.warning("notification dispatch failed (%s): %s", ch.type, exc)


async def _send_telegram(chat_id: str, text: str) -> None:
    bot = cast("Bot | None", state.get_telegram_bot())
    if bot is None:
        logger.warning("telegram bot not configured; skipping notification to %s", chat_id)
        return
    await bot.send_message(chat_id=int(chat_id), text=text)


async def _send_discord(channel_id: str, text: str) -> None:
    import discord  # local import — keep notifications.py importable without discord.py

    client = cast("discord.Client | None", state.get_discord_client())
    if client is None or not client.is_ready():
        logger.warning("discord client not ready; skipping notification to %s", channel_id)
        return
    try:
        cid = int(channel_id)
    except ValueError:
        logger.warning("invalid discord channel_id: %s", channel_id)
        return

    channel = client.get_channel(cid)
    if channel is None:
        try:
            channel = await client.fetch_channel(cid)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("discord fetch_channel(%s) failed: %s", cid, exc)
            return

    if not isinstance(channel, discord.abc.Messageable):
        logger.warning("discord channel %s is not messageable (%s)", cid, type(channel).__name__)
        return

    out = text if len(text) <= _MAX_DISCORD_LEN else text[: _MAX_DISCORD_LEN - 1] + "…"
    await channel.send(out)
