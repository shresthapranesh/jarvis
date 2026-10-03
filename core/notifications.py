"""Outbound notifications for automation/workflow run completion."""

from __future__ import annotations

import json
import logging
import os

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from db.ops import get_notification_channels_by_ids

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
    header = title if status == "done" else f"[{status.upper()}] {title}"
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
    # Straight to the Bot API rather than through a running bot: behind the
    # edge, the bots live in the edge process, not this one.
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning("telegram bot not configured; skipping notification to %s", chat_id)
        return
    base = (os.environ.get("TELEGRAM_API_URL") or "https://api.telegram.org").rstrip("/")
    proxy = os.environ.get("TELEGRAM_PROXY_URL") or None
    async with httpx.AsyncClient(proxy=proxy, timeout=30) as client:
        resp = await client.post(f"{base}/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text})
    body = resp.json()
    if not body.get("ok"):
        logger.warning("telegram sendMessage to %s failed: %s", chat_id, body.get("description"))


async def _send_discord(channel_id: str, text: str) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        logger.warning("discord bot not configured; skipping notification to %s", channel_id)
        return
    if not channel_id.isdigit():
        logger.warning("invalid discord channel_id: %s", channel_id)
        return
    base = (os.environ.get("DISCORD_API_URL") or "https://discord.com/api/v10").rstrip("/")
    out = text if len(text) <= _MAX_DISCORD_LEN else text[: _MAX_DISCORD_LEN - 1] + "…"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": out, "allowed_mentions": {"parse": []}},
        )
    if resp.status_code >= 400:
        logger.warning("discord message to %s failed: %s %s", channel_id, resp.status_code, resp.text[:200])
