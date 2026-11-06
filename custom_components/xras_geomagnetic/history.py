"""Historical XRAS daily Kp maxima as Home Assistant external statistics."""

from __future__ import annotations

import asyncio
import json
import logging
from calendar import monthrange
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from math import isclose
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

try:
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # Compatibility with HA releases before StatisticMeanType.
    StatisticMeanType = None  # type: ignore[assignment,misc]

from .const import (
    BASE_URL,
    CONF_CODE,
    CONF_HISTORY_MONTHS,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    REQUEST_TIMEOUT,
    USER_AGENT,
)
from .http import MAX_API_RESPONSE_BYTES, XrasResponseTooLarge, read_utf8_limited
from .parser import Dataset, XrasParseError, parse_json, parse_txt

if TYPE_CHECKING:
    from .coordinator import XrasCoordinator

_LOGGER = logging.getLogger(__name__)

# Historical traffic must respect the same minimum source cadence as live polling.
_HISTORY_REQUEST_DELAY = MIN_SCAN_INTERVAL
_HISTORY_RETRY_COOLDOWN = timedelta(hours=6)
_HISTORY_INVALID_MAX_ATTEMPTS = 3
_HISTORY_MARKER_HOUR_LOCAL = 12


class HistoryArchiveNotFound(Exception):
    """The requested XRAS monthly archive does not exist."""


class HistoryFetchError(Exception):
    """An XRAS monthly archive could not be downloaded or parsed."""


class HistoryArchiveInvalid(HistoryFetchError):
    """Every reachable archive format was invalid, without transport failures."""


def history_statistic_id(code: str) -> str:
    """Return the external statistic id for one XRAS region."""
    return f"{DOMAIN}:kp_daily_max_{code.lower()}"


def _statistics_metadata(code: str) -> StatisticMetaData:
    """Build recorder metadata compatible with old and current HA releases."""
    metadata: dict[str, Any] = {
        "has_sum": False,
        "name": f"XRAS {code} daily Kp maximum",
        "source": DOMAIN,
        "statistic_id": history_statistic_id(code),
        "unit_of_measurement": None,
    }
    if StatisticMeanType is None:
        metadata["has_mean"] = True
    else:
        metadata["mean_type"] = StatisticMeanType.ARITHMETIC
        metadata["unit_class"] = None
    return cast(StatisticMetaData, metadata)


def _history_day_marker(day: date, source_tz: timezone) -> datetime:
    """Return an hourly Recorder marker near source-local noon for one day."""
    local_noon = datetime(
        day.year,
        day.month,
        day.day,
        _HISTORY_MARKER_HOUR_LOCAL,
        tzinfo=source_tz,
    )
    # External statistics are hourly. Align the source-local midday marker to a
    # whole UTC hour so half-hour/quarter-hour source offsets remain valid for
    # Recorder while staying safely inside the same source calendar day.
    return local_noon.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )


def daily_statistics(
    rows: Iterable[dict[str, Any]],
    source_tz: timezone,
    *,
    now: datetime | None = None,
) -> list[StatisticData]:
    """Convert source-day maxima into one neutral hourly statistic per day.

    XRAS archives contain one maximum per source calendar day, not the original
    3-hour Kp samples. We therefore keep a separate external statistic and use
    an hourly marker near noon in the source timezone for that source date
    instead of pretending that the maximum was measured at a particular 3-hour
    slot.

    For the live current-month dataset, callers may pass ``now`` to avoid
    creating a statistic whose source-day marker is still in the future.
    """
    statistics: list[StatisticData] = []
    for row in sorted(rows, key=lambda item: item["date"]):
        day = row.get("date")
        kp = row.get("kp")
        if not isinstance(day, date) or kp is None:
            continue
        marker = _history_day_marker(day, source_tz)
        if now is not None and marker > now:
            continue
        value = float(kp)
        statistics.append(
            {
                "start": marker,
                "mean": value,
                "min": value,
                "max": value,
            }
        )
    return statistics


def _previous_months(current_month: date, count: int) -> list[date]:
    """Return completed months, newest first."""
    months: list[date] = []
    cursor = current_month
    for _ in range(count):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        months.append(cursor)
    return months


def _last_day(month: date) -> date:
    """Return the last calendar day of month."""
    return month.replace(day=monthrange(month.year, month.month)[1])


