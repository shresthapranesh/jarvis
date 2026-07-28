"""Hybrid retrieval primitives: lexical (SQLite FTS5) + dense fusion, and cutoff.

Two problems this fixes, shared by `memory_store.search_memory` and
`doc_index.search_chunks`:

1. **Dense-only search misses exact tokens.** Embeddings encode meaning, and an
   error code / filename / person's name has no meaning to encode — it is just a
   token. BM25 over FTS5 catches precisely those, so the two arms fail in
   different directions and the union beats either alone.
2. **Dense-only search cannot say "nothing".** Cosine values sit in a narrow,
   query-dependent band, so a fixed floor is close to meaningless and callers
   end up returning top-k unconditionally — injecting the k least-irrelevant
   rows into every prompt. `select_hybrid` makes an empty result a real outcome.

Fusion is **rank**-based (RRF), not score-based: `bm25()` is unbounded and
negative-is-better while cosine is [-1, 1], so any linear blend of the two raw
scores is meaningless. RRF only ever compares "came 1st" to "came 1st", which
needs no per-query calibration.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# FTS5 MATCH is a query *language* — bare `"`, `*`, `:`, `-`, `^` and the bare
# keywords AND/OR/NOT/NEAR are operators, and raw user text routinely throws
# `sqlite3.OperationalError: fts5: syntax error`. So we never pass user text
# through: tokenize to word characters, quote every token (a quoted "or" is a
# literal, not the operator), and OR them together.
_FTS_TOKEN_RE = re.compile(r"[0-9A-Za-z_]+")

# Cap terms so a pasted paragraph doesn't turn into a 500-clause MATCH.
_MAX_FTS_TERMS = 24

# BM25's IDF already de-weights these to near zero; dropping them just keeps the
# candidate scan from touching most of the table. Deliberately small — an
# aggressive stoplist eats meaningful tokens ("no", "on", "can").
_STOPWORDS = frozenset("""
a an and are as at be by for from has have how i if in is it its of on or that
the their then there these they this to was what when where which who will with
you your me my do does did but not can could would should
""".split())

# Reciprocal-rank-fusion damping. 60 is the value from the original RRF paper
# and is not sensitive — it only controls how fast the vote decays with rank.
_RRF_K = 60


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r", name, raw)
        return default


def fts_match_expr(query: str) -> str | None:
    """Turn free user text into a safe FTS5 MATCH expression.

    Returns None when nothing usable survives (empty query, all stopwords,
    all single characters) — the caller then runs dense-only.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for tok in _FTS_TOKEN_RE.findall(query):
        low = tok.lower()
        if len(low) < 2 or low in _STOPWORDS or low in seen:
            continue
        seen.add(low)
        terms.append(f'"{low}"')
        if len(terms) >= _MAX_FTS_TERMS:
            break
    if not terms:
        return None
    return " OR ".join(terms)


def rrf_fuse(rankings: list[tuple[float, list[str]]], *, k: int = _RRF_K) -> dict[str, float]:
    """Weighted Reciprocal Rank Fusion.

    `rankings` is [(weight, ids_best_first), ...]. An id's score is the sum over
    the lists it appears in of `weight / (k + rank)`, rank 0-based. Ids missing
    from a list simply contribute nothing from it.
    """
    scores: dict[str, float] = {}
    for weight, ids in rankings:
        for rank, item_id in enumerate(ids):
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank + 1)
    return scores


def select_hybrid(
    *,
    dense: list[tuple[str, float]],
    sparse: list[str],
    k: int,
    min_score: float,
    rel_drop: float,
    dense_weight: float = 0.75,
    sparse_weight: float = 0.25,
    label: str = "hybrid",
) -> list[str]:
    """Fuse both arms for ordering, then cut. Returns surviving ids, best first.

    An item survives if it is strong on *either* axis:

    * **dense** — cosine at or above ``max(min_score, rel_drop * best_cosine)``.
      `min_score` is the "this is all garbage" floor; `rel_drop` is the "this is
      much worse than the best hit" rule. Both are needed: rel_drop alone keeps
      everything when the whole result set is uniformly bad, which is exactly
      the case worth rejecting.
    * **lexical** — inside the top-`k` of the BM25 ranking. FTS5 only returns
      rows that literally contain a query term, so a top lexical hit is hard
      evidence even when its cosine is mediocre. Bypassing the dense floor here
      is the entire point of adding the sparse arm — it recovers exact-token
      matches that embeddings score as unremarkable.

    ``min_score`` is model-specific and cannot be derived a priori — cosine
    distributions differ per embedding model. The kept/dropped scores are logged
    so it can be calibrated against real traffic. A cross-encoder reranker would
    replace this two-rule test with one calibrated score.
    """
    survivors: set[str] = set()

    if dense:
        best = dense[0][1]
        floor = max(min_score, rel_drop * best)
        survivors.update(item_id for item_id, score in dense if score >= floor)
    else:
        floor = None

    lexical_bypass = set(sparse[:k]) - survivors
    survivors.update(lexical_bypass)

    if not survivors:
        logger.info(
            "%s: no hit cleared the cutoff (dense=%d best=%.3f floor=%.3f sparse=%d) — returning nothing",
            label,
            len(dense),
            dense[0][1] if dense else 0.0,
            floor if floor is not None else 0.0,
            len(sparse),
        )
        return []

    fused = rrf_fuse([
        (dense_weight, [i for i, _ in dense]),
        (sparse_weight, sparse),
    ])
    ordered = sorted(survivors, key=lambda i: fused.get(i, 0.0), reverse=True)[:k]

    if logger.isEnabledFor(logging.DEBUG):
        by_id = dict(dense)
        logger.debug(
            "%s: kept %d/%d (floor=%s, lexical_bypass=%d) scores=%s",
            label,
            len(ordered),
            len(dense) + len(sparse),
            f"{floor:.3f}" if floor is not None else "n/a",
            len(lexical_bypass),
            [round(by_id.get(i, float("nan")), 3) for i in ordered],
        )
    return ordered
