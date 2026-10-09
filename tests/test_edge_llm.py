"""The edge's LLM layer (`edge/src/llm/`) against what Python's path did:
`core/messages.py` shaping, then the LangChain integrations, recorded
(`tests/python_golden.py`).

One fake provider server records what the edge sends and replays a stream.
Compared with Python's recorded answers:

- the shaped prompt (`--llm-shape`) against `build_llm_messages` over
  `strip_historical_thinking` + `repair_orphan_tool_calls`;
- the request body (`--llm-call`) against what the LangChain integrations
  sent for the same history — equal except for the differences the edge makes
  on purpose, each undone by name in `_INTENDED_*` below;
- the record each made of the same reply;
- and the next request after a reply either side recorded.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import struct
import subprocess
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from edge_support import edge_binary  # noqa: F401 — a fixture
from python_golden import GOLDEN_DIR, recorded_sync

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake image bytes").decode()

TOOLS = [
    {
        "name": "run_cell",
        "description": "Run Python in the conversation's kernel.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "the cell"},
                "timeout": {"type": "integer", "description": "seconds"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "write_todos",
        "description": "Replace the task list.",
        "parameters": {
            "type": "object",
            "properties": {"todos": {"type": "array", "items": {"type": "string"}}},
            "required": ["todos"],
        },
    },
]

SEGMENTS = [
    {"name": "skills", "content": "## Skills\n\n- research", "cacheable": True},
    {"name": "project", "content": "## Project\n\nJarvis", "cacheable": True},
    {"name": "memories", "content": "## Memories\n\nlikes tea", "cacheable": False},
]


# ── histories ────────────────────────────────────────────────────────────────

# Threads as Python's transcript encoder recorded them (records and their
# blobs): a chat, a tool loop with an image and a cancelled call, calls from
# another model, a Responses API thread, one long enough to compact — and two
# edge cases, results that are only images and blank text.
HISTORIES = ["chat", "tool_loop", "foreign_calls", "responses_thread", "long_loop"]
HISTORY: dict[str, tuple[list[dict], dict[str, str]]] = {
    name: (records, blobs)
    for name, (records, blobs) in json.loads((GOLDEN_DIR / "test_edge_llm_histories.json").read_text()).items()
}


# ── the fake provider ────────────────────────────────────────────────────────

GEMINI_REPLY = [
    {"candidates": [{"content": {"parts": [{"text": "Weighing ", "thought": True}], "role": "model"}, "index": 0}],
     "modelVersion": "gemma-4-31b-it"},
    {"candidates": [{"content": {"parts": [{"text": "it.", "thought": True}, {"text": "Let me"}], "role": "model"},
                     "index": 0}], "modelVersion": "gemma-4-31b-it"},
    {"candidates": [{"content": {"parts": [
        {"text": " check.", "thoughtSignature": "dGV4dC1zaWc="},
        {"functionCall": {"name": "run_cell", "args": {"code": "check()"}}, "thoughtSignature": "Y2FsbC1zaWc="},
        {"functionCall": {"name": "write_todos", "args": {"todos": ["x"]}}}], "role": "model"},
        "finishReason": "STOP", "index": 0}],
     "usageMetadata": {"promptTokenCount": 812, "candidatesTokenCount": 40, "totalTokenCount": 864,
                       "thoughtsTokenCount": 12, "cachedContentTokenCount": 700},
     "modelVersion": "gemma-4-31b-it", "responseId": "r1"},
]

OLLAMA_REPLY = [
    {"model": "gemma4:26b", "created_at": "2026-10-03T00:00:00Z",
     "message": {"role": "assistant", "content": "", "thinking": "Hmm."}, "done": False},
    {"model": "gemma4:26b", "created_at": "2026-10-03T00:00:00Z",
     "message": {"role": "assistant", "content": "Let me"}, "done": False},
    {"model": "gemma4:26b", "created_at": "2026-10-03T00:00:00Z",
     "message": {"role": "assistant", "content": " check.",
                 "tool_calls": [{"function": {"name": "run_cell", "arguments": {"code": "check()"}}}]}, "done": False},
    {"model": "gemma4:26b", "created_at": "2026-10-03T00:00:01Z", "message": {"role": "assistant", "content": ""},
     "done": True, "done_reason": "stop", "total_duration": 5, "load_duration": 1, "prompt_eval_count": 50,
     "prompt_eval_duration": 500_000_000, "eval_count": 7, "eval_duration": 2_000_000_000},
]


CHAT_REPLY = [
    {"id": "gen-1", "object": "chat.completion.chunk", "model": "anthropic/claude-sonnet-4.5",
     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "", "reasoning": "Weighing it."},
                  "finish_reason": None}]},
    {"id": "gen-1", "object": "chat.completion.chunk", "model": "anthropic/claude-sonnet-4.5",
     "choices": [{"index": 0, "delta": {"content": "Let me"}, "finish_reason": None}]},
    {"id": "gen-1", "object": "chat.completion.chunk", "model": "anthropic/claude-sonnet-4.5",
     "choices": [{"index": 0, "delta": {"content": " check.", "tool_calls": [
         {"index": 0, "id": "call_1", "type": "function", "function": {"name": "run_cell", "arguments": ""}}]},
                  "finish_reason": None}]},
    {"id": "gen-1", "object": "chat.completion.chunk", "model": "anthropic/claude-sonnet-4.5",
     "choices": [{"index": 0, "delta": {"tool_calls": [
         {"index": 0, "function": {"arguments": "{\"code\": \"che"}}]}, "finish_reason": None}]},
    {"id": "gen-1", "object": "chat.completion.chunk", "model": "anthropic/claude-sonnet-4.5",
     "choices": [{"index": 0, "delta": {"tool_calls": [
         {"index": 0, "function": {"arguments": "ck()\"}"}},
         {"index": 1, "id": "call_2", "type": "function",
          "function": {"name": "write_todos", "arguments": "{\"todos\": [\"x\"]}"}}]}, "finish_reason": "tool_calls"}]},
    {"id": "gen-1", "object": "chat.completion.chunk", "model": "anthropic/claude-sonnet-4.5", "choices": [],
     "usage": {"prompt_tokens": 812, "completion_tokens": 40, "total_tokens": 852,
               "prompt_tokens_details": {"cached_tokens": 700}, "completion_tokens_details": {"reasoning_tokens": 12}}},
]

RESPONSES_REPLY = [
    ("response.created", {"response": {"id": "resp_1", "object": "response", "model": "muse-spark-1.1",
                                       "status": "in_progress", "output": []}}),
    ("response.output_item.added", {"output_index": 0, "item": {"type": "reasoning", "id": "rs_1", "summary": []}}),
    ("response.reasoning_summary_part.added", {"item_id": "rs_1", "output_index": 0, "summary_index": 0,
                                                "part": {"type": "summary_text", "text": ""}}),
    ("response.reasoning_summary_text.delta", {"item_id": "rs_1", "output_index": 0, "summary_index": 0,
                                                "delta": "Weighing it."}),
    ("response.output_item.done", {"output_index": 0, "item": {
        "type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "Weighing it."}]}}),
    ("response.output_item.added", {"output_index": 1, "item": {
        "type": "message", "id": "msg_1", "role": "assistant", "status": "in_progress", "content": [],
        "phase": "commentary"}}),
    ("response.content_part.added", {"item_id": "msg_1", "output_index": 1, "content_index": 0,
                                     "part": {"type": "output_text", "text": "", "annotations": []}}),
    ("response.output_text.delta", {"item_id": "msg_1", "output_index": 1, "content_index": 0, "delta": "Let me"}),
    ("response.output_text.delta", {"item_id": "msg_1", "output_index": 1, "content_index": 0, "delta": " check."}),
    ("response.output_text.done", {"item_id": "msg_1", "output_index": 1, "content_index": 0,
                                   "text": "Let me check."}),
    ("response.output_item.done", {"output_index": 1, "item": {
        "type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "phase": "commentary",
        "content": [{"type": "output_text", "text": "Let me check.", "annotations": []}]}}),
    ("response.output_item.added", {"output_index": 2, "item": {
        "type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "run_cell", "arguments": "",
        "status": "in_progress"}}),
    ("response.function_call_arguments.delta", {"item_id": "fc_1", "output_index": 2, "delta": "{\"code\": "}),
    ("response.function_call_arguments.delta", {"item_id": "fc_1", "output_index": 2, "delta": "\"check()\"}"}),
    ("response.output_item.done", {"output_index": 2, "item": {
        "type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "run_cell",
        "arguments": "{\"code\": \"check()\"}", "status": "completed"}}),
    ("response.completed", {"response": {
        "id": "resp_1", "object": "response", "model": "muse-spark-1.1", "status": "completed", "output": [],
        "usage": {"input_tokens": 812, "input_tokens_details": {"cached_tokens": 700}, "output_tokens": 40,
                  "output_tokens_details": {"reasoning_tokens": 12}, "total_tokens": 852}}}),
]


ANTHROPIC_REPLY = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6", "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 112, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 700,
                  "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "thinking_delta", "thinking": "Weighing "}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "thinking_delta", "thinking": "it."}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "signature_delta", "signature": "c2lnLXRoaW5r"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("content_block_start", {"type": "content_block_start", "index": 1,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "text_delta", "text": "Let me"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "text_delta", "text": " check."}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    ("content_block_start", {"type": "content_block_start", "index": 2, "content_block": {
        "type": "tool_use", "id": "toolu_1", "name": "run_cell", "input": {}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 2,
                             "delta": {"type": "input_json_delta", "partial_json": "{\"code\": \"che"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 2,
                             "delta": {"type": "input_json_delta", "partial_json": "ck()\"}"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 2}),
    ("content_block_start", {"type": "content_block_start", "index": 3, "content_block": {
        "type": "tool_use", "id": "toolu_2", "name": "write_todos", "input": {}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 3,
                             "delta": {"type": "input_json_delta", "partial_json": "{\"todos\": [\"x\"]}"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 3}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                       "usage": {"input_tokens": 112, "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 700, "output_tokens": 40}}),
    ("message_stop", {"type": "message_stop"}),
]


def _header(name: str, value: str) -> bytes:
    n, v = name.encode(), value.encode()
    return bytes([len(n)]) + n + bytes([7]) + struct.pack(">H", len(v)) + v


def _frame(event_type: str, payload: dict) -> bytes:
    headers = _header(":event-type", event_type) + _header(":content-type", "application/json") \
        + _header(":message-type", "event")
    body = json.dumps(payload).encode()
    total = 12 + len(headers) + len(body) + 4
    prelude = struct.pack(">II", total, len(headers))
    msg = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + body
    return msg + struct.pack(">I", zlib.crc32(msg))


BEDROCK_REPLY = [
    ("messageStart", {"role": "assistant"}),
    ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"reasoningContent": {"text": "Weighing "}}}),
    ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"reasoningContent": {"text": "it."}}}),
    ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"reasoningContent": {"signature": "c2lnLXRoaW5r"}}}),
    ("contentBlockStop", {"contentBlockIndex": 0}),
    ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"text": "Let me"}}),
    ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"text": " check."}}),
    ("contentBlockStop", {"contentBlockIndex": 1}),
    ("contentBlockStart", {"contentBlockIndex": 2, "start": {"toolUse": {"toolUseId": "tooluse_1", "name": "run_cell"}}}),
    ("contentBlockDelta", {"contentBlockIndex": 2, "delta": {"toolUse": {"input": "{\"code\": \"che"}}}),
    ("contentBlockDelta", {"contentBlockIndex": 2, "delta": {"toolUse": {"input": "ck()\"}"}}}),
    ("contentBlockStop", {"contentBlockIndex": 2}),
    ("contentBlockStart", {"contentBlockIndex": 3, "start": {"toolUse": {"toolUseId": "tooluse_2", "name": "write_todos"}}}),
    ("contentBlockDelta", {"contentBlockIndex": 3, "delta": {"toolUse": {"input": "{\"todos\": [\"x\"]}"}}}),
    ("contentBlockStop", {"contentBlockIndex": 3}),
    ("messageStop", {"stopReason": "tool_use"}),
    ("metadata", {"usage": {"inputTokens": 112, "outputTokens": 40, "totalTokens": 852,
                            "cacheReadInputTokens": 700, "cacheWriteInputTokens": 0},
                  "metrics": {"latencyMs": 1234}}),
]


class _Provider:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — http.server's spelling
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                provider.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
                self.send_response(200)
                if self.path.endswith("/converse-stream"):
                    self.send_header("content-type", "application/vnd.amazon.eventstream")
                    self.end_headers()
                    for name, data in BEDROCK_REPLY:
                        self.wfile.write(_frame(name, data))
                elif self.path.endswith("/v1/messages"):
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    for name, data in ANTHROPIC_REPLY:
                        self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
                elif "generatecontent" in self.path.lower():
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    for c in GEMINI_REPLY:
                        self.wfile.write(b"data: " + json.dumps(c).encode() + b"\r\n\r\n")
                elif self.path.endswith("/chat/completions"):
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    for c in CHAT_REPLY:
                        self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                elif self.path.endswith("/responses"):
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    for i, (name, data) in enumerate(RESPONSES_REPLY):
                        event = json.dumps({"type": name, "sequence_number": i, **data})
                        self.wfile.write(f"event: {name}\ndata: {event}\n\n".encode())
                    # OpenAI's stream just ends; OpenRouter's Responses endpoint says so.
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.send_header("content-type", "application/x-ndjson")
                    self.end_headers()
                    for c in OLLAMA_REPLY:
                        self.wfile.write(json.dumps(c).encode() + b"\n")

            def log_message(self, format, *args):  # noqa: A002 — the parent's name
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def take(self) -> dict[str, Any]:
        assert len(self.requests) == 1, self.requests
        return self.requests.pop()


@pytest.fixture
def provider():
    p = _Provider()
    yield p
    p.server.shutdown()


# ── the two sides ────────────────────────────────────────────────────────────

SYSTEM = "You are jarvis."
VOLATILE = "## Current Tasks\n\n[ ] pack"


_PROVIDER_ENV = (
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "OLLAMA_HOST", "OPENROUTER_API_KEY", "META_API_KEY", "MODEL_API_BASE",
    "ANTHROPIC_API_KEY", "ANTHROPIC_API_URL", "ANTHROPIC_BASE_URL", "JARVIS_CACHE_TTL",
    "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
)
# What both sides sign Bedrock calls with: keys in the environment, no files.
_AWS = {"AWS_ACCESS_KEY_ID": "AKIDEXAMPLE", "AWS_SECRET_ACCESS_KEY": "test-secret", "AWS_REGION": "us-east-1",
        "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null"}


def _endpoint_rows(url: str) -> list[dict]:
    """`models.endpoints` as stored: two OpenAI-compatible endpoints at the fake."""
    return [{"name": "local", "base_url": url, "api_key": "test-key"}, {"name": "keyless", "base_url": f"{url}/"}]


def _edge(edge_binary: Path, flag: str, tmp_path: Path, payload: dict, provider: _Provider | None = None) -> list:
    env = {k: v for k, v in os.environ.items() if k not in _PROVIDER_ENV}
    if provider:
        payload = {**payload, "endpoints": _endpoint_rows(provider.url)}
        env.update(
            JARVIS_GOOGLE_BASE_URL=provider.url, GOOGLE_API_KEY="test-key", OLLAMA_HOST=provider.url,
            JARVIS_OPENROUTER_BASE_URL=provider.url, OPENROUTER_API_KEY="test-key",
            MODEL_API_BASE=provider.url, META_API_KEY="test-key",
            ANTHROPIC_API_URL=provider.url, ANTHROPIC_API_KEY="test-key",
            AWS_ENDPOINT_URL_BEDROCK_RUNTIME=provider.url, **_AWS,
        )
    # cwd = tmp_path so the repo's .env can't supply a real key.
    out = subprocess.run(
        [str(edge_binary), flag], input=json.dumps(payload), capture_output=True, text=True,
        env=env, cwd=tmp_path, timeout=30, check=True,
    )
    return [json.loads(line) for line in out.stdout.splitlines()]


def _edge_input(model: str, records, blobs, *, cache: bool, segments=SEGMENTS) -> dict:
    return {
        "model": model,
        "system": SYSTEM,
        # The agent step folds the non-cacheable segments into the volatile
        # text in Python; the edge takes them as segments and does it itself.
        "segments": [s for s in segments if s["cacheable"]],
        "volatile": "\n\n".join([s["content"] for s in segments if not s["cacheable"]] + [VOLATILE]),
        "cache": cache,
        "history": records,
        "tools": TOOLS,
        "blobs": blobs,
    }


def _cached(model: str) -> bool:
    """Whether Python laid this model's prompt out for a cache, as recorded."""
    return recorded_sync()


