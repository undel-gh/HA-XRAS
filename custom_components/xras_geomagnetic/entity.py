"""Общая база для сущностей всех платформ."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import XrasCoordinator


def build_device_info(coordinator: XrasCoordinator, entry: ConfigEntry) -> DeviceInfo:
    """Одно устройство на запись конфигурации."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"XRAS {coordinator.code}",
        manufacturer="ИКИ РАН, Лаборатория солнечной астрономии",
        model="xras.ru",
        configuration_url="https://xras.ru/magnetic_storms.html",
    )


class XrasEntity(CoordinatorEntity[XrasCoordinator]):
    """Общий предок: устройство, уникальный ID и имя из переводов."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: XrasCoordinator, entry: ConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = build_device_info(coordinator, entry)
