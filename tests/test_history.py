"""Tests for optional XRAS historical daily-Kp backfill."""

from __future__ import annotations

from datetime import date, datetime, timezone
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
    _HISTORY_REQUEST_DELAY,
    HistoryArchiveNotFound,
    HistoryFetchError,
    StatisticMeanType,
    XrasHistoryBackfill,
    _previous_months,
    _statistics_metadata,
    async_fetch_history_month,
    daily_statistics,
    history_statistic_id,
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


def _json_month(day: str = "2026-10-05", value: str = "5.33") -> str:
    return (
        '{"version":"1.1","type":"kp","error":"",'
        '"tzone":"Moscow (UTC+03)","source":"noaa","data":['
        f'{{"time":"{day}","f10":"100","sn":"58","ap":"24",'
        f'"max_kp":"{value}"}}]}}'
    )


def _txt_month(day: str = "2026-10-05", value: str = "5.33") -> str:
    return (
        "# VERSION: 1.1\n"
        "# TYPE: kp\n"
        "# ERROR:\n"
        "# TZONE: Moscow (UTC+03)\n"
        "# SOURCE: noaa\n"
        "# [0] time, [1] f10, [2] sn, [3] ap, [4] max_kp\n"
        f"{day}| 100| 58| 24| {value}\n"
    )


def test_history_statistic_id_is_external_and_region_specific() -> None:
    assert history_statistic_id("RAL5") == "xras_geomagnetic:kp_daily_max_ral5"


def test_daily_statistics_are_sorted_and_use_neutral_noon_marker() -> None:
    stats = daily_statistics(
        [
            {"date": date(2026, 10, 5), "kp": 5.33},
            {"date": date(2026, 10, 4), "kp": 3.67},
            {"date": date(2026, 10, 6), "kp": None},
        ]
    )

    assert stats == [
        {
            "start": datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            "mean": 3.67,
            "min": 3.67,
            "max": 3.67,
        },
        {
            "start": datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
            "mean": 5.33,
            "min": 5.33,
            "max": 5.33,
        },
    ]


def test_previous_months_are_completed_months_newest_first() -> None:
    assert _previous_months(date(2026, 11, 1), 3) == [
        date(2026, 10, 1),
        date(2026, 9, 1),
        date(2026, 8, 1),
    ]


def test_statistics_metadata_matches_home_assistant_generation() -> None:
    metadata = _statistics_metadata("RAL5")

    assert metadata["source"] == DOMAIN
    assert metadata["statistic_id"] == history_statistic_id("RAL5")
    assert metadata["has_sum"] is False
    assert metadata["unit_of_measurement"] is None
    if StatisticMeanType is None:
        assert metadata["has_mean"] is True
        assert "mean_type" not in metadata
    else:
        assert metadata["mean_type"] == StatisticMeanType.ARITHMETIC
        assert metadata["unit_class"] is None


@pytest.mark.asyncio
async def test_fetch_history_month_json(hass, aioclient_mock) -> None:
    url = f"{BASE_URL}/kpm_RAL5_202610.json"
    aioclient_mock.get(url, text=_json_month())

    dataset = await async_fetch_history_month(hass, "RAL5", date(2026, 10, 1))

    assert dataset.daily == [{"date": date(2026, 10, 5), "kp": 5.33}]


