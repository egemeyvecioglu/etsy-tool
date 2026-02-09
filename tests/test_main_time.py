"""Tests for sync timestamp formatting helpers in main module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.main import _age_label_for_sync, _format_datetime_for_ui, _format_sync_timestamp


def _fake_t(key: str, **kwargs) -> str:
    """Simple translation stub used by helper tests."""

    if kwargs:
        return f"{key}:{kwargs}"
    return key


def test_age_label_accepts_naive_datetime() -> None:
    """Naive datetime values should be treated as UTC without errors."""

    naive_time = datetime.utcnow() - timedelta(minutes=5)
    label = _age_label_for_sync(naive_time, _fake_t)
    assert "pages.create_job.sync.minutes_ago" in label


def test_format_sync_timestamp_normalizes_to_utc() -> None:
    """Formatter should always produce UTC display/ISO strings."""

    aware_time = datetime.now(tz=timezone.utc)
    iso_value, display_value = _format_sync_timestamp(aware_time)
    assert "+00:00" in iso_value
    assert display_value.endswith("UTC")


def test_format_datetime_for_ui_readable_english() -> None:
    """Dashboard datetime formatter should produce human-friendly English values."""

    formatted = _format_datetime_for_ui("2026-02-08T23:50:04.606240", "en")
    assert formatted == "Feb 08, 2026 23:50 UTC"


def test_format_datetime_for_ui_readable_turkish() -> None:
    """Dashboard datetime formatter should produce human-friendly Turkish values."""

    formatted = _format_datetime_for_ui("2026-02-08T23:50:04.606240", "tr")
    assert formatted == "08.02.2026 23:50 UTC"
