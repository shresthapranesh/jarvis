"""Kernel-side SDK — preloaded into every run_cell kernel as `jarvis`.

These are plain sync functions the agent calls from Python code, NOT bound
LLM tools. Keeping them out of the tool schemas is what keeps the per-call
prompt small (see core/agents.py `main_tools`); the agent discovers them with
`jarvis.help()` instead of paying for their schemas on every call.

Two transports, chosen by what the operation needs:

* **Reads** go straight to the app database over a read-only sqlite3
  connection (`mode=ro` — cannot take write locks against the server), plus
  `core.doc_index.get_embedder()` for the semantic searches.
* **Writes** go through the server's own GraphQL API over HTTP. The kernel is
  a separate process, so a direct DB write would miss the in-process side
  effects that make a write actually take effect — `_register_scheduler_job`
  for automations (a missed registration means the cron silently never fires)
  and `dispatch_board_tasks()` for board tasks. Routing through the mutation
  runs that code in the server where it belongs, and gets the mutation's own
  argument validation for free.

What deliberately stays a bound tool: anything coupled to the agent graph —
todos (`Command` state deltas), complete/block_task (current-run lifecycle),
spawn_workers/run_workflow (subgraphs on the parent's LLM), write_artifact/
write_artifact_file (their live side-panel event is tied to this run's
stream writer), and `remember` (there is no createMemory mutation to route to).

A third kind of helper (e.g. `text_to_speech`) is neither a DB read nor a
write — pure local compute, callable directly with no transport at all.

Conversation and project scope are injected per kernel by core/kernels.py via
`set_conversation()` / `set_project()`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

_conversation_id: str | None = None
_project_id: str | None = None
_embedding_override_applied = False

DEFAULT_API_URL = "http://127.0.0.1:8000/graphql"


def set_conversation(conversation_id: str | None) -> None:
    """Scope subsequent calls to a conversation. Called by the kernel bootstrap."""
    global _conversation_id
    _conversation_id = conversation_id


def set_project(project_id: str | None) -> None:
    """Scope project_memory to a project. Called by the kernel bootstrap."""
    global _project_id
    _project_id = project_id


def _db_path() -> str:
    from core.config import get_config

    url = get_config().database_url
    if "sqlite" not in url:
        raise RuntimeError(f"jarvis SDK requires a sqlite database_url, got: {url}")
    return url.rsplit(":///", 1)[-1]


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


def _embedder() -> Any:
    """The app's embeddings client, honoring the `embedding.model` config row."""
    global _embedding_override_applied
    from core.doc_index import configure_embedding_model, get_embedder

    if not _embedding_override_applied:
        _embedding_override_applied = True
        try:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT value FROM config_settings WHERE key = 'embedding.model'"
                ).fetchone()
            if row is not None:
                value = row["value"]
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    pass
                if isinstance(value, str) and value:
                    configure_embedding_model(value)
        except sqlite3.Error:
            pass

    embedder = get_embedder()
    if embedder is None:
        raise RuntimeError("No embedding model available (GOOGLE_API_KEY unset?).")
    return embedder


def _cosine_top_k(qvec: Any, rows: list[tuple[bytes, Any]], k: int) -> list[tuple[float, Any]]:
    """Score (embedding_bytes, payload) rows against qvec, best first."""
    import numpy as np

    q = np.asarray(qvec, dtype=np.float32)
    qnorm = float(np.linalg.norm(q)) or 1.0
    scored: list[tuple[float, Any]] = []
    for blob, payload in rows:
        vec = np.frombuffer(blob, dtype=np.float32)
        if vec.shape != q.shape:  # embedded with a different model — skip
            continue
        score = float(np.dot(vec, q) / ((float(np.linalg.norm(vec)) or 1.0) * qnorm))
        scored.append((score, payload))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[:k]


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
    from pathlib import Path

    from core.config import get_config

    artifacts_dir = get_config().artifacts_dir
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


# ── Indexed documents ─────────────────────────────────────────────────────────

_READ_WINDOW_CHARS = 6000


def search_documents(query: str, k: int = 6) -> list[dict]:
    """Top-k passages from this conversation's indexed attachments.

    Phrase `query` as the content you want to find. Follow up with
    read_document(document_id, offset=hit["seq"]) to read around a hit.
    """
    if not _conversation_id:
        raise RuntimeError("No conversation scope — document search is only available in chats.")
    qvec = _embedder().embed_query(query)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT c.embedding, c.document_id, c.seq, c.text, d.filename"
            " FROM document_chunks c JOIN documents d ON c.document_id = d.id"
            " WHERE c.conversation_id = ? AND c.embedding IS NOT NULL",
            (_conversation_id,),
        ).fetchall()
    hits = _cosine_top_k(qvec, [(r["embedding"], r) for r in rows], k)
    return [
        {
            "document_id": r["document_id"],
            "filename": r["filename"],
            "seq": r["seq"],
            "score": round(score, 4),
            "text": r["text"],
        }
        for score, r in hits
    ]


