"""
Timezone helpers and shared date/time utilities.

All datetimes in the database are stored as naive UTC. These helpers convert
to/from the configured local timezone (default: America/New_York), mirroring the
sibling apps (Tempus / Munus).
"""
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/New_York")


_UTC = ZoneInfo("UTC")


def utc_to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a naive UTC datetime to a naive local datetime."""
    if dt is None:
        return None
    return dt.replace(tzinfo=_UTC).astimezone(_tz()).replace(tzinfo=None)


def local_to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a naive local datetime to a naive UTC datetime (for DB queries)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=_tz()).astimezone(_UTC).replace(tzinfo=None)


def today_local() -> date:
    """Today's date in the local timezone."""
    return datetime.now(_tz()).date()


def now_utc() -> datetime:
    """Current moment as a naive UTC datetime (matches how the DB stores times)."""
    return datetime.utcnow()


def parse_iso_utc(raw: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string into a naive-UTC datetime, or None if unparseable.

    Accepts values with or without a timezone offset (and a trailing 'Z'). A tz-aware
    value is converted to UTC; a naive value is assumed to already be UTC. Used by the
    read API's `updated_since` incremental-sync filter.
    """
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_UTC).replace(tzinfo=None)
    return dt


def isoformat_utc(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a naive-UTC datetime as an ISO-8601 string with a 'Z' suffix."""
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat() + "Z"
