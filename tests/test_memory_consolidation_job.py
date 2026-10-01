"""The scheduled user-memory consolidation pass.

Drives `consolidate_memory` against a real DB + store with the LLM stubbed. The
property under test is the watermark: every message reaches extraction exactly
once, in order. The job used to fetch the newest 200 messages, keep ~16KB of
them, and then mark everything up to "now" as consumed — on a busy day most
messages never reached memory and nothing said so.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from langchain_core.messages import AIMessage

from core import memory_consolidation as mc


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed(n: int, *, start_minutes_ago: float = 600.0, conv_id: str = "c1", ephemeral: bool = False):
    """One conversation of `n` ~400-char messages, one second apart."""
    from db.engine import async_session
    from db.models import Conversation, Message

    async with async_session() as s:
        s.add(Conversation(id=conv_id, title="Chat", model="stub:model", surface="web", ephemeral=ephemeral))
        await s.commit()
        base = _now() - timedelta(minutes=start_minutes_ago)
        for i in range(n):
            s.add(
                Message(
                    id=f"{conv_id}-m{i}",
                    conversation_id=conv_id,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"<<{conv_id}:{i}>> " + ("I prefer tea over coffee. " * 15),
                    created_at=base + timedelta(seconds=i),
                )
            )
        await s.commit()


def _stub_items_llm(monkeypatch, calls: list[str]):
    """Item path with the extraction call stubbed to record its transcript."""
    monkeypatch.setattr(mc, "embeddings_available", lambda: True)

    async def fake_llm_json_items(model_id, system_prompt, human_content):
        calls.append(human_content)
        return []

    monkeypatch.setattr(mc, "_llm_json_items", fake_llm_json_items)


def _seen(calls: list[str], conv_id: str = "c1") -> list[int]:
    return [int(i) for call in calls for i in re.findall(rf"<<{conv_id}:(\d+)>>", call)]


async def test_backlog_larger_than_one_batch_is_read_in_full_and_in_order(jarvis, monkeypatch):
    from core.state import get_store

    await _seed(120)
    calls: list[str] = []
    _stub_items_llm(monkeypatch, calls)

    result = await mc.consolidate_memory(get_store())

    assert len(calls) > 1, "a backlog this size needs several batches"
    assert _seen(calls) == list(range(120)), "every message exactly once, oldest first"
    assert "backlog" not in result


async def test_backlog_beyond_the_per_run_cap_carries_over(jarvis, monkeypatch):
    from core.state import get_store

    await _seed(400)
    calls: list[str] = []
    _stub_items_llm(monkeypatch, calls)

    first = await mc.consolidate_memory(get_store())
    assert len(calls) == mc._MAX_BATCHES_PER_RUN
    assert "backlog remains" in first
    after_first = _seen(calls)
    assert after_first == list(range(len(after_first)))

    while "backlog remains" in await mc.consolidate_memory(get_store()):
        pass
    assert _seen(calls) == list(range(400)), "nothing skipped, nothing read twice across runs"


async def test_watermark_stops_before_a_reply_still_being_written(jarvis, monkeypatch):
    """The assistant row is created when its run starts. Advancing past it while
    it is `running` would lose the finished reply for good."""
    from core.state import get_store
    from db.engine import async_session
    from db.models import Message

    await _seed(6)
    async with async_session() as s:
        row = await s.get(Message, "c1-m3")
        assert row is not None
        row.status, row.content = "running", ""
        await s.commit()

    calls: list[str] = []
    _stub_items_llm(monkeypatch, calls)
    await mc.consolidate_memory(get_store())
    assert _seen(calls) == [0, 1, 2]

    async with async_session() as s:
        row = await s.get(Message, "c1-m3")
        assert row is not None
        row.status, row.content = "done", "<<c1:3>> the finished reply"
        await s.commit()
    await mc.consolidate_memory(get_store())
    assert _seen(calls) == list(range(6))


async def test_legacy_last_run_at_is_honoured_as_the_watermark(jarvis, monkeypatch):
    """Installs upgrading from the old job only stored last_run_at."""
    from core.state import get_store

    await _seed(4, start_minutes_ago=120)
    await _seed(4, start_minutes_ago=10, conv_id="c2")
    await get_store().aput(
        mc._META_NS, mc._META_KEY, {"last_run_at": (_now() - timedelta(minutes=60)).isoformat()}
    )
    calls: list[str] = []
    _stub_items_llm(monkeypatch, calls)

    await mc.consolidate_memory(get_store())

    assert _seen(calls, "c1") == []
    assert _seen(calls, "c2") == [0, 1, 2, 3]


async def test_incognito_conversations_never_reach_memory(jarvis, monkeypatch):
    from core.state import get_store

    await _seed(3, conv_id="c1")
    await _seed(3, conv_id="secret", ephemeral=True)
    calls: list[str] = []
    _stub_items_llm(monkeypatch, calls)

    await mc.consolidate_memory(get_store())

    assert _seen(calls, "c1") == [0, 1, 2]
    assert _seen(calls, "secret") == []


async def test_nothing_new_is_a_skip_without_an_llm_call(jarvis, monkeypatch):
    from core.state import get_store

    calls: list[str] = []
    _stub_items_llm(monkeypatch, calls)
    assert (await mc.consolidate_memory(get_store())).startswith("skipped")
    assert calls == []


async def test_overlapping_pass_is_skipped(jarvis, monkeypatch):
    from core.state import get_store

    await _seed(2)
    calls: list[str] = []
    _stub_items_llm(monkeypatch, calls)
    async with mc._run_lock:
        result = await mc.consolidate_memory(get_store())
    assert "already running" in result
    assert calls == []


async def test_delete_budget_is_shared_across_batches(monkeypatch):
    import core.memory_store as ms

    deleted: list[str] = []

    async def fake_delete(mid):
        deleted.append(mid)
        return True

    monkeypatch.setattr(ms, "delete_memory_by_id", fake_delete)
    ops = [{"op": "delete", "id": f"m{i}", "reason": "outdated"} for i in range(4)]

    assert await mc._apply_ops(ops, {f"m{i}" for i in range(4)}, max_delete=1) == (0, 0, 1)
    assert await mc._apply_ops(ops, {f"m{i}" for i in range(4)}, max_delete=0) == (0, 0, 0)
    assert deleted == ["m0"]


async def test_blob_path_batches_and_folds_each_batch_into_the_document(jarvis, monkeypatch):
    """Keyless installs: same watermark, each batch sees the previous one's output."""
    from core.state import get_store

    await _seed(120)
    monkeypatch.setattr(mc, "embeddings_available", lambda: False)
    prompts: list[str] = []

    class _LLM:
        async def ainvoke(self, messages):
            prompts.append(messages[-1].content)
            return AIMessage(content=f"## Key Facts\n- version {len(prompts)}")

    class _Spec:
        def build_llm(self):
            return _LLM()

    monkeypatch.setattr(mc, "resolve_model_spec", lambda model_id: _Spec())

    await mc.consolidate_memory(get_store())

    assert len(prompts) > 1
    assert _seen(prompts) == list(range(120))
    assert "version 1" in prompts[1], "batch 2 must build on batch 1's document"
    doc = await get_store().aget(mc._MEMORY_NS, mc._MEMORY_KEY)
    assert doc is not None and f"version {len(prompts)}" in doc.value["content"]
