"""Concurrency regressions for XRAS historical backfill."""

from __future__ import annotations

import asyncio
from datetime import date, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xras_geomagnetic.const import (
    CONF_CODE,
    CONF_HISTORY_MONTHS,
    DOMAIN,
)
from custom_components.xras_geomagnetic.history import (
    HistoryFetchError,
    XrasHistoryBackfill,
)
from custom_components.xras_geomagnetic.parser import Dataset, Day


def _entry(months: int = 3) -> MockConfigEntry:
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
        data={"daily": [{"date": date(2020, 1, 1), "kp": 4.33}]},
        last_update_success=True,
    )


def _dataset(month: date, value: float) -> Dataset:
    return Dataset(
        tzone="UTC+00",
        source="noaa",
        tz=timezone.utc,
        days=[Day(date=month.replace(day=5), max_kp=value)],
    )


@pytest.mark.asyncio
async def test_current_success_preserves_newer_archive_retry(hass) -> None:
    """A current write must not clear a retry registered by archive work."""
    manager = XrasHistoryBackfill(hass, _entry(months=1), _coordinator())
    current_month = date(2026, 11, 1)
    fetch_started = asyncio.Event()
    release_fetch_failure = asyncio.Event()
    current_store_started = asyncio.Event()
    release_current_success = asyncio.Event()

    async def failing_fetch(*_args) -> Dataset:
        fetch_started.set()
        await release_fetch_failure.wait()
        raise HistoryFetchError("source unavailable")

    async def delayed_current_store(_stats) -> None:
        current_store_started.set()
        await release_current_success.wait()

    fetch = AsyncMock(side_effect=failing_fetch)
    with patch.object(
        manager, "_async_completed_months", new=AsyncMock(return_value=set())
    ), patch.object(
        manager, "_async_add_and_verify", new=delayed_current_store
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ):
        archive_task = hass.async_create_task(manager._async_backfill(current_month))
        await fetch_started.wait()

        manager._sync_current_month(manager.coordinator.data["daily"])
        current_task = manager._current_task
        assert current_task is not None
        await current_store_started.wait()

        release_fetch_failure.set()
        await archive_task

        assert manager.status == "retry_wait"
        assert manager.last_error == "source unavailable"
        retry_after = manager._retry_after
        retry_generation = manager._retry_generation
        assert manager.diagnostics["retry_in_seconds"] > 0

        release_current_success.set()
        await current_task
        await asyncio.sleep(0)

        assert manager._last_current_signature is not None
        assert manager.status == "retry_wait"
        assert manager.last_error == "source unavailable"
        assert manager._retry_after == retry_after
        assert manager._retry_generation == retry_generation
        assert manager.diagnostics["retry_in_seconds"] > 0

        manager._handle_update()
        assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_retry_skips_already_revalidated_freshest_month(hass) -> None:
    """A later-month failure must not refetch an already verified fresh archive."""
    manager = XrasHistoryBackfill(hass, _entry(months=3), _coordinator())
    current_month = date(2026, 11, 1)
    october = date(2026, 10, 1)
    september = date(2026, 9, 1)
    august = date(2026, 8, 1)
    completed = AsyncMock(side_effect=[set(), {october, september}])
    fetch = AsyncMock(
        side_effect=[
            _dataset(october, 5.33),
            _dataset(september, 4.67),
            HistoryFetchError("source unavailable"),
            _dataset(august, 3.67),
        ]
    )
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
        await manager._async_backfill(current_month)
        assert manager.status == "retry_wait"
        assert manager._archive_imported_months == {october, september}

        manager._retry_after = 0.0
        await manager._async_backfill(current_month)

    assert [call.args[2] for call in fetch.await_args_list] == [
        october,
        september,
        august,
        august,
    ]
    assert manager._archive_imported_months == {october, september, august}
    assert manager.status == "complete"
