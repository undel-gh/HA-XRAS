"""Historical XRAS daily Kp maxima as Home Assistant external statistics."""

from __future__ import annotations

import asyncio
import json
import logging
from calendar import monthrange
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
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
except ImportError:  # Home Assistant 2024.12 compatibility
    StatisticMeanType = None  # type: ignore[assignment,misc]

from .const import (
    BASE_URL,
    CONF_CODE,
    CONF_HISTORY_MONTHS,
    DOMAIN,
    REQUEST_TIMEOUT,
    USER_AGENT,
)
from .http import MAX_API_RESPONSE_BYTES, XrasResponseTooLarge, read_utf8_limited
from .parser import Dataset, XrasParseError, parse_json, parse_txt

if TYPE_CHECKING:
    from .coordinator import XrasCoordinator

_LOGGER = logging.getLogger(__name__)

_HISTORY_REQUEST_DELAY = 30
_HISTORY_RETRY_COOLDOWN = timedelta(hours=6)
_HISTORY_MARKER_HOUR_UTC = 12


class HistoryArchiveNotFound(Exception):
    """The requested XRAS monthly archive does not exist."""


class HistoryFetchError(Exception):
    """An XRAS monthly archive could not be downloaded or parsed."""


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


def daily_statistics(rows: Iterable[dict[str, Any]]) -> list[StatisticData]:
    """Convert source-day maxima into one neutral hourly statistic per day.

    XRAS archives contain one maximum per source calendar day, not the original
    3-hour Kp samples. We therefore keep a separate external statistic and use
    12:00 UTC as a stable marker for the source date instead of pretending that
    the maximum was measured at a particular 3-hour slot.
    """
    statistics: list[StatisticData] = []
    for row in sorted(rows, key=lambda item: item["date"]):
        day = row.get("date")
        kp = row.get("kp")
        if not isinstance(day, date) or kp is None:
            continue
        value = float(kp)
        statistics.append(
            {
                "start": datetime(
                    day.year,
                    day.month,
                    day.day,
                    _HISTORY_MARKER_HOUR_UTC,
                    tzinfo=timezone.utc,
                ),
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
    last_error: Exception | None = None
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
        except (
            aiohttp.ClientError,
            TimeoutError,
            UnicodeDecodeError,
            XrasResponseTooLarge,
            json.JSONDecodeError,
            XrasParseError,
        ) as err:
            last_error = err
            _LOGGER.debug("XRAS history fetch failed for %s", url, exc_info=True)

    if not_found == 2:
        raise HistoryArchiveNotFound(f"no archive for {month:%Y-%m}")
    if last_error is None:
        raise HistoryFetchError(f"no usable archive for {month:%Y-%m}")
    raise HistoryFetchError(
        f"could not fetch archive for {month:%Y-%m}: {last_error}"
    ) from last_error


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
        self._last_current_signature: tuple[tuple[date, float], ...] | None = None
        self._completed_for_month: date | None = None
        self._not_found_months: set[date] = set()
        self._retry_after = 0.0
        self._task: asyncio.Task[None] | None = None
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
            "archive_not_found": [
                month.strftime("%Y-%m") for month in sorted(self._not_found_months)
            ],
            "retry_in_seconds": max(0, int(self._retry_after - monotonic())),
        }

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
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @callback
    def _handle_update(self) -> None:
        """Sync current month cheaply and schedule bounded archive work."""
        if self._shutdown or not self.coordinator.last_update_success:
            return
        data = self.coordinator.data or {}
        self._sync_current_month(data.get("daily", []))

        current_month = (
            datetime.now(timezone.utc)
            .astimezone(self.coordinator.tz)
            .date()
            .replace(day=1)
        )
        if self._completed_for_month != current_month:
            self._completed_for_month = None
        if (
            self._completed_for_month is None
            and self._task is None
            and monotonic() >= self._retry_after
        ):
            self._task = self.hass.async_create_task(self._async_backfill(current_month))

    @callback
    def _sync_current_month(self, rows: Iterable[dict[str, Any]]) -> None:
        """Update current-month rows without making any extra XRAS request."""
        normalized = tuple(
            sorted(
                (
                    (row["date"], float(row["kp"]))
                    for row in rows
                    if isinstance(row.get("date"), date) and row.get("kp") is not None
                ),
                key=lambda item: item[0],
            )
        )
        if not normalized or normalized == self._last_current_signature:
            return

        stats = daily_statistics(
            {"date": day, "kp": kp} for day, kp in normalized
        )
        if not stats:
            return
        async_add_external_statistics(
            self.hass,
            _statistics_metadata(self.code),
            stats,
        )
        self._last_current_signature = normalized

    async def _async_completed_months(
        self, desired_months: list[date], current_month: date
    ) -> set[date]:
        """Find months with a statistic for their final calendar day.

        The live current-month kpm file is also imported into this statistic. A
        month with only some earlier rows must therefore remain eligible for an
        archive fetch after rollover; presence of the last day proves that the
        whole live-month snapshot reached Recorder or that the archive was
        imported previously.
        """
        if not desired_months:
            return set()
        start = datetime(
            desired_months[-1].year,
            desired_months[-1].month,
            1,
            tzinfo=timezone.utc,
        )
        end = datetime(
            current_month.year,
            current_month.month,
            1,
            tzinfo=timezone.utc,
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
                datetime.fromtimestamp(float(start_ts), timezone.utc).date()
            )
        return {
            month for month in desired_months if _last_day(month) in existing_days
        }

    async def _async_backfill(self, current_month: date) -> None:
        """Backfill requested completed months at a deliberately slow pace."""
        self.status = "running"
        self.last_error = None
        try:
            desired = _previous_months(current_month, self.months)
            completed = await self._async_completed_months(desired, current_month)
            missing = [
                month
                for month in desired
                if month not in completed and month not in self._not_found_months
            ]

            for index, month in enumerate(missing):
                if self._shutdown:
                    return
                try:
                    dataset = await async_fetch_history_month(
                        self.hass, self.code, month
                    )
                except HistoryArchiveNotFound:
                    self._not_found_months.add(month)
                    _LOGGER.debug(
                        "XRAS historical archive %s %s is not available",
                        self.code,
                        month.strftime("%Y-%m"),
                    )
                except HistoryFetchError as err:
                    self.status = "retry_wait"
                    self.last_error = str(err)
                    self._retry_after = monotonic() + _HISTORY_RETRY_COOLDOWN.total_seconds()
                    _LOGGER.debug(
                        "XRAS historical backfill for %s paused for %s after: %s",
                        self.code,
                        _HISTORY_RETRY_COOLDOWN,
                        err,
                    )
                    return
                else:
                    async_add_external_statistics(
                        self.hass,
                        _statistics_metadata(self.code),
                        daily_statistics(dataset.daily),
                    )
                    completed.add(month)
                    self.imported_archive_months += 1
                    _LOGGER.debug(
                        "Queued %d XRAS historical daily maxima for %s %s",
                        len(dataset.daily),
                        self.code,
                        month.strftime("%Y-%m"),
                    )

                if index < len(missing) - 1:
                    await asyncio.sleep(_HISTORY_REQUEST_DELAY)

            self._completed_for_month = current_month
            self.status = "partial" if self._not_found_months else "complete"
        except asyncio.CancelledError:
            raise
        except Exception as err:  # Optional history must not break the integration.
            self.status = "error"
            self.last_error = str(err)
            self._retry_after = monotonic() + _HISTORY_RETRY_COOLDOWN.total_seconds()
            _LOGGER.exception("Unexpected XRAS historical backfill failure")
        finally:
            self._task = None
