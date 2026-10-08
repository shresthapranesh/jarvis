"""Cron schedules are local time, and the displayed next-run agrees with it.

A cron expression is written by a human: "0 9 * * 1" means Monday 9am to the
person who typed it. The scheduler was declared `timezone="UTC"` while every
trigger was built with a bare `CronTrigger.from_crontab(expr)` — which ignores
the scheduler and falls back to the process's local zone. So jobs fired at local
9am while `_compute_next_run_at` (explicitly UTC) told the user a different
time. These tests pin both halves to one resolved timezone.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def scheduler(monkeypatch):
    """A freshly imported core.scheduler with no timezone resolved yet."""
    import core.scheduler as mod

    monkeypatch.delenv("JARVIS_TIMEZONE", raising=False)
    monkeypatch.setattr(mod, "_timezone", None)
    yield mod
    mod._timezone = None


def test_defaults_to_machine_local_timezone(scheduler, monkeypatch):
    """No override → whatever the machine says local is, never a hardcoded UTC."""
    monkeypatch.setattr("tzlocal.get_localzone", lambda: ZoneInfo("America/Chicago"))
    assert str(scheduler.get_scheduler_timezone()) == "America/Chicago"


def test_env_var_overrides_local(scheduler, monkeypatch):
    """JARVIS_TIMEZONE wins — the escape hatch for a UTC container."""
    monkeypatch.setattr("tzlocal.get_localzone", lambda: ZoneInfo("America/Chicago"))
    monkeypatch.setenv("JARVIS_TIMEZONE", "Asia/Kathmandu")
    assert str(scheduler.get_scheduler_timezone()) == "Asia/Kathmandu"


def test_setting_overrides_env(scheduler, monkeypatch):
    """`scheduler.timezone` is the most specific source, so it wins over env."""
    monkeypatch.setenv("JARVIS_TIMEZONE", "Asia/Kathmandu")
    scheduler.set_scheduler_timezone("Europe/Berlin")
    assert str(scheduler.get_scheduler_timezone()) == "Europe/Berlin"


def test_invalid_setting_falls_back_instead_of_crashing(scheduler, monkeypatch):
    """A typo'd zone must not take the lifespan down mid-startup."""
    monkeypatch.setattr("tzlocal.get_localzone", lambda: ZoneInfo("America/Chicago"))
    scheduler.set_scheduler_timezone("Not/AZone")
    assert str(scheduler.get_scheduler_timezone()) == "America/Chicago"


def test_triggers_are_bound_to_the_resolved_timezone(scheduler, monkeypatch):
    """The trigger must carry the resolved zone, not APScheduler's own default."""
    monkeypatch.setenv("JARVIS_TIMEZONE", "Asia/Kathmandu")
    trigger = scheduler._cron("0 9 * * 1")
    assert str(trigger.timezone) == "Asia/Kathmandu"
