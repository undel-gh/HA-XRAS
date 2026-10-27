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
    MIN_SCAN_INTERVAL,
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


@pytest.mark.asyncio
async def test_current_value_expires_and_requests_immediate_refresh(
    hass, monkeypatch
) -> None:
    """Expiry must invalidate current Kp and immediately request a fresh slot."""
    coordinator = XrasCoordinator(hass, _entry())
    measured_at = NOW - timedelta(hours=2, minutes=30)
    valid_until = measured_at + timedelta(hours=3)
    coordinator.data = {
        "kp_current": 5.33,
        "kp_current_ts": measured_at,
        "kp_current_valid_until": valid_until,
        "level_key": "g1",
        "updated_at": NOW,
    }

    scheduled: dict = {}

    def fake_track_point(hass_arg, action, point):
        assert hass_arg is hass
        scheduled["action"] = action
        scheduled["point"] = point
        return lambda: scheduled.setdefault("cancelled", True)

    def fail_if_polling_is_reset(*_args, **_kwargs):
        raise AssertionError("expiry state change must not use async_set_updated_data")

    notifications: list[bool] = []
    refreshes: list[bool] = []

    async def fake_refresh() -> None:
        refreshes.append(True)

    monkeypatch.setattr(
        coordinator_module, "async_track_point_in_utc_time", fake_track_point
    )
    monkeypatch.setattr(
        coordinator, "async_set_updated_data", fail_if_polling_is_reset
    )
    monkeypatch.setattr(
        coordinator, "async_update_listeners", lambda: notifications.append(True)
    )
    monkeypatch.setattr(coordinator, "async_refresh", fake_refresh)

    coordinator._schedule_current_expiry()

    assert scheduled["point"] == valid_until.astimezone(timezone.utc)
    await scheduled["action"](valid_until.astimezone(timezone.utc))

    assert coordinator.data["kp_current"] is None
    assert coordinator.data["kp_current_ts"] == measured_at
    assert coordinator.data["kp_current_valid_until"] == valid_until
    assert coordinator.data["level_key"] is None
    assert coordinator.data["updated_at"] == NOW
    assert notifications == [True]
    assert refreshes == [True]
    assert coordinator._current_refresh_retries_remaining == 3


@pytest.mark.asyncio
async def test_expiry_refresh_retries_are_bounded(hass, monkeypatch) -> None:
    """A delayed XRAS slot gets at most three 5-minute recovery retries."""
    coordinator = XrasCoordinator(hass, _entry())
    coordinator.data = {
        "kp_current": None,
        "kp_current_ts": None,
        "kp_current_valid_until": None,
        "level_key": None,
    }
    coordinator._current_refresh_retries_remaining = 3

    scheduled: list[dict] = []

    def fake_call_later(hass_arg, delay, action):
        assert hass_arg is hass
        item = {"delay": delay, "action": action, "cancelled": False}
        scheduled.append(item)

        def cancel() -> None:
            item["cancelled"] = True

        return cancel

    refreshes: list[bool] = []

    async def fake_refresh() -> None:
        refreshes.append(True)
        coordinator._async_refresh_finished()

    monkeypatch.setattr(coordinator_module, "async_call_later", fake_call_later)
    monkeypatch.setattr(coordinator, "async_refresh", fake_refresh)

    coordinator._async_refresh_finished()
    assert scheduled[0]["delay"] == timedelta(seconds=MIN_SCAN_INTERVAL)

    await scheduled[0]["action"](NOW)
    assert coordinator._current_refresh_retries_remaining == 2
    assert len(scheduled) == 2

    await scheduled[1]["action"](NOW + timedelta(minutes=5))
    assert coordinator._current_refresh_retries_remaining == 1
    assert len(scheduled) == 3

    await scheduled[2]["action"](NOW + timedelta(minutes=10))
    assert coordinator._current_refresh_retries_remaining == 0
    assert len(scheduled) == 3
    assert refreshes == [True, True, True]


def test_fresh_slot_stops_expiry_retries(hass, monkeypatch) -> None:
    """A successful fresh slot cancels any pending expiry-recovery retry."""
    coordinator = XrasCoordinator(hass, _entry())
    measured_at = NOW
    valid_until = NOW + timedelta(hours=3)
    coordinator.data = {
        "kp_current": 1.33,
        "kp_current_ts": measured_at,
        "kp_current_valid_until": valid_until,
        "level_key": "quiet",
    }
    coordinator._current_refresh_retries_remaining = 2

    cancelled: list[bool] = []
    coordinator._unsub_current_refresh_retry = lambda: cancelled.append(True)
    monkeypatch.setattr(coordinator, "_schedule_current_expiry", lambda: None)

    coordinator._async_refresh_finished()

    assert cancelled == [True]
    assert coordinator._unsub_current_refresh_retry is None
    assert coordinator._current_refresh_retries_remaining == 0


@pytest.mark.asyncio
async def test_stale_expiry_callback_does_not_clear_newer_slot(hass) -> None:
    """An already queued callback must not wipe data from a newer refresh."""
    coordinator = XrasCoordinator(hass, _entry())
    old_measured_at = NOW - timedelta(hours=3)
    old_valid_until = NOW
    new_measured_at = NOW
    new_valid_until = NOW + timedelta(hours=3)
    coordinator.data = {
        "kp_current": 1.33,
        "kp_current_ts": new_measured_at,
        "kp_current_valid_until": new_valid_until,
        "level_key": "quiet",
    }

    await coordinator._expire_current(old_measured_at, old_valid_until, NOW)

    assert coordinator.data["kp_current"] == 1.33
    assert coordinator.data["kp_current_ts"] == new_measured_at
    assert coordinator.data["kp_current_valid_until"] == new_valid_until
    assert coordinator.data["level_key"] == "quiet"