# ── shaping ──────────────────────────────────────────────────────────────────


def _normalized(prompt: dict) -> dict:
    """Content as `normalize_history_content` sends it: a user or tool
    message's non-blank string is one text block."""
    prompt = copy.deepcopy(prompt)
    for m in prompt["messages"]:
        if m["role"] in ("user", "tool") and isinstance(m["content"], str) and m["content"].strip():
            m["content"] = [{"type": "text", "text": m["content"]}]
    return prompt


@pytest.mark.parametrize("name", HISTORIES)
@pytest.mark.parametrize("provider_id", ["anthropic", "bedrock", "openrouter", "google_genai"])
@pytest.mark.parametrize("cache", [True, False])
def test_shaping_matches_python(edge_binary, tmp_path, name, provider_id, cache):
    records, blobs = HISTORY[name]
    expected = recorded_sync()
    [got] = _edge(edge_binary, "--llm-shape", tmp_path, _edge_input(f"{provider_id}:m", records, blobs, cache=cache))
    if cache:
        got = _normalized(got)
    assert got == expected


def test_shaping_without_segments(edge_binary, tmp_path):
    records, blobs = HISTORY["chat"]
    expected = recorded_sync()
    [got] = _edge(edge_binary, "--llm-shape", tmp_path,
                  _edge_input("ollama:m", records, blobs, cache=False, segments=[]))
    assert got == expected


