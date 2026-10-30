"""Tests for XRAS config and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xras_geomagnetic.config_flow import (
    CannotConnect,
    RegionAmbiguous,
    RegionNotFound,
    _code_from_catalog,
    _code_from_region,
    _resolve,
    _validate,
)
from custom_components.xras_geomagnetic.const import (
    BASE_URL,
    CONF_CODE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.xras_geomagnetic.regions import (
    Region,
    RegionIndex,
    RegionIndexError,
)


def _catalog(*regions: Region) -> RegionIndex:
    return RegionIndex(tuple(regions))


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
async def test_user_flow_reports_ambiguous_region_and_preserves_input(hass) -> None:
    with patch(
        "custom_components.xras_geomagnetic.config_flow._resolve",
        new=AsyncMock(side_effect=RegionAmbiguous("Гамильтон")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CODE: "Гамильтон", CONF_SCAN_INTERVAL: 777},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "ambiguous_region"}
    defaults = result["data_schema"]({})
    assert defaults[CONF_CODE] == "Гамильтон"
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
async def test_catalog_resolves_russian_name_without_region_page(hass) -> None:
    index = _catalog(Region("PIL9", "Краснодар", "krasnodar", "ru"))
    page_lookup = AsyncMock()

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_region_index",
        new=AsyncMock(return_value=index),
    ), patch(
        "custom_components.xras_geomagnetic.config_flow._code_from_region",
        page_lookup,
    ):
        assert await _code_from_catalog(hass, "Краснодар") == "PIL9"

    page_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_resolves_slug_with_spaces(hass) -> None:
    index = _catalog(
        Region("S0KJ", "Санкт-Петербург", "saint_petersburg", "ru")
    )

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_region_index",
        new=AsyncMock(return_value=index),
    ):
        assert await _code_from_catalog(hass, "Saint Petersburg") == "S0KJ"


@pytest.mark.asyncio
async def test_catalog_ambiguous_name_requires_slug_or_code(hass) -> None:
    index = _catalog(
        Region("HD5U", "Гамильтон", "hamilton_canada", "ca"),
        Region("HRFI", "Гамильтон", "hamilton_new_zeland", "oc"),
    )

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_region_index",
        new=AsyncMock(return_value=index),
    ), pytest.raises(RegionAmbiguous):
        await _code_from_catalog(hass, "Гамильтон")

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_region_index",
        new=AsyncMock(return_value=index),
    ):
        assert await _code_from_catalog(hass, "hamilton_canada") == "HD5U"


@pytest.mark.asyncio
async def test_catalog_miss_falls_back_to_region_page(hass) -> None:
    index = _catalog(Region("RAL5", "Москва", "moscow", "ru"))
    page_lookup = AsyncMock(return_value="TEST")

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_region_index",
        new=AsyncMock(return_value=index),
    ), patch(
        "custom_components.xras_geomagnetic.config_flow._code_from_region",
        page_lookup,
    ):
        assert await _code_from_catalog(hass, "future_slug") == "TEST"

    page_lookup.assert_awaited_once_with(hass, "future_slug")


@pytest.mark.asyncio
async def test_catalog_failure_preserves_known_alias_page_fallback(hass) -> None:
    page_lookup = AsyncMock(return_value="RAL5")

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_region_index",
        new=AsyncMock(side_effect=RegionIndexError("source down")),
    ), patch(
        "custom_components.xras_geomagnetic.config_flow._code_from_region",
        page_lookup,
    ):
        assert await _code_from_catalog(hass, "Москва") == "RAL5"

    page_lookup.assert_awaited_once_with(hass, "Москва")


@pytest.mark.asyncio
async def test_catalog_failure_does_not_misreport_unknown_cyrillic_region(hass) -> None:
    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_region_index",
        new=AsyncMock(side_effect=RegionIndexError("source down")),
    ), pytest.raises(CannotConnect, match="каталог регионов"):
        await _code_from_catalog(hass, "Новый Город")


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


@pytest.mark.asyncio
async def test_direct_code_source_failure_is_not_misreported_as_invalid_region(
    hass,
) -> None:
    validate = AsyncMock(side_effect=CannotConnect("source unavailable"))
    catalog_lookup = AsyncMock()

    with patch(
        "custom_components.xras_geomagnetic.config_flow._validate", validate
    ), patch(
        "custom_components.xras_geomagnetic.config_flow._code_from_catalog",
        catalog_lookup,
    ), pytest.raises(CannotConnect, match="source unavailable"):
        await _resolve(hass, "RAL5")

    catalog_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_code_region_skips_direct_api_probe(hass) -> None:
    validate = AsyncMock(return_value="Moscow (UTC+03)")
    catalog_lookup = AsyncMock(return_value="RAL5")

    with patch(
        "custom_components.xras_geomagnetic.config_flow._validate", validate
    ), patch(
        "custom_components.xras_geomagnetic.config_flow._code_from_catalog",
        catalog_lookup,
    ):
        assert await _resolve(hass, "Москва") == ("RAL5", "Moscow (UTC+03)")

    catalog_lookup.assert_awaited_once_with(hass, "Москва")
    validate.assert_awaited_once_with(hass, "RAL5")
