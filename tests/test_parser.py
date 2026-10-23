"""Tests for XRAS JSON/TXT parsing."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from custom_components.xras_geomagnetic.parser import (
    XrasParseError,
    parse_json,
    parse_txt,
    tz_from_string,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _json_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_json_sorts_descending_days_and_parses_named_timezone() -> None:
    dataset = parse_json(_json_fixture("kp_moscow.json"), "RAL5")

    assert [day.date.isoformat() for day in dataset.days] == [
        "2026-10-22",
        "2026-10-23",
    ]
    assert dataset.tz.utcoffset(None) == timedelta(hours=3)
    assert len(dataset.series) == 16
    assert dataset.series[0]["ts"].hour == 0


def test_month_null_days_are_not_exposed_as_daily_values() -> None:
    dataset = parse_json(_json_fixture("kp_month.json"), "RAL5")

    assert [row["date"].isoformat() for row in dataset.daily] == [
        "2026-10-04",
        "2026-10-05",
    ]
    assert [row["kp"] for row in dataset.daily] == [5.67, 5.33]


def test_norilsk_h00_is_continuity_point_not_three_hour_slot() -> None:
    dataset = parse_json(_json_fixture("kp_norilsk.json"), "NJ7D")
    day = dataset.day_for(dataset.days[0].date)

    assert day is not None
    assert 0 in day.slots  # Raw source value is retained.
    assert {point["ts"].hour for point in dataset.series} <= {
        1,
        4,
        7,
        10,
        13,
        16,
        19,
        22,
    }
    assert all(point["ts"].hour != 0 for point in dataset.series)


def test_txt_header_handles_norilsk_layout() -> None:
    dataset = parse_txt(
        (FIXTURES / "kp_norilsk.txt").read_text(encoding="utf-8"),
        "NJ7D",
    )

    assert dataset.days[0].slots[0] == 3.67
    assert [point["ts"].hour for point in dataset.series] == [
        1,
        4,
        7,
        10,
        13,
        16,
        19,
        22,
    ]


def test_txt_15_column_fallback_uses_timezone_phase() -> None:
    text = """\
# TZONE: Norilsk (UTC+07)
2026-10-23|106|49|9|2.67|3.67|3.67|3.67|3.33|1.67|1.67|1.67|2.33|2.67|2.67
"""
    dataset = parse_txt(text, "NJ7D")

    assert dataset.days[0].slots[0] == 3.67
    assert dataset.days[0].slots[1] == 3.67
    assert dataset.days[0].slots[22] == 2.67


def test_unknown_txt_width_without_header_is_rejected() -> None:
    with pytest.raises(XrasParseError, match="неизвестное число колонок"):
        parse_txt("# TZONE: UTC+03\n2026-10-23|1|2|3|4|5|6", "UP03")


def test_unknown_timezone_is_rejected_instead_of_falling_back_to_utc() -> None:
    payload = {
        "version": "1.1",
        "type": "kp",
        "tzone": "Moscow (MSK)",
        "data": [{"time": "2026-10-23", "h00": "2.0"}],
    }

    with pytest.raises(XrasParseError, match="часовую зону"):
        parse_json(payload, "RAL5")


def test_invalid_timezone_offset_returns_none() -> None:
    assert tz_from_string("UTC+99") is None


def test_derived_daily_max_ignores_h00_continuity_value() -> None:
    payload = {
        "version": "1.1",
        "type": "kp",
        "tzone": "Norilsk (UTC+07)",
        "data": [
            {
                "time": "2026-10-23",
                "max_kp": "null",
                "h00": "8.0",
                "h01": "2.0",
                "h04": "3.0",
            }
        ],
    }

    dataset = parse_json(payload, "NJ7D")
    assert dataset.days[0].max_kp == 3.0
