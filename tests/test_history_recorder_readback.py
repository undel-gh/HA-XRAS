"""Recorder readback regressions for delayed statistics import visibility."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
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
    _RECORDER_VERIFY_ATTEMPTS,
    XrasHistoryBackfill,
)


def _manager(hass) -> XrasHistoryBackfill:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="XRAS — RAL5",
        unique_id="RAL5",
        version=3,
        data={CONF_CODE: "RAL5"},
        options={CONF_HISTORY_MONTHS: 1},
    )
    coordinator = SimpleNamespace(
        code="RAL5", tz=timezone.utc, data={}, last_update_success=True
    )
    return XrasHistoryBackfill(hass, entry, coordinator)


def _stat(day: int, value: float) -> dict:
    return {
        "start": datetime(2020, 1, day, 12, tzinfo=timezone.utc),
        "mean": value,
        "min": value,
        "max": value,
    }


def _result(manager: XrasHistoryBackfill, *stats: dict) -> dict:
    return {
        manager.statistic_id: [
            {"start": stat["start"].timestamp(), "max": stat["max"]}
            for stat in stats
        ]
    }


@pytest.mark.asyncio
async def test_recorder_readback_retries_in_flight_import_without_requeue(hass) -> None:
    """The first barrier may return while the importer is still in flight."""
    manager = _manager(hass)
    stat = _stat(2, 5.33)
    recorder = SimpleNamespace(
        async_block_till_done=AsyncMock(),
        async_add_executor_job=AsyncMock(
            side_effect=[_result(manager), _result(manager, stat)]
        ),
    )

    with patch(
        "custom_components.xras_geomagnetic.history.get_instance",
        return_value=recorder,
    ), patch(
        "custom_components.xras_geomagnetic.history.async_add_external_statistics"
    ) as enqueue, patch(
        "custom_components.xras_geomagnetic.history._RECORDER_VERIFY_DELAY",
        0,
    ):
        await manager._async_add_and_verify([stat])

    enqueue.assert_called_once()
    assert recorder.async_block_till_done.await_count == 2
    assert recorder.async_add_executor_job.await_count == 2


@pytest.mark.asyncio
async def test_recorder_readback_requires_every_timestamp_and_exact_value(hass) -> None:
    manager = _manager(hass)
    first, second = _stat(2, 5.33), _stat(3, 4.67)
    wrong = {**second, "max": 4.33}
    recorder = SimpleNamespace(
        async_block_till_done=AsyncMock(),
        async_add_executor_job=AsyncMock(
            side_effect=[
                _result(manager, first),
                _result(manager, first, wrong),
                _result(manager, first, second),
            ]
        ),
    )

    with patch(
        "custom_components.xras_geomagnetic.history.get_instance",
        return_value=recorder,
    ), patch(
        "custom_components.xras_geomagnetic.history.async_add_external_statistics"
    ) as enqueue, patch(
        "custom_components.xras_geomagnetic.history._RECORDER_VERIFY_DELAY",
        0,
    ):
        await manager._async_add_and_verify([first, second])

    enqueue.assert_called_once()
    assert recorder.async_block_till_done.await_count == 3
    assert recorder.async_add_executor_job.await_count == 3


@pytest.mark.asyncio
async def test_recorder_readback_exhaustion_fails_closed(hass) -> None:
    manager = _manager(hass)
    stat = _stat(2, 5.33)
    recorder = SimpleNamespace(
        async_block_till_done=AsyncMock(),
        async_add_executor_job=AsyncMock(return_value=_result(manager)),
    )

    with patch(
        "custom_components.xras_geomagnetic.history.get_instance",
        return_value=recorder,
    ), patch(
        "custom_components.xras_geomagnetic.history.async_add_external_statistics"
    ) as enqueue, patch(
        "custom_components.xras_geomagnetic.history._RECORDER_VERIFY_DELAY",
        0,
    ):
        with pytest.raises(RuntimeError, match="Recorder did not persist"):
            await manager._async_add_and_verify([stat])

    enqueue.assert_called_once()
    assert recorder.async_block_till_done.await_count == _RECORDER_VERIFY_ATTEMPTS
    assert recorder.async_add_executor_job.await_count == _RECORDER_VERIFY_ATTEMPTS


@pytest.mark.asyncio
async def test_current_month_failed_readback_enters_retry_without_signature(hass) -> None:
    manager = _manager(hass)
    rows = [{"date": date(2020, 1, 2), "kp": 5.33}]
    recorder = SimpleNamespace(
        async_block_till_done=AsyncMock(),
        async_add_executor_job=AsyncMock(return_value=_result(manager)),
    )

    with patch(
        "custom_components.xras_geomagnetic.history.get_instance",
        return_value=recorder,
    ), patch(
        "custom_components.xras_geomagnetic.history.async_add_external_statistics"
    ) as enqueue, patch(
        "custom_components.xras_geomagnetic.history._RECORDER_VERIFY_DELAY",
        0,
    ):
        manager._sync_current_month(rows)
        task = manager._current_task
        assert task is not None
        await task

    enqueue.assert_called_once()
    assert manager.status == "retry_wait"
    assert manager._pending_current_signature is None
    assert manager._last_current_signature is None
    assert manager.diagnostics["retry_in_seconds"] > 0


@pytest.mark.asyncio
async def test_readback_cancellation_during_delay_propagates(hass) -> None:
    manager = _manager(hass)
    stat = _stat(2, 5.33)
    recorder = SimpleNamespace(
        async_block_till_done=AsyncMock(),
        async_add_executor_job=AsyncMock(return_value=_result(manager)),
    )
    with patch(
        "custom_components.xras_geomagnetic.history.get_instance",
        return_value=recorder,
    ), patch(
        "custom_components.xras_geomagnetic.history.asyncio.sleep",
        new=AsyncMock(side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await manager._async_statistics_persisted([stat])

    recorder.async_add_executor_job.assert_awaited_once()
