"""Timezone + ISO datetime helpers."""
from datetime import datetime

from app.utils import (
    isoformat_utc, local_to_utc, now_utc, parse_iso_utc, utc_to_local,
)


def test_utc_local_roundtrip():
    dt = datetime(2026, 7, 6, 15, 30, 0)
    assert local_to_utc(utc_to_local(dt)) == dt


def test_none_passthrough():
    assert utc_to_local(None) is None
    assert local_to_utc(None) is None
    assert isoformat_utc(None) is None
    assert parse_iso_utc(None) is None
    assert parse_iso_utc("") is None


def test_isoformat_has_z_suffix():
    s = isoformat_utc(datetime(2026, 7, 6, 15, 30, 45))
    assert s == "2026-07-06T15:30:45Z"


def test_parse_iso_accepts_z_and_offset_and_date():
    assert parse_iso_utc("2026-07-06T15:30:45Z") == datetime(2026, 7, 6, 15, 30, 45)
    # +02:00 offset normalizes back to UTC.
    assert parse_iso_utc("2026-07-06T17:30:45+02:00") == datetime(2026, 7, 6, 15, 30, 45)
    # A bare date parses at midnight.
    assert parse_iso_utc("2026-07-06") == datetime(2026, 7, 6, 0, 0, 0)


def test_parse_iso_invalid_returns_none():
    assert parse_iso_utc("not-a-date") is None


def test_now_utc_naive():
    assert now_utc().tzinfo is None
