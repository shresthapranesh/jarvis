"""Throughput tracking — prompt-processing (prefill) and generation (eval) tok/s.

Token *counts* live in `core.log_callback.UsageAccumulator` (per-run totals) and
`core.budget.BudgetTracker` (live totals + ceilings). Neither carries timing, and
the run-level `elapsed_seconds` is useless as a throughput denominator because it
includes tool calls, retrieval, and compaction. This module measures the LLM call
itself and splits it in two:

    prefill  — start → first streamed token   (prompt processing)
    decode   — first token → end              (generation / "eval")

Two things make a naive `tokens / elapsed` wrong, and both are handled here:

- **Prompt caching.** A cache hit does almost no prefill work, so counting the
  full re-sent context against the prefill clock reports throughput several times
  the real number. `cache_read` tokens are subtracted; a fully-cached call is
  excluded from the prefill aggregate entirely rather than logged as 0 tok/s.
- **Provider-reported timings beat wall clock.** Ollama returns
  `prompt_eval_duration` / `eval_duration` measured server-side, which excludes
  queueing and transport. When present they win, and the call is tagged
  `source="provider"`.

Callbacks propagate to child runnables, so a tracker attached to a run also sees
worker/subagent calls — same scope as `UsageAccumulator`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)

# A rate computed over a shorter span than this is noise — clock granularity and
# the first-chunk callback hop dominate. Such calls contribute tokens and time to
# the aggregate but get no per-call rate of their own.
_MIN_SPAN_SECONDS = 0.005

# The wall-clock split assumes the first chunk marks the moment generation began.
# That holds when a provider streams as it decodes, and breaks when it buffers
# and flushes at the end — then TTFT swallows the generation and the leftover
# span measures how fast the socket drained, not how fast the model ran.
#
# Measured against `google_genai:gemma-4-31b-it`, the tell is the *duration* of
# the decode span, not its chunk count. Real generations: 74 tokens / 6 chunks /
# 1757ms (42 tok/s) and 640 tokens / 31 chunks / 17.4s (37 tok/s) — both accurate
# despite carrying 12-20 tokens per chunk. The buffered one: 7 tokens arriving
# 12ms before the call ended, an apparent 570 tok/s.
#
# So the guard is a floor on the span itself: below this, a flush and a
# generation are indistinguishable, and no eval rate is reported. Deliberately
# free of any assumption about how fast a model "should" decode — that ceiling is
# model-specific and would be the same guess this is meant to avoid.
_MIN_DECODE_SECONDS = 0.25
# One interval is the minimum evidence of a cadence.
_MIN_CHUNKS_FOR_EVAL = 2


@dataclass(frozen=True)
class LlmCallPerf:
    """Throughput for one LLM round trip."""

    model: str
    input_tokens: int
    output_tokens: int
    # Input tokens served from the prompt cache — billed, but no prefill work.
    cached_input_tokens: int
    # input_tokens - cached_input_tokens: what prefill actually had to process.
    prefill_tokens: int
    ttft_ms: float | None
    total_ms: float
    decode_ms: float | None
    # The spans the rates divide by, and the same numbers PerfTracker sums into
    # its aggregate — so a provider-reported duration can't drift from the wall
    # clock the aggregate would otherwise re-derive.
    prefill_seconds: float | None
    decode_seconds: float | None
    # Streamed chunks seen for this call. 0 = the provider didn't stream.
    chunks: int
    # "provider"     — server-side durations, exact.
    # "measured"     — wall clock either side of the first chunk.
    # "prefill_only" — decode span too short to tell a flush from a generation;
    #                  eval_tps withheld, prefill kept (see _MIN_DECODE_SECONDS).
    # "unsplit"      — nothing streamed, so there is no boundary at all.
    source: str

    @property
    def prefill_tps(self) -> float | None:
        return _rate(self.prefill_tokens, self.prefill_seconds)

    @property
    def eval_tps(self) -> float | None:
        return _rate(self.output_tokens, self.decode_seconds)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "prefill_tokens": self.prefill_tokens,
            "ttft_ms": _round(self.ttft_ms),
            "total_ms": _round(self.total_ms),
            "decode_ms": _round(self.decode_ms),
            "prefill_tps": _round(self.prefill_tps),
            "eval_tps": _round(self.eval_tps),
            "chunks": self.chunks,
            "source": self.source,
        }


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _rate(tokens: int, seconds: float | None) -> float | None:
    if not tokens or seconds is None or seconds < _MIN_SPAN_SECONDS:
        return None
    return tokens / seconds


class PerfTracker:
    """Per-run accumulator of `LlmCallPerf` records.

    Aggregates are token-weighted (total tokens / total seconds), not a mean of
    per-call rates — a 50-token call and a 5000-token call are not equal evidence
    about how fast the model runs.
    """

    def __init__(self, task_state: Any | None = None, keep_calls: int = 50) -> None:
        self.calls: list[LlmCallPerf] = []
        self._task_state = task_state
        # Only the newest N records are retained for the snapshot; the running
        # sums below are what the aggregates read, so trimming loses no rate.
        self._keep_calls = keep_calls
        self._prefill_tokens = 0
        self._prefill_seconds = 0.0
        self._output_tokens = 0
        self._decode_seconds = 0.0
        self._llm_seconds = 0.0
        self._first_ttft_ms: float | None = None
        self._llm_calls = 0

    # ── Aggregates ────────────────────────────────────────────────────────

    @property
    def ttft_ms(self) -> float | None:
        """Time to first token of the run's *first* LLM call — what the user waited."""
        return self._first_ttft_ms

    @property
    def llm_ms(self) -> float:
        """Wall time spent inside LLM calls (excludes tools, retrieval, compaction)."""
        return self._llm_seconds * 1000.0

    @property
    def prefill_tps(self) -> float | None:
        return _rate(self._prefill_tokens, self._prefill_seconds)

    @property
    def eval_tps(self) -> float | None:
        return _rate(self._output_tokens, self._decode_seconds)

    def snapshot(self) -> dict[str, Any]:
        return {
            "ttft_ms": _round(self.ttft_ms),
            "llm_ms": _round(self.llm_ms),
            "prefill_tps": _round(self.prefill_tps),
            "eval_tps": _round(self.eval_tps),
            "prefill_tokens": self._prefill_tokens,
            "output_tokens": self._output_tokens,
            "llm_calls": self._llm_calls,
            "calls": [c.as_dict() for c in self.calls],
        }

    def message_perf(self) -> dict[str, Any] | None:
        """The four values persisted on the Message row, or None if nothing measured."""
        if not self._llm_calls:
            return None
        return {
            "ttft_ms": _round(self.ttft_ms),
            "llm_ms": _round(self.llm_ms),
            "prefill_tps": _round(self.prefill_tps),
            "eval_tps": _round(self.eval_tps),
        }

    # ── Recording ─────────────────────────────────────────────────────────

    def record(self, call: LlmCallPerf) -> None:
        self._llm_calls += 1
        self.calls.append(call)
        if len(self.calls) > self._keep_calls:
            del self.calls[: len(self.calls) - self._keep_calls]

        self._llm_seconds += call.total_ms / 1000.0
        if self._first_ttft_ms is None and call.ttft_ms is not None:
            self._first_ttft_ms = call.ttft_ms

        # A fully cache-served prefill did no work — including it would report
        # its (real, tiny) duration against zero tokens and drag the aggregate.
        if call.prefill_tokens > 0 and call.prefill_seconds:
            self._prefill_tokens += call.prefill_tokens
            self._prefill_seconds += call.prefill_seconds
        if call.output_tokens > 0 and call.decode_seconds:
            self._output_tokens += call.output_tokens
            self._decode_seconds += call.decode_seconds

        logger.debug(
            "perf: %s ttft=%sms prefill=%s tok/s (%d tok) eval=%s tok/s (%d tok) [%s]",
            call.model, _round(call.ttft_ms), _round(call.prefill_tps),
            call.prefill_tokens, _round(call.eval_tps), call.output_tokens, call.source,
        )
        self._emit()

    def _emit(self) -> None:
        """One event per LLM call — bounded by the run's `max_llm_calls` ceiling."""
        if self._task_state is None:
            return
        try:
            from core.state import emit_event

            emit_event(
                self._task_state,
                "perf_update",
                ttft_ms=_round(self.ttft_ms),
                llm_ms=_round(self.llm_ms),
                prefill_tps=_round(self.prefill_tps),
                eval_tps=_round(self.eval_tps),
                llm_calls=self._llm_calls,
                snapshot=self.snapshot(),
            )
        except Exception:
            pass


