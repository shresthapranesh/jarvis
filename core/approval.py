"""Reading a free-text approval answer as yes, no, or neither."""

from __future__ import annotations


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
