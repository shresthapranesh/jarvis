"""Long-running tool approval."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Lazy import of current_ctx to avoid circular import via tools/__init__.py
# (tools/files.py -> core/approval.py -> tools/context.py would trigger
# tools package init). We import inside request_tool_approval instead.


AFFIRMATIVE = {
    "yes", "y", "approve", "approved", "ok", "okay",
    "proceed", "confirm", "confirmed", "allow", "allowed",
    "go", "go ahead", "do it", "sure", "aye",
}

NEGATIVE = {
    "no", "n", "deny", "denied", "cancel", "abort", "stop",
    "reject", "rejected", "block", "nope", "don't", "dont",
}


def _normalize_answer(text: str) -> str:
    return text.strip().lower()


def is_affirmative_answer(text: str) -> bool | None:
    """Parse free-text approval answer.

    Returns True if affirmative, False if negative, None if ambiguous.

    Fixed from substring bug: earlier `if neg in norm` with NEGATIVE containing
    "n" made any answer with letter 'n' deny. Now we match on word tokens and
    require word boundaries; single-char entries only count on exact match.
    """
    import re

    norm = _normalize_answer(text)
    if not norm:
        return None

    # Direct exact match (covers single-char y/n too)
    if norm in AFFIRMATIVE:
        return True
    if norm in NEGATIVE:
        return False

    # Token set for whole-word matching (splits on word boundaries)
    # Keep apostrophes inside words for "don't" -> we normalize by stripping them
    # but also check substring for phrases with apostrophes.
    tokens = set(re.findall(r"\b\w+\b", norm))

    # Helper: does phrase appear as whole word(s)?
    def _phrase_in_text(phrase: str) -> bool:
        phrase = phrase.lower().strip()
        if not phrase:
            return False
        # Single-char tokens only match exact (already handled)
        if len(phrase) == 1:
            return False
        if " " in phrase:
            # Multi-word: substring, but with word boundaries at ends
            # "go ahead" in "go ahead and do it" -> True
            return phrase in norm
        else:
            # Single word: whole-word match via tokens or \b regex
            if phrase in tokens:
                return True
            # Also handle "don't" where tokenization splits -> check substring
            # for known contractions
            if "'" in phrase or phrase == "dont":
                return phrase in norm or phrase.replace("'", "") in norm
            # Fallback regex with boundaries to avoid "no" in "known"
            return bool(re.search(rf"\b{re.escape(phrase)}\b", norm))

    # Negative takes precedence — if any negative phrase found, deny
    # Check longer phrases first so "don't" beats "do"
    for neg in sorted(NEGATIVE, key=len, reverse=True):
        if len(neg) == 1:
            continue  # single-char already handled via exact match
        if _phrase_in_text(neg):
            # Special case: if answer also contains "but yes" or "but approve"
            # after the negative, treat as affirmative override? Keep simple:
            # if explicit "but yes" pattern, let affirmative win.
            if re.search(r"\bbut\b.*\b(yes|approve|ok|proceed)\b", norm):
                break
            return False

    for aff in sorted(AFFIRMATIVE, key=len, reverse=True):
        if len(aff) == 1:
            continue
        if _phrase_in_text(aff):
            return True

    # Heuristic: starts with affirmative prefix
    if norm.startswith(("yes", "approve", "ok")):
        return True

    return None


def request_tool_approval(tool_name: str, args: dict[str, Any], reason: str) -> bool:
    """Request human approval for a tool action.

    Emits an `approval_request` event for UI, then interrupts the run.
    Returns True if approved, False if denied.

    If called outside an agent run (no thread_id / no interrupt capability),
    it logs and auto-approves to keep tests and CLI one-shots working.

    The interrupt payload is a dict so streaming.py extracts a human-readable
    question; the custom event carries structured data for richer UI.
    """
    # Lazy import to avoid circular import (see module docstring)
    from tools.context import current_ctx  # noqa: WPS433

    ctx = current_ctx()

    # Surface policy: only interactive web chats should pause for approval.
    # Automations, board tasks, bot threads, workflow sub-agents, and CLI/tests
    # have no UI to resume the interrupt and would hang forever.
    def _is_headless(c) -> bool:
        # No run context at all (tests, direct invocation)
        if c.thread_id is None and c.conversation_id is None:
            return True
        # Board tasks
        if c.board_task_id:
            return True
        # No conversation (workflow AgentNode uses random thread_id, no conv)
        if c.conversation_id is None:
            return True
        cid = c.conversation_id.lower()
        tid = (c.thread_id or "").lower()
        # Prefixed conversation ids: automation_, boardtask_, telegram_, discord_, task_
        if any(
            cid.startswith(p)
            for p in ("automation_", "boardtask_", "telegram_", "discord_", "task_", "workflow_")
        ):
            return True
        if any(tid.startswith(p) for p in ("automation_", "boardtask_", "telegram_", "discord_")):
            return True
        return False

    if _is_headless(ctx):
        logger.debug(
            "approval auto-approved (headless surface): %s conv=%s thread=%s board=%s",
            tool_name,
            ctx.conversation_id,
            ctx.thread_id,
            ctx.board_task_id,
        )
        return True

    # Human-readable question for the existing InterruptPrompt component
    pretty_args = json.dumps(args, indent=2)[:1000] if args else "{}"
    question = (
        f"Approval needed for `{tool_name}`\n"
        f"Reason: {reason}\n"
        f"Args: {pretty_args}\n\n"
        "Reply 'approve' to proceed or 'deny' to cancel (you can also explain)."
    )

    # This raises GraphInterrupt on first call; on resume returns the user's answer.
    # MUST NOT swallow GraphInterrupt — it is how LangGraph pauses the run.
    # The browser tool calls request_input bare for this reason.
    try:
        from langgraph.errors import GraphInterrupt  # noqa: WPS433
    except ImportError:
        GraphInterrupt = BaseException  # fallback: don't swallow BaseException

    def _emit_request():
        try:
            safe_args = {}
            for k, v in args.items():
                s = str(v)
                safe_args[k] = s[:500] + ("..." if len(s) > 500 else "")
            ctx.emit(
                "approval_request",
                tool=tool_name,
                args=safe_args,
                reason=reason,
            )
        except Exception as exc:
            logger.debug("approval_request emit failed: %s", exc)

    try:
        answer = ctx.request_input(
            {
                "type": "approval",
                "tool": tool_name,
                "args": args,
                "reason": reason,
                "question": question,
            }
        )
    except GraphInterrupt:
        # First call — emit request for richer UI, then pause.
        _emit_request()
        raise
    except Exception as exc:
        # If interrupt mechanism unavailable (should not happen inside a run),
        # fall back to deny for safety on destructive actions.
        logger.warning("approval interrupt failed (%s) — denying by default: %s %s", exc, tool_name, reason)
        return False

    # answer is the user's free-text response
    answer_str = str(answer) if answer is not None else ""
    parsed = is_affirmative_answer(answer_str)

    if parsed is True:
        logger.info("approval granted for %s: %r", tool_name, answer_str[:100])
        try:
            ctx.emit("approval_resolved", tool=tool_name, approved=True, answer=answer_str[:500])
        except Exception:
            pass
        return True
    if parsed is False:
        logger.info("approval denied for %s: %r", tool_name, answer_str[:100])
        try:
            ctx.emit("approval_resolved", tool=tool_name, approved=False, answer=answer_str[:500])
        except Exception:
            pass
        return False

    # Ambiguous — deny conservatively, whatever the length. A reply that
    # matches no approve/deny keyword is usually a question or an alternative
    # instruction; running the original destructive action on that basis is
    # the wrong default. The denial surfaces the text so the agent can adapt.
    logger.info("approval ambiguous — denying: %r", answer_str[:100])
    try:
        ctx.emit("approval_resolved", tool=tool_name, approved=False, answer=answer_str[:500], ambiguous=True)
    except Exception:
        pass
    return False


# ── Convenience: decorator for tools that always need approval ─────────────

def require_approval(reason_template: str = "This action requires approval"):
    """Decorator factory for tool functions that always need approval.

    The reason can reference args via format, e.g. "Delete workflow {workflow_id}?"

    Example:
        @require_approval("Delete workflow {workflow_id}?")
        async def delete_workflow(workflow_id: str) -> str: ...
    """

    def decorator(fn):
        import functools
        import inspect

        sig = inspect.signature(fn)

        def _bound_args(args, kwargs) -> dict[str, Any]:
            # Map positional args onto parameter names so the reason template
            # and the approval UI's Args display see them (a positional call
            # would otherwise show `Args: {}` for the action being approved).
            try:
                return dict(sig.bind_partial(*args, **kwargs).arguments)
            except TypeError:
                return dict(kwargs)

        def _reason_and_args(args, kwargs) -> tuple[str, dict[str, Any]]:
            bound = _bound_args(args, kwargs)
            try:
                reason = reason_template.format(**bound)
            except Exception:
                reason = reason_template
            return reason, bound

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                reason, bound = _reason_and_args(args, kwargs)
                tool_name = getattr(fn, "__name__", "tool")
                if not request_tool_approval(tool_name, bound, reason):
                    return "User denied approval — action cancelled."
                return await fn(*args, **kwargs)

            return async_wrapper
        else:

            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                reason, bound = _reason_and_args(args, kwargs)
                tool_name = getattr(fn, "__name__", "tool")
                if not request_tool_approval(tool_name, bound, reason):
                    return "User denied approval — action cancelled."
                return fn(*args, **kwargs)

            return sync_wrapper

    return decorator
