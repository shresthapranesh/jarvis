"""Regression tests for `strip_historical_thinking`.

Reasoning content has three provider spellings — Anthropic's `thinking` and
`redacted_thinking`, and LangChain's v1 `reasoning`. The sanitizer must remove
all three, because providers dereference these blocks without guarding: a
`reasoning` block left in history made every google_genai call on that thread
fail with `KeyError: 'reasoning'` before it ever reached the network.

The blocks below are the real shape recovered from a live checkpointer thread
after a Meta-model run, which is what triggered the crash: `summary`-style
reasoning blocks that carry no `reasoning` key at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage

from core.messages import strip_historical_thinking

# Verbatim from checkpoints.db, thread 6059576b — note: no "reasoning" key.
_V1_REASONING_BLOCK: dict[Any, Any] = {
    "id": "rs_6a572c7a9b74d2951b2d41dc:rs_bf48ca9c26b64009",
    "summary": [],
    "type": "reasoning",
    "index": 0,
}

_TEXT_BLOCK: dict[Any, Any] = {"type": "text", "text": "hello"}


def _blocks(msg: BaseMessage) -> list[dict]:
    assert isinstance(msg.content, list)
    return [b for b in msg.content if isinstance(b, dict)]


def test_strips_v1_reasoning_block_that_has_no_reasoning_key():
    msg = AIMessage(content=[_V1_REASONING_BLOCK, _TEXT_BLOCK])

    [out] = strip_historical_thinking([msg])

    assert _blocks(out) == [{"type": "text", "text": "hello"}]


@pytest.mark.parametrize("block_type", ["thinking", "redacted_thinking", "reasoning"])
def test_strips_every_provider_spelling(block_type: str):
    blocks: list[dict[Any, Any] | str] = [
        {"type": block_type, "text": "internal"},
        {"type": "text", "text": "answer"},
    ]
    msg = AIMessage(content=blocks)

    [out] = strip_historical_thinking([msg])

    assert _blocks(out) == [{"type": "text", "text": "answer"}]


def test_reasoning_only_content_keeps_a_placeholder_block():
    """Providers reject an AIMessage with empty content, so one must remain."""
    msg = AIMessage(content=[_V1_REASONING_BLOCK])

    [out] = strip_historical_thinking([msg])

    assert _blocks(out) == [{"type": "text", "text": ""}]


def test_strips_additional_kwargs_mirror_even_without_a_content_block():
    """The kwargs copy is the same content under the provider's own key."""
    msg = AIMessage(
        content="plain string content",
        additional_kwargs={"reasoning": {"summary": []}, "keep": "me"},
    )

    [out] = strip_historical_thinking([msg])

    assert "reasoning" not in out.additional_kwargs
    assert out.additional_kwargs["keep"] == "me"
    assert out.content == "plain string content"


def test_leaves_clean_messages_untouched():
    msgs: list[AnyMessage] = [
        HumanMessage(content="question"),
        AIMessage(content=[{"type": "text", "text": "answer"}]),
        AIMessage(content="plain"),
    ]

    out = strip_historical_thinking(msgs)

    # Identity, not just equality — the hot path should not copy needlessly.
    assert all(a is b for a, b in zip(out, msgs, strict=True))


def test_google_genai_conversion_no_longer_raises_key_error():
    """Pins the actual crash: the provider call that dereferenced the block."""
    _convert_to_parts = pytest.importorskip(
        "langchain_google_genai.chat_models"
    )._convert_to_parts

    dirty: list[dict[Any, Any] | str] = [_V1_REASONING_BLOCK, _TEXT_BLOCK]
    with pytest.raises(KeyError, match="reasoning"):
        _convert_to_parts(dirty)

    [cleaned] = strip_historical_thinking([AIMessage(content=dirty)])
    assert _convert_to_parts(cleaned.content)  # no raise
