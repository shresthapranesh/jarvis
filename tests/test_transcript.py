"""The v1 transcript format (`core/transcript.py`, `core/transcript_format.md`).

Every shape a provider integration leaves in history must come back equal
through JSON, and the neutral fields must land where the Rust loop will look
for them. The shapes are the ones found in real checkpoints (Gemini, Ollama,
OpenAI's Responses API via OpenRouter) plus what ChatAnthropic and
ChatBedrockConverse produce.
"""

from __future__ import annotations

import base64
import json

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from core.transcript import TranscriptError, check_checkpoints, decode, encode

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
PNG_B64 = base64.b64encode(PNG).decode()


def _round_trip(msg: BaseMessage) -> dict:
    rec, blobs = encode(msg)
    stored = json.loads(json.dumps(rec))
    assert decode(stored, {b.hash: b.data for b in blobs}) == msg
    return stored


GEMINI = AIMessage(
    id="lc_run--g1",
    content=[
        {"type": "thinking", "thinking": "The user wants news.", "index": 0},
        "Let me look.",
        {"type": "text", "text": "Done.", "index": 1, "extras": {"signature": "EjQKMg=="}},
    ],
    tool_calls=[{"id": "c1", "name": "run_cell", "args": {"code": "1+1"}, "type": "tool_call"}],
    additional_kwargs={
        "function_call": {"name": "run_cell", "arguments": '{"code": "1+1"}'},
        "__gemini_function_call_thought_signatures__": {"c1": "sig-c1", "gone": "sig-gone"},
    },
    response_metadata={"finish_reason": "STOP", "model_name": "gemini-2.5-pro",
                       "model_provider": "google_genai", "safety_ratings": []},
    usage_metadata={"input_tokens": 8483, "output_tokens": 350, "total_tokens": 8833,
                    "input_token_details": {"cache_read": 0}, "output_token_details": {"reasoning": 277}},
)

OLLAMA = AIMessage(
    content="hi",
    name="main",
    response_metadata={"model": "gemma4:26b", "created_at": "2026-04-07T04:43:34Z", "done": True,
                       "done_reason": "stop", "total_duration": 28383111750, "eval_count": 69,
                       "model_name": "gemma4:26b", "model_provider": "ollama"},
    usage_metadata={"input_tokens": 8476, "output_tokens": 69, "total_tokens": 8545},
)

OPENAI_RESPONSES = AIMessage(
    content=[
        {"id": "rs_1", "summary": [], "type": "reasoning", "index": 0},
        {"type": "text", "text": "Checking.", "phase": "commentary", "index": 1, "id": "rs_2"},
        {"type": "function_call", "name": "search_memory", "arguments": '{"k": 20}',
         "call_id": "call_1", "id": "fc_1", "index": 2},
    ],
    tool_calls=[{"id": "call_1", "name": "search_memory", "args": {"k": 20}, "type": "tool_call"}],
    response_metadata={"id": "resp_1", "object": "response", "status": "completed",
                       "model_name": "gpt-5", "model_provider": "openai"},
)

ANTHROPIC = AIMessage(
    content=[
        {"type": "thinking", "thinking": "Plan.", "signature": "ErUBCkYI"},
        {"type": "redacted_thinking", "data": "EmwKAhgB"},
        {"type": "text", "text": "Running it.", "cache_control": {"type": "ephemeral"}},
        {"type": "tool_use", "id": "toolu_1", "name": "run_cell", "input": {"code": "x"}},
    ],
    tool_calls=[{"id": "toolu_1", "name": "run_cell", "args": {"code": "x"}, "type": "tool_call"}],
    invalid_tool_calls=[{"id": "toolu_2", "name": "run_cell", "args": "{not json", "error": "bad json",
                         "type": "invalid_tool_call"}],
    response_metadata={"id": "msg_1", "stop_reason": "tool_use", "model_name": "claude-opus-4-7",
                       "model_provider": "anthropic"},
    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                    "input_token_details": {"cache_read": 4, "cache_creation": 2}},
)

BEDROCK = AIMessage(
    content=[
        {"type": "reasoning_content", "reasoning_content": {"text": "Hmm.", "signature": "s"}},
        {"type": "text", "text": "Answer."},
    ],
    response_metadata={"stopReason": "end_turn", "model_name": "us.anthropic.claude", "model_provider": "bedrock"},
)


@pytest.mark.parametrize("msg", [
    GEMINI, OLLAMA, OPENAI_RESPONSES, ANTHROPIC, BEDROCK,
    AIMessage(content=""),
    HumanMessage(content="plain", id="u1"),
    HumanMessage(content=[{"type": "text", "text": "here is my resume"}]),
    SystemMessage(content="[Conversation summary]\nearlier…", id="s1"),
    ToolMessage(content="42", tool_call_id="c1", name="run_cell", id="t1"),
    ToolMessage(content="boom", tool_call_id="c2", status="error", artifact={"raw": [1, 2]}),
    ToolMessage(content=[{"type": "text", "text": "listed"}], tool_call_id="c3"),
], ids=["gemini", "ollama", "openai-responses", "anthropic", "bedrock", "empty-ai", "human", "human-parts",
        "system", "tool", "tool-error", "tool-parts"])
