"""The `jarvis` SDK's calls that go through the server.

`search_memory` embeds its query in the server (`searchMemory`): the kernel
has no embedding client of its own. Its answer is the one the SDK computed
itself before — numpy's cosine over the stored facts, best first, to four
places.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np

from edge_support import _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from seed import insert
from test_edge_loop import FakeOllama, fake, fake_blob, fake_vector  # noqa: F401 — fake is a fixture

FACTS = ["The user's favourite colour is green", "The user deploys the rust edge", "Lunch is at noon",
         "Green tea at noon"]


def _numpy_ranking(query: str, k: int) -> list[dict]:
    """What `search_memory` returned when the SDK embedded the query itself."""
    q = np.asarray(fake_vector(query), dtype=np.float32)
    scored = []
    for i, text in enumerate(FACTS):
        v = np.frombuffer(fake_blob(text), dtype=np.float32)
        score = float(np.dot(v, q) / ((float(np.linalg.norm(v)) or 1.0) * (float(np.linalg.norm(q)) or 1.0)))
        scored.append((score, {"id": f"f{i}", "text": text}))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [{**hit, "score": round(score, 4)} for score, hit in scored[:k]]


async def test_search_memory_ranks_facts_through_the_server(database: Path, work_dir: Path, edge_binary: Path,
                                                            fake: FakeOllama, monkeypatch):
    from tools import sdk

    for i, text in enumerate(FACTS):
        insert(database, "memories", id=f"f{i}", kind="fact", text=text, embedding=fake_blob(text))
    # Not facts, not embedded, another model's size: never a hit.
    insert(database, "memories", id="core", kind="core", text="green green green", embedding=fake_blob("green green"))
    insert(database, "memories", id="bare", kind="fact", text="green, unembedded")
    insert(database, "memories", id="odd", kind="fact", text="green", embedding=np.ones(3, np.float32).tobytes())

    async with _run_edge(edge_binary, work_dir, database, {"OLLAMA_HOST": fake.url}) as client:
        monkeypatch.setenv("JARVIS_API_URL", f"{client.base_url}/graphql")
        hits = await asyncio.to_thread(sdk.search_memory, "green tea at noon", 3)
    assert hits == _numpy_ranking("green tea at noon", 3)
    assert [h["id"] for h in hits] == ["f3", "f2", "f0"]
    assert fake.embeds == [["green tea at noon"]]