def _validate_archive_month(dataset: Dataset, month: date) -> Dataset:
    """Reject a response that does not represent the requested archive month."""
    if not dataset.days:
        raise XrasParseError("historical monthly archive is empty")
    if any(
        day.date.year != month.year or day.date.month != month.month
        for day in dataset.days
    ):
        raise XrasParseError(
            f"historical monthly archive does not match {month:%Y-%m}"
        )
    if not dataset.daily:
        raise XrasParseError("historical monthly archive has no Kp maxima")
    return dataset


async def async_fetch_history_month(
    hass: HomeAssistant, code: str, month: date
) -> Dataset:
    """Fetch one completed kpm_<code>_YYYYMM archive with JSON→TXT fallback."""
    session = async_get_clientsession(hass)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain"}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    transport_error: Exception | None = None
    invalid_error: Exception | None = None
    not_found = 0

    for suffix in ("json", "txt"):
        url = f"{BASE_URL}/kpm_{code}_{month:%Y%m}.{suffix}"
        try:
            async with session.get(url, headers=headers, timeout=timeout) as response:
                if response.status == 404:
                    not_found += 1
                    continue
                response.raise_for_status()
                raw = await read_utf8_limited(
                    response, limit=MAX_API_RESPONSE_BYTES
                )
            dataset = (
                parse_json(json.loads(raw), code)
                if suffix == "json"
                else parse_txt(raw, code)
            )
            return _validate_archive_month(dataset, month)
        except (aiohttp.ClientError, TimeoutError) as err:
            transport_error = err
            _LOGGER.debug("XRAS history transport failed for %s", url, exc_info=True)
        except (
            UnicodeDecodeError,
            XrasResponseTooLarge,
            json.JSONDecodeError,
            XrasParseError,
        ) as err:
            invalid_error = err
            _LOGGER.debug("XRAS history content invalid for %s", url, exc_info=True)

    if not_found == 2:
        raise HistoryArchiveNotFound(f"no archive for {month:%Y-%m}")
    # An HTTP/transport failure is not evidence of malformed source data.
    # If either format could not be fetched, retry without a per-month cap.
    if transport_error is not None:
        raise HistoryFetchError(
            f"could not fetch archive for {month:%Y-%m}: {transport_error}"
        ) from transport_error
    if invalid_error is not None:
        raise HistoryArchiveInvalid(
            f"invalid archive for {month:%Y-%m}: {invalid_error}"
        ) from invalid_error
    raise HistoryFetchError(f"no usable archive for {month:%Y-%m}")


