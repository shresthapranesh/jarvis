"""Prefill / eval throughput measurement.

The arithmetic is trivial; what these guard are the cases where a naive
`tokens / elapsed` reports a number that looks plausible and is wrong — a
cache-served prefill, a non-streaming call with no first-token boundary, and a
provider that reports its own server-side durations.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from core.perf import PerfCallbackHandler, PerfTracker


def _result(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int | None = None,
    response_metadata: dict | None = None,
) -> LLMResult:
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cache_read is not None:
        # LangChain folds cache_read into input_tokens and reports the split here.
        usage["input_token_details"] = {"cache_read": cache_read}
    msg = AIMessage(
        content="hi",
        usage_metadata=usage,
        response_metadata=response_metadata or {},
    )
    return LLMResult(generations=[[ChatGeneration(message=msg)]])


def _num(value: float | None) -> float:
    """Assert a rate was measurable, then narrow it for comparison."""
    assert value is not None
    return value


def _drive(
    handler: PerfCallbackHandler,
    result: LLMResult,
    *,
    prefill_s: float,
    decode_s: float,
    stream: bool = True,
    run_id=None,
) -> None:
    """Run one fake LLM call with controlled prefill/decode spans."""
    run_id = run_id or uuid4()
    handler.on_chat_model_start({"kwargs": {"model": "test-model"}}, [[]], run_id=run_id)
    time.sleep(prefill_s)
    if stream:
        handler.on_llm_new_token("a", run_id=run_id)
        handler.on_llm_new_token("b", run_id=run_id)  # only the first marks TTFT
    time.sleep(decode_s)
    handler.on_llm_end(result, run_id=run_id)


# ── The split ────────────────────────────────────────────────────────────────

def test_prefill_and_eval_are_measured_separately():
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    _drive(
        handler,
        _result(input_tokens=1000, output_tokens=100),
        prefill_s=0.10,
        decode_s=0.30,
    )

    call = tracker.calls[0]
    assert call.source == "measured"
    # 1000 tokens over ~0.1s and 100 over ~0.3s. Sleep overshoots, never
    # undershoots, so the measured rate is bounded above by the ideal.
    assert 6_000 < _num(call.prefill_tps) <= 10_000
    assert 250 < _num(call.eval_tps) <= 334
    assert call.ttft_ms == pytest.approx(100, abs=60)


def test_first_token_marks_the_boundary_not_the_last():
    """A rate keyed off the *last* chunk would report decode as instantaneous."""
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    run_id = uuid4()
    handler.on_chat_model_start({"kwargs": {"model": "m"}}, [[]], run_id=run_id)
    time.sleep(0.05)
    handler.on_llm_new_token("a", run_id=run_id)
    time.sleep(0.15)
    handler.on_llm_new_token("b", run_id=run_id)
    time.sleep(0.15)
    handler.on_llm_end(_result(input_tokens=10, output_tokens=30), run_id=run_id)

    call = tracker.calls[0]
    # Decode spans both post-first-token sleeps (~0.3s), not just the last.
    assert call.decode_ms == pytest.approx(300, abs=80)


# ── Prompt caching ───────────────────────────────────────────────────────────

def test_cache_read_tokens_are_excluded_from_prefill():
    """Counting cached context as prefill work inflates the rate several-fold."""
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    _drive(
        handler,
        _result(input_tokens=10_000, output_tokens=50, cache_read=9_500),
        prefill_s=0.05,
        decode_s=0.05,
    )

    call = tracker.calls[0]
    assert call.cached_input_tokens == 9_500
    assert call.prefill_tokens == 500
    # Full-context accounting would put this ~20x higher.
    assert _num(call.prefill_tps) < 20_000


def test_fully_cached_prefill_contributes_no_prefill_rate():
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    _drive(
        handler,
        _result(input_tokens=8_000, output_tokens=40, cache_read=8_000),
        prefill_s=0.02,
        decode_s=0.30,
    )

    assert tracker.calls[0].prefill_tokens == 0
    assert tracker.calls[0].prefill_tps is None
    # Zero tokens over a real span would otherwise report the run at 0 tok/s.
    assert tracker.prefill_tps is None
    assert tracker.eval_tps is not None


# ── Provider-reported durations ──────────────────────────────────────────────

def test_provider_durations_win_over_wall_clock():
    """Ollama measures server-side, excluding queueing and transport."""
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    _drive(
        handler,
        _result(
            input_tokens=900,
            output_tokens=64,
            response_metadata={
                "prompt_eval_count": 900,
                "prompt_eval_duration": 300_000_000,  # 0.3s in ns
                "eval_count": 64,
                "eval_duration": 800_000_000,  # 0.8s in ns
            },
        ),
        prefill_s=0.02,
        decode_s=0.02,
    )

    call = tracker.calls[0]
    assert call.source == "provider"
    assert call.prefill_tps == pytest.approx(3000, rel=0.01)
    assert call.eval_tps == pytest.approx(80, rel=0.01)


def test_partial_provider_durations_fall_back_to_measured():
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    _drive(
        handler,
        _result(
            input_tokens=100,
            output_tokens=10,
            response_metadata={"prompt_eval_duration": 300_000_000},  # no eval_duration
        ),
        prefill_s=0.05,
        decode_s=0.30,
    )
    assert tracker.calls[0].source == "measured"


# ── Buffered streams ─────────────────────────────────────────────────────────

def test_buffered_flush_reports_no_eval_rate():
    """The failure this guard exists for, reproduced from a live run.

    `google_genai:gemma-4-31b-it` answering a short question delivered its whole
    reply in the last few milliseconds of the call: TTFT swallowed the
    generation, leaving 7 tokens over a 12ms span — an apparent 570 tok/s from a
    model that actually runs at ~40.
    """
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    _drive(
        handler,
        _result(input_tokens=4686, output_tokens=7),
        prefill_s=0.85,
        decode_s=0.012,
    )

    call = tracker.calls[0]
    assert call.source == "prefill_only"
    assert call.eval_tps is None
    assert tracker.eval_tps is None
    # Prefill survives: the swallowed generation inflates TTFT, which understates
    # the prefill rate — wrong in the direction that can't mislead.
    assert _num(call.prefill_tps) < 4686 / 0.85


def test_coarse_chunks_are_fine_when_the_span_is_real():
    """Chunk *count* is not the signal — a real generation can arrive in few.

    Also from a live run: 74 tokens over 6 chunks spanning 1.76s is an accurate
    42 tok/s. An earlier tokens-per-chunk heuristic threw this away.
    """
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    run_id = uuid4()
    handler.on_chat_model_start({"kwargs": {"model": "m"}}, [[]], run_id=run_id)
    time.sleep(0.05)
    for _ in range(6):  # six coarse chunks across a real span
        handler.on_llm_new_token("...", run_id=run_id)
        time.sleep(0.06)
    handler.on_llm_end(_result(input_tokens=100, output_tokens=74), run_id=run_id)

    call = tracker.calls[0]
    assert call.source == "measured"
    assert call.chunks == 6
    assert _num(call.eval_tps) == pytest.approx(74 / 0.36, rel=0.4)


# ── Degraded inputs ──────────────────────────────────────────────────────────

def test_non_streaming_call_reports_no_rates_rather_than_wrong_ones():
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    _drive(
        handler,
        _result(input_tokens=500, output_tokens=50),
        prefill_s=0.05,
        decode_s=0.05,
        stream=False,
    )

    call = tracker.calls[0]
    assert call.source == "unsplit"
    assert call.ttft_ms is None
    assert call.prefill_tps is None and call.eval_tps is None
    # The call still counts — total_ms is real, it just can't be split.
    assert call.total_ms > 0
    assert tracker.message_perf() is not None


def test_missing_start_callback_is_dropped_not_guessed():
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    handler.on_llm_end(_result(input_tokens=10, output_tokens=10), run_id=uuid4())
    assert tracker.calls == []
    assert tracker.message_perf() is None


# ── Aggregation ──────────────────────────────────────────────────────────────

def test_aggregate_is_token_weighted_not_a_mean_of_rates():
    """A 10-token call and a 500-token call are not equal evidence."""
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    _drive(handler, _result(input_tokens=20, output_tokens=10), prefill_s=0.01, decode_s=0.26)
    _drive(handler, _result(input_tokens=2000, output_tokens=500), prefill_s=0.05, decode_s=1.00)

    small = _num(tracker.calls[0].eval_tps)   # ~38 tok/s
    large = _num(tracker.calls[1].eval_tps)   # ~500 tok/s
    aggregate = _num(tracker.eval_tps)
    # The mean of the two per-call rates ignores that one call produced 98% of
    # the tokens; the weighted aggregate lands near that call.
    assert abs(aggregate - large) < abs(aggregate - small)
    assert aggregate != pytest.approx((small + large) / 2, rel=0.05)


def test_ttft_is_the_first_call_not_the_last():
    """Later calls in a run are re-prefills the user never waited on."""
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    _drive(handler, _result(input_tokens=100, output_tokens=10), prefill_s=0.15, decode_s=0.01)
    _drive(handler, _result(input_tokens=100, output_tokens=10), prefill_s=0.01, decode_s=0.01)

    assert tracker.ttft_ms == pytest.approx(150, abs=70)
    assert tracker.snapshot()["llm_calls"] == 2


def test_call_records_are_capped_but_aggregates_are_not():
    tracker = PerfTracker(keep_calls=3)
    handler = PerfCallbackHandler(tracker)
    for _ in range(6):
        _drive(handler, _result(input_tokens=100, output_tokens=10), prefill_s=0.01, decode_s=0.26)

    assert len(tracker.calls) == 3
    assert tracker.snapshot()["llm_calls"] == 6
    assert tracker.snapshot()["output_tokens"] == 60


def test_reused_run_id_does_not_leak_a_stale_first_token():
    """LangGraph reuses one run_id across an agent turn's successive calls.

    A leaked first-token stamp would place the next call's boundary before its
    own start — a negative prefill span presented as a rate.
    """
    tracker = PerfTracker()
    handler = PerfCallbackHandler(tracker)
    shared = uuid4()
    _drive(handler, _result(input_tokens=50, output_tokens=10),
           prefill_s=0.02, decode_s=0.26, run_id=shared)
    _drive(handler, _result(input_tokens=60, output_tokens=12),
           prefill_s=0.10, decode_s=0.26, run_id=shared)

    assert len(tracker.calls) == 2
    for call in tracker.calls:
        assert _num(call.ttft_ms) > 0
        assert _num(call.prefill_tps) > 0
    # The second call's own, longer prefill — not the first call's.
    assert tracker.calls[1].ttft_ms == pytest.approx(100, abs=60)
