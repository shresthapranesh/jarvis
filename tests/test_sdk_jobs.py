"""The `jobs` SDK category — shell commands too long for a cell, run detached
with a log, found again from their files after the kernel that started them
is gone. Real processes, under a throwaway `WORK_DIR`."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def sdk(work_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from tools import sdk

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(sdk, "_conversation_id", "conv-a")
    monkeypatch.setattr(sdk, "_job_procs", {})
    return sdk


def _until(sdk, job_id: str, status: str) -> dict:
    for _ in range(100):
        state = sdk.job(job_id)
        if state["status"] == status:
            return state
        time.sleep(0.05)
    raise AssertionError(f"never {status}: {state}")


def test_a_job_runs_in_the_background_with_its_log_and_exit_code(sdk, work_dir: Path):
    job_id = sdk.start_job("echo one; echo two >&2; sleep 0.3; echo three; exit 3", name="count")
    assert sdk.job(job_id)["status"] == "running"
    done = _until(sdk, job_id, "exited")
    assert done["exit_code"] == 3 and done["name"] == "count"
    assert done["tail"].splitlines() == ["one", "two", "three"]
    assert sdk.job(job_id, lines=1)["tail"] == "three"
    assert 0.3 <= done["elapsed"] < 10
    assert Path(done["log"]).parent == work_dir / "jobs"


def test_a_job_outlives_its_kernel_and_is_found_again(sdk):
    job_id = sdk.start_job(["sh", "-c", "sleep 0.5; echo 'done here'"])
    # As after a kernel restart: only the files are left.
    sdk._job_procs.clear()
    assert sdk.job(job_id)["status"] == "running"
    done = _until(sdk, job_id, "exited")
    assert done["exit_code"] == 0 and done["tail"] == "done here"
    assert done["command"] == "sh -c 'sleep 0.5; echo '\"'\"'done here'\"'\"''"


def test_stopping_a_job_stops_what_it_started(sdk, work_dir: Path):
    pid_file = work_dir / "child.pid"
    job_id = sdk.start_job(f"sleep 60 & echo $! > {pid_file}; wait")
    state = sdk.job(job_id)
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        time.sleep(0.05)
    child = int(pid_file.read_text())
    stopped = sdk.stop_job(job_id, grace=2)
    assert stopped["status"] == "stopped" and stopped["exit_code"] is None
    assert stopped["elapsed"] < 10 and state["pid"] == stopped["pid"]
    time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    # Stopping it again is a no-op.
    assert sdk.stop_job(job_id)["status"] == "stopped"


def test_jobs_are_listed_newest_first_by_conversation(sdk, monkeypatch: pytest.MonkeyPatch):
    first = sdk.start_job("true")
    monkeypatch.setattr(sdk, "_conversation_id", "conv-b")
    second = sdk.start_job("true", name="other's")
    assert [j["id"] for j in sdk.list_jobs()] == [second]
    assert [j["id"] for j in sdk.list_jobs(all_conversations=True)] == [second, first]
    assert "tail" not in sdk.list_jobs()[0]
    with pytest.raises(ValueError, match="No job 'nope'"):
        sdk.job("nope")
    assert "jarvis.start_job(command" in sdk.help("jobs")
