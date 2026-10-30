"""Tests for the XRAS region catalog parser and cache."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.xras_geomagnetic.regions import (
    REGION_INDEX_CACHE_TTL,
    REGION_INDEX_URL,
    Region,
    RegionIndex,
    RegionIndexError,
    _async_fetch_region_index,
    async_get_region_index,
    normalize_region_query,
    parse_region_index,
)

_SAMPLE_INDEX = """
function ls(key) {
    var reglist = [
        ["RAL5", "Москва", "moscow", "ru"],
        ["RIK0", "Рига", "riga", "eu"],
        ["S0KJ", "Санкт-Петербург", "saint_petersburg", "ru"],
        ["PUKF", "Кишинёв", "chisinau", "ru"],
        ["HD5U", "Гамильтон", "hamilton_canada", "ca"],
        ["HRFI", "Гамильтон", "hamilton_new_zeland", "oc"]
    ];
    var geolist = {"ru": "Россия"};
}
"""


def test_parse_region_index_and_lookup_names_and_slugs() -> None:
    index = parse_region_index(_SAMPLE_INDEX)

    assert len(index.regions) == 6
    assert index.find("Москва")[0].code == "RAL5"
    assert index.find("moscow")[0].code == "RAL5"
    assert index.find("Saint Petersburg")[0].code == "S0KJ"
    assert index.find("Кишинев")[0].code == "PUKF"


def test_region_index_preserves_ambiguous_names() -> None:
    index = parse_region_index(_SAMPLE_INDEX)

    assert {region.code for region in index.find("Гамильтон")} == {"HD5U", "HRFI"}
    assert index.find("hamilton_canada")[0].code == "HD5U"
    assert index.find("hamilton_new_zeland")[0].code == "HRFI"


def test_region_query_normalization_handles_punctuation_and_case() -> None:
    assert normalize_region_query("  SAINT_Petersburg ") == "saint petersburg"
    assert normalize_region_query("Ростов-на-Дону") == "ростов на дону"
    assert normalize_region_query("КИШИНЁВ") == "кишинев"


@pytest.mark.parametrize(
    "text,match",
    [
        ("var something = [];", "does not contain reglist"),
        ("var reglist = nope;", "invalid reglist JSON"),
        ("var reglist = [];", "empty or invalid"),
        (
            'var reglist = [["RAL5", "Москва", "moscow"]];',
            "invalid row",
        ),
        (
            'var reglist = [["!", "Москва", "moscow", "ru"]];',
            "invalid code",
        ),
    ],
)
def test_parse_region_index_rejects_malformed_source(text: str, match: str) -> None:
    with pytest.raises(RegionIndexError, match=match):
        parse_region_index(text)


def test_parse_region_index_rejects_duplicate_code_or_slug() -> None:
    duplicate_code = (
        'var reglist = [["RAL5", "Москва", "moscow", "ru"],'
        '["RAL5", "Другая", "other", "ru"]];'
    )
    duplicate_slug = (
        'var reglist = [["RAL5", "Москва", "moscow", "ru"],'
        '["RIK0", "Другая", "moscow", "eu"]];'
    )

    with pytest.raises(RegionIndexError, match="duplicate code RAL5"):
        parse_region_index(duplicate_code)
    with pytest.raises(RegionIndexError, match="duplicate slug moscow"):
        parse_region_index(duplicate_slug)


@pytest.mark.asyncio
async def test_fetch_region_index_from_xras_source(hass, aioclient_mock) -> None:
    aioclient_mock.get(REGION_INDEX_URL, text=_SAMPLE_INDEX)

    index = await _async_fetch_region_index(hass)

    assert index.find("Москва")[0].code == "RAL5"
    assert index.find("riga")[0].code == "RIK0"


@pytest.mark.asyncio
async def test_fetch_region_index_wraps_http_failure(hass, aioclient_mock) -> None:
    aioclient_mock.get(REGION_INDEX_URL, status=500)

    with pytest.raises(RegionIndexError, match="could not load") as exc_info:
        await _async_fetch_region_index(hass)

    assert exc_info.value.__cause__ is not None


@pytest.mark.asyncio
async def test_region_index_is_cached_for_one_hour(hass) -> None:
    index = RegionIndex((Region("RAL5", "Москва", "moscow", "ru"),))
    fetch = AsyncMock(return_value=index)

    with patch(
        "custom_components.xras_geomagnetic.regions._async_fetch_region_index",
        fetch,
    ):
        assert await async_get_region_index(hass) is index
        assert await async_get_region_index(hass) is index

    fetch.assert_awaited_once_with(hass)


@pytest.mark.asyncio
async def test_region_index_uses_stale_cache_when_refresh_fails(hass) -> None:
    index = RegionIndex((Region("RAL5", "Москва", "moscow", "ru"),))
    initial_fetch = AsyncMock(return_value=index)

    with patch(
        "custom_components.xras_geomagnetic.regions._async_fetch_region_index",
        initial_fetch,
    ):
        assert await async_get_region_index(hass) is index

    cache = hass.data["xras_geomagnetic_region_index_cache"]
    cache.fetched_at = 0.0
    failed_fetch = AsyncMock(side_effect=RegionIndexError("source down"))
    with patch(
        "custom_components.xras_geomagnetic.regions.monotonic",
        return_value=REGION_INDEX_CACHE_TTL + 1,
    ), patch(
        "custom_components.xras_geomagnetic.regions._async_fetch_region_index",
        failed_fetch,
    ):
        assert await async_get_region_index(hass) is index

    failed_fetch.assert_awaited_once_with(hass)
