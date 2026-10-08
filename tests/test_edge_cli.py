"""The command line in the edge (`edge/src/cli/`) against `main.py`.

Each command runs twice, as a subprocess: `main.py` over one database and
`jarvis-edge` over a copy. What they print, their exit codes and what they
leave in the database are diffed. The edge runs with a `JARVIS_APP_DIR`
that has no checkout in it, so nothing it does can lean on the Python code;
only `run`, which reads the system prompt from the checkout, is given it.

Tables are compared by their contents, not their borders: `main.py` draws
Rich tables, the edge aligned columns.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from edge_support import edge_binary, fresh_db  # noqa: F401 — edge_binary is a fixture
from python_golden import RECORD, portable, recorded_sync
from test_edge_loop import MODEL, FakeOllama, Reply
from test_edge_model_sync import _PROVIDER_ENV, Fake, _providers

REPO = Path(__file__).resolve().parent.parent


@dataclass
class Out:
    code: int
    out: str


def _env(work: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _PROVIDER_ENV}
    env.update({
        "WORK_DIR": str(work),
        "NO_COLOR": "1",
        "COLUMNS": "200",
        # Nothing a provider reads but what a test gives it.
        "AWS_CONFIG_FILE": str(work / "aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(work / "aws-credentials"),
        "AWS_EC2_METADATA_DISABLED": "true",
    })
    for var in ("FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE", "CLICOLOR_FORCE"):
        env.pop(var, None)
    env.update(extra or {})
    return env


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _out(proc: subprocess.CompletedProcess) -> Out:
    # Rich pads a line to the console width; the edge doesn't.
    text = _ANSI.sub("", proc.stdout)
    return Out(proc.returncode, "\n".join(line.rstrip() for line in text.splitlines()))


class Twins:
    def __init__(self, root: Path, binary: Path) -> None:
        self.py, self.rs, self.binary = root / "py", root / "rs", binary
        # An app dir with no checkout in it.
        self.no_python = root / "no-python"
        for d in (self.py, self.rs, self.no_python):
            d.mkdir(parents=True)

    def python(self, *args: str, stdin: str | None = None, env: dict[str, str] | None = None) -> Out:
        """What `main.py` printed and exited with, as recorded."""
        code, out = recorded_sync(lambda: self._main(*args, stdin=stdin, env=env))
        return Out(code, out.replace("<tmp>", str(self.py.parent)))

    def _main(self, *args: str, stdin: str | None = None, env: dict[str, str] | None = None) -> tuple[int, str]:
        proc = subprocess.run([sys.executable, str(REPO / "main.py"), *args], cwd=self.py, input=stdin,
                              capture_output=True, text=True, env=_env(self.py, env), timeout=120)
        out = _out(proc)
        return out.code, out.out.replace(str(self.py.parent), "<tmp>")

    def edge(self, *args: str, stdin: str | None = None, env: dict[str, str] | None = None,
             checkout: bool = False) -> Out:
        extra = {"JARVIS_APP_DIR": str(REPO if checkout else self.no_python), **(env or {})}
        proc = subprocess.run([str(self.binary), *args], cwd=self.rs, input=stdin,
                              capture_output=True, text=True, env=_env(self.rs, extra), timeout=120)
        return _out(proc)

    def same(self, *args: str, **kw) -> Out:
        python, edge = self.python(*args, **kw), self.edge(*args, **kw)
        assert edge == python, args
        return python

    def rows(self, sql: str) -> tuple[list, list]:
        """Python's rows (as recorded) and the edge's."""
        def read(db: Path) -> list:
            with sqlite3.connect(db) as conn:
                return conn.execute(sql).fetchall()
        return recorded_sync(lambda: read(self.py / "database.db")), read(self.rs / "database.db")

    def same_rows(self, sql: str) -> list:
        python, edge = (portable(rows) for rows in self.rows(sql))
        assert edge == python, sql
        return python

    def seed(self, sql: str, *args) -> None:
        for db in (self.py / "database.db", self.rs / "database.db"):
            with sqlite3.connect(db) as conn:
                conn.execute(sql, args)

    def setting(self, key: str, value: str) -> None:
        self.seed("INSERT OR REPLACE INTO config_settings (key, value, updated_at) VALUES (?, ?, ?)",
                  key, value, "2026-10-05 00:00:00.000000")


SETTINGS = "SELECT key, value FROM config_settings ORDER BY key"
KV = "SELECT namespace, key, value FROM kv_store WHERE namespace != 'jarvis.migrations' ORDER BY namespace, key"


@pytest.fixture
def twins(tmp_path: Path, edge_binary: Path) -> Twins:
    t = Twins(tmp_path, edge_binary)
    assert t.python("config", "list").code == 0  # Python made its schema
    fresh_db(edge_binary, t.rs / "database.db")
    if not RECORD:
        # Somewhere for seeds to go; what Python read back was recorded.
        fresh_db(edge_binary, t.py / "database.db")
    return t


def test_config(twins):
    twins.same("config", "set", "foo.bar", "hello")
    twins.same("config", "set", "foo.bar", "héllo wörld")
    twins.same("config", "get", "foo.bar")
    twins.same("config", "get", "nope")
    twins.same("config", "set", "a.b", "1")
    twins.same("config", "delete", "a.b")
    twins.same("config", "delete", "a.b")
    twins.same_rows(SETTINGS)
    for side in (twins.python("config", "list"), twins.edge("config", "list")):
        assert side.code == 0
        assert re.search(r"foo\.bar\s+.*héllo wörld", side.out)


def test_config_list_after_the_settings_are_gone(twins):
    # Each side's `init_db` writes its migration marker back first.
    twins.seed("DELETE FROM config_settings")
    for side in (twins.python("config", "list"), twins.edge("config", "list")):
        assert side.code == 0 and "migration.artifact_message_ids" in side.out
    twins.same_rows("SELECT key, value FROM config_settings")


def test_model_writes(twins):
    twins.same("model", "add", "google_genai:x-1", "X One", "--context-window", "1048576")
    twins.same("model", "add", "nocolon", "Y")
    twins.same("model", "add", "zz:abc", "Z")
    twins.same("config", "set", "models.endpoints", '[{"name": "lab", "base_url": "http://lab.test/v1"}]')
    twins.same("model", "add", "lab:m1", "Lab M1")
    # An edit without a window keeps the one the row had.
    twins.same("model", "add", "google_genai:x-1", "X One renamed")
    twins.same("model", "add", "ollama:q", "Q", "--provider", "lab")
    twins.same("model", "add", "lab:m2", "Lab M2", "--context-window", "0")
    twins.same("model", "remove", "lab:m2")
    twins.same("model", "remove", "lab:m2")
    twins.same("model", "remove", "google_genai:gemini-2.0-flash")
    twins.same("model", "set-default", "nope:x")
    twins.same("model", "set-default", "lab:m1")
    rows = twins.same_rows(SETTINGS)
    custom = dict(rows)["models.custom"]
    assert '"context_window": 1048576' in custom and "X One renamed" in custom

    for side in (twins.python("model", "list"), twins.edge("model", "list")):
        assert side.code == 0
        assert re.search(r"lab:m1\s+.*Lab M1\s+.*custom\s+.*◀ default", side.out)
        assert re.search(r"google_genai:x-1\s+.*X One renamed\s+.*custom", side.out)


def test_memory(twins, tmp_path):
    # The LangGraph store import runs once, on each side.
    twins.same("memory", "show")
    twins.same_rows("SELECT namespace, key FROM kv_store")
    note = tmp_path / "agents.md"
    note.write_bytes("# Notes\r\nPrefers metric — café ✓\r\n".encode())
    twins.same("memory", "set", str(note))
    twins.same("memory", "set", str(note))  # unchanged: updated_at stays
    twins.same_rows(KV)
    python, edge = twins.python("memory", "show"), twins.edge("memory", "show")
    stamp = re.compile(r"^Updated: \S+ \S+ \((\d+) chars\)$")
    assert stamp.match(python.out.splitlines()[0]) and stamp.match(edge.out.splitlines()[0])
    assert stamp.match(edge.out.splitlines()[0])[1] == stamp.match(python.out.splitlines()[0])[1] == "32"
    assert "Prefers metric — café ✓" in edge.out
    twins.same("memory", "reset", stdin="n\n")
    twins.same_rows(KV)
    twins.same("memory", "reset", "--yes")
    twins.same("memory", "reset", "-y")
    empty = tmp_path / "empty.md"
    empty.write_text("  \n\t\n")
    twins.same("memory", "set", str(empty))
    twins.same("memory", "set", str(tmp_path / "missing.md"))
    twins.same_rows(KV)


def test_a_database_that_isnt_there_yet(tmp_path, edge_binary):
    fresh = Twins(tmp_path, edge_binary)
    assert fresh.same("config", "get", "x") == Out(0, "Not set: x")
    assert fresh.same("memory", "show").code == 0
    with sqlite3.connect(fresh.rs / "database.db") as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] > 26


def test_what_python_fails_on_fails_here(twins, tmp_path):
    """Python dies of these with a traceback; the edge exits the same way,
    saying why in a line."""
    def error(*args: str) -> str:
        proc = subprocess.run([str(twins.binary), *args], cwd=twins.rs, capture_output=True, text=True,
                              env=_env(twins.rs, {"JARVIS_APP_DIR": str(twins.no_python)}), timeout=120)
        return proc.stderr

    # A catalog that won't load.
    twins.setting('models.custom', '[{"id": 5}]')
    assert twins.same("model", "list") == Out(1, "")
    assert "the models.custom setting has a malformed row (model id 5)" in error("model", "list")
    twins.setting('models.custom', '[]')
    # A file that isn't UTF-8 text.
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe")
    assert twins.same("memory", "set", str(tmp_path / "bad.md")) == Out(1, "")
    assert "bad.md isn't UTF-8 text" in error("memory", "set", str(tmp_path / "bad.md"))


@pytest.fixture
def provider(tmp_path):
    f = Fake()
    yield f
    f.close()


def test_model_sync(twins, provider):
    twins.setting('models.custom', (
        '[{"id": "local:qwen-7b", "label": "Qwen"},'
        ' {"id": "bedrock:anthropic.claude-3-haiku-20240307-v1:0", "label": "Haiku 3"}]'
    ))
    twins.setting('models.endpoints', f'[{{"name": "local", "base_url": "{provider.url}/ep/", "api_key": "ep-key"}}]')
    env = {k: v for k, v in _providers(provider).items() if "OPENROUTER" not in k}
    said = {}
    for args in (["google_genai"], ["anthropic"], ["bedrock"], ["ollama"], ["local"], ["google_genai", "--probe"],
                 ["meta"], ["nope"]):
        said[" ".join(args)] = twins.same("model", "sync", *args, env=env).out
    # The fake gave each something to report.
    assert "gone — in the catalog, no longer offered:" in said["google_genai"]
    assert "new — offered, not in the catalog" in said["bedrock"]
    assert "context_window available — catalog has None:" in said["local"]
    assert "unreachable — offered but this credential cannot call it:" in said["google_genai --probe"]
    assert said["meta"].startswith("No discovery adapter for 'meta'.")
    # Nothing to reach: every provider says why.
    twins.same("model", "sync", "google_genai")
    twins.same("model", "sync", "local", "--add-new", env=env)
    twins.same("model", "sync", "google_genai", "--add-new", "--include-non-chat", env=env)
    twins.same_rows(SETTINGS)


@pytest.fixture
def model():
    f = FakeOllama()
    yield f
    f.server.shutdown()


def test_run(twins, model):
    twins.setting('models.custom', f'[{{"id": "{MODEL}", "label": "Fake"}}]')
    env = {"OLLAMA_HOST": model.url, "JARVIS_BROWSER_CDP_URL": "http://127.0.0.1:9"}
    script = [Reply("", [("write_todos", {"todos": ["look", "answer"]})]), Reply("The answer is **42**.")]

    def python_run() -> tuple[tuple[int, str], list]:
        model.reset(script)
        return twins._main("run", "--model", MODEL, "What is it?", env=env), model.requests

    (code, out), asked = recorded_sync(python_run)
    python = Out(code, out)
    model.reset(script)
    # The agent reads its prompt from the checkout. That it's the edge, not
    # Python, shows in the reply: raw Markdown, no Rich panel.
    edge = twins.edge("run", "--model", MODEL, "What is it?", env=env, checkout=True)
    assert python.code == edge.code == 0
    assert "The answer is 42." in python.out  # Rich renders the Markdown
    assert edge.out == "The answer is **42**."
    assert len(model.requests) == len(asked) == 2
    assert model.requests[1]["messages"][-1]["content"] == asked[1]["messages"][-1]["content"]

    # A typo'd model runs on the default, and says so.
    twins.setting('default.model', MODEL)
    model.reset([Reply("ok")])
    edge = subprocess.run([str(twins.binary), "run", "--model", "ollama:typo", "hi"], cwd=twins.rs, capture_output=True,
                          text=True, env=_env(twins.rs, {**env, "JARVIS_APP_DIR": str(REPO)}), timeout=120)
    assert (edge.returncode, edge.stdout.strip()) == (0, "ok")
    assert "Unknown model 'ollama:typo' — running on ollama:fake instead." in edge.stderr

