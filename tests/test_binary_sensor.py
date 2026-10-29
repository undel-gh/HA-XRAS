"""Tests for XRAS binary-sensor value semantics."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xras_geomagnetic.binary_sensor import XrasStormNow
from custom_components.xras_geomagnetic.const import (
    CONF_CODE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.xras_geomagnetic.coordinator import XrasCoordinator


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="XRAS — RAL5",
        unique_id="RAL5",
        version=3,
        data={CONF_CODE: "RAL5", CONF_SCAN_INTERVAL: 600},
    )


def test_storm_now_distinguishes_zero_from_missing(hass) -> None:
    """Kp=0.0 means a valid quiet interval; None means unknown."""
    entry = _entry()
    coordinator = XrasCoordinator(hass, entry)
    entity = XrasStormNow(coordinator, entry)

    coordinator.data = {"kp_current": 0.0, "level_key": "quiet"}
    assert entity.is_on is False

    coordinator.data = {"kp_current": None, "level_key": None}
    assert entity.is_on is None
