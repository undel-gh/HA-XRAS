"""Compatibility imports for Home Assistant's legacy and first-party test harnesses."""

from __future__ import annotations

try:
    from pytest_homeassistant.common import MockConfigEntry
except ModuleNotFoundError as err:
    # Only fall back if the first-party harness itself is absent. A missing
    # dependency *inside* that package must remain visible as a real failure.
    if err.name != "pytest_homeassistant":
        raise
    from pytest_homeassistant_custom_component.common import MockConfigEntry

__all__ = ["MockConfigEntry"]
