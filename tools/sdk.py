"""Kernel-side SDK — preloaded into every run_cell kernel as `jarvis`.

These are plain sync functions the agent calls from Python code, NOT bound
LLM tools. Keeping them out of the tool schemas is what keeps the per-call
prompt small (`edge/src/agent/tools.json` is all that's bound); the agent
discovers them with `jarvis.help()` instead of paying for their schemas on
every call.

Two transports, chosen by what the operation needs:

* **Reads** go straight to the app database over a read-only sqlite3
  connection (`mode=ro` — cannot take write locks against the server).
* **Writes**, and anything that needs the server — embedding a query for
  `search_memory`, a human's approval — go through the server's GraphQL API
  over HTTP. The kernel is a separate process, so a direct DB write would miss
  the side effects that make a write take effect: the scheduler reloading an
  automation (a missed reload means the cron silently never fires), board
  dispatch. Routing through the mutation runs that in the server, and gets
  the mutation's own argument validation for free.

What stays a bound tool: anything coupled to the agent loop — the todo tools,
complete/block_task (current-run lifecycle), spawn_workers/run_workflow (runs
on the agent's own model), write_artifact (its live event belongs to the run),
and `remember`.

The SDK needs nothing but the standard library, httpx and numpy: it finds the
database as the server does (`DATABASE_URL`, else `$WORK_DIR/database.db`),
and its scope is injected per kernel by the server (`edge/src/kernels/`) via
`set_conversation()` / `set_project()`.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

from tools.text_dedupe import dedupe_against

_conversation_id: str | None = None
_project_id: str | None = None

DEFAULT_API_URL = "http://127.0.0.1:8000/graphql"


def set_conversation(conversation_id: str | None) -> None:
    """Scope subsequent calls to a conversation. Called by the kernel bootstrap."""
    global _conversation_id
    _conversation_id = conversation_id


def set_project(project_id: str | None) -> None:
    """Scope project_memory to a project. Called by the kernel bootstrap."""
    global _project_id
    _project_id = project_id


def _work_dir() -> Path:
    """`$WORK_DIR`, else `~/.jarvis` — as the server resolves it."""
    return Path(os.environ.get("WORK_DIR") or Path.home() / ".jarvis")


def _db_path() -> str:
    """`DATABASE_URL` (a `sqlite:///…` URL), else `$WORK_DIR/database.db`."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return str(_work_dir() / "database.db")
    if "sqlite" not in url:
        raise RuntimeError(f"jarvis SDK requires a sqlite DATABASE_URL, got: {url}")
    return url.rsplit(":///", 1)[-1]


def _artifacts_dir() -> Path:
    """`$ARTIFACTS_DIR`, else `$WORK_DIR/artifacts`."""
    return Path(os.environ.get("ARTIFACTS_DIR") or _work_dir() / "artifacts")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """A read-only connection, closed on exit.

    `with sqlite3.connect(...)` is a *transaction* context manager — it commits
    or rolls back and leaves the connection open, so the old form relied on GC
    to close. That is fine until a call raises: the traceback pins the frame,
    and IPython keeps tracebacks in the kernel namespace, so one failed SDK call
    would hold a connection for the kernel's whole 30-minute idle life.
    """
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ── Artifacts ─────────────────────────────────────────────────────────────────

def list_artifacts(all_conversations: bool = False) -> list[dict]:
    """Saved artifacts, newest first — current conversation unless all_conversations."""
    sql = (
        "SELECT a.id, a.title, a.conversation_id, a.updated_at,"
        " (SELECT COALESCE(MAX(v.version), 0) FROM artifact_versions v"
        "  WHERE v.artifact_id = a.id) AS versions"
        " FROM artifacts a"
    )
    params: tuple = ()
    if not all_conversations:
        sql += " WHERE a.conversation_id = ?"
        params = (_conversation_id,)
    sql += " ORDER BY a.updated_at DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def read_artifact(artifact_id: str, version: int | None = None) -> str:
    """Markdown content of an artifact — latest, or a specific version."""
    artifacts_dir = _artifacts_dir()
    with _connect() as conn:
        art = conn.execute(
            "SELECT id FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if art is None:
            raise LookupError(f"Artifact not found: {artifact_id}")
        if version is not None:
            ver = conn.execute(
                "SELECT filename FROM artifact_versions WHERE artifact_id = ? AND version = ?",
                (artifact_id, version),
            ).fetchone()
            if ver is None:
                raise LookupError(f"Artifact version {version} not found for {artifact_id}")
            path = Path(ver["filename"])
            if not path.exists():
                path = artifacts_dir / f"{artifact_id}_v{version}.md"
            if not path.exists():
                raise LookupError(f"Artifact version file missing: {artifact_id} v{version}")
            return path.read_text(encoding="utf-8")
    path = artifacts_dir / f"{artifact_id}.md"
    if not path.exists():
        raise LookupError(f"Artifact file missing on disk: {artifact_id}")
    return path.read_text(encoding="utf-8")


def list_artifact_versions(artifact_id: str) -> list[dict]:
    """Version history for an artifact, oldest first."""
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT version, title, created_at FROM artifact_versions"
                " WHERE artifact_id = ? ORDER BY version",
                (artifact_id,),
            )
        ]


