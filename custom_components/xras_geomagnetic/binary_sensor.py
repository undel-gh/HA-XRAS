"""Бинарные сенсоры: идёт ли буря и ожидается ли она."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, STORM_THRESHOLD
from .coordinator import XrasCoordinator
from .entity import XrasEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XrasCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            XrasStormNow(coordinator, entry),
            XrasStormExpected(coordinator, entry),
        ]
    )


class _Base(XrasEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.SAFETY


class XrasStormNow(_Base):
    """Kp >= 5 в текущем трёхчасовом интервале."""

    _attr_icon = "mdi:magnet-on"

    def __init__(self, coordinator: XrasCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "storm")

    @property
    def is_on(self) -> bool | None:
        kp = (self.coordinator.data or {}).get("kp_current")
        return None if kp is None else kp >= STORM_THRESHOLD

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {"kp": data.get("kp_current"), "level_key": data.get("level_key")}


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
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "kp_forecast_max_24h": data.get("kp_forecast_max_24h"),
            "level_key": data.get("forecast_level_key"),
        }
