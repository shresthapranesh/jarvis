"""The chat bots, run by the Rust edge (`edge/src/bots/`).

A fake chat service stands in for Telegram's Bot API, Discord's REST API and
gateway; the edge runs its bots against it, and runs the turns they start
against a fake model that echoes the message (`test_edge_loop.py`). What a
message *writes* is also diffed against Python's own bot handler, recorded
(`python_golden.py`), as every ported operation is (`test_edge_parity.py`).

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from edge_support import _free_port, _run_edge, edge_binary, until  # noqa: F401 — edge_binary is a fixture
from python_golden import recorded
from test_edge_loop import MODEL, FakeOllama, Reply, fake  # noqa: F401 — fake is a fixture
from test_edge_parity import SESSION_START, _dump, _mask

REPO = Path(__file__).resolve().parent.parent
_until = until

TG_TOKEN = "123:tg"
DC_TOKEN = "dc-token"
ME = "900"  # the Discord bot's user id
QUEUED_NOTE = "📥 Added to what I'm working on — it'll be picked up in a moment."


class FakeChat:
    """Telegram + Discord, on one port."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.calls: list[tuple[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.next_update = 1
        self.files: dict[str, bytes] = {}
        self.channels: dict[str, dict[str, Any]] = {}
        self.gateway: list[dict[str, Any]] = []  # what the edge sent on the gateway
        self.dispatches: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.seq = 0
        self.ids = 1000
        self._server: Any = None
        self._task: asyncio.Task | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def env(self) -> dict[str, str]:
        return {
            "TELEGRAM_BOT_TOKEN": TG_TOKEN,
            "TELEGRAM_API_URL": f"{self.base}/tg",
            "DISCORD_BOT_TOKEN": DC_TOKEN,
            "DISCORD_API_URL": f"{self.base}/dc",
            "DISCORD_GATEWAY_URL": f"ws://127.0.0.1:{self.port}/gateway",
        }

    def new_id(self) -> str:
        self.ids += 1
        return str(self.ids)

    # ── what tests do ───────────────────────────────────────────────────────

    def telegram(self, message: dict[str, Any]) -> None:
        self.updates.append({"update_id": self.next_update, "message": message})
        self.next_update += 1

    async def discord(self, event: str, data: dict[str, Any]) -> None:
        await self.dispatches.put({"t": event, "d": data})

    def sent(self, method: str) -> list[Any]:
        return [body for m, body in self.calls if m == method]

    async def until_sent(self, method: str, check=lambda body: True) -> Any:
        async def found() -> bool:
            return any(check(b) for b in self.sent(method))

        await _until(found)
        return next(b for b in self.sent(method) if check(b))

    # ── the server ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route, WebSocketRoute
        from starlette.websockets import WebSocket, WebSocketDisconnect

        async def telegram(request: Request) -> Response:
            token, method = request.path_params["token"], request.path_params["method"]
            assert token == f"bot{TG_TOKEN}"
            body = await request.json()
            if method != "getUpdates":
                self.calls.append((method, body))
            if method == "getUpdates":
                for _ in range(20):  # a short long poll
                    pending = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
                    if pending:
                        return JSONResponse({"ok": True, "result": pending})
                    await asyncio.sleep(0.05)
                return JSONResponse({"ok": True, "result": []})
            if method == "getFile":
                return JSONResponse({"ok": True, "result": {"file_id": body["file_id"],
                                                            "file_path": f"files/{body['file_id']}"}})
            if method == "sendMessage":
                return JSONResponse({"ok": True, "result": {"message_id": int(self.new_id())}})
            return JSONResponse({"ok": True, "result": True})

        async def telegram_file(request: Request) -> Response:
            assert request.path_params["token"] == f"bot{TG_TOKEN}"
            return Response(self.files[request.path_params["path"].removeprefix("files/")])

        async def discord(request: Request) -> Response:
            assert request.headers["authorization"] == f"Bot {DC_TOKEN}"
            path = "/" + request.path_params["path"]
            body = await request.json() if request.method in ("POST", "PATCH") and await request.body() else None
            self.calls.append((f"{request.method} {path}", body))
            parts = path.strip("/").split("/")
            if request.method == "GET" and parts[0] == "channels" and len(parts) == 2:
                info = self.channels.get(parts[1])
                return JSONResponse(info or {"message": "Unknown Channel"}, status_code=200 if info else 404)
            if path.endswith("/typing"):
                return Response(status_code=204)
            if path.endswith("/threads"):
                thread = {"id": self.new_id(), "type": 11, "owner_id": ME}
                self.channels[thread["id"]] = thread
                return JSONResponse(thread)
            if request.method == "POST" and path.endswith("/messages"):
                return JSONResponse({"id": self.new_id(), "channel_id": parts[1]})
            return JSONResponse({"id": parts[-1]})

        async def cdn(request: Request) -> Response:
            return Response(self.files[request.path_params["name"]])

        async def gateway(ws: WebSocket) -> None:
            await ws.accept()
            await ws.send_json({"op": 10, "d": {"heartbeat_interval": 45000}})
            opening = await ws.receive_json()
            self.gateway.append(opening)
            if opening["op"] == 2:
                self.seq += 1
                await ws.send_json({"op": 0, "s": self.seq, "t": "READY", "d": {
                    "session_id": "sess-1", "resume_gateway_url": f"ws://127.0.0.1:{self.port}/gateway",
                    "user": {"id": ME, "username": "jarvis"},
                }})
            else:
                self.seq += 1
                await ws.send_json({"op": 0, "s": self.seq, "t": "RESUMED", "d": {}})

            async def read() -> None:
                with contextlib.suppress(WebSocketDisconnect):
                    while True:
                        msg = await ws.receive_json()
                        self.gateway.append(msg)
                        if msg["op"] == 1:
                            await ws.send_json({"op": 11})

            reader = asyncio.create_task(read())
            try:
                while not reader.done():
                    try:
                        event = await asyncio.wait_for(self.dispatches.get(), 0.1)
                    except TimeoutError:
                        continue
                    if event.get("op") == 7:
                        await ws.send_json({"op": 7, "d": None})
                        continue
                    self.seq += 1
                    await ws.send_json({"op": 0, "s": self.seq, **event})
            finally:
                reader.cancel()

        app = Starlette(routes=[
            Route("/tg/{token}/{method}", telegram, methods=["POST"]),
            Route("/tg/file/{token}/{path:path}", telegram_file),
            Route("/cdn/{name}", cdn),
            Route("/dc/{path:path}", discord, methods=["GET", "POST", "PATCH"]),
            WebSocketRoute("/gateway", gateway),
        ])
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        await _until(lambda: _true(self._server.started))

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, 5)