# ── Usage / metadata extraction ──────────────────────────────────────────────


def _iter_messages(response: LLMResult):
    for gen_list in response.generations:
        for gen in gen_list:
            msg = getattr(gen, "message", None)
            if msg is not None:
                yield gen, msg


def _extract_usage(response: LLMResult) -> tuple[int, int, int]:
    """(input_tokens, output_tokens, cached_input_tokens) summed over generations.

    Mirrors `UsageAccumulator`'s two-path read (usage_metadata, then the
    `llm_output` fallback for providers that only populate that), plus the cache
    detail LangChain nests under `input_token_details`.
    """
    input_tokens = output_tokens = cached = 0
    found = False
    for _gen, msg in _iter_messages(response):
        usage = getattr(msg, "usage_metadata", None)
        if not usage:
            continue
        found = True
        input_tokens += usage.get("input_tokens", 0) or 0
        output_tokens += usage.get("output_tokens", 0) or 0
        details = usage.get("input_token_details") or {}
        if isinstance(details, dict):
            cached += details.get("cache_read", 0) or 0
    if not found:
        fallback = (response.llm_output or {}).get("token_usage") or {}
        input_tokens = fallback.get("prompt_tokens") or fallback.get("input_tokens") or 0
        output_tokens = fallback.get("completion_tokens") or fallback.get("output_tokens") or 0
    if not cached:
        # Providers that don't route cache counts through usage_metadata still
        # put the raw field on response_metadata (Anthropic/Bedrock shape).
        for _gen, msg in _iter_messages(response):
            meta = getattr(msg, "response_metadata", None) or {}
            raw = meta.get("usage") if isinstance(meta.get("usage"), dict) else meta
            if isinstance(raw, dict):
                cached += raw.get("cache_read_input_tokens", 0) or 0
    # LangChain folds cache_read into input_tokens; a provider that doesn't would
    # otherwise yield a negative prefill count.
    return input_tokens, output_tokens, min(cached, input_tokens)