def test_round_trips(msg: BaseMessage):
    _round_trip(msg)


def test_neutral_fields_are_where_the_loop_reads_them():
    rec = _round_trip(GEMINI)
    assert rec["role"] == "assistant"
    assert rec["model"] == {"provider": "google_genai", "name": "gemini-2.5-pro"}
    assert rec["finish_reason"] == "STOP"
    # The thought signature travels with its call; one for a call that's gone stays opaque.
    assert rec["tool_calls"] == [{"id": "c1", "name": "run_cell", "args": {"code": "1+1"}, "signature": "sig-c1"}]
    assert rec["extras"]["additional_kwargs"]["__gemini_function_call_thought_signatures__"] == {"gone": "sig-gone"}
    assert rec["content"] == [
        {"type": "thinking", "thinking": "The user wants news.", "extras": {"index": 0}},
        "Let me look.",
        {"type": "text", "text": "Done.", "signature": "EjQKMg==", "extras": {"index": 1}},
    ]
    assert rec["usage"] == {"input": 8483, "output": 350, "total": 8833,
                            "input_details": {"cache_read": 0}, "output_details": {"reasoning": 277}}

    rec = _round_trip(ANTHROPIC)
    assert [p["type"] for p in rec["content"]] == ["thinking", "redacted_thinking", "text", "opaque"]
    assert rec["content"][0]["signature"] == "ErUBCkYI"
    assert rec["content"][2]["extras"] == {"cache_control": {"type": "ephemeral"}}
    assert rec["invalid_tool_calls"] == [{"id": "toolu_2", "name": "run_cell", "args": "{not json", "error": "bad json"}]

    # Provider items keep their place among the parts.
    rec = _round_trip(OPENAI_RESPONSES)
    assert [p["type"] for p in rec["content"]] == ["opaque", "text", "opaque"]

    rec = _round_trip(ToolMessage(content="boom", tool_call_id="c2", status="error"))
    assert (rec["role"], rec["tool_call_id"], rec["status"]) == ("tool", "c2", "error")
    assert "id" not in rec


def test_media_goes_to_blobs_once():
    image = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}}
    msg = HumanMessage(content=[
        {"type": "text", "text": "compare"},
        image,
        {"type": "image_url", "image_url": f"data:image/png;base64,{PNG_B64}"},  # the bare-string spelling
        {"type": "media", "mime_type": "image/png", "data": PNG_B64},
        {"type": "media", "mime_type": "application/pdf", "data": base64.b64encode(b"%PDF").decode()},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png", "detail": "low"}},
    ])
    rec, blobs = encode(msg)
    assert decode(json.loads(json.dumps(rec)), {b.hash: b.data for b in blobs}) == msg
    digest = rec["content"][1]["blob"]
    assert digest.startswith("sha256:") and len({b.hash for b in blobs if b.data == PNG}) == 1
    assert rec["content"][1] == {"type": "image", "mime_type": "image/png", "blob": digest,
                                 "extras": {"lc_type": "image_url"}}
    assert rec["content"][4]["type"] == "file"
    assert rec["content"][5] == {"type": "image", "url": "https://example.com/a.png",
                                 "extras": {"lc_type": "image_url", "lc_image_url_extras": {"detail": "low"}}}
    # No base64 left in the record itself.
    assert PNG_B64 not in json.dumps(rec)


def test_base64_that_wouldnt_rebuild_stays_inline():
    odd = PNG_B64[:20] + "\n" + PNG_B64[20:]
    msg = HumanMessage(content=[{"type": "media", "mime_type": "image/png", "data": odd}])
    rec, blobs = encode(msg)
    assert not blobs and rec["content"][0]["data"] == odd
    assert decode(rec) == msg


def test_refuses_what_it_cannot_read():
    with pytest.raises(TranscriptError):
        decode({"v": 2, "role": "user", "content": "x"})
    with pytest.raises(TranscriptError):
        decode({"v": 1, "role": "robot", "content": "x"})
    with pytest.raises(TranscriptError):
        decode({"v": 1, "role": "user", "content": [{"type": "image", "blob": "sha256:00", "mime_type": "image/png",
                                                     "extras": {"lc_type": "media"}}]})


def test_check_reads_a_checkpoints_db(tmp_path):
    """The `maintenance check-transcript` path, over a real LangGraph saver."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import START, MessagesState, StateGraph

    db = tmp_path / "checkpoints.db"
    graph = StateGraph(MessagesState)  # type: ignore[bad-specialization]
    graph.add_node("echo", lambda state: {"messages": [GEMINI, ToolMessage(content="2", tool_call_id="c1")]})
    graph.add_edge(START, "echo")
    with SqliteSaver.from_conn_string(str(db)) as saver:
        graph.compile(checkpointer=saver).invoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {"thread_id": "t1"}},
        )
    report = check_checkpoints(str(db))
    assert report.messages > 0
    assert report.mismatches == []