def test_long_loop_is_compacted(edge_binary, tmp_path):
    """The parity above is only worth something if compaction fired."""
    records, blobs = HISTORY["long_loop"]
    [got] = _edge(edge_binary, "--llm-shape", tmp_path, _edge_input("ollama:m", records, blobs, cache=False))
    texts = [m["content"] for m in got["messages"] if isinstance(m["content"], str)]
    assert sum(t.startswith("[Previous tool activity: run_cell => run_cell: 0: é…x") for t in texts[:2]) == 1
    assert sum("chars of stale tool output elided" in t for t in texts) == 3


def test_stub_leaves_a_result_without_text_unquoted(edge_binary, tmp_path):
    """Intended: a collapsed result with no text isn't quoted. Python quotes
    the repr of its content — a truncated data URL the model pays for."""
    records, blobs = HISTORY["image_results"]
    expected = recorded_sync()
    [got] = _edge(edge_binary, "--llm-shape", tmp_path, _edge_input("ollama:m", records, blobs, cache=False))
    for m in expected["messages"][1:5]:
        assert m["content"].startswith("[Previous tool activity: run_cell => run_cell: [{'type': 'image_url'")
        m["content"] = "[Previous tool activity: run_cell => run_cell]"
    assert got == expected


# ── requests ─────────────────────────────────────────────────────────────────

