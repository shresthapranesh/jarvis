"""The edge's LLM layer (`edge/src/llm/`) diffed against the Python path it
replaces: `core/messages.py` shaping, then the LangChain integrations.

One fake provider server records what each side sends and replays the same
stream to both. Compared:

- the shaped prompt (`--llm-shape`) against `build_llm_messages` over
  `strip_historical_thinking` + `repair_orphan_tool_calls`;
- the request body (`--llm-call`) against what `ChatGoogleGenerativeAI` /
  `ChatOllama` send for the same history — equal except for the differences
  the edge makes on purpose, each undone by name in `_INTENDED_*` below;
- the record each side builds from the same reply;
- and that a record the edge wrote goes back out through Python intact, so a
  thread can move between the runtimes.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import message_chunk_to_message

from core.context_cache import CacheSegment
from core.messages import build_llm_messages, repair_orphan_tool_calls, strip_historical_thinking
from core.transcript import decode, encode
from edge_support import edge_binary  # noqa: F401 — a fixture

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
    CacheSegment(name="skills", content="## Skills\n\n- research"),
    CacheSegment(name="project", content="## Project\n\nJarvis"),
    CacheSegment(name="memories", content="## Memories\n\nlikes tea", cacheable=False),
]


# ── histories ────────────────────────────────────────────────────────────────


def _gemini_ai(text: str, calls: list[tuple[str, str, dict, str | None]], thinking: str | None = None) -> AIMessage:
    """An assistant message as langchain-google-genai records one."""
    content: list[Any] = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking, "index": 0})
    if text:
        content.append(text)
    sigs = {cid: sig for cid, _, _, sig in calls if sig}
    kwargs: dict[str, Any] = {}
    if sigs:
        kwargs["__gemini_function_call_thought_signatures__"] = sigs
    if calls:
        kwargs["function_call"] = {"name": calls[0][1], "arguments": json.dumps(calls[0][2])}
    return AIMessage(
        content=content or "",
        tool_calls=[{"id": cid, "name": name, "args": args} for cid, name, args, _ in calls],
        additional_kwargs=kwargs,
        response_metadata={"model_provider": "google_genai", "model_name": "gemma-4-31b-it", "finish_reason": "STOP"},
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        id="lc_run--a",
    )


def _chat() -> list:
    return [
        SystemMessage("Summary of earlier turns: the user is planning a trip."),
        HumanMessage("Where should I go in May?", id="u1"),
        _gemini_ai("Try Lisbon.", [], thinking="They like warm places."),
        HumanMessage("And in June?", id="u2"),
    ]


def _tool_loop() -> list:
    return [
        HumanMessage("Add 1 and 1, then plan the week.", id="u1"),
        _gemini_ai(
            "",
            [("c1", "run_cell", {"code": "1+1"}, "c2lnLWMx"), ("c2", "write_todos", {"todos": ["a", "b"]}, None)],
            thinking="Two things to do.",
        ),
        ToolMessage(content='{"stdout": "2"}', tool_call_id="c1", name="run_cell"),
        ToolMessage(content='["a", "b"]', tool_call_id="c2", name="write_todos"),
        _gemini_ai("Done: 2.", []),
        HumanMessage(
            content=[{"type": "text", "text": "What is in this picture?"},
                     {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}}],
            id="u2",
        ),
        _gemini_ai("", [("c3", "run_cell", {"code": "inspect()"}, "c2lnLWMz")]),
        ToolMessage(content="Traceback: boom", tool_call_id="c3", status="error"),
        # Cancelled between the model step and its tools: no result.
        _gemini_ai("", [("c4", "run_cell", {"code": "retry()"}, "c2lnLWM0")]),
        HumanMessage("Never mind.", id="u3"),
    ]


def _foreign_calls() -> list:
    """Calls recorded without thought signatures — a thread from another model."""
    return [
        HumanMessage("Look it up.", id="u1"),
        AIMessage(content="", tool_calls=[{"id": "x1", "name": "run_cell", "args": {"code": "search()"}},
                                          {"id": "x2", "name": "run_cell", "args": {"code": "read()"}}]),
        ToolMessage(content="found", tool_call_id="x1"),
        ToolMessage(content="read", tool_call_id="x2"),
    ]


HISTORIES = {"chat": _chat, "tool_loop": _tool_loop, "foreign_calls": _foreign_calls}


def _records(history: list) -> tuple[list[dict], dict[str, str]]:
    records, blobs = [], {}
    for m in history:
        rec, bs = encode(m)
        records.append(json.loads(json.dumps(rec)))
        blobs.update({b.hash: base64.b64encode(b.data).decode() for b in bs})
    return records, blobs


def _python_history(records: list[dict], blobs: dict[str, str]) -> list:
    raw = {h: base64.b64decode(b) for h, b in blobs.items()}
    return [decode(r, raw) for r in records]


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
     "prompt_eval_duration": 2, "eval_count": 7, "eval_duration": 3},
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
                if "generatecontent" in self.path.lower():
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    for c in GEMINI_REPLY:
                        self.wfile.write(b"data: " + json.dumps(c).encode() + b"\r\n\r\n")
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


def _edge(edge_binary: Path, flag: str, tmp_path: Path, payload: dict, provider: _Provider | None = None) -> list:
    env = {k: v for k, v in os.environ.items() if k not in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "OLLAMA_HOST")}
    if provider:
        env.update(JARVIS_GOOGLE_BASE_URL=provider.url, GOOGLE_API_KEY="test-key", OLLAMA_HOST=provider.url)
    # cwd = tmp_path so the repo's .env can't supply a real key.
    out = subprocess.run(
        [str(edge_binary), flag], input=json.dumps(payload), capture_output=True, text=True,
        env=env, cwd=tmp_path, timeout=30, check=True,
    )
    return [json.loads(line) for line in out.stdout.splitlines()]


def _python_prompt(history: list, *, cache: bool, provider: str, segments=SEGMENTS) -> list:
    shaped = repair_orphan_tool_calls(strip_historical_thinking(history))
    cacheable = [s for s in segments if s.cacheable]
    volatile = "\n\n".join([s.content for s in segments if not s.cacheable] + [VOLATILE])
    return build_llm_messages(
        SYSTEM, cache, shaped, volatile_suffix=volatile, cache_segments=cacheable or None, cache_provider=provider
    )


def _edge_input(model: str, records, blobs, *, cache: bool, segments=SEGMENTS) -> dict:
    return {
        "model": model,
        "system": SYSTEM,
        # The agent step folds the non-cacheable segments into the volatile
        # text in Python; the edge takes them as segments and does it itself.
        "segments": [{"name": s.name, "content": s.content, "cacheable": s.cacheable} for s in segments if s.cacheable],
        "volatile": "\n\n".join([s.content for s in segments if not s.cacheable] + [VOLATILE]),
        "cache": cache,
        "history": records,
        "tools": TOOLS,
        "blobs": blobs,
    }


def _llm(model: str, url: str):
    provider, _, name = model.partition(":")
    if provider == "google_genai":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=name, base_url=url, google_api_key="test-key")
    from langchain_ollama import ChatOllama

    return ChatOllama(model=name, base_url=url)


def _python_call(model: str, url: str, messages: list) -> BaseMessage:
    llm = _llm(model, url).bind_tools([{"type": "function", "function": t} for t in TOOLS])

    async def run() -> BaseMessage:
        out: Any = None
        async for chunk in llm.astream(messages):
            out = chunk if out is None else out + chunk
        return message_chunk_to_message(cast(BaseMessage, out))

    return asyncio.run(run())


# ── shaping ──────────────────────────────────────────────────────────────────


def _neutral_python(messages: list) -> dict:
    """`build_llm_messages`' output in the shape `--llm-shape` prints:
    breakpoints as flags, content as `normalize_history_content` leaves it."""
    system, *rest = messages
    blocks: list[dict] = []
    content = system.content if isinstance(system.content, list) else [system.content]
    for b in content:
        if isinstance(b, dict) and "cachePoint" in b:
            blocks[-1]["breakpoint"] = True
        elif isinstance(b, str):
            blocks.append({"text": b, "breakpoint": False})
        else:
            blocks.append({"text": b["text"], "breakpoint": "cache_control" in b})
    out, marked = [], None
    for i, m in enumerate(rest):
        rec, _ = encode(m)
        parts = rec["content"] if isinstance(rec["content"], list) else None
        if parts and isinstance(parts[-1], dict) and parts[-1].get("type") == "opaque" \
                and "cachePoint" in parts[-1]["data"]:
            parts.pop()
            marked = i
        elif parts and isinstance(parts[-1], dict) and "cache_control" in (parts[-1].get("extras") or {}):
            parts[-1]["extras"].pop("cache_control")
            if not parts[-1]["extras"]:
                del parts[-1]["extras"]
            marked = i
        out.append(rec)
    return {"system": blocks, "messages": out, "history_breakpoint": marked}


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
    records, blobs = _records(HISTORIES[name]())
    expected = _neutral_python(_python_prompt(_python_history(records, blobs), cache=cache, provider=provider_id))
    [got] = _edge(edge_binary, "--llm-shape", tmp_path, _edge_input(f"{provider_id}:m", records, blobs, cache=cache))
    if cache:
        got = _normalized(got)
    assert got == expected


def test_shaping_without_segments(edge_binary, tmp_path):
    records, blobs = _records(_chat())
    expected = _neutral_python(_python_prompt(_python_history(records, blobs), cache=False, provider="ollama",
                                              segments=[]))
    [got] = _edge(edge_binary, "--llm-shape", tmp_path,
                  _edge_input("ollama:m", records, blobs, cache=False, segments=[]))
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


MODELS = ["google_genai:gemma-4-31b-it", "google_genai:gemini-3.1-flash-lite", "ollama:gemma4:26b"]


@pytest.mark.parametrize("name", HISTORIES)
@pytest.mark.parametrize("model", MODELS)
def test_request_matches_python(edge_binary, tmp_path, provider, name, model):
    records, blobs = _records(HISTORIES[name]())
    _python_call(model, provider.url, _python_prompt(_python_history(records, blobs), cache=False,
                                                     provider=model.split(":")[0]))
    python = provider.take()
    out = _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=False), provider)
    assert "message" in out[-1], out[-1]
    edge = provider.take()

    assert edge["path"] == python["path"]
    python, edge = python["body"], edge["body"]
    if model.startswith("google_genai"):
        assert provider.requests == []
        _intended_gemini(python, edge)
    else:
        _intended_ollama(python, edge)
    assert edge == python


def test_gemini_key_header(edge_binary, tmp_path, provider):
    records, blobs = _records(_chat())
    _edge(edge_binary, "--llm-call", tmp_path, _edge_input(MODELS[0], records, blobs, cache=False), provider)
    headers = {k.lower(): v for k, v in provider.take()["headers"].items()}
    assert headers["x-goog-api-key"] == "test-key"


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


@pytest.mark.parametrize("model", [MODELS[0], MODELS[2]])
def test_reply_matches_python(edge_binary, tmp_path, provider, model):
    records, blobs = _records(_chat())
    reply = _python_call(model, provider.url, _python_prompt(_python_history(records, blobs), cache=False,
                                                             provider=model.split(":")[0]))
    python = _semantics(encode(reply)[0])
    out = _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=False), provider)
    edge_rec = out[-1]["message"]
    edge = _semantics(edge_rec)
    deltas = out[:-1]

    if model.startswith("ollama"):
        # LangChain dropped Ollama's thinking and left the stop reason in
        # its response metadata; the edge keeps both.
        assert edge.pop("thinking") == "Hmm." and python.pop("thinking") == ""
        assert edge["finish_reason"] == encode(reply)[0]["extras"]["response_metadata"]["done_reason"]
        python["finish_reason"] = edge["finish_reason"]
        assert deltas == [{"thinking": "Hmm."}, {"text": "Let me"}, {"text": " check."}]
    else:
        assert deltas == [{"thinking": "Weighing "}, {"thinking": "it."}, {"text": "Let me"}, {"text": " check."}]
    assert edge == python
    assert all(c["id"] for c in edge_rec["tool_calls"])


def test_edge_record_goes_back_out_through_python(edge_binary, tmp_path, provider):
    """A thread the edge wrote to can continue in Python: its record decodes,
    and LangChain sends the call's thought signature back."""
    model = MODELS[0]
    records, blobs = _records(_chat())
    out = _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, records, blobs, cache=False), provider)
    provider.take()
    reply = out[-1]["message"]
    calls = reply["tool_calls"]
    follow = [
        *records,
        reply,
        *({"v": 1, "role": "tool", "content": "ok", "tool_call_id": c["id"], "status": "success"} for c in calls),
    ]
    _python_call(model, provider.url, _python_prompt(_python_history(follow, blobs), cache=False,
                                                     provider="google_genai"))
    sent = provider.take()["body"]["contents"]
    model_turn = [c for c in sent if c["role"] == "model"][-1]
    assert [p.get("thoughtSignature") for p in model_turn["parts"] if "functionCall" in p] == ["Y2FsbC1zaWc=", None]
    # …and the edge itself sends it back with the text's signature too.
    _edge(edge_binary, "--llm-call", tmp_path, _edge_input(model, follow, blobs, cache=False), provider)
    edge_turn = [c for c in provider.take()["body"]["contents"] if c["role"] == "model"][-1]
    assert edge_turn["parts"][0] == {"text": "Let me check.", "thoughtSignature": "dGV4dC1zaWc="}
