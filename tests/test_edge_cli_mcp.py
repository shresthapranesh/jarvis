"""`jarvis-edge mcp add|list|get|remove` (`edge/src/cli/mcp.rs`).

The edge's own commands — `main.py` had none, so nothing is recorded: each
test says what it expects. A saved token never prints; a running server is
told to reconnect and asked for tools.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from edge_support import _run_edge, edge_binary, fresh_db  # noqa: F401 — edge_binary is a fixture

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class Out:
    code: int
    out: str
    err: str


class Cli:
    def __init__(self, root: Path, binary: Path) -> None:
        self.binary, self.work, self.app = binary, root / "work", root / "app"
        self.work.mkdir()
        self.app.mkdir()
        self.db = self.work / "database.db"
        # Nothing but the database configures a server.
        self.env = {
            **{k: v for k, v in os.environ.items() if k not in ("JARVIS_MCP_SERVERS", "MCP_SERVERS")},
            "HOME": str(root), "WORK_DIR": str(self.work), "DATABASE_URL": f"sqlite+aiosqlite:///{self.db}",
            "JARVIS_APP_DIR": str(self.app), "NO_COLOR": "1", "JARVIS_EDGE_BIND": "127.0.0.1:1",
        }

    def __call__(self, *args: str, stdin: str = "") -> Out:
        proc = subprocess.run([str(self.binary), "mcp", *args], input=stdin, capture_output=True, text=True,
                              env=self.env, cwd=self.work, timeout=120)
        return Out(proc.returncode, re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout), proc.stderr)

    def saved(self) -> dict:
        with sqlite3.connect(self.db) as c:
            row = c.execute("SELECT value FROM config_settings WHERE key = 'mcp.servers'").fetchone()
        return json.loads(row[0]) if row else {}


@pytest.fixture
def cli(tmp_path: Path, edge_binary: Path) -> Cli:
    c = Cli(tmp_path, edge_binary)
    fresh_db(edge_binary, c.db)
    return c


def test_a_token_is_saved_and_never_printed(cli):
    out = cli("add", "booking", "https://example.com/mcp", "--token", "-", "-H", "X-Team: t1", stdin="s3cret\n")
    assert out.code == 0, out.err
    assert "Added MCP server booking" in out.out
    assert "isn't running" in out.out
    assert cli.saved()["booking"] == {
        "transport": "http", "url": "https://example.com/mcp",
        "headers": {"X-Team": "t1", "Authorization": "Bearer s3cret"},
    }

    get = cli("get", "booking")
    assert get.code == 0
    assert "s3cret" not in get.out and "t1" not in get.out
    assert json.loads(get.out[get.out.index("{"):get.out.rindex("}") + 1])["headers"] == {
        "X-Team": "••••", "Authorization": "••••",
    }
    listed = cli("list")
    assert "s3cret" not in listed.out
    assert re.search(r"booking\s+http\s+https://example.com/mcp\s+always\s+—\s+settings", listed.out)


def test_a_command_and_its_env(cli):
    out = cli("add", "gh", "--lazy", "-e", "GITHUB_TOKEN=abc", "--", "npx", "-y", "server-github")
    assert out.code == 0, out.err
    assert cli.saved()["gh"] == {
        "transport": "stdio", "command": "npx", "args": ["-y", "server-github"],
        "env": {"GITHUB_TOKEN": "abc"}, "x-jarvis-load": "lazy",
    }
    get = cli("get", "gh")
    assert "abc" not in get.out and "Load: lazy" in get.out
    # A scheme goes as written.
    assert cli("add", "basic", "https://x.test/mcp", "--token", "Basic dXNlcg==").code == 0
    assert cli.saved()["basic"]["headers"] == {"Authorization": "Basic dXNlcg=="}


@pytest.mark.parametrize(
    "args, message",
    [
        (["x"], "give the server's URL"),
        (["x", "https://x.test", "--", "npx"], "not both"),
        (["x", "https://x.test", "-e", "K=v"], "--env is for a command"),
        (["x", "--token", "t", "--", "npx"], "--token and --header are for a URL"),
        (["x", "https://x.test", "-t", "grpc"], "--transport must be one of"),
        (["x", "not a url"], "invalid URL"),
        (["x", "https://x.test", "-H", "nocolon"], "--header takes Name: value"),
        (["x", "https://x.test", "--token", "t", "-H", "authorization: b"], "don't give it with --header too"),
        (["x", "https://x.test", "--token", "-"], "the token is empty"),
    ],
)
def test_refused(cli, args, message):
    out = cli("add", *args)
    assert out.code == 1
    assert message in out.err
    assert cli.saved() == {}


def test_names_are_unique_and_remove_finds_them(cli):
    assert cli("add", "a", "https://x.test/mcp").code == 0
    again = cli("add", "a", "https://y.test/mcp")
    assert again.code == 1 and "already exists" in again.err
    assert cli("remove", "a").code == 0
    assert cli.saved() == {}
    gone = cli("remove", "a")
    assert gone.code == 1 and "No MCP server named 'a'" in gone.err.replace('"', "'")
    assert "No MCP servers configured." in cli("list").out


def test_a_server_from_mcp_json_is_not_removed_here(cli):
    (cli.app / "mcp.json").write_text(json.dumps({"mcpServers": {"filed": {"url": "https://x.test/mcp"}}}))
    assert "file/env" in cli("list").out
    out = cli("remove", "filed")
    assert out.code == 1 and "remove it there" in out.err


async def test_a_running_server_connects_it(cli, edge_binary):
    async with _run_edge(edge_binary, cli.work, cli.db, {"HOME": cli.env["HOME"], "JARVIS_APP_DIR": str(cli.app)}) as client:
        cli.env["JARVIS_EDGE_BIND"] = str(client.base_url).removeprefix("http://").rstrip("/")
        out = cli("add", "ping", "--", sys.executable, str(FIXTURES / "other_mcp_server.py"))
        assert out.code == 0, out.err
        assert "Connected — 1 tool" in out.out
        assert re.search(r"ping\s+stdio\s+.*\s+always\s+1\s+settings", cli("list").out)
        assert "Tools (1): ping" in cli("get", "ping").out
        servers = (await client.post("/graphql", json={"query": "{ mcpServers { name toolCount } }"})).json()
        assert {"name": "ping", "toolCount": 1} in servers["data"]["mcpServers"]

        assert cli("remove", "ping").code == 0
        servers = (await client.post("/graphql", json={"query": "{ mcpServers { name } }"})).json()
        assert servers["data"]["mcpServers"] == []
