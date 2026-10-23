"""Diagnostics support for XRAS Geomagnetic."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import XrasCoordinator


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return non-sensitive diagnostics for one XRAS config entry."""
    coordinator: XrasCoordinator = entry.runtime_data
    data = coordinator.data or {}
    return {
        "code": coordinator.code,
        "polling_interval_seconds": int(coordinator.update_interval.total_seconds()),
        "last_update_success": coordinator.last_update_success,
        "timezone": data.get("tzone"),
        "source": data.get("source"),
        "dataset_available": data.get("dataset_available", {}),
        "kp_current_timestamp": _iso(data.get("kp_current_ts")),
        "kp_current_valid_until": _iso(data.get("kp_current_valid_until")),
        "updated_at": _iso(data.get("updated_at")),
    }