# What `parametersJsonSchema` carries: the tool's schema as given.
_GEMINI_TOOLS = [{"functionDeclarations": [
    {"name": t["name"], "parametersJsonSchema": t["parameters"], "description": t["description"]} for t in TOOLS
]}]


def _intended_gemini(python: dict, edge: dict) -> None:
    """Undo, by name, each difference the edge makes on purpose."""
    # Tool schemas go as the JSON Schema they are, not LangChain's lossy
    # conversion to Gemini's OpenAPI subset.
    assert edge.pop("tools") == _GEMINI_TOOLS
    python.pop("tools")
    # Defaults LangChain spells out.
    assert python.pop("safetySettings") == []
    config = python.get("generationConfig", {})
    assert config.pop("candidateCount", 1) == 1
    if not config:
        python.pop("generationConfig", None)
    for content in edge["contents"]:
        if content["role"] == "model" and any("functionCall" in p for p in content["parts"]):
            # LangChain dropped the text beside a call, signature and all.
            content["parts"] = [p for p in content["parts"] if "text" not in p]
    for content in python["contents"]:
        for p in content["parts"]:
            # LangChain's stand-in signature is a real one from another
            # conversation; the edge sends Google's documented one.
            if "functionCall" in p and p.get("thoughtSignature", "").startswith("ErQCCrECAdHtim8M"):
                p["thoughtSignature"] = "skip_thought_signature_validator"


