"""Опрос xras.ru и подготовка данных."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BASE_URL,
    CONF_CODE,
    CONF_SCAN_INTERVAL,
    DATASETS,
    DEFAULT_CODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STORM_LEVELS,
    STORM_THRESHOLD,
    USER_AGENT,
)
from .parser import Dataset, XrasParseError, parse_json, parse_txt, tz_from_string

_LOGGER = logging.getLogger(__name__)


class XrasCoordinator(DataUpdateCoordinator):
    """Забирает четыре выгрузки и раскладывает их по нужным срезам."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        options = {**entry.data, **entry.options}
        self.entry = entry
        self.code: str = str(options.get(CONF_CODE, DEFAULT_CODE)).strip()
        self.tz = tz_from_string(self.code) or timezone.utc
        interval = int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({self.code})",
            update_interval=timedelta(seconds=interval),
        )

    # --- сеть -------------------------------------------------------------

    async def _fetch(self, prefix: str) -> Dataset:
        """Сначала JSON, при неудаче — TXT."""
        session = async_get_clientsession(self.hass)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain"}
        timeout = aiohttp.ClientTimeout(total=30)
        last_error: Exception | None = None

        for suffix in ("json", "txt"):
            url = f"{BASE_URL}/{prefix}_{self.code}.{suffix}"
            try:
                async with session.get(url, headers=headers, timeout=timeout) as response:
                    response.raise_for_status()
                    raw = await response.text()

                if suffix == "json":
                    return parse_json(json.loads(raw), self.code)
                return parse_txt(raw, self.code)

            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, XrasParseError) as err:
                last_error = err
                _LOGGER.debug("%s: %s", url, err)

        raise UpdateFailed(f"{prefix}_{self.code}: {last_error}")

    # --- основной цикл ----------------------------------------------------

    async def _async_update_data(self) -> dict:
        results = await asyncio.gather(
            *(self._fetch(prefix) for prefix, _ in DATASETS.values()),
            return_exceptions=True,
        )

        sets: dict[str, Dataset | None] = {}
        for (key, (prefix, required)), result in zip(DATASETS.items(), results):
            if isinstance(result, Exception):
                if required:
                    raise UpdateFailed(str(result))
                _LOGGER.debug("Необязательный набор %s недоступен: %s", prefix, result)
                sets[key] = None
            else:
                sets[key] = result

        fact = sets["fact"]
        assert fact is not None
        self.tz = fact.tz

        now = datetime.now(timezone.utc)
        today = now.astimezone(self.tz).date()

        fact_series = fact.series
        past = [point for point in fact_series if point["ts"] <= now]
        current = past[-1] if past else (fact_series[-1] if fact_series else None)
        current_kp = current["kp"] if current else None

        forecast = sets["forecast"]
        forecast_series = [
            point for point in (forecast.series if forecast else []) if point["ts"] > now
        ]
        next_24h = [
            point for point in forecast_series if point["ts"] <= now + timedelta(hours=24)
        ]

        today_fact = fact.day_for(today)
        month = sets["month"]
        month_forecast = sets["month_forecast"]

        # Ap / F10.7 / SN — из суток факта, при отсутствии берём последние известные
        latest = today_fact or (fact.days[-1] if fact.days else None)
        forecast_tomorrow = None
        if forecast:
            forecast_tomorrow = forecast.day_for(today + timedelta(days=1)) or (
                forecast.days[0] if forecast.days else None
            )

        level_key, level_name = level_for(current_kp)
        forecast_max_24h = max((point["kp"] for point in next_24h), default=None)

        month_daily = month.daily if month else fact.daily
        month_days = [row for row in month_daily if row["kp"] is not None]

        return {
            "kp_current": current_kp,
            "kp_current_ts": current["ts"] if current else None,
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
            "storm_days_month": sum(
                1 for row in month_days if row["kp"] >= STORM_THRESHOLD
            ),
            "ap": latest.ap if latest else None,
            "f10": latest.f10 if latest else None,
            "sn": latest.sn if latest else None,
            "ap_forecast": forecast_tomorrow.ap if forecast_tomorrow else None,
            "f10_forecast": forecast_tomorrow.f10 if forecast_tomorrow else None,
            "level_key": level_key,
            "level_name": level_name,
            "forecast_level_name": level_for(forecast_max_24h)[1],
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
                for day in (month.days if month else fact.days)
                if day.max_kp is not None
            ],
            "tzone": fact.tzone,
            "source": fact.source,
            "updated_at": now,
        }


def level_for(kp: float | None) -> tuple[str | None, str | None]:
    """Kp -> (ключ уровня, название)."""
    if kp is None:
        return None, None
    key, name = STORM_LEVELS[0][1], STORM_LEVELS[0][2]
    for threshold, level_key, level_name in STORM_LEVELS:
        if kp >= threshold:
            key, name = level_key, level_name
    return key, name
