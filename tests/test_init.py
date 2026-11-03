"""Tests for config-entry setup and migration."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xras_geomagnetic import async_migrate_entry, async_setup_entry
from custom_components.xras_geomagnetic.const import (
    CONF_CODE,
    CONF_HISTORY_MONTHS,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.xras_geomagnetic.history import XrasHistoryBackfill


@pytest.mark.usefixtures("recorder_mock")
@pytest.mark.asyncio
async def test_setup_entry_wires_history_lifecycle(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="XRAS — RAL5",
        unique_id="RAL5",
        version=3,
        data={CONF_CODE: "RAL5", CONF_SCAN_INTERVAL: 600},
        options={CONF_HISTORY_MONTHS: 1},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.xras_geomagnetic.XrasCoordinator."
        "async_config_entry_first_refresh",
        new=AsyncMock(),
    ), patch.object(
        XrasHistoryBackfill,
        "_handle_update",
        autospec=True,
    ) as handle_update, patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        new=AsyncMock(),
    ) as forward_setups:
        assert await async_setup_entry(hass, entry)

        coordinator = entry.runtime_data
        history = coordinator.history
        assert isinstance(history, XrasHistoryBackfill)
        assert history.status == "idle"
        assert history._unsub is not None
        handle_update.assert_called_once_with(history)
        forward_setups.assert_awaited_once()

        await entry._async_process_on_unload(hass)

    assert history._shutdown is True
    assert history._unsub is None
    assert history._task is None
    assert history._current_task is None


@pytest.mark.asyncio
async def test_migrate_v2_options_code_to_entry_identity(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Геомагнитная обстановка — RAL5 (Moscow)",
        unique_id="RAL5",
        version=2,
        data={CONF_CODE: "RAL5", CONF_SCAN_INTERVAL: 600},
        options={CONF_CODE: "rik0", CONF_SCAN_INTERVAL: 900},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == 3
    assert entry.unique_id == "RIK0"
    assert entry.title == "XRAS — RIK0"
    assert entry.data[CONF_CODE] == "RIK0"
    assert entry.options == {CONF_SCAN_INTERVAL: 900}


@pytest.mark.asyncio
async def test_migration_rejects_duplicate_effective_region(hass) -> None:
    existing = MockConfigEntry(
        domain=DOMAIN,
        title="XRAS — RIK0",
        unique_id="RIK0",
        version=3,
        data={CONF_CODE: "RIK0", CONF_SCAN_INTERVAL: 600},
    )
    existing.add_to_hass(hass)
    legacy = MockConfigEntry(
        domain=DOMAIN,
        title="XRAS — RAL5",
        unique_id="RAL5",
        version=2,
        data={CONF_CODE: "RAL5", CONF_SCAN_INTERVAL: 600},
        options={CONF_CODE: "RIK0"},
    )
    legacy.add_to_hass(hass)

    assert not await async_migrate_entry(hass, legacy)
    assert legacy.version == 2
    assert legacy.unique_id == "RAL5"


@pytest.mark.asyncio
async def test_migration_rejects_invalid_legacy_code(hass) -> None:
    legacy = MockConfigEntry(
        domain=DOMAIN,
        title="XRAS legacy",
        unique_id=None,
        version=2,
        data={CONF_CODE: "RAL5", CONF_SCAN_INTERVAL: 600},
        options={CONF_CODE: "../../bad code"},
    )
    legacy.add_to_hass(hass)

    assert not await async_migrate_entry(hass, legacy)
    assert legacy.version == 2
    assert legacy.data[CONF_CODE] == "RAL5"
