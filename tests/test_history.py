"""Tests for optional XRAS historical daily-Kp backfill."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xras_geomagnetic.const import (
    BASE_URL,
    CONF_CODE,
    CONF_HISTORY_MONTHS,
    DOMAIN,
    MIN_SCAN_INTERVAL,
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


def _coordinator(source_tz: timezone = timezone.utc) -> SimpleNamespace:
    return SimpleNamespace(
        code="RAL5",
        tz=source_tz,
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


def test_history_archive_requests_respect_minimum_source_cadence() -> None:
    assert _HISTORY_REQUEST_DELAY >= MIN_SCAN_INTERVAL


def test_daily_statistics_are_sorted_and_use_source_local_noon() -> None:
    stats = daily_statistics(
        [
            {"date": date(2026, 10, 5), "kp": 5.33},
            {"date": date(2026, 10, 4), "kp": 3.67},
            {"date": date(2026, 10, 6), "kp": None},
        ],
        timezone.utc,
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


def test_source_local_noon_preserves_utc_plus_14_calendar_day() -> None:
    source_tz = timezone(timedelta(hours=14))
    stats = daily_statistics(
        [{"date": date(2026, 11, 3), "kp": 4.33}],
        source_tz,
    )

    marker = stats[0]["start"]
    assert marker == datetime(2026, 11, 2, 22, tzinfo=timezone.utc)
    assert marker.astimezone(source_tz) == datetime(
        2026, 11, 3, 12, tzinfo=source_tz
    )
    assert marker.astimezone(source_tz).date() == date(2026, 11, 3)


def test_subhour_offset_marker_is_hour_aligned_and_keeps_source_day() -> None:
    source_tz = timezone(timedelta(hours=5, minutes=30))
    stats = daily_statistics(
        [{"date": date(2026, 11, 3), "kp": 4.33}],
        source_tz,
    )

    marker = stats[0]["start"]
    assert marker == datetime(2026, 11, 3, 6, tzinfo=timezone.utc)
    assert marker.minute == 0
    assert marker.second == 0
    assert marker.astimezone(source_tz).date() == date(2026, 11, 3)
    assert marker.astimezone(source_tz).time().hour == 11
    assert marker.astimezone(source_tz).time().minute == 30


def test_current_day_marker_is_not_emitted_before_source_local_noon() -> None:
    source_tz = timezone(timedelta(hours=3))
    rows = [
        {"date": date(2026, 11, 2), "kp": 3.67},
        {"date": date(2026, 11, 3), "kp": 4.33},
    ]

    before_noon = daily_statistics(
        rows,
        source_tz,
        now=datetime(2026, 11, 3, 8, 59, tzinfo=timezone.utc),
    )
    at_noon = daily_statistics(
        rows,
        source_tz,
        now=datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc),
    )

    assert [stat["max"] for stat in before_noon] == [3.67]
    assert [stat["max"] for stat in at_noon] == [3.67, 4.33]


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


def test_start_without_recorder_reports_unavailable(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(), _coordinator())

    manager.start()

    assert manager.status == "recorder_unavailable"
    assert manager._unsub is None
    assert manager._task is None
    assert manager._current_task is None


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
    rows = [{"date": date(2020, 1, 1), "kp": 4.33}]

    with patch.object(
        manager,
        "_async_statistics_persisted",
        new=AsyncMock(return_value=True),
    ), patch(
        "custom_components.xras_geomagnetic.history.async_add_external_statistics"
    ) as add_stats:
        manager._sync_current_month(rows)
        current_task = manager._current_task
        assert current_task is not None
        await current_task
        await asyncio.sleep(0)

        manager._sync_current_month(rows)
        assert manager._current_task is None

        manager._sync_current_month(
            [{"date": date(2020, 1, 1), "kp": 5.0}]
        )
        current_task = manager._current_task
        assert current_task is not None
        await current_task
        await asyncio.sleep(0)

    assert add_stats.call_count == 2


@pytest.mark.asyncio
async def test_current_month_recorder_failure_is_isolated(hass) -> None:
    coordinator = _coordinator()
    coordinator.data = {"daily": [{"date": date(2020, 1, 1), "kp": 4.33}]}
    manager = XrasHistoryBackfill(hass, _entry(), coordinator)

    with patch(
        "custom_components.xras_geomagnetic.history.async_add_external_statistics",
        side_effect=RuntimeError("recorder unavailable"),
    ) as add_stats:
        manager._handle_update()
        current_task = manager._current_task
        archive_task = manager._task
        assert current_task is not None
        assert archive_task is not None
        await current_task
        await archive_task
        await asyncio.sleep(0)

    assert add_stats.call_count == 1
    assert manager.status == "retry_wait"
    assert manager.last_error == "recorder unavailable"
    assert manager.diagnostics["retry_in_seconds"] > 0
    assert manager._current_task is None
    assert manager._task is None


@pytest.mark.asyncio
async def test_successful_current_retry_clears_stale_diagnostics(hass) -> None:
    coordinator = _coordinator()
    coordinator.data = {"daily": [{"date": date(2020, 1, 1), "kp": 4.33}]}
    manager = XrasHistoryBackfill(hass, _entry(), coordinator)
    current_month = (
        datetime.now(timezone.utc)
        .astimezone(coordinator.tz)
        .date()
        .replace(day=1)
    )
    manager._completed_for_month = current_month

    store = AsyncMock(side_effect=[RuntimeError("recorder unavailable"), None])
    with patch.object(manager, "_async_add_and_verify", new=store):
        manager._handle_update()
        current_task = manager._current_task
        assert current_task is not None
        await current_task
        await asyncio.sleep(0)

        assert manager.status == "retry_wait"
        assert manager.last_error == "recorder unavailable"
        assert manager._task is None

        manager._retry_after = 0.0
        manager._handle_update()
        current_task = manager._current_task
        assert current_task is not None
        await current_task
        await asyncio.sleep(0)

    assert store.await_count == 2
    assert manager.status == "complete"
    assert manager.last_error is None
    assert manager.diagnostics["retry_in_seconds"] == 0
    assert manager._task is None


@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.asyncio
async def test_real_recorder_round_trip_is_persisted_and_idempotent(hass) -> None:
    recorder = get_instance(hass)
    source_tz = timezone(timedelta(hours=14))
    coordinator = _coordinator(source_tz)
    manager = XrasHistoryBackfill(hass, _entry(), coordinator)
    rows = [{"date": date(2020, 1, 2), "kp": 5.33}]

    manager._sync_current_month(rows)
    current_task = manager._current_task
    assert current_task is not None
    await current_task
    await asyncio.sleep(0)
    assert manager._last_current_signature is not None

    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = datetime(2020, 1, 3, tzinfo=timezone.utc)
    result = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {manager.statistic_id},
        "hour",
        None,
        {"max"},
    )
    assert len(result[manager.statistic_id]) == 1
    assert result[manager.statistic_id][0]["max"] == 5.33
    assert datetime.fromtimestamp(
        result[manager.statistic_id][0]["start"], timezone.utc
    ).astimezone(source_tz).date() == date(2020, 1, 2)

    # A reload loses the in-memory signature, but Recorder upsert remains idempotent.
    reloaded = XrasHistoryBackfill(hass, _entry(), coordinator)
    reloaded._sync_current_month(rows)
    current_task = reloaded._current_task
    assert current_task is not None
    await current_task
    await asyncio.sleep(0)

    result = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {manager.statistic_id},
        "hour",
        None,
        {"max"},
    )
    assert len(result[manager.statistic_id]) == 1
    assert result[manager.statistic_id][0]["max"] == 5.33


@pytest.mark.asyncio
async def test_completed_month_requires_final_source_calendar_day(hass) -> None:
    source_tz = timezone(timedelta(hours=14))
    manager = XrasHistoryBackfill(hass, _entry(), _coordinator(source_tz))
    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(
            return_value={
                manager.statistic_id: [
                    {
                        "start": datetime(
                            2026, 9, 15, 12, tzinfo=source_tz
                        ).timestamp(),
                        "max": 4.33,
                    },
                    {
                        "start": datetime(
                            2026, 10, 31, 12, tzinfo=source_tz
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
    store = AsyncMock(return_value=None)

    with patch.object(
        manager, "_async_completed_months", new=AsyncMock(return_value=set())
    ), patch.object(
        manager, "_async_add_and_verify", new=store
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ), patch(
        "custom_components.xras_geomagnetic.history.asyncio.sleep",
        new=AsyncMock(),
    ) as sleep:
        await manager._async_backfill(date(2026, 11, 1))

    assert [call.args[2] for call in fetch.await_args_list] == [
        date(2026, 10, 1),
        date(2026, 9, 1),
    ]
    assert store.await_count == 2
    sleep.assert_awaited_once_with(_HISTORY_REQUEST_DELAY)
    assert manager.imported_archive_months == 2
    assert manager.diagnostics["imported_archive_months_this_session"] == 2
    assert manager.diagnostics["archive_imported_months"] == [
        "2026-09",
        "2026-10",
    ]
    assert manager.status == "complete"


@pytest.mark.asyncio
async def test_archive_is_not_counted_before_recorder_confirmation(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(months=1), _coordinator())
    fetch = AsyncMock(return_value=_dataset(date(2026, 10, 1), 5.33))

    with patch.object(
        manager, "_async_completed_months", new=AsyncMock(return_value=set())
    ), patch.object(
        manager,
        "_async_add_and_verify",
        new=AsyncMock(side_effect=RuntimeError("Recorder did not persist")),
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ):
        await manager._async_backfill(date(2026, 11, 1))

    assert manager.imported_archive_months == 0
    assert manager._completed_for_month is None
    assert manager.status == "retry_wait"
    assert manager.last_error == "Recorder did not persist"
    assert manager.diagnostics["retry_in_seconds"] > 0


@pytest.mark.asyncio
async def test_freshest_completed_month_is_revalidated_from_archive(hass) -> None:
    manager = XrasHistoryBackfill(hass, _entry(months=2), _coordinator())
    fetch = AsyncMock(return_value=_dataset(date(2026, 10, 1), 5.33))
    store = AsyncMock(return_value=None)

    with patch.object(
        manager,
        "_async_completed_months",
        new=AsyncMock(
            return_value={date(2026, 10, 1), date(2026, 9, 1)}
        ),
    ), patch.object(
        manager, "_async_add_and_verify", new=store
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ):
        await manager._async_backfill(date(2026, 11, 1))

    fetch.assert_awaited_once_with(hass, "RAL5", date(2026, 10, 1))
    store.assert_awaited_once()
    assert manager.imported_archive_months == 1
    assert manager.status == "complete"


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
    store = AsyncMock(return_value=None)

    with patch.object(
        manager, "_async_completed_months", new=AsyncMock(return_value=set())
    ), patch.object(
        manager, "_async_add_and_verify", new=store
    ), patch(
        "custom_components.xras_geomagnetic.history.async_fetch_history_month",
        fetch,
    ), patch(
        "custom_components.xras_geomagnetic.history.asyncio.sleep",
        new=AsyncMock(),
    ):
        await manager._async_backfill(date(2026, 11, 1))

    assert fetch.await_count == 2
    assert store.await_count == 1
    assert manager.status == "partial"
    assert manager.diagnostics["archive_not_found"] == ["2026-10"]