def _intended_ollama(python: dict, edge: dict) -> None:
    assert python.pop("options") == {}
    for m in edge["messages"]:
        if m["role"] == "tool":
            # The edge says which tool a result answers.
            assert m.pop("tool_name")
    for m in python["messages"]:
        # LangChain leaves an empty assistant turn's content out; the edge
        # always sends one.
        m.setdefault("content", "")
        # LangChain joins a list content's text with a leading newline.
        if m["content"].startswith("\n"):
            m["content"] = m["content"][1:]


def _intended_openai_chat(python: dict, edge: dict) -> None:
    for m in python["messages"]:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            # LangChain sent a recorded bare string as it was, which isn't a
            # content part; the edge sends it as a text block.
            m["content"] = [{"type": "text", "text": b} if isinstance(b, str) else b for b in m["content"]]


def _intended_openai_responses(python: dict, edge: dict) -> None:
    # LangChain skipped a recorded bare-string text part, so the assistant's
    # words vanished from the model's context; the edge sends them as an
    # output message (no server id, since it had none).
    said = {json.dumps(i["content"], sort_keys=True) for i in python["input"] if i.get("role") == "assistant"}
    edge["input"] = [
        i for i in edge["input"]
        if not (i.get("role") == "assistant" and "id" not in i and json.dumps(i["content"], sort_keys=True) not in said)
    ]


MODELS = [
    "anthropic:claude-sonnet-4-6",
    # Not in langchain-anthropic's profiles: its fallback max_tokens.
    "anthropic:claude-opus-5-5",
    "google_genai:gemma-4-31b-it",
    "google_genai:gemini-3.1-flash-lite",
    "ollama:gemma4:26b",
    # cached: an Anthropic upstream honors cache_control
    "openrouter:anthropic/claude-sonnet-4.5",
    "openrouter:deepseek/deepseek-r1:free",
    "meta:muse-spark-1.1",
    # OpenAI-compatible endpoints the operator named (`models.endpoints`)
    "local:qwen3-32b",
    "keyless:llama-3.3-70b",
    # cached: a Claude model on Bedrock takes cachePoint blocks
    "bedrock:us.anthropic.claude-sonnet-4-6",
    # an id with a colon, which the path escapes
    "bedrock:amazon.nova-pro-v1:0",
]
INTENDED = {
    "anthropic": lambda python, edge: None,
    "bedrock": lambda python, edge: None,
    "google_genai": _intended_gemini,
    "ollama": _intended_ollama,
    "openrouter": _intended_openai_chat,
    "meta": _intended_openai_responses,
    "local": _intended_openai_chat,
    "keyless": _intended_openai_chat,
}


def _both(edge_binary, tmp_path, provider, model, records, blobs) -> tuple[dict, dict, list]:
    """What Python and the edge each send for `records`, and the edge's output."""
    cache = _cached(model)
    python = recorded_sync()
    out = _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=cache), provider)
    assert "message" in out[-1], out[-1]
    return python, provider.take(), out


@pytest.mark.parametrize("name", HISTORIES)
@pytest.mark.parametrize("model", MODELS)
def test_request_matches_python(edge_binary, tmp_path, provider, name, model):
    records, blobs = HISTORY[name]
    if model.startswith("bedrock") and name == "responses_thread":
        # Neither can send Bedrock another provider's call items: LangChain
        # raises, and the edge fails the call with the same words.
        refused = recorded_sync()
        assert refused.startswith("ValueError: Unsupported content block type")
        out = _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=_cached(model)), provider)
        assert out[-1]["error"]["message"] == "Unsupported content block type: function_call"
        assert provider.requests == []
        return
    if model.startswith("ollama") and name == "responses_thread":
        # LangChain can't send Ollama a thread that came from the Responses
        # API: the function_call item left in the content is a ValueError.
        refused = recorded_sync()
        assert refused.startswith("ValueError: Unsupported message content type")
        out = _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=False), provider)
        assert "message" in out[-1], out[-1]
        sent = provider.take()["body"]["messages"]
        assert [m["tool_calls"] for m in sent if m.get("tool_calls")] == [
            [{"function": {"name": "run_cell", "arguments": {"code": "search_memory('user')"}}}]
        ]
        assert [m["content"] for m in sent if m["role"] == "assistant"] == [
            "I'll check what I have stored.", "Nothing yet — café orders aside."
        ]
        return
    python, edge, _ = _both(edge_binary, tmp_path, provider, model, records, blobs)
    assert edge["path"] == python["path"]
    python, edge = python["body"], edge["body"]
    INTENDED[model.split(":")[0]](python, edge)
    assert edge == python


