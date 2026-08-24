"""Сенсоры геомагнитной обстановки."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, STORM_LEVELS
from .coordinator import XrasCoordinator
from .entity import XrasEntity


@dataclass(frozen=True, kw_only=True)
class XrasSensorDescription(SensorEntityDescription):  # noqa: D101
    """Описание сенсора с функциями значения и атрибутов."""

    value_fn: Callable[[dict], Any]
    attrs_fn: Callable[[dict], dict] | None = None


def _half_up(value: float) -> int:
    """Округление «половина вверх»: 2.5 -> 3.

    Встроенный round() округляет к чётному (round(2.5) == 2), что расходится
    с тем, как значения показывает сайт.
    """
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _kp_notation(kp: float) -> str:
    """Kp в записи третями, как на сайте: 1.67 -> «2-», 7.33 -> «7+», 8.0 -> «8»."""
    thirds = _half_up(kp * 3)
    whole, remainder = divmod(thirds, 3)
    if remainder == 0:
        return str(whole)
    if remainder == 1:
        return f"{whole}+"
    return f"{whole + 1}-"


def _kp_attrs(data: dict) -> dict:
    """Ряды для графиков и «сайтовые» представления значения."""
    kp = data.get("kp_current")
    return {
        "kp_display": _half_up(kp) if kp is not None else None,
        "kp_notation": _kp_notation(kp) if kp is not None else None,
        "measured_at": data["kp_current_ts"].isoformat()
        if data.get("kp_current_ts")
        else None,
        "timezone": data.get("tzone"),
        "source": data.get("source"),
        "series": [
            {"time": point["ts"].isoformat(), "kp": point["kp"]}
            for point in data.get("series", [])
        ],
        "forecast_series": [
            {"time": point["ts"].isoformat(), "kp": point["kp"]}
            for point in data.get("forecast_series", [])
        ],
    }


def _daily_attrs(data: dict) -> dict:
    return {
        "daily": [
            {"date": row["date"].isoformat(), "kp": row["kp"]}
            for row in data.get("daily", [])
        ],
        "daily_forecast": [
            {"date": row["date"].isoformat(), "kp": row["kp"]}
            for row in data.get("daily_forecast", [])
        ],
    }


def _month_attrs(data: dict) -> dict:
    """Помесячная история: Kp, Ap, F10.7 и число пятен по суткам."""
    peak = data.get("kp_max_month")
    return {
        "storm_days": data.get("storm_days_month"),
        "kp_notation": _kp_notation(peak) if peak is not None else None,
        "history": [
            {
                "date": row["date"].isoformat(),
                "kp": row["kp"],
                "ap": row["ap"],
                "f10": row["f10"],
                "sn": row["sn"],
            }
            for row in data.get("month_history", [])
        ],
    }


SENSORS: tuple[XrasSensorDescription, ...] = (
    XrasSensorDescription(
        key="kp_current",
        icon="mdi:magnet",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("kp_current"),
        attrs_fn=_kp_attrs,
    ),
    XrasSensorDescription(
        key="storm_level",
        icon="mdi:weather-lightning",
        device_class=SensorDeviceClass.ENUM,
        options=[key for _, key in STORM_LEVELS],
        value_fn=lambda data: data.get("level_key"),
        attrs_fn=None,
    ),
    XrasSensorDescription(
        key="kp_max_today",
        icon="mdi:calendar-today",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("kp_max_today"),
        attrs_fn=_daily_attrs,
    ),
    XrasSensorDescription(
        key="kp_max_month",
        icon="mdi:calendar-month",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("kp_max_month"),
        attrs_fn=_month_attrs,
    ),
    XrasSensorDescription(
        key="storm_days_month",
        icon="mdi:counter",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("storm_days_month"),
    ),
    XrasSensorDescription(
        key="kp_max_24h",
        icon="mdi:chart-line",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("kp_max_24h"),
    ),
    XrasSensorDescription(
        key="kp_forecast_max_24h",
        icon="mdi:chart-timeline-variant",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("kp_forecast_max_24h"),
        attrs_fn=lambda data: {"level_key": data.get("forecast_level_key")},
    ),
    XrasSensorDescription(
        key="kp_forecast_max_3d",
        icon="mdi:chart-timeline-variant-shimmer",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("kp_forecast_max_3d"),
    ),
    XrasSensorDescription(
        key="ap",
        icon="mdi:sine-wave",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.get("ap"),
        attrs_fn=lambda data: {"forecast": data.get("ap_forecast")},
    ),
    XrasSensorDescription(
        key="f10",
        icon="mdi:radio-tower",
        native_unit_of_measurement="s.f.u.",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.get("f10"),
        attrs_fn=lambda data: {"forecast": data.get("f10_forecast")},
    ),
    XrasSensorDescription(
        key="sn",
        icon="mdi:white-balance-sunny",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get("sn"),
    ),
    XrasSensorDescription(
        key="updated_at",
        icon="mdi:clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get("updated_at"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XrasCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        XrasSensor(coordinator, entry, description) for description in SENSORS
    )


class XrasSensor(XrasEntity, SensorEntity):
    """Сенсор на основе данных XRAS."""

    entity_description: XrasSensorDescription

    def __init__(
        self,
        coordinator: XrasCoordinator,
        entry: ConfigEntry,
        description: XrasSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict | None:
        if not self.coordinator.data or not self.entity_description.attrs_fn:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