async def _true(value: bool) -> bool:
    return value


async def _allow() -> None:
    from db import async_session
    from db.models import ConfigSetting

    async with async_session() as s:
        s.add_all([
            ConfigSetting(key="telegram.allowed_users", value="42, 43"),
            ConfigSetting(key="discord.allowed_users", value="7"),
            # Turns run on the fake model.
            ConfigSetting(key="models.custom", value=json.dumps([{"id": MODEL, "label": "Fake"}])),
            ConfigSetting(key="default.model", value=MODEL),
        ])
        await s.commit()


@pytest.fixture
async def chat():
    fake = FakeChat()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


def echoed(text: str) -> str:
    """The fake model's reply to `text`. Long enough that the run's token
    batch (64 characters) goes out before the reply holds."""
    return f"echo: {text} " + "·" * 64


class Bots:
    """The edge running both bots, its turns on a model that echoes each
    message and then holds until `release()`."""

    def __init__(self, fake: FakeOllama) -> None:
        self.fake = fake
        fake.pause = threading.Event()

        def echo(n: int, body: dict) -> Reply:
            return Reply(echoed(body["messages"][-1]["content"]))

        fake.script = echo

    def release(self) -> None:
        assert self.fake.pause is not None
        self.fake.pause.set()


@pytest.fixture
async def bots(database, work_dir: Path, edge_binary: Path, chat: FakeChat, fake: FakeOllama):
    await _allow()
    env = {**chat.env(), "JARVIS_RUN_JOBS": "1", "OLLAMA_HOST": fake.url, "JARVIS_APP_DIR": str(REPO),
           "HOME": str(work_dir), "JARVIS_BROWSER_CDP_URL": f"http://127.0.0.1:{_free_port()}"}
    harness = Bots(fake)
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env):
        try:
            yield harness
        finally:
            harness.release()