@pytest.mark.asyncio
async def test_fetch_history_month_falls_back_to_txt(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/kpm_RAL5_202610.json", text="not json")
    aioclient_mock.get(f"{BASE_URL}/kpm_RAL5_202610.txt", text=_txt_month())

    dataset = await async_fetch_history_month(hass, "RAL5", date(2026, 10, 1))

    assert dataset.daily == [{"date": date(2026, 10, 5), "kp": 5.33}]


@pytest.mark.asyncio
async def test_fetch_history_month_reports_absent_archive(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/kpm_RAL5_202610.json", status=404)
    aioclient_mock.get(f"{BASE_URL}/kpm_RAL5_202610.txt", status=404)

    with pytest.raises(HistoryArchiveNotFound, match="2026-10"):
        await async_fetch_history_month(hass, "RAL5", date(2026, 10, 1))


@pytest.mark.asyncio
async def test_fetch_history_month_rejects_wrong_month(hass, aioclient_mock) -> None:
    aioclient_mock.get(
        f"{BASE_URL}/kpm_RAL5_202610.json",
        text=_json_month(day="2026-09-30"),
    )
    aioclient_mock.get(f"{BASE_URL}/kpm_RAL5_202610.txt", status=404)

    with pytest.raises(HistoryFetchError, match="does not match 2026-10"):
        await async_fetch_history_month(hass, "RAL5", date(2026, 10, 1))


@pytest.mark.asyncio
async def test_current_month_statistics_are_not_requeued_when_unchanged(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(), _coordinator())
    rows = [{"date": date(2026, 11, 1), "kp": 4.33}]

    with patch(
        "custom_components.xras_geomagnetic.history.async_add_external_statistics"
    ) as add_stats:
        manager._sync_current_month(rows)
        manager._sync_current_month(rows)
        manager._sync_current_month(
            [{"date": date(2026, 11, 1), "kp": 5.0}]
        )

    assert add_stats.call_count == 2


@pytest.mark.asyncio
async def test_completed_month_requires_final_calendar_day(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(), _coordinator())
    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(
            return_value={
                manager.statistic_id: [
                    {
                        "start": datetime(
                            2026, 9, 15, 12, tzinfo=timezone.utc
                        ).timestamp(),
                        "max": 4.33,
                    },
                    {
                        "start": datetime(
                            2026, 10, 31, 12, tzinfo=timezone.utc
                        ).timestamp(),
                        "max": 5.33,
                    },
                ]
            }
        )
    )

    with patch(
        "custom_components.xras_geomagnetic.history.get_instance",
        return_value=recorder,
    ):
        completed = await manager._async_completed_months(
            [date(2026, 10, 1), date(2026, 9, 1)],
            date(2026, 11, 1),
        )

    assert completed == {date(2026, 10, 1)}


@pytest.mark.asyncio
async def test_backfill_is_sequential_and_completes(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(months=2), _coordinator())
    fetch = AsyncMock(
        side_effect=[
            _dataset(date(2026, 10, 1), 5.33),
            _dataset(date(2026, 9, 1), 4.67),
        ]
    )

    with patch.object(
        manager, "_async_completed_months", new=AsyncMock(return_value=set())
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ), patch(
        "custom_components.xras_geomagnetic.history.async_add_external_statistics"
    ) as add_stats, patch(
        "custom_components.xras_geomagnetic.history.asyncio.sleep",
        new=AsyncMock(),
    ) as sleep:
        await manager._async_backfill(date(2026, 11, 1))

    assert [call.args[2] for call in fetch.await_args_list] == [
        date(2026, 10, 1),
        date(2026, 9, 1),
    ]
    assert add_stats.call_count == 2
    sleep.assert_awaited_once_with(_HISTORY_REQUEST_DELAY)
    assert manager.imported_archive_months == 2
    assert manager.status == "complete"


@pytest.mark.asyncio
async def test_completed_archive_month_is_not_downloaded_again(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(months=2), _coordinator())
    fetch = AsyncMock(return_value=_dataset(date(2026, 9, 1)))

    with patch.object(
        manager,
        "_async_completed_months",
        new=AsyncMock(return_value={date(2026, 10, 1)}),
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ), patch(
        "custom_components.xras_geomagnetic.history.async_add_external_statistics"
    ), patch(
        "custom_components.xras_geomagnetic.history.asyncio.sleep",
        new=AsyncMock(),
    ):
        await manager._async_backfill(date(2026, 11, 1))

    fetch.assert_awaited_once_with(hass, "RAL5", date(2026, 9, 1))


@pytest.mark.asyncio
async def test_source_outage_pauses_backfill_without_retry_burst(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(months=12), _coordinator())
    fetch = AsyncMock(side_effect=HistoryFetchError("source unavailable"))

    with patch.object(
        manager, "_async_completed_months", new=AsyncMock(return_value=set())
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ):
        await manager._async_backfill(date(2026, 11, 1))

    assert fetch.await_count == 1
    assert manager.status == "retry_wait"
    assert manager.last_error == "source unavailable"
    assert manager.diagnostics["retry_in_seconds"] > 0


@pytest.mark.asyncio
async def test_absent_archive_is_skipped_and_next_month_continues(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(months=2), _coordinator())
    fetch = AsyncMock(
        side_effect=[
            HistoryArchiveNotFound("not published"),
            _dataset(date(2026, 9, 1), 4.33),
        ]
    )

    with patch.object(
        manager, "_async_completed_months", new=AsyncMock(return_value=set())
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ), patch(
        "custom_components.xras_geomagnetic.history.async_add_external_statistics"
    ) as add_stats, patch(
        "custom_components.xras_geomagnetic.history.asyncio.sleep",
        new=AsyncMock(),
    ):
        await manager._async_backfill(date(2026, 11, 1))

    assert fetch.await_count == 2
    assert add_stats.call_count == 1
    assert manager.status == "partial"
    assert manager.diagnostics["archive_not_found"] == ["2026-10"]
