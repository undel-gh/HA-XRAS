"""Опрос xras.ru и подготовка данных."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial

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
_CURRENT_REFRESH_RETRY_DELAY = timedelta(seconds=MIN_SCAN_INTERVAL)
_CURRENT_REFRESH_RETRY_LIMIT = 3


class XrasCoordinator(DataUpdateCoordinator):
    """Забирает четыре выгрузки и раскладывает их по нужным срезам."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.code: str = str(entry.data.get(CONF_CODE, DEFAULT_CODE)).strip().upper()
        self.tz = tz_from_string(self.code) or timezone.utc
        self._unsub_current_expiry: Callable[[], None] | None = None
        self._unsub_current_refresh_retry: Callable[[], None] | None = None
        self._current_refresh_retries_remaining = 0
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
        self._schedule_current_expiry()

        data = self.data or {}
        if data.get("kp_current") is not None:
            self._stop_current_refresh_recovery()
        elif self._current_refresh_retries_remaining > 0:
            self._schedule_current_refresh_retry()

    @callback
    def _schedule_current_expiry(self) -> None:
        """Запланировать сброс текущего Kp ровно в конце его трёхчасового слота."""
        self._cancel_current_expiry()
        data = self.data or {}
        if data.get("kp_current") is None:
            return

        measured_at = data.get("kp_current_ts")
        valid_until = data.get("kp_current_valid_until")
        if measured_at is None or valid_until is None:
            return

        self._unsub_current_expiry = async_track_point_in_utc_time(
            self.hass,
            partial(self._expire_current, measured_at, valid_until),
            valid_until.astimezone(timezone.utc),
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
        """Завершить ускоренное получение Kp после успешного обновления."""
        self._cancel_current_refresh_retry()
        self._current_refresh_retries_remaining = 0

    @callback
    def _schedule_current_refresh_retry(self) -> None:
        """Повторить refresh через 5 минут, если новый слот ещё не опубликован."""
        if self._current_refresh_retries_remaining <= 0:
            return
        if self._unsub_current_refresh_retry is not None:
            return

        self._unsub_current_refresh_retry = async_call_later(
            self.hass,
            _CURRENT_REFRESH_RETRY_DELAY,
            self._retry_current_refresh,
        )

    async def _retry_current_refresh(self, _now: datetime) -> None:
        """Сделать одну ограниченную повторную попытку после истечения Kp."""
        self._unsub_current_refresh_retry = None
        if self._current_refresh_retries_remaining <= 0:
            return
        if (self.data or {}).get("kp_current") is not None:
            self._stop_current_refresh_recovery()
            return

        self._current_refresh_retries_remaining -= 1
        await self.async_refresh()

        if (
            self._current_refresh_retries_remaining == 0
            and (self.data or {}).get("kp_current") is None
        ):
            _LOGGER.debug(
                "Current Kp for %s is still unavailable after bounded expiry retries",
                self.code,
            )

    async def _expire_current(
        self,
        expected_measured_at: datetime,
        expected_valid_until: datetime,
        now: datetime,
    ) -> None:
        """Сбросить истёкший Kp и сразу запросить новый слот у XRAS."""
        self._unsub_current_expiry = None
        data = self.data
        if not data:
            return

        # Refresh мог успеть принести следующий слот одновременно с таймером.
        # Старый callback не должен очищать более новое значение.
        if (
            data.get("kp_current_ts") != expected_measured_at
            or data.get("kp_current_valid_until") != expected_valid_until
        ):
            return

        if now < expected_valid_until:
            self._schedule_current_expiry()
            return

        self.data = {
            **data,
            "kp_current": None,
            # Сохраняем границы последнего слота до ответа источника: это полезно
            # для диагностики и не выдаёт старое значение за текущее.
            "kp_current_ts": expected_measured_at,
            "kp_current_valid_until": expected_valid_until,
            "level_key": None,
        }
        self.async_update_listeners()

        _LOGGER.debug(
            "Current Kp for %s expired at %s; requesting a fresh slot",
            self.code,
            expected_valid_until.isoformat(),
        )

        # Один запрос делаем сразу. Если XRAS публикует новый слот с задержкой,
        # _async_refresh_finished() поставит максимум три повтора по 5 минут.
        self._cancel_current_refresh_retry()
        self._current_refresh_retries_remaining = _CURRENT_REFRESH_RETRY_LIMIT
        await self.async_refresh()

    async def async_shutdown(self) -> None:
        """Отменить локальные таймеры вместе со штатными задачами coordinator."""
        self._cancel_current_expiry()
        self._stop_current_refresh_recovery()
        await super().async_shutdown()

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
                    raw = await response.text()

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
        for (key, (prefix, required)), result in zip(DATASETS.items(), results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                if required:
                    raise UpdateFailed(str(result)) from result
                _LOGGER.debug("Необязательный набор %s недоступен: %s", prefix, result)
                sets[key] = None
            else:
                sets[key] = result

        fact = sets["fact"]
        if fact is None:
            raise UpdateFailed("обязательный набор kp_* не получен")
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

        # Ap / F10.7 / SN — из сегодняшних суток факта, а при отсутствии
        # сегодняшней строки берём последние известные сутки не позднее today.
        latest = today_fact or next(
            (day for day in reversed(fact.days) if day.date <= today),
            None,
        )

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
            "kp_current_ts": current["ts"] if current else None,
            "kp_current_valid_until": candidate_valid_until if current else None,
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
            "ap": latest.ap if latest else None,
            "f10": latest.f10 if latest else None,
            "sn": latest.sn if latest else None,
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
