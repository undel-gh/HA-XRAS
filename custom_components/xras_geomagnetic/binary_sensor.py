"""Бинарные сенсоры: идёт ли буря и ожидается ли она."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import STORM_THRESHOLD
from .coordinator import XrasCoordinator
from .entity import XrasEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XrasCoordinator = entry.runtime_data
    async_add_entities(
        [
            XrasStormNow(coordinator, entry),
            XrasStormExpected(coordinator, entry),
        ]
    )


class _Base(XrasEntity, BinarySensorEntity):
    """Общая база бинарных сенсоров XRAS без safety-семантики HA."""


class XrasStormNow(_Base):
    """Kp >= 5 в текущем валидном трёхчасовом интервале."""

    _attr_icon = "mdi:magnet-on"

    def __init__(self, coordinator: XrasCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "storm")

    @property
    def is_on(self) -> bool | None:
        kp = (self.coordinator.data or {}).get("kp_current")
        return None if kp is None else kp >= STORM_THRESHOLD

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        attrs: dict[str, Any] = {}
        if (kp := data.get("kp_current")) is not None:
            attrs["kp"] = kp
        if (level := data.get("level_key")) is not None:
            attrs["level_key"] = level
        if (valid_until := data.get("kp_current_valid_until")) is not None:
            attrs["valid_until"] = valid_until.isoformat()
        return attrs or None


class XrasStormExpected(_Base):
    """Буря прогнозируется в ближайшие 24 часа."""

    _attr_icon = "mdi:calendar-alert"

    def __init__(self, coordinator: XrasCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "storm_forecast")

    @property
    def is_on(self) -> bool | None:
        kp = (self.coordinator.data or {}).get("kp_forecast_max_24h")
        return None if kp is None else kp >= STORM_THRESHOLD

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        attrs: dict[str, Any] = {}
        if (kp := data.get("kp_forecast_max_24h")) is not None:
            attrs["kp_forecast_max_24h"] = kp
        if (level := data.get("forecast_level_key")) is not None:
            attrs["level_key"] = level
        return attrs or None
