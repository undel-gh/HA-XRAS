"""Tests for deterministic and bounded XRAS HTTP response handling."""

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
from custom_components.xras_geomagnetic.http import (
    MAX_API_RESPONSE_BYTES,
    MAX_REGION_RESPONSE_BYTES,
    XrasResponseTooLarge,
    read_utf8_limited,
)


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.offset = 0
        self.max_requested = 0

    async def read(self, size: int = -1) -> bytes:
        self.max_requested = max(self.max_requested, size)
        if self.offset >= len(self._body):
            return b""
        end = len(self._body) if size < 0 else min(len(self._body), self.offset + size)
        chunk = self._body[self.offset : end]
        self.offset = end
        return chunk


class _FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        body: bytes | None = None,
        status: int = 200,
        content_length: int | None = None,
    ) -> None:
        self.status = status
        self.content = _FakeContent(body if body is not None else text.encode("utf-8"))
        self.content_length = content_length

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        return None


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
async def test_limited_reader_accepts_exact_limit() -> None:
    response = _FakeResponse(body=b"abcdefgh")

    assert await read_utf8_limited(response, limit=8) == "abcdefgh"
    assert response.content.offset == 8


@pytest.mark.asyncio
async def test_limited_reader_stops_after_limit_plus_one_without_length() -> None:
    response = _FakeResponse(body=b"abcdefghij")

    with pytest.raises(XrasResponseTooLarge, match="exceeds 8 bytes"):
        await read_utf8_limited(response, limit=8)

    assert response.content.offset == 9
    assert response.content.max_requested <= 9


@pytest.mark.asyncio
async def test_limited_reader_rejects_large_content_length_before_reading() -> None:
    response = _FakeResponse(body=b"ignored", content_length=9)

    with pytest.raises(XrasResponseTooLarge, match="Content-Length: 9"):
        await read_utf8_limited(response, limit=8)

    assert response.content.offset == 0


@pytest.mark.asyncio
async def test_coordinator_decode_error_falls_back_to_unicode_txt(hass) -> None:
    bad_json = _FakeResponse(body=b"\xff")
    good_txt = _FakeResponse(text=_unicode_txt())
    session = _FakeSession(bad_json, good_txt)

    with patch(
        "custom_components.xras_geomagnetic.coordinator.async_get_clientsession",
        return_value=session,
    ):
        dataset = await XrasCoordinator(hass, _entry())._fetch("kp")

    assert dataset.tzone == "Москва (UTC+03)"
    assert dataset.source == "НОАА"


@pytest.mark.asyncio
async def test_coordinator_oversized_json_falls_back_to_txt(hass) -> None:
    oversized_json = _FakeResponse(content_length=MAX_API_RESPONSE_BYTES + 1)
    good_txt = _FakeResponse(text=_unicode_txt())
    session = _FakeSession(oversized_json, good_txt)

    with patch(
        "custom_components.xras_geomagnetic.coordinator.async_get_clientsession",
        return_value=session,
    ):
        dataset = await XrasCoordinator(hass, _entry())._fetch("kp")

    assert dataset.tzone == "Москва (UTC+03)"
    assert oversized_json.content.offset == 0


@pytest.mark.asyncio
async def test_validate_decode_error_falls_back_to_txt(hass) -> None:
    bad_json = _FakeResponse(body=b"\xff")
    good_txt = _FakeResponse(text=_unicode_txt())
    session = _FakeSession(bad_json, good_txt)

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_clientsession",
        return_value=session,
    ):
        assert await _validate(hass, "RAL5") == "Москва (UTC+03)"


@pytest.mark.asyncio
async def test_validate_oversized_responses_become_cannot_connect(hass) -> None:
    oversized_json = _FakeResponse(content_length=MAX_API_RESPONSE_BYTES + 1)
    oversized_txt = _FakeResponse(content_length=MAX_API_RESPONSE_BYTES + 1)
    session = _FakeSession(oversized_json, oversized_txt)

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_clientsession",
        return_value=session,
    ), pytest.raises(CannotConnect) as exc_info:
        await _validate(hass, "RAL5")

    assert isinstance(exc_info.value.__cause__, XrasResponseTooLarge)
    assert oversized_json.content.offset == 0
    assert oversized_txt.content.offset == 0


@pytest.mark.asyncio
async def test_region_page_decode_error_becomes_cannot_connect(hass) -> None:
    bad_page = _FakeResponse(body=b"\xff")
    session = _FakeSession(bad_page)

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_clientsession",
        return_value=session,
    ), pytest.raises(CannotConnect) as exc_info:
        await _code_from_region(hass, "moscow")

    assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)


@pytest.mark.asyncio
async def test_region_page_oversized_becomes_cannot_connect(hass) -> None:
    oversized_page = _FakeResponse(content_length=MAX_REGION_RESPONSE_BYTES + 1)
    session = _FakeSession(oversized_page)

    with patch(
        "custom_components.xras_geomagnetic.config_flow.async_get_clientsession",
        return_value=session,
    ), pytest.raises(CannotConnect) as exc_info:
        await _code_from_region(hass, "moscow")

    assert isinstance(exc_info.value.__cause__, XrasResponseTooLarge)
    assert oversized_page.content.offset == 0
