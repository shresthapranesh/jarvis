"""Chat attachments (`edge/src/uploads.rs`): `POST /uploads` stages a file,
`startTask` sends it, the turn saves it where the agent can open it and shows
an image to the model.

A real edge over a fresh database, its model a fake Ollama that records what
it was sent. Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from edge_support import _run_edge, edge_binary, until  # noqa: F401 — edge_binary is a fixture

MODEL = "ollama:fake"
START = """mutation($input: StartTaskInput!) {
  startTask(input: $input) { taskId conversationId queued } }"""
# A 1×1 PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class FakeOllama:
    """Answers every chat with "seen", and keeps each request's body."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — the stdlib's name
                body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
                if self.path == "/api/embed":
                    self._json({"embeddings": [[1.0, 0.0] for _ in body.get("input", [])]})
                    return
                if self.path != "/api/chat":
                    self.send_response(404)
                    self.end_headers()
                    return
                fake.requests.append(body)
                self.send_response(200)
                self.send_header("content-type", "application/x-ndjson")
                self.end_headers()
                base = {"model": "fake", "created_at": "2026-10-04T00:00:00Z"}
                for chunk in (
                    {**base, "message": {"role": "assistant", "content": "seen"}, "done": False},
                    {**base, "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                     "prompt_eval_count": 10, "eval_count": 1},
                ):
                    self.wfile.write(json.dumps(chunk).encode() + b"\n")
                    self.wfile.flush()

            def _json(self, value: dict) -> None:
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format, *args):  # noqa: A002 — the parent's name
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    server = FakeOllama()
    yield server
    server.server.shutdown()


async def _upload(client, name: str, data: bytes, mime: str) -> dict:
    resp = await client.post("/uploads", files={"file": (name, data, mime)})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _start(client, **input_) -> dict:
    resp = await client.post("/graphql", json={"query": START, "variables": {"input": {"model": MODEL, **input_}}})
    return resp.json()


async def test_files_reach_the_workspace_and_images_the_model(edge_binary, tmp_path: Path, fake: FakeOllama):
    work = tmp_path / "work"
    work.mkdir()
    db = work / "database.db"
    env = {"JARVIS_RUN_JOBS": "1", "OLLAMA_HOST": fake.url}
    async with _run_edge(edge_binary, work, db, env) as client:
        added = await client.post("/graphql", json={"query": 'mutation { addModel(id: "ollama:fake", label: "Fake", provider: "ollama") { default } }'})
        assert "errors" not in added.json(), added.text
        image = await _upload(client, "../dot.png", PNG, "image/png")
        assert image["filename"] == "dot.png" and image["mimeType"] == "image/png" and image["size"] == len(PNG)
        # No type from the browser: the name says.
        notes = await _upload(client, "notes.csv", b"a,b\n1,2\n", "application/octet-stream")
        assert notes["mimeType"] == "text/csv"

        started = await _start(client, query="What's in these?",
                               attachmentUploads=[{"uploadId": image["uploadId"]}, {"uploadId": notes["uploadId"]}])
        conv = started["data"]["startTask"]["conversationId"]

        async def replied() -> bool:
            with sqlite3.connect(db) as c:
                row = c.execute("SELECT status FROM messages WHERE conversation_id = ? AND role = 'assistant'",
                                (conv,)).fetchone()
            return row is not None and row[0] == "done"

        await until(replied, timeout=30)

        # Moved out of staging into the conversation's directory.
        uploads = work / "uploads" / conv
        assert (uploads / "dot.png").read_bytes() == PNG
        assert (uploads / "notes.csv").read_bytes() == b"a,b\n1,2\n"
        assert [p for p in (work / "staging").iterdir()] == []

        # The user message renders its chips.
        with sqlite3.connect(db) as c:
            (content,) = c.execute("SELECT content FROM messages WHERE conversation_id = ? AND role = 'user'",
                                   (conv,)).fetchone()
        assert json.loads(content) == [
            {"type": "text", "text": "What's in these?"},
            {"type": "image", "name": "dot.png", "size": len(PNG), "mimeType": "image/png"},
            {"type": "document", "name": "notes.csv", "size": 8, "mimeType": "text/csv"},
        ]

        # The model saw the image, and was told where both files are.
        [user] = [m for m in fake.requests[0]["messages"] if m["role"] == "user"]
        assert user["images"] == [base64.b64encode(PNG).decode()]
        assert user["content"].startswith("What's in these?\n\n[Attached")
        assert f"{uploads / 'dot.png'} (image/png, 68 B, also shown to you as an image)" in user["content"]
        assert f"{uploads / 'notes.csv'} (text/csv, 8 B)" in user["content"]

        # The thread keeps the image as a blob, as the transcript keeps media.
        with sqlite3.connect(db) as c:
            blobs = c.execute("SELECT mime_type, data FROM transcript_blobs").fetchall()
        assert blobs == [("image/png", PNG)]

        # A used upload is gone; an id that was never one is refused.
        again = await _start(client, query="again", conversationId=conv,
                             attachmentUploads=[{"uploadId": image["uploadId"]}])
        assert again["errors"][0]["message"] == f"unknown or expired upload id: {image['uploadId']}"
        bad = await _start(client, query="x", attachmentUploads=[{"uploadId": "../database.db"}])
        assert bad["errors"][0]["message"] == "unknown or expired upload id: ../database.db"

        # The files go with the conversation.
        gid = base64.b64encode(f"Conversation:{conv}".encode()).decode()
        deleted = await client.post("/graphql", json={"query": "mutation($id: ID!) { deleteConversation(id: $id) }",
                                                      "variables": {"id": gid}})
        assert deleted.json()["data"]["deleteConversation"] is True
        assert not uploads.exists()


async def test_only_files_is_a_message_and_titles_it(edge_binary, tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    async with _run_edge(edge_binary, work, work / "database.db") as client:
        report = await _upload(client, "report.pdf", b"%PDF-1.4", "application/pdf")
        started = await _start(client, query="", attachmentUploads=[{"uploadId": report["uploadId"]}])
        conv = started["data"]["startTask"]["conversationId"]
        with sqlite3.connect(work / "database.db") as c:
            (title,) = c.execute("SELECT title FROM conversations WHERE id = ?", (conv,)).fetchone()
        assert title == "report.pdf"
        # Nothing at all is not a message.
        empty = await _start(client, query="  ")
        assert empty["errors"][0]["message"] == "empty message"


async def test_upload_needs_a_file(edge_binary, tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    async with _run_edge(edge_binary, work, work / "database.db") as client:
        resp = await client.post("/uploads", data={"other": "x"}, files={"note": ("a.txt", b"x", "text/plain")})
        assert resp.status_code == 400 and resp.json() == {"error": "no `file` field in the form"}
        resp = await client.post("/uploads", content=b"not a form")
        assert resp.status_code == 400