@pytest.mark.parametrize("name", HISTORIES)
def test_anthropic_request_without_a_cache_matches_python(edge_binary, tmp_path, provider, name):
    """Anthropic always caches here; its plain layout (string content, one
    system string) is what a run with caching turned off would send."""
    model = MODELS[0]
    records, blobs = HISTORY[name]
    python = recorded_sync()["body"]
    _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=False), provider)
    assert provider.take()["body"] == python


def test_anthropic_max_tokens_follow_langchains_profiles(edge_binary, tmp_path, provider):
    records, blobs = HISTORY["chat"]
    for name, expected in recorded_sync().items():
        _edge(edge_binary, "--llm-call", tmp_path, _edge_input(f"anthropic:{name}", records, blobs, cache=False),
              provider)
        assert provider.take()["body"]["max_tokens"] == expected, name


@pytest.mark.parametrize("model,header,value", [
    (MODELS[0], "x-api-key", "test-key"),
    (MODELS[0], "anthropic-version", "2023-06-01"),
    (MODELS[2], "x-goog-api-key", "test-key"),
    (MODELS[5], "authorization", "Bearer test-key"),
    (MODELS[7], "authorization", "Bearer test-key"),
    (MODELS[8], "authorization", "Bearer test-key"),
    # No key, no header — where LangChain's client sends a placeholder.
    (MODELS[9], "authorization", None),
])
def test_key_header(edge_binary, tmp_path, provider, model, header, value):
    records, blobs = HISTORY["chat"]
    _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=False), provider)
    headers = {k.lower(): v for k, v in provider.take()["headers"].items()}
    assert headers.get(header) == value


# ── replies ──────────────────────────────────────────────────────────────────


def _semantics(rec: dict) -> dict:
    """What a reply means, apart from how its parts are cut up."""
    parts = rec["content"] if isinstance(rec["content"], list) else [rec["content"]]
    text = "".join(p if isinstance(p, str) else p.get("text", "") for p in parts)
    thinking = "".join(p.get("thinking", "") for p in parts if isinstance(p, dict))
    signatures = [p["signature"] for p in parts if isinstance(p, dict) and p.get("type") == "text" and "signature" in p]
    return {
        "text": text,
        "thinking": thinking,
        "text_signatures": signatures,
        "tool_calls": [{k: c.get(k) for k in ("name", "args", "signature")} for c in rec.get("tool_calls", [])],
        "usage": rec.get("usage"),
        "model": rec.get("model"),
        "finish_reason": rec.get("finish_reason"),
    }


@pytest.mark.parametrize("model", [MODELS[0], MODELS[2], MODELS[4], MODELS[5], MODELS[7], MODELS[8], MODELS[10]])
def test_reply_matches_python(edge_binary, tmp_path, provider, model):
    records, blobs = HISTORY["chat"]
    python_rec = recorded_sync()
    python = _semantics(python_rec)
    if model.startswith(("openrouter", "meta", "local")):
        # LangChain named the provider by wire format; the edge records the
        # catalog's provider id, as the transcript format says.
        assert python["model"]["provider"] == "openai"
        python["model"]["provider"] = model.split(":")[0]
    out = _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=False), provider)
    edge_rec = out[-1]["message"]
    edge = _semantics(edge_rec)
    deltas = [line for line in out[:-1] if "event" not in line]

    if model.startswith("bedrock"):
        # LangChain kept the reasoning only in an opaque block, named the
        # provider by its class and left the stop reason in its metadata; the
        # edge records thinking, the catalog's provider id and the reason.
        assert edge.pop("thinking") == "Weighing it." and python.pop("thinking") == ""
        assert python["model"]["provider"] == "bedrock_converse"
        python["model"]["provider"] = "bedrock"
        assert edge["finish_reason"] == "tool_use" and python["finish_reason"] is None
        python["finish_reason"] = "tool_use"
        assert deltas == [{"thinking": "Weighing "}, {"thinking": "it."}, {"text": "Let me"}, {"text": " check."}]
    elif model.startswith("anthropic"):
        # LangChain left the stop reason in its response metadata; the edge
        # keeps it as the finish reason.
        assert edge["finish_reason"] == "tool_use" and python["finish_reason"] is None
        python["finish_reason"] = "tool_use"
        assert deltas == [{"thinking": "Weighing "}, {"thinking": "it."}, {"text": "Let me"}, {"text": " check."}]
    elif model.startswith("ollama"):
        # LangChain dropped Ollama's thinking and left the stop reason in
        # its response metadata; the edge keeps both.
        assert edge.pop("thinking") == "Hmm." and python.pop("thinking") == ""
        assert edge["finish_reason"] == python_rec["extras"]["response_metadata"]["done_reason"]
        python["finish_reason"] = edge["finish_reason"]
        assert deltas == [{"thinking": "Hmm."}, {"text": "Let me"}, {"text": " check."}]
    elif model.startswith(("openrouter", "meta", "local")):
        # LangChain dropped OpenRouter's reasoning, and kept the Responses
        # summary only inside an opaque item; the edge keeps it as thinking.
        assert edge.pop("thinking") == "Weighing it." and python.pop("thinking") == ""
        if model.startswith("meta"):
            # …and the response's status as its finish reason.
            assert edge["finish_reason"] == "completed" and python["finish_reason"] is None
            python["finish_reason"] = "completed"
        assert deltas == [{"thinking": "Weighing it."}, {"text": "Let me"}, {"text": " check."}]
    else:
        assert deltas == [{"thinking": "Weighing "}, {"thinking": "it."}, {"text": "Let me"}, {"text": " check."}]
    assert edge == python
    assert all(c["id"] for c in edge_rec["tool_calls"])


