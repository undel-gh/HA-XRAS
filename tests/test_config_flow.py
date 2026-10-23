"""Tests for XRAS config and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xras_geomagnetic.config_flow import (
    CannotConnect,
    RegionNotFound,
    _code_from_region,
    _validate,
)
from custom_components.xras_geomagnetic.const import (
    BASE_URL,
    CONF_CODE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)


@pytest.mark.asyncio
async def test_user_flow_creates_normalized_entry(hass) -> None:
    with patch(
        "custom_components.xras_geomagnetic.config_flow._resolve",
        new=AsyncMock(return_value=("RAL5", "Moscow (UTC+03)")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CODE: "Москва", CONF_SCAN_INTERVAL: 600},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "XRAS — RAL5"
    assert result["data"] == {CONF_CODE: "RAL5", CONF_SCAN_INTERVAL: 600}
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "RAL5"


@pytest.mark.asyncio
async def test_user_flow_preserves_entered_values_on_invalid_region(hass) -> None:
    with patch(
        "custom_components.xras_geomagnetic.config_flow._resolve",
        new=AsyncMock(side_effect=RegionNotFound("bad-region")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CODE: "bad-region", CONF_SCAN_INTERVAL: 777},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_region"}
    defaults = result["data_schema"]({})
    assert defaults[CONF_CODE] == "bad-region"
    assert defaults[CONF_SCAN_INTERVAL] == 777


@pytest.mark.asyncio
async def test_options_only_expose_polling_interval(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="XRAS — RAL5",
        unique_id="RAL5",
        version=3,
        data={CONF_CODE: "RAL5", CONF_SCAN_INTERVAL: 600},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = result["data_schema"]({})

    assert result["type"] is FlowResultType.FORM
    assert defaults == {CONF_SCAN_INTERVAL: 600}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 900}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCAN_INTERVAL: 900}


@pytest.mark.asyncio
async def test_region_redirect_is_not_followed(hass, aioclient_mock) -> None:
    url = "https://xras.ru/magnetic_storms.html/does_not_exist/"
    aioclient_mock.get(url, status=302, headers={"Location": "https://xras.ru/"})

    with pytest.raises(RegionNotFound):
        await _code_from_region(hass, "does_not_exist")


@pytest.mark.asyncio
async def test_region_page_must_have_single_distinct_api_code(
    hass, aioclient_mock
) -> None:
    url = "https://xras.ru/magnetic_storms.html/test_region/"
    aioclient_mock.get(
        url,
        text=(
            '<a href="/txt/kp_RAL5.json">JSON</a>'
            '<a href="/txt/kpf_RIK0.json">forecast</a>'
        ),
    )

    with pytest.raises(RegionNotFound):
        await _code_from_region(hass, "test_region")


@pytest.mark.asyncio
async def test_validate_does_not_hide_programming_errors(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/kp_RAL5.json", text='{"data": []}')

    with patch(
        "custom_components.xras_geomagnetic.config_flow.parse_json",
        side_effect=RuntimeError("parser bug"),
    ), pytest.raises(RuntimeError, match="parser bug"):
        await _validate(hass, "RAL5")


@pytest.mark.asyncio
async def test_validate_preserves_expected_failure_as_cause(
    hass, aioclient_mock
) -> None:
    aioclient_mock.get(f"{BASE_URL}/kp_RAL5.json", status=500)
    aioclient_mock.get(f"{BASE_URL}/kp_RAL5.txt", status=500)

    with pytest.raises(CannotConnect) as exc_info:
        await _validate(hass, "RAL5")

    assert exc_info.value.__cause__ is not None
