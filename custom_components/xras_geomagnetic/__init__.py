"""Интеграция XRAS Geomagnetic для Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_CODE, DOMAIN, REGION_CODE_RE
from .coordinator import XrasCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Перенести старые записи на неизменяемый код региона в data."""
    if entry.version >= 3:
        return True

    options = dict(entry.options)
    effective_code = str(
        options.pop(CONF_CODE, entry.data.get(CONF_CODE, ""))
    ).strip().upper()
    if not effective_code:
        _LOGGER.error(
            "Cannot migrate XRAS entry %s without a region code", entry.entry_id
        )
        return False
    if REGION_CODE_RE.fullmatch(effective_code) is None:
        _LOGGER.error(
            "Cannot migrate XRAS entry %s with invalid region code %r",
            entry.entry_id,
            effective_code,
        )
        return False

    duplicate = next(
        (
            other
            for other in hass.config_entries.async_entries(DOMAIN)
            if other.entry_id != entry.entry_id
            and str(other.unique_id or "").upper() == effective_code
        ),
        None,
    )
    if duplicate is not None:
        _LOGGER.error(
            "Cannot migrate XRAS entry %s to %s: config entry %s already uses "
            "that region",
            entry.entry_id,
            effective_code,
            duplicate.entry_id,
        )
        return False

    data = {**entry.data, CONF_CODE: effective_code}
    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        unique_id=effective_code,
        title=f"XRAS — {effective_code}",
        version=3,
    )
    _LOGGER.info("Migrated XRAS config entry %s to version 3", entry.entry_id)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = XrasCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
