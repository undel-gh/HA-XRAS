"""Regression tests for bounded invalid archives and publication-lag handling."""

from __future__ import annotations

from datetime import date, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xras_geomagnetic.const import (
    BASE_URL,
    CONF_CODE,
    CONF_HISTORY_MONTHS,
    DOMAIN,
)
from custom_components.xras_geomagnetic.history import (
    _HISTORY_INVALID_MAX_ATTEMPTS,
    HistoryArchiveInvalid,
    HistoryArchiveNotFound,
    HistoryFetchError,
    XrasHistoryBackfill,
    async_fetch_history_month,
)
from custom_components.xras_geomagnetic.parser import Dataset, Day


def _entry(months: int = 2) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="XRAS — RAL5",
        unique_id="RAL5",
        version=3,
        data={CONF_CODE: "RAL5"},
        options={CONF_HISTORY_MONTHS: months},
    )


def _coordinator() -> SimpleNamespace:
    return SimpleNamespace(
        code="RAL5",
        tz=timezone.utc,
        data={},
        last_update_success=True,
    )


def _dataset(month: date, value: float = 5.33) -> Dataset:
    return Dataset(
        tzone="UTC+00",
        source="noaa",
        tz=timezone.utc,
        days=[Day(date=month.replace(day=5), max_kp=value)],
    )


@pytest.mark.asyncio
async def test_fetch_distinguishes_invalid_archive_from_missing(hass, aioclient_mock):
    aioclient_mock.get(f"{BASE_URL}/kpm_RAL5_202610.json", text="not json")
    aioclient_mock.get(f"{BASE_URL}/kpm_RAL5_202610.txt", status=404)

    with pytest.raises(HistoryArchiveInvalid, match="2026-10"):
        await async_fetch_history_month(hass, "RAL5", date(2026, 10, 1))


@pytest.mark.asyncio
async def test_partial_transport_failure_is_not_classified_invalid(
    hass, aioclient_mock
):
    aioclient_mock.get(f"{BASE_URL}/kpm_RAL5_202610.json", status=503)
    aioclient_mock.get(f"{BASE_URL}/kpm_RAL5_202610.txt", text="not txt data")

    with pytest.raises(HistoryFetchError, match="2026-10") as exc:
        await async_fetch_history_month(hass, "RAL5", date(2026, 10, 1))

    assert not isinstance(exc.value, HistoryArchiveInvalid)


@pytest.mark.asyncio
async def test_invalid_archive_is_skipped_after_three_spaced_attempts(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(), _coordinator())
    current = date(2026, 11, 1)
    october = date(2026, 10, 1)
    september = date(2026, 9, 1)
    fetch = AsyncMock(
        side_effect=[
            HistoryArchiveInvalid("invalid 2026-10"),
            HistoryArchiveInvalid("invalid 2026-10"),
            HistoryArchiveInvalid("invalid 2026-10"),
            _dataset(september),
        ]
    )
    completed = AsyncMock(side_effect=[set(), set(), set(), {september}])
    store = AsyncMock(return_value=None)

    with patch.object(manager, "_async_completed_months", new=completed), patch.object(
        manager, "_async_add_and_verify", new=store
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ), patch(
        "custom_components.xras_geomagnetic.history.asyncio.sleep",
        new=AsyncMock(),
    ):
        for attempt in range(1, _HISTORY_INVALID_MAX_ATTEMPTS):
            await manager._async_backfill(current)
            assert manager.status == "retry_wait"
            assert manager.diagnostics["retry_in_seconds"] > 0
            assert manager.diagnostics["archive_invalid_attempts"] == {
                "2026-10": attempt
            }
            assert manager._completed_for_month is None
            manager._retry_after = 0.0  # Simulate cooldown expiration.

        await manager._async_backfill(current)
        assert manager.status == "partial"
        assert manager._completed_for_month == current
        assert manager.diagnostics["archive_invalid_attempts"] == {"2026-10": 3}
        assert manager.diagnostics["archive_invalid_skipped"] == ["2026-10"]
        assert manager.diagnostics["archive_imported_months"] == ["2026-09"]

        # Even a new backfill pass must not request the exhausted month.
        await manager._async_backfill(current)

    assert [call.args[2] for call in fetch.await_args_list] == [
        october,
        october,
        october,
        september,
    ]
    assert store.await_count == 1


@pytest.mark.asyncio
async def test_transport_failures_never_exhaust_invalid_budget(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(months=1), _coordinator())
    fetch = AsyncMock(side_effect=HistoryFetchError("network down"))
    current = date(2026, 11, 1)

    with patch.object(
        manager, "_async_completed_months", new=AsyncMock(return_value=set())
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ):
        for attempt in range(4):
            await manager._async_backfill(current)
            assert manager.status == "retry_wait"
            assert manager.diagnostics["retry_in_seconds"] > 0
            assert manager.diagnostics["archive_invalid_attempts"] == {}
            assert manager.diagnostics["archive_invalid_skipped"] == []
            manager._retry_after = 0.0

    assert fetch.await_count == 4