def _rows(db: Path, sql: str, *args: Any) -> list[dict[str, Any]]:
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args)]


def _tg_message(text: str | None = None, *, user: int = 42, chat_id: int = 100, **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"message_id": 1, "chat": {"id": chat_id, "type": "private"},
                               "from": {"id": user, "is_bot": False, "first_name": "U"}, "date": 0, **extra}
    if text is not None:
        message["text"] = text
    return message


# ── Telegram ─────────────────────────────────────────────────────────────────


async def test_telegram_message_round_trip(bots, chat, work_dir):
    db = work_dir / "database.db"
    # Pending updates are dropped at start, as start_polling(drop_pending_updates=True) does.
    await chat.until_sent("deleteWebhook")
    assert chat.sent("deleteWebhook") == [{"drop_pending_updates": True}]

    chat.telegram(_tg_message("hello there"))
    # The reply's message appears with the first text — never a placeholder.
    first = await chat.until_sent("sendMessage")
    assert first == {"chat_id": 100, "text": echoed("hello there")}
    assert chat.sent("sendChatAction")[0] == {"chat_id": 100, "action": "typing"}

    bots.release()
    # Done: the final text lands on the same message.
    await chat.until_sent("editMessageText", lambda b: b["text"] == echoed("hello there"))
    assert len(chat.sent("sendMessage")) == 1

    conv = _rows(db, "SELECT id, title, surface, model FROM conversations WHERE id = 'telegram_100'")
    assert conv == [{"id": "telegram_100", "title": "hello there", "surface": "telegram",
                     "model": conv[0]["model"]}]
    msgs = _rows(db, "SELECT role, content FROM messages WHERE conversation_id = 'telegram_100' ORDER BY rowid")
    assert msgs[0] == {"role": "user", "content": "hello there"}
    assert msgs[1]["role"] == "assistant"
    job = _rows(db, "SELECT kind, payload FROM jobs ORDER BY rowid DESC LIMIT 1")[0]
    assert job["kind"] == "chat"
    assert json.loads(job["payload"]) == {"query": "hello there", "model": conv[0]["model"], "conv_id": "telegram_100"}