def _provider_durations(response: LLMResult) -> tuple[int, float, int, float] | None:
    """Server-side (prompt_tokens, prefill_s, eval_tokens, decode_s) — Ollama shape.

    Durations are nanoseconds. Returns None unless both spans are present and
    positive, since a partial read would mix two clocks in one rate.
    """
    for gen, msg in _iter_messages(response):
        meta = dict(getattr(msg, "response_metadata", None) or {})
        info = getattr(gen, "generation_info", None)
        if isinstance(info, dict):
            for k, v in info.items():
                meta.setdefault(k, v)
        p_dur = meta.get("prompt_eval_duration")
        e_dur = meta.get("eval_duration")
        if not p_dur or not e_dur:
            continue
        try:
            p_s, e_s = float(p_dur) / 1e9, float(e_dur) / 1e9
            p_tok = int(meta.get("prompt_eval_count") or 0)
            e_tok = int(meta.get("eval_count") or 0)
        except (TypeError, ValueError):
            continue
        if p_s > 0 and e_s > 0:
            return p_tok, p_s, e_tok, e_s
    return None


def _model_name(serialized: dict[str, Any] | None, kwargs: dict[str, Any]) -> str:
    s = serialized or {}
    kw = s.get("kwargs") or {}
    return (
        kw.get("model")
        or kw.get("model_name")
        or kw.get("model_id")
        or s.get("name")
        or (kwargs.get("invocation_params") or {}).get("model")
        or "?"
    )


