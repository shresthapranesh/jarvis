"""The `automations` SDK category's run reads — the agent's way to see what
its automations said. Driven against a real database, as the SDK reads it."""

from __future__ import annotations

from pathlib import Path

import pytest

from seed import insert


def _seed(database: Path) -> None:
    insert(database, "automations", id="a1", name="NVDA watch", input_type="monitor")
    insert(database, "automations", id="a2", name="Digest", input_type="prompt")
    insert(database, "automation_runs", id="r1", automation_id="a1", status="done", triggered_by="schedule",
           output="baseline: " + "x" * 300, started_at="2026-01-01 09:00:00.000000")
    insert(database, "automation_runs", id="r2", automation_id="a1", status="error", triggered_by="manual",
           error="boom", started_at="2026-01-02 09:00:00.000000")
    insert(database, "automation_runs", id="r3", automation_id="a2", status="no_change", triggered_by="schedule",
           output="NO_CHANGE", started_at="2026-01-03 09:00:00.000000")


@pytest.fixture
def sdk(work_dir: Path):
    from tools import sdk

    return sdk


def test_lists_one_automations_runs_newest_first(database, sdk):
    _seed(database)

    runs = sdk.list_automation_runs("a1")

    assert [r["id"] for r in runs] == ["r2", "r1"]
    assert runs[0]["automation"] == "NVDA watch"
    assert runs[0]["preview"] == "boom", "no output: the error previews"
    assert runs[1]["preview"].endswith("…") and len(runs[1]["preview"]) == 201
    assert "output" not in runs[1] and "error" not in runs[1], "the full text is read_automation_run's"


def test_lists_every_automation_and_limits(database, sdk):
    _seed(database)

    assert [r["id"] for r in sdk.list_automation_runs()] == ["r3", "r2", "r1"]
    assert [r["id"] for r in sdk.list_automation_runs(limit=1)] == ["r3"]


def test_unknown_automation_raises(database, sdk):
    _seed(database)

    with pytest.raises(LookupError):
        sdk.list_automation_runs("nope")


def test_reads_a_run_in_full(database, sdk):
    _seed(database)

    run = sdk.read_automation_run("r1")

    assert run["output"] == "baseline: " + "x" * 300
    assert run["error"] is None
    assert (run["automation"], run["status"], run["triggered_by"]) == ("NVDA watch", "done", "schedule")
    with pytest.raises(LookupError):
        sdk.read_automation_run("nope")