def read_document(document_id: str, offset: int = 0) -> dict:
    """Sequential window of an indexed document; continue with offset=next_offset."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT c.text, d.filename"
            " FROM document_chunks c JOIN documents d ON c.document_id = d.id"
            " WHERE c.document_id = ? ORDER BY c.seq",
            (document_id,),
        ).fetchall()
    if not rows:
        raise LookupError(
            f"Document {document_id} has no index — small attachments are inlined in the message."
        )
    total = len(rows)
    offset = max(0, min(offset, total - 1))
    parts: list[str] = []
    used = 0
    i = offset
    while i < total and used < _READ_WINDOW_CHARS:
        parts.append(rows[i]["text"])
        used += len(rows[i]["text"])
        i += 1
    return {
        "filename": rows[0]["filename"],
        "text": "\n\n".join(parts),
        "offset": offset,
        "next_offset": i if i < total else None,
        "total_chunks": total,
    }


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
    qvec = _embedder().embed_query(query)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, text, embedding FROM memories"
            " WHERE kind = 'fact' AND embedding IS NOT NULL"
        ).fetchall()
    hits = _cosine_top_k(qvec, [(r["embedding"], r) for r in rows], k)
    return [{"id": r["id"], "text": r["text"], "score": round(score, 4)} for score, r in hits]


# ── GraphQL transport (write paths) ───────────────────────────────────────────

def _global_id(type_name: str, raw_id: str) -> str:
    """Relay GlobalID — base64("TypeName:rawId"), what the mutations expect."""
    import base64

    return base64.b64encode(f"{type_name}:{raw_id}".encode()).decode()


def api(query: str, variables: dict | None = None) -> dict:
    """POST a GraphQL query/mutation to the local server and return `data`.

    The endpoint comes from $JARVIS_API_URL (default http://127.0.0.1:8000/graphql).
    Raises RuntimeError carrying the server's message on a GraphQL error.
    """
    import httpx

    url = os.environ.get("JARVIS_API_URL") or DEFAULT_API_URL
    try:
        resp = httpx.post(
            url, json={"query": query, "variables": variables or {}}, timeout=30.0
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

def project_memory(action: str = "read", content: str | None = None) -> str:
    """Read or update the shared memory of this conversation's project.

    A free-text notepad shared by every conversation in the project, injected
    into your context each turn. Store ONLY facts tied to THIS project — its
    stack, architecture decisions, conventions, key paths, goals. Global facts
    (user info, general prefs) belong in the `remember` tool instead.

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
    new = f"{current.rstrip()}\n\n{content}".strip() if action == "append" else content
    api(
        "mutation($id: ID!, $input: ProjectUpdateInput!) {"
        " updateProject(id: $id, input: $input) { id } }",
        {"id": _global_id("Project", _project_id), "input": {"memory": new}},
    )
    return f"Project memory updated ({len(new)} chars)."


def text_to_speech(text: str) -> str:
    """Synthesize speech from text using the local Piper TTS voice.

    Writes a scratch .wav file and returns its path — pass that path to the
    write_artifact_file tool (kind="audio") to save it as a shareable artifact.
    Raises if piper-tts isn't installed in this build, or the voice model file
    is missing (set PIPER_VOICE env var).
    """
    from core.config import get_config
    from core.tts import synthesize_wav_bytes

    data = synthesize_wav_bytes(text)
    cfg = get_config()
    out_dir = cfg.work_dir
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"tts-{uuid4().hex[:12]}.wav")
    with open(path, "wb") as f:
        f.write(data)
    return path


# ── Discovery ─────────────────────────────────────────────────────────────────

_CATEGORIES: dict[str, tuple[str, list]] = {
    "artifacts": (
        "read/list saved deliverables (write_artifact/write_artifact_file stay tools)",
        [list_artifacts, read_artifact, list_artifact_versions],
    ),
    "documents": (
        "search/read large attached documents that were indexed",
        [search_documents, read_document],
    ),
    "automations": (
        "scheduled or on-demand tasks (cron, code, webhook, monitor)",
        [list_automations, create_automation, update_automation, delete_automation],
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
    "media": (
        "local audio synthesis (write_artifact_file stays a tool to save the result)",
        [text_to_speech],
    ),
}


def help(category: str | None = None) -> str:  # noqa: A001 — deliberate `jarvis.help`
    """List what the jarvis SDK can do. Call with a category name for signatures."""
    import inspect

    if category is None:
        lines = ["jarvis SDK — call jarvis.help('<category>') for full signatures.", ""]
        lines += [f"  {name:<12} {blurb}" for name, (blurb, _) in _CATEGORIES.items()]
        return "\n".join(lines)

    key = category.strip().lower()
    if key not in _CATEGORIES:
        return f"Unknown category {category!r}. Available: {', '.join(_CATEGORIES)}"
    blurb, funcs = _CATEGORIES[key]
    out = [f"jarvis.{key} — {blurb}", ""]
    for fn in funcs:
        out.append(f"jarvis.{fn.__name__}{inspect.signature(fn)}")
        doc = inspect.getdoc(fn) or ""
        out += [f"    {line}" for line in doc.splitlines()]
        out.append("")
    return "\n".join(out).rstrip()