# ── Conversations ─────────────────────────────────────────────────────────────

# How many matching messages to pull per requested conversation. Search ranks
# messages but returns conversations, so the top `limit` rows would collapse to
# far fewer than `limit` results whenever one chat dominates the ranking.
_CONV_SEARCH_FANOUT = 20
_SNIPPET_TOKENS = 14
_MESSAGE_TRUNCATE = 2000


_FTS_TOKEN_RE = re.compile(r"[0-9A-Za-z_]+")
_MAX_FTS_TERMS = 24
# BM25's IDF already de-weights these to near zero; dropping them keeps the
# candidate scan from touching most of the table. Deliberately small — an
# aggressive stoplist eats meaningful tokens ("no", "on", "can").
_STOPWORDS = frozenset("""
a an and are as at be by for from has have how i if in is it its of on or that
the their then there these they this to was what when where which who will with
you your me my do does did but not can could would should
""".split())


def _fts_match_expr(query: str) -> str | None:
    """Free text as a safe FTS5 MATCH expression — never raw user text — or
    None when nothing usable survives (`edge/src/agent/retrieve.rs` has the
    same)."""
    seen: set[str] = set()
    terms: list[str] = []
    for tok in _FTS_TOKEN_RE.findall(query):
        low = tok.lower()
        if len(low) < 2 or low in _STOPWORDS or low in seen:
            continue
        seen.add(low)
        terms.append(f'"{low}"')
        if len(terms) >= _MAX_FTS_TERMS:
            break
    return " OR ".join(terms) if terms else None


