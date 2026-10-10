"""`jarvis-edge mcp add|list|get|remove` (`edge/src/cli/mcp.rs`).

The edge's own commands — `main.py` had none, so nothing is recorded: each
test says what it expects. A saved token never prints; a running server is
told to reconnect and asked for tools. `login` signs in to a fake OAuth
server (`fixtures/oauth_mcp_server.py`), the test playing the browser.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from edge_support import _free_port, _run_edge, edge_binary, fresh_db  # noqa: F401 — edge_binary is a fixture

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


# ── login ────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _guarded(**env: str):
    """The OAuth-protected MCP server, its base URL."""
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(FIXTURES / "oauth_mcp_server.py"), str(port)],
                            env={**os.environ, **env})
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "the OAuth fixture didn't start"
                time.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _login(cli: Cli, name: str, *extra: str, paste: bool = False) -> Out:
    """`mcp login`, the test following its link as a browser would — back to
    the CLI's listener, or pasting where it lands."""
    proc = subprocess.Popen([str(cli.binary), "mcp", "login", name, "--no-browser", *extra], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=cli.env, cwd=cli.work)
    seen = []
    assert proc.stdout is not None and proc.stdin is not None
    for line in proc.stdout:
        seen.append(line)
        if "/authorize?" in line:
            back = httpx.get(line.strip(), follow_redirects=False)
            assert back.status_code == 302, back.text
            if paste:
                proc.stdin.write(back.headers["location"] + "\n")
                proc.stdin.flush()
            else:
                page = httpx.get(back.headers["location"])
                assert (page.status_code, "Signed in to Jarvis" in page.text) == (200, True)
            break
    out, err = proc.communicate(timeout=60)
    return Out(proc.returncode, "".join(seen) + out, err)


def _grant(cli: Cli, name: str) -> dict | None:
    with contextlib.closing(sqlite3.connect(cli.db)) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM mcp_oauth WHERE server = ?", (name,)).fetchone()
    return dict(row) if row else None


@pytest.mark.parametrize("paste, env", [(False, {}), (True, {"NO_HINT": "1"})], ids=["browser", "pasted-no-hint"])
def test_login_signs_in(cli, paste, env):
    with _guarded(**env) as base:
        assert cli("add", "g", f"{base}/mcp").code == 0
        out = _login(cli, "g", paste=paste)
        assert out.code == 0, out.err
        assert "Signed in to g" in out.out
        grant = _grant(cli, "g")
        assert grant is not None
        assert (grant["url"], grant["resource"], grant["token_endpoint"], grant["scope"]) == (
            f"{base}/mcp", f"{base}/mcp", f"{base}/token", "mcp")
        assert grant["access_token"] and grant["refresh_token"] and grant["expires_at"]
        assert grant["access_token"] not in out.out + out.err
        assert "Sign-in: OAuth, renewed as it expires" in cli("get", "g").out
        assert httpx.get(f"{base}/log").json() == ["authorization_code"]
        # Signing in again replaces the grant.
        assert _login(cli, "g").code == 0
        assert _grant(cli, "g")["access_token"] != grant["access_token"]

        assert "Signed out of g" in cli("logout", "g").out
        assert _grant(cli, "g") is None
        assert "Not signed in: g" in cli("logout", "g").out


def test_a_server_without_registration_takes_a_client_id(cli):
    with _guarded(NO_REGISTRATION="1") as base:
        cli("add", "g", f"{base}/mcp")
        out = _login(cli, "g")
        assert out.code == 1
        assert "doesn't let apps register themselves" in out.err and "--client-id" in out.err
        out = _login(cli, "g", "--client-id", "fixed")
        assert out.code == 0, out.err
        assert _grant(cli, "g")["client_id"] == "fixed"


def test_login_refusals(cli):
    with _guarded() as base:
        cli("add", "cmd", "--", "npx", "x")
        cli("add", "open", f"{base}/log")  # answers without asking for sign-in
        for name, message in [("nope", "No MCP server named"), ("cmd", "runs a command"),
                              ("open", "didn't ask for sign-in (HTTP 405)")]:
            out = cli("login", name, "--no-browser")
            assert (out.code, message in out.err) == (1, True), (name, out.err)


def test_removing_a_server_forgets_its_sign_in(cli):
    with _guarded() as base:
        cli("add", "g", f"{base}/mcp")
        assert _login(cli, "g").code == 0
        assert cli("remove", "g").code == 0
        assert _grant(cli, "g") is None


async def test_a_running_server_uses_and_renews_the_sign_in(cli, edge_binary):
    call = 'mutation { callMcpTool(server: "g", tool: "whoami", argsJson: "{}") { content isError } }'
    with _guarded() as base:
        cli("add", "g", f"{base}/mcp")
        assert _login(cli, "g").code == 0
        env = {"HOME": cli.env["HOME"], "JARVIS_APP_DIR": str(cli.app)}
        async with _run_edge(edge_binary, cli.work, cli.db, env) as client:
            client.timeout = httpx.Timeout(60)
            cli.env["JARVIS_EDGE_BIND"] = str(client.base_url).removeprefix("http://").rstrip("/")

            async def gql(q: str) -> dict:
                return (await client.post("/graphql", json={"query": q})).json()["data"]

            assert (await gql(call))["callMcpTool"] == {"content": "signed in", "isError": False}

            # Turned away: renewed and tried again.
            httpx.post(f"{base}/revoke-all")
            assert (await gql(call))["callMcpTool"] == {"content": "signed in", "isError": False}
            assert httpx.get(f"{base}/log").json() == ["authorization_code", "refresh_token"]

            # Expired: renewed before it's sent.
            with contextlib.closing(sqlite3.connect(cli.db)) as c:
                c.execute("UPDATE mcp_oauth SET expires_at = '2020-01-01 00:00:00.000000'")
                c.commit()
            servers = (await gql("mutation { reloadMcpServers { name toolCount } }"))["reloadMcpServers"]
            assert servers == [{"name": "g", "toolCount": 1}]
            assert httpx.get(f"{base}/log").json()[-1] == "refresh_token"
            assert _grant(cli, "g")["expires_at"] > "2026"

            # Signed out: refused, and adding one that needs sign-in says so.
            assert "Signed out of g" in cli("logout", "g").out
            servers = (await gql("{ mcpServers { name toolCount } }"))["mcpServers"]
            assert servers == [{"name": "g", "toolCount": 0}]
            added = cli("add", "g2", f"{base}/mcp")
            assert "Not connected" in added.out and "jarvis-edge mcp login g2" in added.out