class PerfCallbackHandler(BaseCallbackHandler):
    """Feeds a `PerfTracker` by timing each LLM call's prefill and decode spans.

    Attach alongside `AgentLogger` / `UsageAccumulator` / `BudgetCallbackHandler`.

    Two cases yield no split, and both report nothing rather than a fabricated
    number: a non-streaming call (no `on_llm_new_token`, so there is no boundary
    at all) and a coarsely-buffered stream (`_COARSE_TOKENS_PER_CHUNK`, where the
    boundary exists but is in the wrong place). Both still contribute `ttft_ms`
    where available and `total_ms`.
    """

    # Same reason as BudgetCallbackHandler: the tracker emits TaskState events,
    # which must happen on the event loop.
    run_inline = True

    def __init__(self, tracker: PerfTracker) -> None:
        self.tracker = tracker
        self._started: dict[UUID, float] = {}
        self._first_token: dict[UUID, float] = {}
        self._chunks: dict[UUID, int] = {}
        self._names: dict[UUID, str] = {}

    # ── Span boundaries ───────────────────────────────────────────────────

    def _start(self, serialized: dict[str, Any] | None, run_id: UUID, kwargs: dict[str, Any]) -> None:
        # LangGraph reuses one run_id across a graph's successive model calls
        # (observed: both iterations of an agent turn share it). The end handler
        # pops, so sequential reuse is safe — but a missed on_llm_end would
        # otherwise leave a stale first-token stamp that becomes the next call's
        # boundary, reporting a negative-length prefill.
        self._first_token.pop(run_id, None)
        self._chunks.pop(run_id, None)
        self._started[run_id] = time.perf_counter()
        self._names[run_id] = _model_name(serialized, kwargs)

    def on_chat_model_start(
        self, serialized: dict[str, Any] | None, messages: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._start(serialized, run_id, kwargs)

    def on_llm_start(
        self, serialized: dict[str, Any] | None, prompts: list[str], *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._start(serialized, run_id, kwargs)

    def on_llm_new_token(
        self, token: str | list[dict[str, Any] | str], *, run_id: UUID, **kwargs: Any
    ) -> None:
        # setdefault, not assignment — only the *first* chunk marks the boundary.
        self._first_token.setdefault(run_id, time.perf_counter())
        self._chunks[run_id] = self._chunks.get(run_id, 0) + 1

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._forget(run_id)

    def _forget(self, run_id: UUID) -> None:
        self._started.pop(run_id, None)
        self._first_token.pop(run_id, None)
        self._chunks.pop(run_id, None)
        self._names.pop(run_id, None)

    # ── Measurement ───────────────────────────────────────────────────────

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        ended = time.perf_counter()
        started = self._started.pop(run_id, None)
        first_token = self._first_token.pop(run_id, None)
        chunks = self._chunks.pop(run_id, 0)
        model = self._names.pop(run_id, "?")
        if started is None:
            return  # start callback missed — no clock to measure against

        total_s = ended - started
        try:
            input_tokens, output_tokens, cached = _extract_usage(response)
        except Exception:  # never let measurement break a run
            logger.debug("perf: usage extraction failed", exc_info=True)
            return
        prefill_tokens = max(0, input_tokens - cached)

        ttft_s = None if first_token is None else first_token - started
        decode_s = None if ttft_s is None else max(0.0, total_s - ttft_s)

        provider = _provider_durations(response)
        if provider is not None:
            p_tok, prefill_s, e_tok, decode_s_prov = provider
            # Prefer the provider's own token counts too — they're what its
            # durations were measured over.
            prefill_tokens = p_tok or prefill_tokens
            output_tokens = e_tok or output_tokens
            decode_s = decode_s_prov
            source = "provider"
        else:
            prefill_s = ttft_s
            if ttft_s is None:
                source = "unsplit"
            elif chunks < _MIN_CHUNKS_FOR_EVAL or (decode_s or 0.0) < _MIN_DECODE_SECONDS:
                # Buffered flush, or too short to tell one from a generation.
                # Prefill survives: a flush folded into TTFT inflates it, which
                # understates the prefill rate — wrong in the safe direction.
                decode_s = None
                source = "prefill_only"
            else:
                source = "measured"

        self.tracker.record(LlmCallPerf(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            prefill_tokens=prefill_tokens,
            ttft_ms=None if ttft_s is None else ttft_s * 1000.0,
            total_ms=total_s * 1000.0,
            decode_ms=None if decode_s is None else decode_s * 1000.0,
            prefill_seconds=prefill_s,
            decode_seconds=decode_s,
            chunks=chunks,
            source=source,
        ))