async def test_telegram_ignores_strangers_and_commands(bots, chat, work_dir):
    chat.telegram(_tg_message("who are you", user=9))
    chat.telegram(_tg_message("/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}]))
    chat.telegram(_tg_message(None, sticker={"file_id": "s"}))
    # Then one it answers, so the three above have certainly been seen.
    chat.telegram(_tg_message("ping", chat_id=101))
    await chat.until_sent("sendMessage")
    assert [b["chat_id"] for b in chat.sent("sendMessage")] == [101]
    assert _rows(work_dir / "database.db", "SELECT id FROM conversations WHERE id LIKE 'telegram_%'") == [
        {"id": "telegram_101"}
    ]


async def test_telegram_message_mid_run_joins_it(bots, chat, work_dir):
    chat.telegram(_tg_message("first"))
    await chat.until_sent("sendMessage")
    chat.telegram(_tg_message("and also this"))
    await chat.until_sent("sendMessage", lambda b: b["text"] == QUEUED_NOTE)
    queued = _rows(work_dir / "database.db",
                   "SELECT content FROM messages WHERE conversation_id = 'telegram_100' AND status = 'queued'")
    assert queued == [{"content": "and also this"}]


async def test_telegram_voice_notes_and_photos_are_text_only(bots, chat, work_dir):
    chat.telegram(_tg_message(None, voice={"file_id": "vo", "duration": 2}))
    chat.telegram(_tg_message(None, chat_id=101, photo=[{"file_id": "ph", "width": 9, "height": 9}], caption="what"))
    for chat_id in (100, 101):
        sent = await chat.until_sent("sendMessage", lambda b, c=chat_id: b["chat_id"] == c)
        assert sent["text"] == "Only text messages are supported — send text instead."
    assert chat.sent("getFile") == []
    assert _rows(work_dir / "database.db", "SELECT id FROM jobs") == []


async def test_telegram_writes_what_python_writes(database, work_dir, tmp_path_factory, edge_binary, chat):
    """The edge's bot on a copy of the test database writes what Python's
    handlers wrote. Nothing runs the turn."""
    await _allow()
    b_dir = tmp_path_factory.mktemp("twin")
    with contextlib.closing(sqlite3.connect(work_dir / "database.db")) as src, \
            contextlib.closing(sqlite3.connect(b_dir / "database.db")) as dst:
        src.backup(dst)
    dirs = (str(work_dir), str(b_dir))
    tables = ("conversations", "messages", "jobs")

    # What Python's `handle_message` / `handle_unsupported` wrote for the same three.
    want = await recorded()

    async with _run_edge(edge_binary, b_dir, b_dir / "database.db", chat.env()):
        chat.telegram(_tg_message("hello", chat_id=100))
        await chat.until_sent("sendChatAction", lambda b: b["chat_id"] == 100)
        chat.telegram(_tg_message(None, chat_id=101, photo=[{"file_id": "img"}], caption="what is this"))
        await chat.until_sent("sendMessage", lambda b: b["chat_id"] == 101)
        chat.telegram(_tg_message(None, chat_id=102, voice={"file_id": "vo"}))
        await chat.until_sent("sendMessage", lambda b: b["chat_id"] == 102)

        async def written() -> bool:
            return len(_rows(b_dir / "database.db", "SELECT id FROM jobs")) == 1

        await _until(written)

    b = _dump(b_dir / "database.db")
    for table in tables:
        assert _mask(b[table], SESSION_START, dirs) == want[table], table


# ── Discord ──────────────────────────────────────────────────────────────────


def _dc_message(content: str, *, id: str = "m1", channel: str = "dm1", guild: str | None = None,
                author: str = "7", **extra: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"id": id, "channel_id": channel, "content": content,
                           "author": {"id": author, "username": "u"}, "mentions": [], "attachments": [],
                           "referenced_message": None, **extra}
    if guild is not None:
        msg["guild_id"] = guild
    return msg


async def _identified(chat: FakeChat) -> dict[str, Any]:
    await _until(lambda: _true(bool(chat.gateway)))
    return chat.gateway[0]


async def test_discord_identifies_and_answers_dms(bots, chat, work_dir):
    hello = await _identified(chat)
    assert hello["op"] == 2
    assert hello["d"]["token"] == DC_TOKEN
    assert hello["d"]["intents"] == 1 | 1 << 9 | 1 << 12 | 1 << 15
    chat.channels["dm1"] = {"id": "dm1", "type": 1}

    await chat.discord("MESSAGE_CREATE", _dc_message("hi bot"))
    reply = await chat.until_sent("POST /channels/dm1/messages")
    assert reply == {
        "content": echoed("hi bot"),
        "allowed_mentions": {"parse": [], "replied_user": False},
        "message_reference": {"message_id": "m1", "channel_id": "dm1", "fail_if_not_exists": False},
    }
    conv = _rows(work_dir / "database.db", "SELECT surface, title FROM conversations WHERE id = 'discord_dm1'")
    assert conv == [{"surface": "discord", "title": "hi bot"}]


async def test_discord_images_and_voice_notes_are_text_only(bots, chat, work_dir):
    await _identified(chat)
    chat.channels["dm1"] = {"id": "dm1", "type": 1}
    image = {"id": "f1", "filename": "a.png", "content_type": "image/png", "url": "http://x/a.png"}
    await chat.discord("MESSAGE_CREATE", _dc_message("what is this", attachments=[image]))
    reply = await chat.until_sent("POST /channels/dm1/messages")
    assert reply["content"] == "Only text messages are supported — send text instead."
    assert _rows(work_dir / "database.db", "SELECT id FROM jobs") == []


async def test_discord_in_a_server_needs_a_mention_and_opens_a_thread(bots, chat, work_dir):
    await _identified(chat)
    chat.channels["general"] = {"id": "general", "type": 0, "guild_id": "g1"}
    await chat.discord("MESSAGE_CREATE", _dc_message("just chatting", id="m1", channel="general", guild="g1"))
    # Mentioned, but by someone not on the allowlist.
    await chat.discord("MESSAGE_CREATE", _dc_message("hey", id="m2", channel="general", guild="g1",
                                                     author="8", mentions=[{"id": ME}]))
    await chat.discord("MESSAGE_CREATE", _dc_message(f"<@{ME}> plan my\nweek", id="m3", channel="general",
                                                     guild="g1", mentions=[{"id": ME}]))
    thread = await chat.until_sent("POST /channels/general/messages/m3/threads")
    assert thread == {"name": "plan my", "auto_archive_duration": 1440}
    thread_id = next(cid for cid, c in chat.channels.items() if c.get("owner_id") == ME)
    reply = await chat.until_sent(f"POST /channels/{thread_id}/messages")
    # In the new thread, so not a reply to a message in another channel.
    assert reply == {"content": echoed("plan my\nweek"), "allowed_mentions": {"parse": [], "replied_user": False}}
    convs = _rows(work_dir / "database.db", "SELECT id FROM conversations WHERE id LIKE 'discord_%'")
    assert convs == [{"id": f"discord_{thread_id}"}]
    assert not any(path.startswith("POST /channels/general/messages") and not path.endswith("/threads")
                   for path, _ in chat.calls)

    # Inside the thread it started, no mention is needed; mid-run, it joins the run.
    await chat.discord("MESSAGE_CREATE", _dc_message("and the weekend", id="m4", channel=thread_id, guild="g1"))
    note = await chat.until_sent(f"POST /channels/{thread_id}/messages", lambda b: b["content"] == QUEUED_NOTE)
    assert note["message_reference"]["message_id"] == "m4"


async def test_discord_resumes_after_reconnect(bots, chat):
    await _identified(chat)
    await chat.dispatches.put({"op": 7})
    await _until(lambda: _true(any(m["op"] == 6 for m in chat.gateway)))
    resume = next(m for m in chat.gateway if m["op"] == 6)
    assert resume["d"]["token"] == DC_TOKEN
    assert resume["d"]["session_id"] == "sess-1"
    assert resume["d"]["seq"] >= 1


# ── Notifications ────────────────────────────────────────────────────────────


async def test_notifications_go_straight_to_the_apis(chat, monkeypatch):
    from core import notifications

    for k, v in chat.env().items():
        monkeypatch.setenv(k, v)
    await notifications._send_telegram("555", "[ERROR] nightly\n\nboom")
    await notifications._send_discord("777", "x" * 2500)
    assert chat.sent("sendMessage") == [{"chat_id": "555", "text": "[ERROR] nightly\n\nboom"}]
    sent = chat.sent("POST /channels/777/messages")
    assert sent == [{"content": "x" * 1899 + "…", "allowed_mentions": {"parse": []}}]
