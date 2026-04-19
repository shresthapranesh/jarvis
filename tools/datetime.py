"""Date and time utility tool."""

from datetime import datetime, timezone


def get_current_datetime() -> dict:
    """Return the current UTC date, time, and timezone. Use this whenever you need to know today's date or the current time."""
    now = datetime.now(timezone.utc)
    return {
        "utc": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": "UTC",
        "weekday": now.strftime("%A"),
    }