# Read off the wall clock, so equal only in whether they were measured.
_WALL_CLOCK = {"ttft_ms", "llm_ms", "total_ms", "elapsed_seconds", "decode_ms"}
# Wall-clock rates: whether one clears the 5 ms floor depends on the machine.
_WALL_RATES = {"prefill_tps", "eval_tps"}
# What Ollama's own durations give — the same numbers from either side.
_SERVER_TIMED = {"decode_ms", "prefill_tps", "eval_tps"}


def _clockless(value: Any, server_timed: bool) -> Any:
    if isinstance(value, list):
        return [_clockless(v, server_timed) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for k, v in value.items():
        if k == "chunks":
            # Intended: the edge counts output pieces (text, thinking, a
            # tool-call fragment); Python counts LangChain's stream chunks,
            # which an integration cuts its own way.
            continue
        if server_timed and k in _SERVER_TIMED:
            out[k] = v
        elif k in _WALL_RATES:
            continue
        elif k in _WALL_CLOCK:
            out[k] = v is not None
        else:
            out[k] = _clockless(v, server_timed)
    return out


@pytest.mark.parametrize("model", MODELS)
def test_run_events_match_python(edge_binary, tmp_path, provider, model):
    """A one-call run's `budget_update` and `perf_update`, and the perf a
    chat turn stores, as Python's callback handlers produce them."""
    records, blobs = HISTORY["chat"]

    python, python_perf = recorded_sync()
    out = _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=False), provider)
    edge = [line for line in out if "event" in line]
    assert [e["event"] for e in edge] == ["budget_update", "perf_update"]
    server_timed = model.startswith("ollama")
    python_call = python[1]["data"]["snapshot"]["calls"][0]
    if python_call["model"] != edge[1]["data"]["snapshot"]["calls"][0]["model"]:
        # Intended: LangChain's serialized ChatOllama doesn't carry the
        # model, so Python's perf names the class.
        assert model.startswith("ollama") and python_call["model"] == "ChatOllama"
        python_call["model"] = model.split(":", 1)[1]
    assert _clockless(edge, server_timed) == _clockless(python, server_timed)
    assert _clockless(out[-1]["perf"], server_timed) == _clockless(python_perf, server_timed)
    call = edge[1]["data"]["snapshot"]["calls"][0]
    if server_timed:
        assert (call["source"], call["prefill_tps"], call["eval_tps"], call["decode_ms"]) == ("provider", 100.0, 3.5, 2000.0)
    else:
        # The fake answers at once: too fast a decode to tell from a flush.
        assert call["source"] == "prefill_only" and call["ttft_ms"] is not None


def test_edge_record_goes_back_out_through_python(edge_binary, tmp_path, provider):
    """A thread the edge wrote to can continue in Python: its record decodes,
    and LangChain sends the call's thought signature back."""
    model = MODELS[2]
    records, blobs = HISTORY["chat"]
    out = _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=False), provider)
    provider.take()
    reply = out[-1]["message"]
    calls = reply["tool_calls"]
    follow = [
        *records,
        reply,
        *({"v": 1, "role": "tool", "content": "ok", "tool_call_id": c["id"], "status": "success"} for c in calls),
    ]
    sent = recorded_sync()["body"]["contents"]
    model_turn = [c for c in sent if c["role"] == "model"][-1]
    assert [p.get("thoughtSignature") for p in model_turn["parts"] if "functionCall" in p] == ["Y2FsbC1zaWc=", None]
    # …and the edge itself sends it back with the text's signature too.
    _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, follow, blobs, cache=False), provider)
    edge_turn = [c for c in provider.take()["body"]["contents"] if c["role"] == "model"][-1]
    assert edge_turn["parts"][0] == {"text": "Let me check.", "thoughtSignature": "dGV4dC1zaWc="}


