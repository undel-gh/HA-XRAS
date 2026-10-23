"""Tests for coordinator time-window semantics."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.xras_geomagnetic.coordinator as coordinator_module
from custom_components.xras_geomagnetic.const import (
    CONF_CODE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.xras_geomagnetic.coordinator import XrasCoordinator
from custom_components.xras_geomagnetic.parser import Dataset, Day

NOW = datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc)  # 13:30 UTC+03
MOSCOW_TZ = timezone(timedelta(hours=3))


class FrozenDateTime(datetime):
    """Freeze datetime.now() inside the coordinator module."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW.replace(tzinfo=None)
        return NOW.astimezone(tz)


def _dataset(*days: Day) -> Dataset:
    return Dataset(
        type="kp",
        version="1.1",
        tzone="Moscow (UTC+03)",
        source="noaa",
        tz=MOSCOW_TZ,
        days=list(days),
    )


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="XRAS — RAL5",
        unique_id="RAL5",
        version=3,
        data={CONF_CODE: "RAL5", CONF_SCAN_INTERVAL: 600},
    )


async def _update_with(monkeypatch, hass, datasets: dict[str, Dataset | Exception]):
    coordinator = XrasCoordinator(hass, _entry())
    monkeypatch.setattr(coordinator_module, "datetime", FrozenDateTime)

    async def fake_fetch(prefix: str) -> Dataset:
        result = datasets[prefix]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(coordinator, "_fetch", fake_fetch)
    return await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_current_value_must_be_inside_its_three_hour_interval(
    hass, monkeypatch
) -> None:
    fact = _dataset(
        Day(
            date=date(2026, 10, 23),
            ap=9,
            f10=106,
            max_kp=5.33,
            slots={9: 4.0, 12: 5.33},
        )
    )
    forecast = _dataset(
        Day(date=date(2026, 10, 23), slots={12: 6.0, 15: 2.0}),
        Day(date=date(2026, 10, 24), ap=12, f10=110, slots={0: 3.0}),
    )
    month = _dataset(Day(date=date(2026, 10, 4), max_kp=5.67))

    data = await _update_with(
        monkeypatch,
        hass,
        {
            "kp": fact,
            "kpf": forecast,
            "kpm": month,
            "kpfl": UpdateFailed("missing"),
        },
    )

    assert data["kp_current"] == 5.33
    assert data["kp_current_ts"].hour == 12
    assert data["kp_current_valid_until"].hour == 15
    assert data["kp_forecast_max_24h"] == 6.0
    assert data["ap_forecast"] == 12
    assert data["f10_forecast"] == 110
    assert data["dataset_available"]["month_forecast"] is False


@pytest.mark.asyncio
async def test_stale_fact_is_not_reported_as_current(hass, monkeypatch) -> None:
    fact = _dataset(
        Day(
            date=date(2026, 10, 23),
            ap=9,
            f10=106,
            max_kp=7.0,
            slots={6: 7.0},
        )
    )

    data = await _update_with(
        monkeypatch,
        hass,
        {
            "kp": fact,
            "kpf": UpdateFailed("missing"),
            "kpm": UpdateFailed("missing"),
            "kpfl": UpdateFailed("missing"),
        },
    )

    assert data["kp_current"] is None
    assert data["kp_current_ts"] is None
    assert data["level_key"] is None
    assert data["kp_max_24h"] == 7.0


@pytest.mark.asyncio
async def test_future_fact_is_never_used_as_current(hass, monkeypatch) -> None:
    fact = _dataset(
        Day(
            date=date(2026, 10, 24),
            ap=99,
            f10=999,
            max_kp=8.0,
            slots={0: 8.0},
        )
    )

    data = await _update_with(
        monkeypatch,
        hass,
        {
            "kp": fact,
            "kpf": UpdateFailed("missing"),
            "kpm": UpdateFailed("missing"),
            "kpfl": UpdateFailed("missing"),
        },
    )

    assert data["kp_current"] is None
    assert data["ap"] is None
    assert data["f10"] is None


@pytest.mark.asyncio
async def test_stale_forecast_day_does_not_become_tomorrow(hass, monkeypatch) -> None:
    fact = _dataset(
        Day(date=date(2026, 10, 23), ap=9, f10=106, max_kp=2.0, slots={12: 2.0})
    )
    stale_forecast = _dataset(
        Day(date=date(2026, 10, 22), ap=99, f10=999, slots={21: 8.0})
    )

    data = await _update_with(
        monkeypatch,
        hass,
        {
            "kp": fact,
            "kpf": stale_forecast,
            "kpm": UpdateFailed("missing"),
            "kpfl": UpdateFailed("missing"),
        },
    )

    assert data["ap_forecast"] is None
    assert data["f10_forecast"] is None
    assert data["kp_forecast_max_24h"] is None
