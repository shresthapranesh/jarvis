"""Near-duplicate detection for line-oriented memory documents.

Two writers of project memory ask "is this line already said?":
`jarvis.project_memory(append)` (tools/sdk.py, here) and the server's
consolidation merge (`edge/src/consolidate/dedupe.rs`, a port, difflib's ratio
included). Answering differently in the two would let a fact one rejects slip
in through the other — a change here is made there too.

Dependency-free (re + difflib): the kernel imports it at boot.
"""

from __future__ import annotations

import difflib
import re

_DUP_RATIO = 0.85
_DUP_MIN_LEN = 15  # below this, fuzzy matching is noise — require an exact hit


def normalize_entry(line: str) -> str:
    """Reduce a memory line to comparable form: no markers, case, or punctuation."""
    s = re.sub(r"^\s*(?:[-*•+]|\d+[.)])\s*", "", line)
    s = re.sub(r"[^\w\s]+", " ", s.lower())
    return " ".join(s.split())


def is_heading(line: str) -> bool:
    s = line.strip()
    return s.startswith("#") or (s.startswith("**") and s.endswith("**") and len(s) > 4)


def is_duplicate(norm: str, known: list[str]) -> bool:
    for k in known:
        if norm == k:
            return True
        if len(norm) < _DUP_MIN_LEN or len(k) < _DUP_MIN_LEN:
            continue
        if norm in k or k in norm:
            return True
        # SequenceMatcher.ratio() is 2*matches/(len(a)+len(b)) and matches can't
        # exceed the shorter string, so this bound is exact — skip the O(n*m)
        # comparison whenever the lengths alone put the threshold out of reach.
        if 2 * min(len(norm), len(k)) / (len(norm) + len(k)) < _DUP_RATIO:
            continue
        if difflib.SequenceMatcher(None, norm, k).ratio() >= _DUP_RATIO:
            return True
    return False


def dedupe_against(existing: str, content: str) -> tuple[str, int]:
    """Drop lines of `content` already stated in `existing` (or in each other).

    Returns (surviving_text, dropped_count). Headings pass through untouched but
    are pruned when every line beneath them turned out to be a duplicate.
    """
    known = [n for n in (normalize_entry(l) for l in existing.splitlines()) if n]
    kept: list[str] = []
    dropped = 0
    for line in content.splitlines():
        norm = normalize_entry(line)
        if not norm or is_heading(line):
            kept.append(line)
            continue
        if is_duplicate(norm, known):
            dropped += 1
            continue
        kept.append(line)
        known.append(norm)
    # A heading whose every bullet was a duplicate would otherwise be appended
    # with nothing under it.
    out: list[str] = []
    for i, line in enumerate(kept):
        if is_heading(line):
            has_body = False
            for nxt in kept[i + 1:]:
                if is_heading(nxt):
                    break
                if nxt.strip():
                    has_body = True
                    break
            if not has_body:
                continue
        out.append(line)
    return "\n".join(out).strip(), dropped