def _has_fts(conn: sqlite3.Connection, name: str) -> bool:
    """Whether an FTS mirror exists — a SQLite build without FTS5 has none."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone() is not None


def _conversation_scope(project_only: bool, surface: str | None) -> tuple[str, list]:
    """WHERE fragment (over `conversations c`) + params shared by the reads below.

    Ephemeral (incognito) conversations are never returned: the point of that
    flag is that nothing about the run outlives it.
    """
    clauses = ["COALESCE(c.ephemeral, 0) = 0"]
    params: list = []
    if surface:
        clauses.append("c.surface = ?")
        params.append(surface)
    if project_only and _project_id:
        clauses.append("c.project_id = ?")
        params.append(_project_id)
    return " AND ".join(clauses), params


def list_conversations(limit: int = 20) -> list[dict]:
    """The other conversations in this project, most recently active first.

    Only meaningful inside a project, where the set is bounded and every
    member is about the same thing — it answers "what else has been worked on
    here". Outside a project there is no such set, only the user's entire
    history, so this raises; use search_conversations, which needs a question.

    Incognito conversations are never listed. Project members are always web
    conversations, so there is no surface to choose.
    """
    if not _project_id:
        raise RuntimeError(
            "list_conversations only works inside a project. "
            "Use search_conversations(query) to find a past chat by what it says."
        )
    where, params = _conversation_scope(True, "web")
    last_message = "(SELECT MAX(m.created_at) FROM messages m WHERE m.conversation_id = c.id)"
    with _connect() as conn:
        rows = conn.execute(
            "SELECT c.id AS conversation_id, c.title, c.surface, c.project_id, c.created_at,"
            " (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS messages,"
            f" {last_message} AS last_message_at"
            f" FROM conversations c WHERE {where}"
            f" ORDER BY COALESCE({last_message}, c.created_at) DESC LIMIT ?",
            [*params, max(1, limit)],
        ).fetchall()
    return [dict(r) for r in rows]


def search_conversations(
    query: str, project_only: bool = True, surface: str | None = "web", limit: int = 8
) -> list[dict]:
    """Past conversations that mention `query`, best match first.

    Keyword search (BM25) over message text plus a substring match on titles —
    there is no embedding here, so unlike search_memory it rewards the exact
    tokens you expect on the page (names, ids, filenames, error strings) rather
    than a paraphrase of the idea. In a project this searches that project's
    other chats; pass project_only=False to search every conversation instead.

    The current conversation is excluded — you are already in it. Each hit
    carries a `snippet` and a `conversation_id` to pass to read_conversation.
    """
    where, params = _conversation_scope(project_only, surface)
    if _conversation_id:
        where += " AND c.id != ?"
        params.append(_conversation_id)
    cols = "c.id AS conversation_id, c.title, c.surface, c.project_id"
    expr = _fts_match_expr(query)
    hits: dict[str, dict] = {}

    with _connect() as conn:
        if expr and _has_fts(conn, "messages_fts"):
            rows = conn.execute(
                f"SELECT {cols}, m.created_at,"
                " snippet(messages_fts, 0, '', '', '…', ?) AS snippet"
                " FROM messages_fts"
                " JOIN messages m ON m.rowid = messages_fts.rowid"
                " JOIN conversations c ON c.id = m.conversation_id"
                f" WHERE messages_fts MATCH ? AND {where}"
                " ORDER BY bm25(messages_fts) LIMIT ?",
                [_SNIPPET_TOKENS, expr, *params, max(1, limit) * _CONV_SEARCH_FANOUT],
            ).fetchall()
        else:
            # No FTS5 in this SQLite build, or nothing indexable survived the
            # query (all stopwords) — substring scan so search still answers,
            # just unranked. Newest first is the only ordering available.
            needle = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute(
                f"SELECT {cols}, m.created_at, m.content AS snippet"
                " FROM messages m JOIN conversations c ON c.id = m.conversation_id"
                f" WHERE m.content LIKE ? ESCAPE '\\' AND {where}"
                " ORDER BY m.created_at DESC LIMIT ?",
                [f"%{needle}%", *params, max(1, limit) * _CONV_SEARCH_FANOUT],
            ).fetchall()

        for row in rows:
            hit = hits.get(row["conversation_id"])
            if hit is None:
                hit = dict(row)
                hit.pop("created_at", None)
                hit["last_match_at"] = row["created_at"]
                hit["matches"] = 0
                hit["matched"] = "message"
                hit["snippet"] = _excerpt(row["snippet"], query)
                hits[row["conversation_id"]] = hit
            hit["matches"] += 1
            if row["created_at"] and row["created_at"] > (hit["last_match_at"] or ""):
                hit["last_match_at"] = row["created_at"]

        # A title can be the only trace of a topic (a chat *about* the Q3 budget
        # may never spell it out), so titles are matched independently.
        title_needle = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        for row in conn.execute(
            f"SELECT {cols}, c.created_at FROM conversations c"
            f" WHERE {where} AND c.title LIKE ? ESCAPE '\\'"
            " ORDER BY c.created_at DESC LIMIT ?",
            [*params, f"%{title_needle}%", max(1, limit)],
        ):
            if row["conversation_id"] in hits:
                continue
            hit = dict(row)
            hit["last_match_at"] = hit.pop("created_at", None)
            hit["matches"] = 0
            hit["matched"] = "title"
            hit["snippet"] = ""
            hits[row["conversation_id"]] = hit

    return list(hits.values())[: max(1, limit)]


def _excerpt(text: str, query: str) -> str:
    """A short window of `text`, centered on the query when it appears verbatim.

    FTS5 `snippet()` already returns a window; this only has to do real work on
    the LIKE fallback path, where the whole message body comes back.
    """
    text = (text or "").strip()
    if len(text) <= 240:
        return text
    at = text.lower().find(query.strip().lower())
    if at < 0:
        return text[:240] + "…"
    start = max(0, at - 100)
    return ("…" if start else "") + text[start:start + 240].strip() + "…"


def read_conversation(
    conversation_id: str | None = None, limit: int = 40, max_chars: int = _MESSAGE_TRUNCATE
) -> dict:
    """The last `limit` messages of a conversation, oldest first.

    Defaults to the current conversation, which is how you re-read turns that
    have dropped out of context after compaction. Message bodies over
    `max_chars` are truncated; tool steps are not included, only what the user
    and the assistant said.
    """
    target = conversation_id or _conversation_id
    if not target:
        raise RuntimeError("No conversation scope — pass a conversation_id.")
    with _connect() as conn:
        conv = conn.execute(
            "SELECT id, title, surface, project_id, created_at FROM conversations WHERE id = ?",
            (target,),
        ).fetchone()
        if conv is None:
            raise LookupError(f"Conversation not found: {target}")
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (target,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT role, content, status, created_at FROM messages"
            " WHERE conversation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (target, max(1, limit)),
        ).fetchall()
    messages = []
    for row in reversed(rows):
        content = row["content"] or ""
        truncated = len(content) > max_chars
        messages.append({
            "role": row["role"],
            "content": content[:max_chars] + ("… [truncated]" if truncated else ""),
            "status": row["status"],
            "created_at": row["created_at"],
        })
    out = dict(conv)
    out["conversation_id"] = out.pop("id")
    out["total_messages"] = total
    out["messages"] = messages
    return out


# ── Task board ────────────────────────────────────────────────────────────────

def list_tasks(status: str | None = None) -> list[dict]:
    """Board tasks (durable background work items), highest priority first.

    status: "todo" | "ready" | "running" | "blocked" | "done" | "archived";
    None lists everything except archived.
    """
    sql = (
        "SELECT id, title, status, priority, blocked_reason, blocked_kind,"
        " summary, created_at, updated_at FROM board_tasks"
    )
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    else:
        sql += " WHERE status != 'archived'"
    sql += " ORDER BY priority DESC, created_at"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


# ── Memory ────────────────────────────────────────────────────────────────────

def search_memory(query: str, k: int = 5) -> list[dict]:
    """Top-k long-term memory facts for `query` by cosine similarity."""
    data = api("query($q: String!, $k: Int!) { searchMemory(query: $q, k: $k) { id text score } }",
               {"q": query, "k": k})
    return data["searchMemory"]


# ── GraphQL transport (write paths) ───────────────────────────────────────────

def _global_id(type_name: str, raw_id: str) -> str:
    """Relay GlobalID — base64("TypeName:rawId"), what the mutations expect."""
    import base64

    return base64.b64encode(f"{type_name}:{raw_id}".encode()).decode()


def api(query: str, variables: dict | None = None, timeout: float = 30.0) -> dict:
    """POST a GraphQL query/mutation to the local server and return `data`.

    The endpoint comes from $JARVIS_API_URL (default http://127.0.0.1:8000/graphql).
    Raises RuntimeError carrying the server's message on a GraphQL error.
    """
    import httpx

    url = os.environ.get("JARVIS_API_URL") or DEFAULT_API_URL
    # Identify the caller so the server can gate destructive writes. A write
    # the agent initiates needs a human's say-so; the same mutation from the
    # web UI *is* the human's say-so and must not be gated.
    headers = {"X-Jarvis-Caller": "agent"}
    if _conversation_id:
        headers["X-Jarvis-Conversation"] = _conversation_id
    try:
        resp = httpx.post(
            url,
            json={"query": query, "variables": variables or {}},
            headers=headers,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach the Jarvis API at {url}: {exc}") from exc
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        messages = "; ".join(e.get("message", str(e)) for e in payload["errors"])
        raise RuntimeError(f"GraphQL error: {messages}")
    return payload.get("data") or {}


def _camel(payload: dict) -> dict:
    """snake_case kwargs -> camelCase GraphQL input keys, dropping Nones."""
    out = {}
    for key, value in payload.items():
        if value is None:
            continue
        head, *rest = key.split("_")
        out[head + "".join(p.title() for p in rest)] = value
    return out


# ── Automations ───────────────────────────────────────────────────────────────

def list_automations() -> list[dict]:
    """All automations with id, name, input_type, schedule, enabled."""
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, name, description, input_type, schedule, enabled, stateful"
                " FROM automations ORDER BY name"
            )
        ]


_RUN_PREVIEW = 200


def list_automation_runs(automation_id: str | None = None, limit: int = 20) -> list[dict]:
    """Automation runs, newest first, with status and a preview of the output.

    One automation's runs, or every automation's when automation_id is None.
    status: "pending" | "running" | "done" | "no_change" (a monitor saw nothing
    new) | "error" | "stopped" | "skipped". `preview` is the start of the output,
    or of the error when there is none; read_automation_run has the whole text.
    """
    sql = (
        "SELECT r.id, r.automation_id, a.name AS automation, r.status, r.triggered_by,"
        " r.started_at, r.finished_at, r.output, r.error"
        " FROM automation_runs r JOIN automations a ON a.id = r.automation_id"
    )
    params: tuple = ()
    with _connect() as conn:
        if automation_id is not None:
            if conn.execute("SELECT 1 FROM automations WHERE id = ?", (automation_id,)).fetchone() is None:
                raise LookupError(f"Automation not found: {automation_id}")
            sql += " WHERE r.automation_id = ?"
            params = (automation_id,)
        sql += " ORDER BY r.started_at DESC, r.rowid DESC LIMIT ?"
        rows = conn.execute(sql, (*params, max(1, limit))).fetchall()
    runs = []
    for row in rows:
        run = dict(row)
        output, error = run.pop("output"), run.pop("error")
        text = (output or error or "").strip()
        run["preview"] = text[:_RUN_PREVIEW] + ("…" if len(text) > _RUN_PREVIEW else "")
        runs.append(run)
    return runs


def read_automation_run(run_id: str) -> dict:
    """One automation run in full: status, times, its whole output and error."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT r.id, r.automation_id, a.name AS automation, r.status, r.triggered_by,"
            " r.started_at, r.finished_at, r.output, r.error"
            " FROM automation_runs r JOIN automations a ON a.id = r.automation_id"
            " WHERE r.id = ?",
            (run_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Automation run not found: {run_id}")
    return dict(row)


def create_automation(
    name: str,
    input_type: str,
    prompt_text: str | None = None,
    schedule: str | None = None,
    model: str | None = None,
    code_text: str | None = None,
    webhook_url: str | None = None,
    webhook_method: str | None = None,
    webhook_headers: str | None = None,
    webhook_body: str | None = None,
    description: str | None = None,
    enabled: bool = True,
    stateful: bool = False,
) -> dict:
    """Create an automation — a task that runs on a cron schedule or on demand.

    input_type:
      "prompt"  — an agent run; set prompt_text (+ optional model, stateful=True
                  to share one conversation across runs).
      "code"    — set code_text; runs as a Python subprocess.
      "webhook" — set webhook_url (+ method / headers-JSON / body).
      "monitor" — always-stateful prompt run that watches prompt_text's target
                  (e.g. "NVDA close; alert below 150") and notifies only on change.
    schedule is a cron expression ("0 9 * * *" = daily 9am), interpreted in the
    server's local timezone, not UTC; None = manual only.
    """
    data = api(
        "mutation($input: AutomationInput!) { createAutomation(input: $input)"
        " { id name inputType schedule enabled } }",
        {
            "input": _camel(
                dict(
                    name=name,
                    input_type=input_type,
                    prompt_text=prompt_text,
                    schedule=schedule,
                    model=model,
                    code_text=code_text,
                    webhook_url=webhook_url,
                    webhook_method=webhook_method,
                    webhook_headers=webhook_headers,
                    webhook_body=webhook_body,
                    description=description,
                    enabled=enabled,
                    stateful=stateful,
                )
            )
        },
    )
    return data["createAutomation"]


def update_automation(automation_id: str, **fields) -> dict:
    """Update an automation. Pass only the fields to change.

    The mutation takes a whole AutomationInput, so current values are read
    first and merged with `fields`. Keys are the create_automation arg names.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT name, description, input_type, prompt_text, model, code_text,"
            " webhook_url, webhook_method, webhook_headers, webhook_body,"
            " schedule, enabled, stateful FROM automations WHERE id = ?",
            (automation_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Automation not found: {automation_id}")
    merged = dict(row)
    merged.update(fields)
    merged["enabled"] = bool(merged["enabled"])
    merged["stateful"] = bool(merged["stateful"])
    data = api(
        "mutation($id: ID!, $input: AutomationInput!) {"
        " updateAutomation(id: $id, input: $input) { id name schedule enabled } }",
        {"id": _global_id("Automation", automation_id), "input": _camel(merged)},
    )
    return data["updateAutomation"]


def delete_automation(automation_id: str) -> bool:
    """Delete an automation by id."""
    data = api(
        "mutation($id: ID!) { deleteAutomation(id: $id) }",
        {"id": _global_id("Automation", automation_id)},
    )
    return bool(data["deleteAutomation"])


# ── Task board (create; complete/block stay bound tools) ──────────────────────

def create_task(
    title: str,
    body: str,
    priority: int = 0,
    depends_on: list[str] | None = None,
    model: str | None = None,
    skill: str | None = None,
    start: bool = True,
    decompose: bool = False,
) -> dict:
    """Create a durable board task that runs in the background on its own agent.

    depends_on: ids of tasks that must finish first; their completion summaries
    are handed to this task as context. start=False parks it in todo.
    decompose=True has a planner split it into parallel subtasks, with this
    task running last as the synthesis step (cannot combine with depends_on).
    For an in-conversation checklist use the write_todos tool instead.
    """
    if decompose and depends_on:
        raise ValueError("decompose cannot be combined with depends_on")
    data = api(
        "mutation($input: BoardTaskInput!) { createBoardTask(input: $input)"
        " { id title status } }",
        {
            "input": _camel(
                dict(
                    title=title,
                    body=body,
                    priority=priority,
                    parent_ids=[_global_id("BoardTask", p) for p in (depends_on or [])] or None,
                    model=model,
                    skill=skill,
                    start=False if decompose else start,
                )
            )
        },
    )
    task = data["createBoardTask"]
    if decompose:
        api(
            "mutation($id: ID!) { decomposeBoardTask(id: $id) { id title } }",
            {"id": task["id"]},
        )
    return task


# ── Workflows (run_workflow stays a bound tool) ───────────────────────────────

def list_workflows() -> list[dict]:
    """All saved workflows with id, name, description."""
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, name, description FROM workflows ORDER BY name"
            )
        ]


def read_workflow(workflow_id: str) -> dict:
    """A workflow including its full `definition` JSON (nodes + edges)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, description, definition FROM workflows WHERE id = ?",
            (workflow_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Workflow not found: {workflow_id}")
    return dict(row)


def create_workflow(name: str, definition: str | dict, description: str | None = None) -> dict:
    """Create a workflow — a graph of nodes for multi-step pipelines.

    definition is JSON (dict or string) with `nodes` + `edges` lists. Node
    types include agent / conditional / map / start / router / sequential /
    parallel / loop / approval / planner.
    """
    if isinstance(definition, dict):
        definition = json.dumps(definition)
    data = api(
        "mutation($input: WorkflowCreateInput!) { createWorkflow(input: $input)"
        " { id name } }",
        {"input": _camel(dict(name=name, definition=definition, description=description))},
    )
    return data["createWorkflow"]


def update_workflow(workflow_id: str, **fields) -> dict:
    """Update a workflow. Pass only what changes: name, description, definition."""
    if isinstance(fields.get("definition"), dict):
        fields["definition"] = json.dumps(fields["definition"])
    data = api(
        "mutation($id: ID!, $input: WorkflowUpdateInput!) {"
        " updateWorkflow(id: $id, input: $input) { id name } }",
        {"id": _global_id("Workflow", workflow_id), "input": _camel(fields)},
    )
    return data["updateWorkflow"]


def delete_workflow(workflow_id: str) -> bool:
    """Delete a workflow by id."""
    data = api(
        "mutation($id: ID!) { deleteWorkflow(id: $id) }",
        {"id": _global_id("Workflow", workflow_id)},
    )
    return bool(data["deleteWorkflow"])


# ── Skills ────────────────────────────────────────────────────────────────────

def list_skills() -> list[dict]:
    """All skills with id, name, description, enabled (bodies not included)."""
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, name, description, enabled FROM skills ORDER BY name"
            )
        ]


def use_skill(name: str) -> str:
    """Load a saved skill's full body — the procedure to follow."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT body, enabled FROM skills WHERE name = ?", (name,)
        ).fetchone()
    if row is None:
        raise LookupError(f"Skill not found: {name}. Use jarvis.list_skills() to see them.")
    return row["body"]


def create_skill(name: str, description: str, body: str, enabled: bool = True) -> dict:
    """Save a reusable procedure you can reload later with use_skill(name).

    name: unique kebab-case handle. description: one line on WHEN to use it —
    the routing key matched against future intent, so make it trigger-oriented.
    body: the full markdown procedure; only loaded on use, so be detailed.
    """
    data = api(
        "mutation($input: SkillCreateInput!) { createSkill(input: $input) { id name } }",
        {"input": _camel(dict(name=name, description=description, body=body, enabled=enabled))},
    )
    return data["createSkill"]


def update_skill(skill_id: str, **fields) -> dict:
    """Update a skill. Pass only what changes: name, description, body, enabled.

    Changing the description re-embeds it for intent retrieval.
    """
    data = api(
        "mutation($id: ID!, $input: SkillUpdateInput!) {"
        " updateSkill(id: $id, input: $input) { id name } }",
        {"id": _global_id("Skill", skill_id), "input": _camel(fields)},
    )
    return data["updateSkill"]


def delete_skill(skill_id: str) -> bool:
    """Delete a skill by id."""
    data = api(
        "mutation($id: ID!) { deleteSkill(id: $id) }",
        {"id": _global_id("Skill", skill_id)},
    )
    return bool(data["deleteSkill"])


# ── Project memory ────────────────────────────────────────────────────────────

# Project memory is injected uncached into every LLM call of every conversation
# in the project, so unbounded growth is a per-turn tax forever. The cap forces
# a condense; the dedup keeps `append` from re-stating what is already there.
# (tools/projects.py holds the same cap, but that tool is unbound — this is the
# only path the agent actually reaches.)
#
# The dedup helpers live in tools/text_dedupe, which the server's consolidation
# merge ports, so both answer "is this already said?" the same way.
_PROJECT_MEMORY_CAP = 24_000


def project_memory(action: str = "read", content: str | None = None) -> str:
    """Read or update the shared memory of this conversation's project.

    A short shared summary — NOT a log — injected into the context of every
    conversation in the project on every turn. Append only what would make a
    future conversation act differently: stack, architecture decisions,
    project-specific conventions, key paths, goals/status. If in doubt, don't
    write. Global facts (user info, general prefs) belong in `remember`;
    current-task progress belongs in todos.

    `append` silently drops lines already present (near-matches included) and
    refuses to push memory past 24k chars — condense with `write` instead.

    action: "read" | "append" (add a note) | "write" (replace the whole memory).
    """
    if not _project_id:
        return "This conversation does not belong to a project — project memory is unavailable."
    with _connect() as conn:
        row = conn.execute(
            "SELECT memory FROM projects WHERE id = ?", (_project_id,)
        ).fetchone()
    current = (row["memory"] if row else "") or ""
    if action == "read":
        return current or "(project memory is empty)"
    if action not in ("append", "write"):
        raise ValueError(f"unknown action {action!r}; use read, append, or write")
    if not content:
        raise ValueError(f"{action} requires content")

    note = ""
    if action == "append":
        addition, dropped = dedupe_against(current, content)
        if not addition:
            return (
                f"Nothing appended — all {dropped} line(s) are already in project "
                "memory. Read it before writing."
            )
        new = f"{current.rstrip()}\n\n{addition}".strip()
        if dropped:
            note = f"; {dropped} duplicate line(s) dropped"
    else:
        new = content.strip()

    if len(new) > _PROJECT_MEMORY_CAP:
        raise ValueError(
            f"project memory would be {len(new)} chars, over the "
            f"{_PROJECT_MEMORY_CAP} cap. Read it, condense it, and call "
            'project_memory(action="write", content=<condensed>) instead.'
        )

    api(
        "mutation($id: ID!, $input: ProjectUpdateInput!) {"
        " updateProject(id: $id, input: $input) { id } }",
        {"id": _global_id("Project", _project_id), "input": {"memory": new}},
    )
    return f"Project memory updated ({len(new)} chars{note})."


# ── MCP (external tool servers) ───────────────────────────────────────────────
# Servers set to `lazy` are connected but unbound: their tool schemas are kept
# out of every LLM call, and reached from here instead. The call itself runs in
# the SERVER process (the MCP client, and any stdio subprocess it owns, live
# there) — this is an API round-trip, not a second MCP client.

_MCP_CALL_TIMEOUT = 120.0


def mcp_servers() -> list[dict]:
    """List connected MCP servers: name, load mode, and tool count.

    load_mode "always" means that server's tools are already bound as normal
    tools — call them directly instead of going through mcp_call.
    """
    data = api("{ mcpServers { name transport loadMode toolCount } }")
    return data["mcpServers"]


def mcp_tools(server: str | None = None) -> list[dict]:
    """Tool names + descriptions for one MCP server (or all of them).

    Descriptions only — call mcp_help(server, tool) for the argument schema.
    """
    if server:
        data = api(
            "query($s: String) { mcpTools(server: $s) { name server description } }",
            {"s": server},
        )
    else:
        data = api("{ mcpTools { name server description } }")
    return data["mcpTools"]


def mcp_help(server: str, tool: str) -> str:
    """Full description + JSON argument schema for one MCP tool.

    Read this before the first call to a tool — argument names are the server's,
    not something to guess.
    """
    import json as _json

    data = api(
        "query($s: String) { mcpTools(server: $s) { name description inputSchema } }",
        {"s": server},
    )
    for entry in data["mcpTools"]:
        if entry["name"] == tool:
            try:
                schema = _json.dumps(_json.loads(entry["inputSchema"]), indent=2)
            except Exception:
                schema = entry["inputSchema"]
            return f"{server}.{tool}\n\n{entry['description']}\n\nArguments (JSON Schema):\n{schema}"
    available = ", ".join(e["name"] for e in data["mcpTools"]) or "(none)"
    return f"No tool {tool!r} on MCP server {server!r}. Available: {available}"


def mcp_call(server: str, tool: str, args: dict | None = None, **kwargs) -> str:
    """Call one MCP tool and return its output as text.

    Arguments go in `args` (or as keywords). A failure *inside* the tool comes
    back as text prefixed with "MCP tool error:" rather than raising, so you can
    read the server's own message and retry with corrected arguments. An unknown
    server or tool name raises — check mcp_tools(server) for the real names.
    """
    import json as _json

    payload = {**(args or {}), **kwargs}
    # A gated MCP tool blocks inside the resolver until a human answers, so the
    # client has to be willing to wait that long. Checked here rather than
    # always waiting the ceiling: an ungated call that hangs should still fail
    # in seconds, not in half an hour.
    client_timeout = _MCP_CALL_TIMEOUT + 15.0
    if _policy_for(f"mcp:{server}/{tool}").approval:
        client_timeout = _gate_timeout() + _MCP_CALL_TIMEOUT + 15.0
    data = api(
        "mutation($server: String!, $tool: String!, $args: String!, $t: Float!) {"
        " callMcpTool(server: $server, tool: $tool, argsJson: $args, timeoutSeconds: $t)"
        " { content isError } }",
        {
            "server": server,
            "tool": tool,
            "args": _json.dumps(payload, default=str),
            "t": _MCP_CALL_TIMEOUT,
        },
        timeout=client_timeout,
    )
    result = data["callMcpTool"]
    if result["isError"]:
        return f"MCP tool error: {result['content']}"
    return result["content"]


# ── Tool policy ───────────────────────────────────────────────────────────────
# Every function below is reachable from a `run_cell` kernel, a separate
# process from the server. The gate here is the other half of the server's
# (`edge/src/approvals.rs`): the server records a durable request, and this
# side *blocks on the row* until a human answers it, polling over the same
# read-only connection every other read uses. `run_cell`'s cell timeout is
# suspended while that request is open (`edge/src/kernels/mod.rs:hold`), which
# is what makes the wait real rather than a minute long.
#
# The policy is the `tools.policy` setting: a JSON map of tool key
# (`sdk:<name>`, `mcp:<server>/<tool>`) to `{enabled, approval}`, holding only
# what differs from the default (enabled, no approval). It is read at most
# every `_POLICY_TTL` seconds, so a toggle in Settings reaches a running kernel
# without any signal from the server; a policy that can't be read is the
# default.

_GATE_POLL_SECONDS = 1.5
_POLICY_TTL = 2.0
_policies: tuple[float, dict[str, Any]] | None = None


class _Policy(NamedTuple):
    enabled: bool = True
    approval: bool = False


def _policy_for(key: str) -> _Policy:
    global _policies
    now = time.monotonic()
    if _policies is None or now - _policies[0] >= _POLICY_TTL:
        stored: Any = {}
        try:
            with _connect() as conn:
                row = conn.execute("SELECT value FROM config_settings WHERE key = 'tools.policy'").fetchone()
            stored = json.loads(row["value"]) if row and row["value"] else {}
        except (sqlite3.Error, ValueError, TypeError):
            pass
        _policies = (now, stored if isinstance(stored, dict) else {})
    entry = _policies[1].get(key)
    if not isinstance(entry, dict):
        return _Policy()
    return _Policy(bool(entry.get("enabled", True)), bool(entry.get("approval", False)))


def _gate_timeout() -> float:
    """How long a gated call waits for a human before denying itself:
    `JARVIS_TOOL_GATE_TIMEOUT` (at least 10 s), else 30 minutes — as the
    server's own gate waits."""
    try:
        return max(10.0, float(os.environ["JARVIS_TOOL_GATE_TIMEOUT"]))
    except (KeyError, ValueError):
        return 1800.0


def _denial(tool: str, answer: str) -> str:
    """What the agent reads when a human says no (`approvals::denial_message`)."""
    reason = f" ({answer})" if answer and answer.lower() not in ("no", "deny", "denied") else ""
    return f"Denied by a human{reason}: `{tool}` was not run. Do not retry it — continue without it, or say what you need and why."


def _await_gate(approval_id: str, deadline: float) -> tuple[bool, str]:
    """Poll the approval row until it leaves `pending`. (approved, answer)."""
    while time.time() < deadline:
        with _connect() as conn:
            row = conn.execute(
                "SELECT status, answer FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            return (False, "the approval request no longer exists")
        if row["status"] != "pending":
            return (row["status"] == "approved", row["answer"] or "")
        time.sleep(_GATE_POLL_SECONDS)
    return (False, "timed out waiting for approval")


def _request_gate(tool_key: str, tool_name: str, args: dict) -> str:
    """Record the request server-side and return its id.

    Through the API rather than the DB for the usual reason: this connection is
    read-only, and the row has to be visible to the inbox and to whatever
    in-process waiter might resolve it.
    """
    data = api(
        "mutation($k: String!, $t: String!, $a: String!, $c: String) {"
        " requestToolApproval(toolKey: $k, tool: $t, argsJson: $a, conversationId: $c)"
        " { id status } }",
        {
            "k": tool_key,
            "t": tool_name,
            "a": json.dumps(args, default=str)[:8000],
            "c": _conversation_id,
        },
    )
    return data["requestToolApproval"]["id"]


def _enforce_policy(fn_name: str, args: dict) -> None:
    """Raise unless this SDK call is allowed to proceed right now."""
    key = f"sdk:{fn_name}"
    policy = _policy_for(key)
    if not policy.enabled:
        raise RuntimeError(
            f"jarvis.{fn_name} is switched off in Settings → Tools. "
            "Do not retry it; use another approach or tell the user what you need."
        )
    if not policy.approval:
        return

    approval_id = _request_gate(key, f"jarvis.{fn_name}", args)
    approved, answer = _await_gate(approval_id, time.time() + _gate_timeout())
    if not approved:
        raise RuntimeError(_denial(f"jarvis.{fn_name}", answer))


def _policed(fn):
    """Wrap one SDK function with its policy check, preserving its signature —
    `help()` renders from `inspect.signature`, so the wrapper must be transparent."""
    import functools
    import inspect

    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            bound = signature.bind_partial(*a, **kw)
            shown = dict(bound.arguments)
        except TypeError:
            shown = dict(kw)
        _enforce_policy(fn.__name__, shown)
        return fn(*a, **kw)

    return wrapper


# ── Discovery ─────────────────────────────────────────────────────────────────

_CATEGORIES: dict[str, tuple[str, list]] = {
    "artifacts": (
        "read/list saved deliverables (write_artifact stays a tool)",
        [list_artifacts, read_artifact, list_artifact_versions],
    ),
    "conversations": (
        "search and re-read past chats (in a project, its other conversations)",
        [search_conversations, read_conversation, list_conversations],
    ),
    "automations": (
        "scheduled or on-demand tasks (cron, code, webhook, monitor)",
        [
            list_automations,
            list_automation_runs,
            read_automation_run,
            create_automation,
            update_automation,
            delete_automation,
        ],
    ),
    "board": (
        "durable background tasks that run on their own agent",
        [list_tasks, create_task],
    ),
    "workflows": (
        "multi-step node graphs (run_workflow stays a tool)",
        [list_workflows, read_workflow, create_workflow, update_workflow, delete_workflow],
    ),
    "skills": (
        "saved reusable procedures you can author and reload",
        [list_skills, use_skill, create_skill, update_skill, delete_skill],
    ),
    "memory": (
        "long-term facts and per-project shared memory",
        [search_memory, project_memory],
    ),
    "mcp": (
        "external MCP tool servers loaded on demand",
        [mcp_servers, mcp_tools, mcp_help, mcp_call],
    ),
}


def _apply_policy_wrappers() -> None:
    """Route every discoverable function through `_policed`, in one place.

    Decorating each definition would be ~30 identical lines and one silent
    omission away from a gate that does not apply; doing it from the catalogue
    means a function is policed exactly when it is discoverable.
    """
    for category, (blurb, funcs) in list(_CATEGORIES.items()):
        wrapped = [_policed(fn) for fn in funcs]
        _CATEGORIES[category] = (blurb, wrapped)
        for fn in wrapped:
            globals()[fn.__name__] = fn


_apply_policy_wrappers()


def _is_enabled(fn_name: str) -> bool:
    return _policy_for(f"sdk:{fn_name}").enabled


def help(category: str | None = None) -> str:  # noqa: A001 — deliberate `jarvis.help`
    """List what the jarvis SDK can do. Call with a category name for signatures."""
    import inspect

    if category is None:
        lines = ["jarvis SDK — call jarvis.help('<category>') for full signatures.", ""]
        lines += [
            f"  {name:<14}{blurb}"
            for name, (blurb, funcs) in _CATEGORIES.items()
            if any(_is_enabled(fn.__name__) for fn in funcs)
        ]
        return "\n".join(lines)

    key = category.strip().lower()
    if key not in _CATEGORIES:
        return f"Unknown category {category!r}. Available: {', '.join(_CATEGORIES)}"
    blurb, funcs = _CATEGORIES[key]
    # A disabled function is omitted rather than listed as unavailable: the
    # point of the catalogue is what you can do, and advertising a locked door
    # only invites a call that raises.
    funcs = [fn for fn in funcs if _is_enabled(fn.__name__)]
    if not funcs:
        return f"jarvis.{key} — every tool in this category is switched off."
    out = [f"jarvis.{key} — {blurb}", ""]
    for fn in funcs:
        out.append(f"jarvis.{fn.__name__}{inspect.signature(fn)}")
        doc = inspect.getdoc(fn) or ""
        out += [f"    {line}" for line in doc.splitlines()]
        out.append("")
    return "\n".join(out).rstrip()