@pytest.mark.parametrize("model", [MODELS[0], MODELS[5], MODELS[7], MODELS[8], MODELS[10]])
def test_a_recorded_reply_goes_back_out_the_same(edge_binary, tmp_path, provider, model):
    """The next request after a reply the edge recorded is the same from
    either runtime — for Responses, the text item's server id and phase go
    back with it."""
    records, blobs = HISTORY["chat"]
    out = _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=_cached(model)), provider)
    provider.take()
    reply = out[-1]["message"]
    follow = [
        *records,
        reply,
        *({"v": 1, "role": "tool", "content": "ok", "tool_call_id": c["id"], "status": "success"}
          for c in reply["tool_calls"]),
    ]
    python, edge, _ = _both(edge_binary, tmp_path, provider, model, follow, blobs)
    python, edge = python["body"], edge["body"]
    INTENDED[model.split(":")[0]](python, edge)
    assert edge == python
    if model.startswith("meta"):
        said = [i for i in edge["input"] if i.get("id") == "msg_1"]
        assert said == [{"type": "message", "role": "assistant", "id": "msg_1", "phase": "commentary",
                         "content": [{"type": "output_text", "text": "Let me check.", "annotations": []}]}]


def test_a_reply_python_recorded_goes_back_out_the_same(edge_binary, tmp_path, provider):
    """Python records Anthropic's tool calls as opaque `tool_use` parts beside
    the calls; the edge sends that record on as Python does — each call once,
    and the thinking left out (`strip_historical_thinking` drops it from every
    assistant turn, on both sides)."""
    model = MODELS[0]
    records, blobs = HISTORY["chat"]
    rec = recorded_sync()
    assert [p.get("data", {}).get("type") for p in rec["content"] if p["type"] == "opaque"] == ["tool_use", "tool_use"]
    follow = [
        *records,
        rec,
        *({"v": 1, "role": "tool", "content": "ok", "tool_call_id": c["id"], "status": "success"}
          for c in rec["tool_calls"]),
    ]
    python, edge, _ = _both(edge_binary, tmp_path, provider, model, follow, blobs)
    assert edge["body"] == python["body"]
    turn = [m for m in edge["body"]["messages"] if m["role"] == "assistant"][-1]["content"]
    assert [b["type"] for b in turn] == ["text", "tool_use", "tool_use"]
    assert [b["id"] for b in turn[1:]] == ["toolu_1", "toolu_2"]


def test_bedrock_calls_are_signed_as_boto3_signs_them(edge_binary, tmp_path, provider):
    """The same signing scope and signed headers — the signature itself
    differs only by the second each side signed in."""
    model = MODELS[10]
    records, blobs = HISTORY["chat"]
    python, edge, _ = _both(edge_binary, tmp_path, provider, model, records, blobs)
    auth = [{k.lower(): v for k, v in r["headers"].items()}["authorization"] for r in (python, edge)]
    scope = [a.split("Signature=")[0] for a in auth]
    assert scope[0].split("/", 2)[2] == scope[1].split("/", 2)[2]  # region, service, signed headers
    assert scope[1].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock/aws4_request, SignedHeaders=content-type;host;x-amz-date, " in scope[1]


@pytest.mark.parametrize("name", HISTORIES)
def test_bedrock_request_without_a_cache_matches_python(edge_binary, tmp_path, provider, name):
    model = MODELS[11]
    if name == "responses_thread":
        return  # refused by both, above
    records, blobs = HISTORY[name]
    python = recorded_sync()["body"]
    _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=False), provider)
    assert provider.take()["body"] == python


def test_a_bedrock_reply_python_recorded_goes_back_out_the_same(edge_binary, tmp_path, provider):
    """LangChain records Bedrock's reasoning as an opaque block that history
    stripping doesn't know, so it rides along signed; the edge sends it as
    LangChain does, and the calls' string inputs as the objects they are."""
    model = MODELS[10]
    records, blobs = HISTORY["chat"]
    rec = recorded_sync()
    follow = [
        *records,
        rec,
        *({"v": 1, "role": "tool", "content": "ok", "tool_call_id": c["id"], "status": "success"}
          for c in rec["tool_calls"]),
    ]
    python, edge, _ = _both(edge_binary, tmp_path, provider, model, follow, blobs)
    assert edge["body"] == python["body"]
    turn = [m for m in edge["body"]["messages"] if m["role"] == "assistant"][-1]["content"]
    assert turn[0] == {"reasoningContent": {"reasoningText": {"text": "Weighing it.", "signature": "c2lnLXRoaW5r"}}}
    assert [b["toolUse"]["input"] for b in turn if "toolUse" in b] == [{"code": "check()"}, {"todos": ["x"]}]


@pytest.mark.parametrize("cache", [True, False])
def test_bedrock_blank_text_is_a_dot(edge_binary, tmp_path, provider, cache):
    """boto3 can't send an empty text block: LangChain sends `"."` in its
    place, and drops an empty bare string."""
    model = MODELS[10]
    records, blobs = HISTORY["blank_text"]
    python = recorded_sync()["body"]
    _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=cache), provider)
    assert provider.take()["body"] == python
    assert {"text": "."} in python["messages"][0]["content"]
