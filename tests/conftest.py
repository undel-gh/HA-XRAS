"""Shared pytest fixtures for the XRAS integration."""

from __future__ import annotations

from importlib.util import find_spec

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest):
    """Enable custom integrations only for the pre-2026.11 test harness.

    pytest-homeassistant enables custom integrations by default, whereas
    pytest-homeassistant-custom-component requires the explicit fixture.
    """
    if find_spec("pytest_homeassistant") is not None:
        return
    if "recorder_mock" in request.fixturenames:
        request.getfixturevalue("recorder_mock")
    request.getfixturevalue("enable_custom_integrations")