class XrasHistoryBackfill:
    """Maintain current daily-max statistics and backfill completed months."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: XrasCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.code = str(entry.data.get(CONF_CODE, coordinator.code)).upper()
        self.months = int(
            entry.options.get(
                CONF_HISTORY_MONTHS,
                entry.data.get(CONF_HISTORY_MONTHS, 0),
            )
        )
        self.statistic_id = history_statistic_id(self.code)
        self.status = "disabled" if self.months <= 0 else "idle"
        self.last_error: str | None = None
        self.imported_archive_months = 0
        self._last_current_signature: tuple[tuple[float, float], ...] | None = None
        self._pending_current_signature: tuple[tuple[float, float], ...] | None = None
        self._completed_for_month: date | None = None
        self._not_found_months: set[date] = set()
        self._archive_imported_months: set[date] = set()
        self._archive_invalid_attempts: dict[date, int] = {}
        self._archive_invalid_skipped: set[date] = set()
        self._fresh_archive_pending: date | None = None
        self._retry_after = 0.0
        self._retry_generation = 0
        self._task: asyncio.Task[None] | None = None
        self._current_task: asyncio.Task[None] | None = None
        self._unsub: Any = None
        self._shutdown = False

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return non-sensitive runtime status for diagnostics."""
        return {
            "enabled": self.months > 0,
            "months": self.months,
            "statistic_id": self.statistic_id,
            "status": self.status,
            "last_error": self.last_error,
            "imported_archive_months_this_session": self.imported_archive_months,
            "archive_imported_months": [
                month.strftime("%Y-%m")
                for month in sorted(self._archive_imported_months)
            ],
            "archive_not_found": [
                month.strftime("%Y-%m") for month in sorted(self._not_found_months)
            ],
            "archive_invalid_attempts": {
                month.strftime("%Y-%m"): self._archive_invalid_attempts[month]
                for month in sorted(self._archive_invalid_attempts)
            },
            "archive_invalid_skipped": [
                month.strftime("%Y-%m")
                for month in sorted(self._archive_invalid_skipped)
            ],
            "fresh_archive_pending": (
                self._fresh_archive_pending.strftime("%Y-%m")
                if self._fresh_archive_pending is not None
                else None
            ),
            "retry_in_seconds": max(0, int(self._retry_after - monotonic())),
        }

    @callback
    def _pause_for_retry(self, error: str) -> None:
        """Schedule the shared cooldown, preserving concurrent error ownership."""
        self.status = "retry_wait"
        self.last_error = error
        self._retry_after = monotonic() + _HISTORY_RETRY_COOLDOWN.total_seconds()
        self._retry_generation += 1

    @callback
    def start(self) -> None:
        """Start syncing after the coordinator completed its first refresh."""
        if self.months <= 0:
            self.status = "disabled"
            return
        if "recorder" not in self.hass.config.components:
            self.status = "recorder_unavailable"
            return
        self._unsub = self.coordinator.async_add_listener(self._handle_update)
        self._handle_update()

    @callback
    def shutdown(self) -> None:
        """Stop listeners and any in-flight historical task."""
        self._shutdown = True
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._current_task is not None:
            self._current_task.cancel()
            self._current_task = None
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @callback
    def _current_task_done(self, task: asyncio.Task[None]) -> None:
        """Clear the tracked current-month task after it finishes."""
        if self._current_task is task:
            self._current_task = None

    @callback
    def _backfill_task_done(self, task: asyncio.Task[None]) -> None:
        """Clear the tracked archive task after it finishes."""
        if self._task is task:
            self._task = None

    @callback
    def _handle_update(self) -> None:
        """Sync current month cheaply and schedule bounded archive work."""
        if self._shutdown or not self.coordinator.last_update_success:
            return
        if monotonic() < self._retry_after:
            return

        data = self.coordinator.data or {}
        try:
            self._sync_current_month(data.get("daily", []))
        except Exception as err:  # Optional history must not affect live entities.
            self.status = "retry_wait"
            self.last_error = str(err)
            self._retry_after = monotonic() + _HISTORY_RETRY_COOLDOWN.total_seconds()
            self._retry_generation += 1
            _LOGGER.exception("XRAS current-month history sync failed")
            return

        current_month = (
            datetime.now(timezone.utc)
            .astimezone(self.coordinator.tz)
            .date()
            .replace(day=1)
        )
        if self._completed_for_month != current_month:
            self._completed_for_month = None
        if self._completed_for_month is None and self._task is None:
            task = self.hass.async_create_task(self._async_backfill(current_month))
            self._task = task
            task.add_done_callback(self._backfill_task_done)

    @callback
    def _sync_current_month(self, rows: Iterable[dict[str, Any]]) -> None:
        """Queue current-month rows without making any extra XRAS request."""
        if self._current_task is not None:
            if not self._current_task.done():
                return
            self._current_task = None

        stats = daily_statistics(
            rows,
            self.coordinator.tz,
            now=datetime.now(timezone.utc),
        )
        signature = tuple(
            (stat["start"].timestamp(), float(stat["max"])) for stat in stats
        )
        if (
            not signature
            or signature == self._last_current_signature
            or signature == self._pending_current_signature
        ):
            return

        self._pending_current_signature = signature
        task = self.hass.async_create_task(
            self._async_store_current_statistics(signature, stats)
        )
        self._current_task = task
        task.add_done_callback(self._current_task_done)

    async def _async_store_current_statistics(
        self,
        signature: tuple[tuple[float, float], ...],
        stats: list[StatisticData],
    ) -> None:
        """Persist and verify current-month statistics before marking them synced."""
        retry_generation = self._retry_generation
        try:
            await self._async_add_and_verify(stats)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # Recorder failure must not affect live entities.
            if self._pending_current_signature == signature:
                self._pending_current_signature = None
            self.status = "retry_wait"
            self.last_error = str(err)
            self._retry_after = monotonic() + _HISTORY_RETRY_COOLDOWN.total_seconds()
            self._retry_generation += 1
            _LOGGER.exception("XRAS current-month history persistence failed")
        else:
            if self._pending_current_signature == signature:
                self._last_current_signature = signature
                self._pending_current_signature = None
                # A successful current write may clear only retry state that
                # already existed when this task started. If another task
                # registered a newer failure while Recorder I/O was in flight,
                # preserve that newer error and cooldown.
                if self._retry_generation == retry_generation:
                    self.last_error = None
                    self._retry_after = 0.0
                    if self.status == "retry_wait":
                        if self._completed_for_month is None:
                            self.status = "idle"
                        else:
                            self.status = (
                                "partial" if self._not_found_months else "complete"
                            )

    async def _async_add_and_verify(self, stats: list[StatisticData]) -> None:
        """Queue statistics, wait for Recorder, and verify the persisted rows."""
        if not stats:
            return
        async_add_external_statistics(
            self.hass,
            _statistics_metadata(self.code),
            stats,
        )
        if not await self._async_statistics_persisted(stats):
            raise RuntimeError("Recorder did not persist queued XRAS statistics")

    async def _async_statistics_persisted(self, stats: list[StatisticData]) -> bool:
        """Confirm queued rows by reading them back after Recorder's commit barrier."""
        recorder = get_instance(self.hass)
        await recorder.async_block_till_done()

        starts = [stat["start"].astimezone(timezone.utc) for stat in stats]
        start = min(starts)
        end = max(starts) + timedelta(hours=1)
        result = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            {self.statistic_id},
            "hour",
            None,
            {"max"},
        )
        persisted = {
            float(row["start"]): row.get("max")
            for row in result.get(self.statistic_id, [])
            if row.get("start") is not None
        }
        for stat in stats:
            expected_start = stat["start"].astimezone(timezone.utc).timestamp()
            actual = persisted.get(expected_start)
            if actual is None or not isclose(
                float(actual), float(stat["max"]), rel_tol=0.0, abs_tol=1e-9
            ):
                return False
        return True

    async def _async_completed_months(
        self, desired_months: list[date], current_month: date
    ) -> set[date]:
        """Find months with a statistic for their final source calendar day.

        This is a fast completeness probe for older history. The freshest
        completed month is still revalidated from its archive by
        ``_async_backfill`` because a live ``kpm_*`` snapshot taken immediately
        before rollover can contain the final day but miss its last three-hour
        contribution.
        """
        if not desired_months:
            return set()
        start = _history_day_marker(
            desired_months[-1], self.coordinator.tz
        ).astimezone(timezone.utc)
        end = _history_day_marker(current_month, self.coordinator.tz).astimezone(
            timezone.utc
        )
        result = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            {self.statistic_id},
            "hour",
            None,
            {"max"},
        )
        existing_days: set[date] = set()
        for row in result.get(self.statistic_id, []):
            start_ts = row.get("start")
            if start_ts is None:
                continue
            existing_days.add(
                datetime.fromtimestamp(float(start_ts), timezone.utc)
                .astimezone(self.coordinator.tz)
                .date()
            )
        return {
            month for month in desired_months if _last_day(month) in existing_days
        }

    async def _async_backfill(self, current_month: date) -> None:
        """Backfill requested completed months at a deliberately slow pace."""
        current_task = self._current_task
        if current_task is not None:
            try:
                await current_task
            except asyncio.CancelledError:
                raise
            if monotonic() < self._retry_after:
                return

        self.status = "running"
        self.last_error = None
        try:
            desired = _previous_months(current_month, self.months)
            newest = desired[0] if desired else None
            # A previous rollover's unpublished month is no longer the newest
            # archive. It follows ordinary (session-skipped) 404 policy now.
            if self._fresh_archive_pending != newest:
                self._fresh_archive_pending = None
            completed = await self._async_completed_months(desired, current_month)
            if desired and desired[0] not in self._archive_imported_months:
                # Always refresh the newest completed month from the immutable
                # archive once per manager lifecycle/current-month rollover. A
                # live kpm_* value for its final day may have been captured just
                # before XRAS switched files and therefore be slightly stale.
                completed.discard(desired[0])
            missing = [
                month
                for month in desired
                if month not in completed
                and month not in self._not_found_months
                and month not in self._archive_invalid_skipped
            ]

            for index, month in enumerate(missing):
                if self._shutdown:
                    return
                try:
                    dataset = await async_fetch_history_month(
                        self.hass, self.code, month
                    )
                except HistoryArchiveNotFound:
                    if month == newest:
                        # XRAS may publish the final month's archive after the
                        # live kpm_* file has already rolled over. Retry this
                        # one missing month after cooldown, but process older
                        # archives during the current pass.
                        self._fresh_archive_pending = month
                        self._pause_for_retry(
                            f"archive {month:%Y-%m} not yet published"
                        )
                    else:
                        self._not_found_months.add(month)
                    _LOGGER.debug(
                        "XRAS historical archive %s %s is not available",
                        self.code,
                        month.strftime("%Y-%m"),
                    )
                except HistoryArchiveInvalid as err:
                    attempts = self._archive_invalid_attempts.get(month, 0) + 1
                    self._archive_invalid_attempts[month] = attempts
                    if attempts < _HISTORY_INVALID_MAX_ATTEMPTS:
                        self._pause_for_retry(str(err))
                        _LOGGER.debug(
                            "XRAS archive %s %s invalid (attempt %d/%d); "
                            "retry after %s",
                            self.code,
                            month.strftime("%Y-%m"),
                            attempts,
                            _HISTORY_INVALID_MAX_ATTEMPTS,
                            _HISTORY_RETRY_COOLDOWN,
                        )
                        return
                    self._archive_invalid_skipped.add(month)
                    if month == self._fresh_archive_pending:
                        self._fresh_archive_pending = None
                    _LOGGER.warning(
                        "XRAS archive %s %s invalid after %d attempts; "
                        "skipping until integration reload",
                        self.code,
                        month.strftime("%Y-%m"),
                        attempts,
                    )
                except HistoryFetchError as err:
                    self.status = "retry_wait"
                    self.last_error = str(err)
                    self._retry_after = (
                        monotonic() + _HISTORY_RETRY_COOLDOWN.total_seconds()
                    )
                    self._retry_generation += 1
                    _LOGGER.debug(
                        "XRAS historical backfill for %s paused for %s after: %s",
                        self.code,
                        _HISTORY_RETRY_COOLDOWN,
                        err,
                    )
                    return
                else:
                    stats = daily_statistics(dataset.daily, dataset.tz)
                    try:
                        await self._async_add_and_verify(stats)
                    # Recorder errors are isolated from live data.
                    except Exception as err:
                        self.status = "retry_wait"
                        self.last_error = str(err)
                        self._retry_after = (
                            monotonic() + _HISTORY_RETRY_COOLDOWN.total_seconds()
                        )
                        self._retry_generation += 1
                        _LOGGER.exception(
                            "XRAS historical statistics persistence failed for %s %s",
                            self.code,
                            month.strftime("%Y-%m"),
                        )
                        return
                    completed.add(month)
                    self._archive_imported_months.add(month)
                    self._archive_invalid_attempts.pop(month, None)
                    if month == self._fresh_archive_pending:
                        self._fresh_archive_pending = None
                    self.imported_archive_months += 1
                    _LOGGER.debug(
                        "Persisted %d XRAS historical daily maxima for %s %s",
                        len(stats),
                        self.code,
                        month.strftime("%Y-%m"),
                    )

                if index < len(missing) - 1:
                    await asyncio.sleep(_HISTORY_REQUEST_DELAY)

            # Do not mark this month finished while its fresh archive is
            # unpublished: the next successful coordinator refresh after the
            # cooldown must schedule another attempt.
            if self._fresh_archive_pending is None:
                self._completed_for_month = current_month
            if monotonic() < self._retry_after:
                self.status = "retry_wait"
            else:
                self.status = (
                    "partial"
                    if any(
                        month in self._not_found_months
                        or month in self._archive_invalid_skipped
                        for month in desired
                    )
                    else "complete"
                )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # Optional history must not break the integration.
            self.status = "error"
            self.last_error = str(err)
            self._retry_after = monotonic() + _HISTORY_RETRY_COOLDOWN.total_seconds()
            self._retry_generation += 1
            _LOGGER.exception("Unexpected XRAS historical backfill failure")
