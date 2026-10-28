"""Chat attachments (`edge/src/uploads.rs`): `POST /uploads` stages a file,
`startTask` sends it, the turn saves it where the agent can open it, shows
an image to the model and has audio transcribed (`edge/src/transcribe.rs`).

A real edge over a fresh database, its model a fake Ollama that records what
it was sent. Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import base64
import email.parser
import io
import json
import math
import sqlite3
import struct
import threading
import wave
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


def _wav(rate: int, channels: int, secs: float) -> bytes:
    """A 440 Hz tone, 16-bit PCM."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = (int(12000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(rate * secs)))
        w.writeframes(b"".join(struct.pack("<h", s) * channels for s in frames))
    return out.getvalue()


class FakeOllama:
    """Answers every chat with "seen", and keeps each request's body. Also
    Meta's `/asr/transcribe` and Gemini's `streamGenerateContent`, as speech
    to text, each answering `heard`."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.meta: list[dict] = []
        self.gemini: list[dict] = []
        self.heard = "hello there"
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — the stdlib's name
                raw = self.rfile.read(int(self.headers["content-length"]))
                if self.path == "/v1/asr/transcribe":
                    form = email.parser.BytesParser().parsebytes(
                        f"content-type: {self.headers['content-type']}\r\n\r\n".encode() + raw)
                    parts = {p.get_param("name", header="content-disposition"): p for p in form.get_payload()}
                    fake.meta.append({
                        "auth": self.headers["authorization"],
                        "request": json.loads(parts["request"].get_payload(decode=True)),
                        "request_type": parts["request"].get_content_type(),
                        "audio": parts["audio"].get_payload(decode=True),
                    })
                    self._json({"sessionId": "s", "transcript": fake.heard, "audioDurationMs": 2000, "turns": []})
                    return
                if ":streamGenerateContent" in self.path:
                    fake.gemini.append({"path": self.path, "body": json.loads(raw)})
                    reply = {"candidates": [{"content": {"role": "model", "parts": [{"text": fake.heard}]},
                                             "finishReason": "STOP"}],
                             "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2}}
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(f"data: {json.dumps(reply)}\r\n\r\n".encode())
                    return
                body = json.loads(raw or b"{}")
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


SET = "mutation($k: String!, $v: String!) { setSetting(key: $k, value: $v) { setting { key value } } }"


async def _transcribed(edge_binary, tmp_path: Path, fake: FakeOllama, model: str, env: dict[str, str],
                       audio: bytes) -> tuple[Path, str]:
    """Send `audio` as clip.wav with `transcription.model` set to `model`;
    the conversation's uploads directory and what the chat model was sent."""
    work = tmp_path / "work"
    work.mkdir()
    db = work / "database.db"
    async with _run_edge(edge_binary, work, db, {"JARVIS_RUN_JOBS": "1", "OLLAMA_HOST": fake.url, **env}) as client:
        await client.post("/graphql", json={"query": 'mutation { addModel(id: "ollama:fake", label: "Fake", provider: "ollama") { default } }'})
        setting = await client.post("/graphql", json={"query": SET, "variables": {"k": "transcription.model", "v": model}})
        assert "errors" not in setting.json(), setting.text
        clip = await _upload(client, "clip.wav", audio, "audio/wav")
        started = await _start(client, query="What did they say?", attachmentUploads=[{"uploadId": clip["uploadId"]}])
        conv = started["data"]["startTask"]["conversationId"]

        async def replied() -> bool:
            with sqlite3.connect(db) as c:
                row = c.execute("SELECT status FROM messages WHERE conversation_id = ? AND role = 'assistant'",
                                (conv,)).fetchone()
            return row is not None and row[0] == "done"

        await until(replied, timeout=30)
    [user] = [m for m in fake.requests[0]["messages"] if m["role"] == "user"]
    return work / "uploads" / conv, user["content"]


def _sent_wav(data: bytes) -> wave.Wave_read:
    w = wave.open(io.BytesIO(data))
    assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
    return w


async def test_audio_is_transcribed_by_meta(edge_binary, tmp_path: Path, fake: FakeOllama):
    # 44.1 kHz stereo: mixed down and resampled to what Meta takes.
    uploads, content = await _transcribed(
        edge_binary, tmp_path, fake, "meta:muse-voice-transcribe-1.0",
        {"MODEL_API_BASE": f"{fake.url}/v1", "META_API_KEY": "test-key"}, _wav(44100, 2, 2.0))
    [call] = fake.meta
    assert call["auth"] == "Bearer test-key"
    assert call["request"] == {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV"}
    assert call["request_type"] == "application/json"
    assert abs(_sent_wav(call["audio"]).getnframes() - 32000) <= 1
    kept = uploads / "clip.wav.transcript.txt"
    assert kept.read_text() == "hello there"
    assert f"{uploads / 'clip.wav'} (audio/wav, 345 KB, transcript below and in {kept})]" in content
    assert content.endswith("\n\n[Transcript of clip.wav]\nhello there")


async def test_audio_is_transcribed_by_gemini(edge_binary, tmp_path: Path, fake: FakeOllama):
    fake.heard = "hola"
    uploads, content = await _transcribed(
        edge_binary, tmp_path, fake, "google_genai:gemini-3.1-flash-lite",
        {"JARVIS_GOOGLE_BASE_URL": fake.url, "GOOGLE_API_KEY": "test-key"}, _wav(16000, 1, 1.0))
    [call] = fake.gemini
    assert call["path"].startswith("/v1beta/models/gemini-3.1-flash-lite:streamGenerateContent")
    [turn] = call["body"]["contents"]
    [ask, audio] = turn["parts"]
    assert ask == {"text": "Transcribe this recording."}
    assert audio["inlineData"]["mimeType"] == "audio/wav"
    assert _sent_wav(base64.b64decode(audio["inlineData"]["data"])).getnframes() == 16000
    assert content.endswith("\n\n[Transcript of clip.wav]\nhola")
    assert (uploads / "clip.wav.transcript.txt").read_text() == "hola"


async def test_audio_that_cant_be_read_says_why(edge_binary, tmp_path: Path, fake: FakeOllama):
    uploads, content = await _transcribed(
        edge_binary, tmp_path, fake, "meta:muse-voice-transcribe-1.0",
        {"MODEL_API_BASE": f"{fake.url}/v1", "META_API_KEY": "test-key"}, b"not audio at all")
    assert fake.meta == []
    assert f"{uploads / 'clip.wav'} (audio/wav, 16 B, couldn't be transcribed: not an audio format" in content
    assert "[Transcript of" not in content


async def test_silence_sends_nothing(edge_binary, tmp_path: Path, fake: FakeOllama):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    uploads, content = await _transcribed(
        edge_binary, tmp_path, fake, "meta:muse-voice-transcribe-1.0",
        {"MODEL_API_BASE": f"{fake.url}/v1", "META_API_KEY": "test-key"}, out.getvalue())
    assert fake.meta == []
    assert "(audio/wav, 31 KB, no speech in it)]" in content
