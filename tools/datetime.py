"""Date and time utility tool."""

from datetime import datetime, timezone


async def get_current_datetime() -> dict:
    """Return the current local date and time (plus the UTC equivalent). Use this whenever you need to know today's date or the current time."""
    now = datetime.now().astimezone()
    return {
        "local": now.isoformat(),
        "utc": now.astimezone(timezone.utc).isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": now.tzname() or str(now.tzinfo),
        "utc_offset": now.strftime("%z"),
        "weekday": now.strftime("%A"),
    }
