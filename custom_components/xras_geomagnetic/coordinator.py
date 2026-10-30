"""Опрос xras.ru и подготовка данных."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from functools import partial
from random import randint
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BASE_URL,
    CONF_CODE,
    CONF_SCAN_INTERVAL,
    DATASETS,
    DEFAULT_CODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    REQUEST_TIMEOUT,
    STORM_LEVELS,
    STORM_THRESHOLD,
    USER_AGENT,
)
from .parser import Dataset, XrasParseError, parse_json, parse_txt, tz_from_string

_LOGGER = logging.getLogger(__name__)
_SLOT_DURATION = timedelta(hours=3)
_CURRENT_REFRESH_RETRY_LIMIT = 3
_CURRENT_REFRESH_JITTER_MIN = 5
_CURRENT_REFRESH_JITTER_MAX = 60
_CURRENT_REFRESH_RECENT_EXPIRY = timedelta(
    seconds=MIN_SCAN_INTERVAL * _CURRENT_REFRESH_RETRY_LIMIT
)


def _latest_daily_value(days: list[Any], today: date, field: str) -> Any:
    """Return the latest non-null daily field not later than today."""
    for day in reversed(days):
        if day.date > today:
            continue
        value = getattr(day, field)
        if value is not None:
            return value
    return None


class XrasCoordinator(DataUpdateCoordinator):
    """Забирает четыре выгрузки и раскладывает их по нужным срезам."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.code: str = str(entry.data.get(CONF_CODE, DEFAULT_CODE)).strip().upper()
        self.tz = tz_from_string(self.code) or timezone.utc
        self._unsub_current_expiry: Callable[[], None] | None = None
        self._unsub_current_refresh_retry: Callable[[], None] | None = None
        self._current_refresh_retries_remaining = 0
        self._current_refresh_recovery_slot: tuple[datetime, datetime] | None = None
        interval = int(
            entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        )

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} ({self.code})",
            update_interval=timedelta(seconds=interval),
        )

    # --- срок действия текущего Kp ---------------------------------------

    @callback
    def _async_refresh_finished(self) -> None:
        """После любого refresh синхронизировать таймеры текущего Kp."""
        super()._async_refresh_finished()
        if self._shutdown_requested:
            return

        recovery_slot = self._current_refresh_recovery_slot
        if recovery_slot is not None:
            _expected_measured_at, expected_valid_until = recovery_slot
            data = self.data or {}
            current_valid_until = data.get("kp_current_valid_until")
            fresh_current = (
                self.last_update_success
                and data.get("kp_current") is not None
                and current_valid_until is not None
                and current_valid_until > expected_valid_until
            )
            if fresh_current:
                self._stop_current_refresh_recovery()
                self._schedule_current_expiry()
                return

            if self._current_refresh_retries_remaining > 0:
                self._schedule_current_refresh_attempt(is_retry=True)
            else:
                _LOGGER.debug(
                    "Current Kp for %s is still unavailable after bounded "
                    "expiry retries",
                    self.code,
                )
                self._stop_current_refresh_recovery()
            return

        self._schedule_current_expiry()

    @callback
    def _schedule_current_expiry(self) -> None:
        """Synchronize the current-slot expiry timer or recent-gap recovery."""
        self._cancel_current_expiry()
        if self._shutdown_requested or self._current_refresh_recovery_slot is not None:
            return

        data = self.data or {}
        measured_at = data.get("kp_current_ts")
        valid_until = data.get("kp_current_valid_until")
        if measured_at is None or valid_until is None:
            return

        now = datetime.now(timezone.utc)
        valid_until_utc = valid_until.astimezone(timezone.utc)
        if now >= valid_until_utc:
            # A still-published Kp must never get stranded after its deadline.
            # Also recover a startup/ordinary-refresh gap when the last slot only
            # just expired and XRAS may be a few minutes late publishing the next.
            if (
                data.get("kp_current") is not None
                or now - valid_until_utc <= _CURRENT_REFRESH_RECENT_EXPIRY
            ):
                self._start_current_refresh_recovery(measured_at, valid_until)
            return

        if data.get("kp_current") is None:
            return

        self._unsub_current_expiry = async_track_point_in_utc_time(
            self.hass,
            partial(self._expire_current, measured_at, valid_until),
            valid_until_utc,
        )

    @callback
    def _cancel_current_expiry(self) -> None:
        """Отменить ранее запланированное истечение текущего Kp."""
        if self._unsub_current_expiry is not None:
            self._unsub_current_expiry()
            self._unsub_current_expiry = None

    @callback
    def _cancel_current_refresh_retry(self) -> None:
        """Отменить отложенную попытку получить новый Kp после границы слота."""
        if self._unsub_current_refresh_retry is not None:
            self._unsub_current_refresh_retry()
            self._unsub_current_refresh_retry = None

    @callback
    def _stop_current_refresh_recovery(self) -> None:
        """Завершить ускоренное получение Kp после границы слота."""
        self._cancel_current_refresh_retry()
        self._current_refresh_retries_remaining = 0
        self._current_refresh_recovery_slot = None

    @callback
    def _start_current_refresh_recovery(
        self, measured_at: datetime, valid_until: datetime
    ) -> None:
        """Start one bounded jittered recovery sequence for an expired slot."""
        if self._shutdown_requested or self._current_refresh_recovery_slot is not None:
            return

        self._cancel_current_expiry()
        self._cancel_current_refresh_retry()
        self._current_refresh_recovery_slot = (measured_at, valid_until)
        self._current_refresh_retries_remaining = _CURRENT_REFRESH_RETRY_LIMIT
        self._schedule_current_refresh_attempt(is_retry=False)
        _LOGGER.debug(
            "Current Kp for %s reached valid_until %s; scheduled a fresh-slot refresh",
            self.code,
            valid_until.isoformat(),
        )

    @callback
    def _schedule_current_refresh_attempt(self, *, is_retry: bool) -> None:
        """Запланировать jittered refresh, чтобы не создавать стадо на XRAS."""
        if self._shutdown_requested or self._current_refresh_recovery_slot is None:
            return
        if is_retry and self._current_refresh_retries_remaining <= 0:
            return
        if self._unsub_current_refresh_retry is not None:
            return

        base_delay = MIN_SCAN_INTERVAL if is_retry else 0
        delay = timedelta(
            seconds=base_delay
            + randint(_CURRENT_REFRESH_JITTER_MIN, _CURRENT_REFRESH_JITTER_MAX)
        )
        self._unsub_current_refresh_retry = async_call_later(
            self.hass,
            delay,
            partial(self._run_current_refresh_attempt, is_retry),
        )

    async def _run_current_refresh_attempt(
        self, is_retry: bool, _now: datetime
    ) -> None:
        """Запросить новый слот один раз в рамках expiry-recovery."""
        self._unsub_current_refresh_retry = None
        if self._shutdown_requested or self._current_refresh_recovery_slot is None:
            return
        if is_retry:
            if self._current_refresh_retries_remaining <= 0:
                return
            self._current_refresh_retries_remaining -= 1
        await self.async_refresh()

    @callback
    def _expire_current(
        self,
        expected_measured_at: datetime,
        expected_valid_until: datetime,
        now: datetime,
    ) -> None:
        """На границе слота запросить новый Kp без промежуточной записи state."""
        self._unsub_current_expiry = None
        if self._shutdown_requested:
            return

        data = self.data
        if not data:
            return

        # Refresh мог успеть принести следующий слот одновременно с таймером.
        if (
            data.get("kp_current_ts") != expected_measured_at
            or data.get("kp_current_valid_until") != expected_valid_until
        ):
            return

        if now < expected_valid_until:
            # Home Assistant normally calls on/after the target instant, but if a
            # callback arrives early we resynchronize against the wall clock. If
            # the wall clock crossed valid_until in the meantime, the scheduler
            # starts recovery instead of silently dropping the timer.
            self._schedule_current_expiry()
            return

        # Не публикуем искусственный промежуточный unknown: успешный refresh сам
        # переведёт старое значение напрямую в новый Kp либо в unknown, если XRAS
        # ещё не опубликовал следующий слот. При сетевой ошибке CoordinatorEntity
        # штатно станет unavailable через last_update_success=False.
        self._start_current_refresh_recovery(
            expected_measured_at, expected_valid_until
        )

    async def async_shutdown(self) -> None:
        """Отменить локальные таймеры вместе со штатными задачами coordinator."""
        await super().async_shutdown()
        self._cancel_current_expiry()
        self._stop_current_refresh_recovery()

    # --- сеть -------------------------------------------------------------

    async def _fetch(self, prefix: str) -> Dataset:
        """Сначала JSON, при неудаче — TXT."""
        session = async_get_clientsession(self.hass)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain"}
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        last_error: Exception | None = None

        for suffix in ("json", "txt"):
            url = f"{BASE_URL}/{prefix}_{self.code}.{suffix}"
            try:
                async with session.get(
                    url, headers=headers, timeout=timeout
                ) as response:
                    response.raise_for_status()
                    raw = await response.text(encoding="utf-8")

                if suffix == "json":
                    return parse_json(json.loads(raw), self.code)
                return parse_txt(raw, self.code)

            except (
                aiohttp.ClientError,
                TimeoutError,
                json.JSONDecodeError,
                XrasParseError,
            ) as err:
                last_error = err
                _LOGGER.debug("XRAS fetch failed for %s", url, exc_info=True)

        if last_error is None:
            raise UpdateFailed(f"{prefix}_{self.code}: no response")
        raise UpdateFailed(f"{prefix}_{self.code}: {last_error}") from last_error

    # --- основной цикл ----------------------------------------------------

    async def _async_update_data(self) -> dict:
        results = await asyncio.gather(
            *(self._fetch(prefix) for prefix, _ in DATASETS.values()),
            return_exceptions=True,
        )

        sets: dict[str, Dataset | None] = {}
        for (key, (prefix, required)), result in zip(
            DATASETS.items(), results, strict=True
        ):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                if required:
                    raise UpdateFailed(str(result)) from result
                _LOGGER.debug("Optional dataset %s is unavailable: %s", prefix, result)
                sets[key] = None
            else:
                sets[key] = result

        fact = sets["fact"]
        if fact is None:
            raise UpdateFailed("required kp_* dataset was not received")
        self.tz = fact.tz

        now = datetime.now(timezone.utc)
        today = now.astimezone(self.tz).date()

        fact_series = fact.series
        past = [point for point in fact_series if point["ts"] <= now]
        candidate = past[-1] if past else None
        candidate_valid_until = (
            candidate["ts"] + _SLOT_DURATION if candidate is not None else None
        )
        current = (
            candidate
            if candidate_valid_until is not None and now < candidate_valid_until
            else None
        )
        current_kp = current["kp"] if current else None

        forecast = sets["forecast"]
        forecast_series = [
            point
            for point in (forecast.series if forecast else [])
            if point["ts"] + _SLOT_DURATION > now
        ]
        next_24h = [
            point
            for point in forecast_series
            if point["ts"] < now + timedelta(hours=24)
        ]

        today_fact = fact.day_for(today)
        month = sets["month"]
        month_forecast = sets["month_forecast"]

        # Суточные индексы XRAS могут появиться позже первых Kp-слотов нового дня.
        # Поэтому откатываемся к последнему непустому значению отдельно по полям.
        ap = _latest_daily_value(fact.days, today, "ap")
        f10 = _latest_daily_value(fact.days, today, "f10")
        sn = _latest_daily_value(fact.days, today, "sn")

        # Прогнозные Ap/F10.7 относятся именно к завтрашним суткам. Если такой
        # строки нет, не подменяем её более поздним или устаревшим днём.
        forecast_tomorrow = (
            forecast.day_for(today + timedelta(days=1)) if forecast else None
        )

        level_key = level_for(current_kp)
        forecast_max_24h = max((point["kp"] for point in next_24h), default=None)

        # Без месячного файла месячные показатели остаются пустыми:
        # подставлять сюда трёхсуточные данные — значит молча врать о периоде.
        month_daily = month.daily if month else []
        month_days = [row for row in month_daily if row["kp"] is not None]

        return {
            "kp_current": current_kp,
            # Даже когда текущий слот уже протух, сохраняем границы последнего
            # фактического candidate: это объясняет unknown в UI/диагностике.
            "kp_current_ts": candidate["ts"] if candidate else None,
            "kp_current_valid_until": candidate_valid_until,
            "kp_max_24h": max(
                (
                    point["kp"]
                    for point in past
                    if point["ts"] >= now - timedelta(hours=24)
                ),
                default=None,
            ),
            "kp_max_today": today_fact.max_kp if today_fact else None,
            "kp_forecast_max_24h": forecast_max_24h,
            "kp_forecast_max_3d": max(
                (point["kp"] for point in forecast_series), default=None
            ),
            "kp_max_month": max((row["kp"] for row in month_days), default=None),
            "storm_days_month": (
                sum(1 for row in month_days if row["kp"] >= STORM_THRESHOLD)
                if month is not None
                else None
            ),
            "ap": ap,
            "f10": f10,
            "sn": sn,
            "ap_forecast": forecast_tomorrow.ap if forecast_tomorrow else None,
            "f10_forecast": forecast_tomorrow.f10 if forecast_tomorrow else None,
            "level_key": level_key,
            "forecast_level_key": level_for(forecast_max_24h),
            "series": fact_series,
            "forecast_series": forecast_series,
            "daily": month_daily,
            "daily_forecast": month_forecast.daily if month_forecast else [],
            "month_history": [
                {
                    "date": day.date,
                    "kp": day.max_kp,
                    "ap": day.ap,
                    "f10": day.f10,
                    "sn": day.sn,
                }
                for day in (month.days if month else [])
                if day.max_kp is not None
            ],
            "dataset_available": {
                key: dataset is not None for key, dataset in sets.items()
            },
            "tzone": fact.tzone,
            "source": fact.source,
            "updated_at": now,
        }


def level_for(kp: float | None) -> str | None:
    """Kp -> ключ уровня («quiet», «active», «g1»…). Название даёт перевод."""
    if kp is None:
        return None
    key = STORM_LEVELS[0][1]
    for threshold, level_key in STORM_LEVELS:
        if kp >= threshold:
            key = level_key
    return key
