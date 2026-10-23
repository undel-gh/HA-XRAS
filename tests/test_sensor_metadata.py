"""Tests for Home Assistant sensor metadata."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from custom_components.xras_geomagnetic.binary_sensor import _Base
from custom_components.xras_geomagnetic.sensor import SENSORS


def test_forecasts_and_aggregates_do_not_claim_measurement_state_class() -> None:
    descriptions = {description.key: description for description in SENSORS}
    for key in (
        "kp_max_today",
        "kp_max_month",
        "storm_days_month",
        "kp_max_24h",
        "kp_forecast_max_24h",
        "kp_forecast_max_3d",
    ):
        assert descriptions[key].state_class is None


def test_geomagnetic_binary_sensors_have_no_safety_device_class() -> None:
    assert getattr(_Base, "_attr_device_class", None) is None
    assert issubclass(_Base, Entity)
