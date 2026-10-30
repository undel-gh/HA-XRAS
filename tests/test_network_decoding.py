"""Tests for deterministic UTF-8 handling on XRAS HTTP responses."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xras_geomagnetic.config_flow import (
    CannotConnect,
    _code_from_region,
    _validate,
)
from custom_components.xras_geomagnetic.const import (
    CONF_CODE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.xras_geomagnetic.coordinator import XrasCoordinator


def _decode_error() -> UnicodeDecodeError:
    return UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


class _FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        error: Exception | None = None,
        status: int = 200,
    ) -> None:
        self._text = text
        self._error = error
        self.status = status
        self.encodings: list[str | None] = []

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def text(self, encoding: str | None = None) -> str:
        self.encodings.append(encoding)
        if self._error is not None:
            raise self._error
        return self._text


class _FakeSession:
    def __init__(self, *responses: _FakeResponse) -> None:
        self._responses: Iterator[_FakeResponse] = iter(responses)

    def get(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        return next(self._responses)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="XRAS — RAL5",
        unique_id="RAL5",
        version=3,
        data={CONF_CODE: "RAL5", CONF_SCAN_INTERVAL: 600},
    )


def _unicode_txt() -> str:
    return "\n".join(
        (
            "# type: kp",
            "# version: 1.1",
            "# tzone: Москва (UTC+03)",
            "# source: НОАА",
            "# [0] time, [1] f10, [2] sn, [3] ap, [4] k, [5] max_kp",
            "2026-10-30 | 100 | 20 | 5 | 1 | 0",
        )
    )


@pytest.mark.asyncio
async def test_coordinator_decode_error_falls_back_to_unicode_txt(hass) -> None:
    bad_json = _FakeResponse(error=_decode_error())
    good_txt = _FakeResponse(text=_unicode_txt())
    session = _FakeSession(bad_json, good_txt)

    with patch(
        "custom_components.xras_geomagnetic.coordinator.async_get_clientsession",
        return_value=session,
    ):
        dataset = await XrasCoordinator(hass, _entry())._fetch("kp")

    assert dataset.tzone == "Москва (UTC+03)"
    assert dataset.source == "НОАА"
    assert bad_json.encodings == ["utf-8"]
    assert good_txt.encodings == ["utf-8"]


@pytest.mark.asyncio
async def test_validate_decode_error_falls_back_to_txt(hass) -> None:
    bad_json = _FakeResponse(error=_decode_error())
    good_txt = _FakeResponse(text=_unicode_txt())
    session = _FakeSession(bad_json, good_txt)

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_clientsession",
        return_value=session,
    ):
        assert await _validate(hass, "RAL5") == "Москва (UTC+03)"

    assert bad_json.encodings == ["utf-8"]
    assert good_txt.encodings == ["utf-8"]


@pytest.mark.asyncio
async def test_region_page_decode_error_becomes_cannot_connect(hass) -> None:
    bad_page = _FakeResponse(error=_decode_error())
    session = _FakeSession(bad_page)

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_clientsession",
        return_value=session,
    ), pytest.raises(CannotConnect) as exc_info:
        await _code_from_region(hass, "moscow")

    assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)
    assert bad_page.encodings == ["utf-8"]