@pytest.mark.asyncio
async def test_fresh_archive_404_is_retried_without_refetching_older_months(
    hass,
) -> None:
    manager = XrasHistoryBackfill(hass, _entry(), _coordinator())
    current = date(2026, 11, 1)
    october = date(2026, 10, 1)
    september = date(2026, 9, 1)
    fetch = AsyncMock(
        side_effect=[
            HistoryArchiveNotFound("October not published yet"),
            _dataset(september),
            _dataset(october),
        ]
    )
    completed = AsyncMock(side_effect=[set(), {september}])
    store = AsyncMock(return_value=None)

    with patch.object(manager, "_async_completed_months", new=completed), patch.object(
        manager, "_async_add_and_verify", new=store
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ), patch(
        "custom_components.xras_geomagnetic.history.asyncio.sleep",
        new=AsyncMock(),
    ):
        await manager._async_backfill(current)

        assert manager.status == "retry_wait"
        assert manager.last_error == "archive 2026-10 not yet published"
        assert manager._completed_for_month is None
        assert manager.diagnostics["archive_not_found"] == []
        assert manager.diagnostics["fresh_archive_pending"] == "2026-10"
        assert manager.diagnostics["archive_imported_months"] == ["2026-09"]
        assert manager.diagnostics["retry_in_seconds"] > 0
        assert fetch.await_count == 2

        # Normal coordinator updates cannot bypass the 6-hour cooldown.
        manager._handle_update()
        assert fetch.await_count == 2

        manager._retry_after = 0.0  # Simulate cooldown expiration.
        await manager._async_backfill(current)

    assert [call.args[2] for call in fetch.await_args_list] == [
        october,
        september,
        october,
    ]
    assert store.await_count == 2
    assert manager.status == "complete"
    assert manager._completed_for_month == current
    assert manager.diagnostics["fresh_archive_pending"] is None
    assert manager.diagnostics["archive_imported_months"] == [
        "2026-09",
        "2026-10",
    ]


@pytest.mark.asyncio
async def test_pending_fresh_404_becomes_older_month_after_rollover(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(), _coordinator())
    november = date(2026, 11, 1)
    december = date(2026, 12, 1)
    october = date(2026, 10, 1)
    fetch = AsyncMock(
        side_effect=[
            HistoryArchiveNotFound("October unpublished"),
            HistoryArchiveNotFound("September old 404"),
            HistoryArchiveNotFound("November unpublished"),
            HistoryArchiveNotFound("October old 404"),
        ]
    )

    with patch.object(
        manager, "_async_completed_months", new=AsyncMock(return_value=set())
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ), patch(
        "custom_components.xras_geomagnetic.history.asyncio.sleep",
        new=AsyncMock(),
    ):
        await manager._async_backfill(november)
        assert manager.diagnostics["fresh_archive_pending"] == "2026-10"
        manager._retry_after = 0.0
        await manager._async_backfill(december)

    assert [call.args[2] for call in fetch.await_args_list] == [
        october,
        date(2026, 9, 1),
        november,
        october,
    ]
    assert manager.diagnostics["fresh_archive_pending"] == "2026-11"
    assert manager.diagnostics["archive_not_found"] == ["2026-09", "2026-10"]
    assert manager.status == "retry_wait"


@pytest.mark.asyncio
async def test_unpublished_fresh_archive_that_becomes_invalid_exhausts_budget(
    hass,
) -> None:
    """The pending 404 flag must not keep rescheduling a permanently bad month."""
    manager = XrasHistoryBackfill(hass, _entry(months=1), _coordinator())
    current = date(2026, 11, 1)
    october = date(2026, 10, 1)
    fetch = AsyncMock(
        side_effect=[
            HistoryArchiveNotFound("not yet published"),
            *[HistoryArchiveInvalid("bad October") for _ in range(3)],
        ]
    )
    with patch.object(
        manager, "_async_completed_months", new=AsyncMock(return_value=set())
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ):
        await manager._async_backfill(current)
        assert manager.diagnostics["fresh_archive_pending"] == "2026-10"
        for _ in range(3):
            manager._retry_after = 0.0
            await manager._async_backfill(current)

    assert fetch.await_count == 4
    assert all(call.args[2] == october for call in fetch.await_args_list)
    assert manager.status == "partial"
    assert manager._completed_for_month == current
    assert manager.diagnostics["fresh_archive_pending"] is None
    assert manager.diagnostics["archive_invalid_skipped"] == ["2026-10"]
