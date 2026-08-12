"""Настройка интеграции через интерфейс."""

from __future__ import annotations

import json
import re
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BASE_URL,
    CONF_CODE,
    CONF_SCAN_INTERVAL,
    DEFAULT_CODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    USER_AGENT,
)
from .parser import XrasParseError, parse_json, parse_txt

# Ссылки вида /txt/kp_RAL5.json или /txt/kpm_RAL5_202506.json
_CODE_IN_PAGE = re.compile(
    r"/txt/kpf?l?m?_([A-Za-z0-9]{3,8}?)(?:_\d{6})?\.(?:json|txt)"
)

# Русские названия -> слаг страницы региона на сайте
_ALIASES = {
    "москва": "moscow",
    "рига": "riga",
    "санкт-петербург": "saint_petersburg",
    "петербург": "saint_petersburg",
    "спб": "saint_petersburg",
    "минск": "minsk",
    "кишинёв": "chisinau",
    "кишинев": "chisinau",
    "ростов-на-дону": "rostov_on_don",
    "екатеринбург": "yekaterinburg",
    "красноярск": "krasnoyarsk",
    "ташкент": "tashkent",
}


def _schema(code: str, interval: int) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CODE, default=code): str,
            vol.Required(CONF_SCAN_INTERVAL, default=interval): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=86400)
            ),
        }
    )


async def _validate(hass, code: str) -> str:
    """Проверить, что файл kp_<code> существует и разбирается."""
    session = async_get_clientsession(hass)
    headers = {"User-Agent": USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=30)
    last: Exception | None = None

    for suffix in ("json", "txt"):
        url = f"{BASE_URL}/kp_{code}.{suffix}"
        try:
            async with session.get(url, headers=headers, timeout=timeout) as response:
                response.raise_for_status()
                raw = await response.text()
            dataset = (
                parse_json(json.loads(raw), code)
                if suffix == "json"
                else parse_txt(raw, code)
            )
            return dataset.tzone or code
        except Exception as err:  # noqa: BLE001
            last = err

    raise CannotConnect(str(last))


class CannotConnect(Exception):
    """Файл недоступен или не разобран."""


def _slug(value: str) -> str:
    """«Санкт-Петербург» или «Saint Petersburg» -> saint_petersburg."""
    text = value.strip().lower()
    if text in _ALIASES:
        return _ALIASES[text]
    return re.sub(r"[\s]+", "_", text)


async def _code_from_region(hass, value: str) -> str | None:
    """Достать код из страницы региона по ссылке «API: JSON»."""
    slug = _slug(value)
    if not re.fullmatch(r"[a-z0-9_\-]+", slug):
        return None

    session = async_get_clientsession(hass)
    url = f"https://xras.ru/magnetic_storms.html/{slug}/"
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            response.raise_for_status()
            page = await response.text()
    except Exception:  # noqa: BLE001
        return None

    found = _CODE_IN_PAGE.findall(page)
    if not found:
        return None
    # Самый частый код на странице — он и есть код региона
    return max(set(found), key=found.count)


async def _resolve(hass, value: str) -> tuple[str, str]:
    """Вернуть (код, часовая зона). Принимает и код, и название региона."""
    value = value.strip()

    try:
        return value, await _validate(hass, value)
    except CannotConnect:
        pass

    code = await _code_from_region(hass, value)
    if code:
        return code, await _validate(hass, code)

    raise CannotConnect(f"не удалось определить код по значению «{value}»")


class XrasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Первичная настройка."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                code, tzone = await _resolve(self.hass, str(user_input[CONF_CODE]))
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(code.upper())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Геомагнитная обстановка — {code} ({tzone})",
                    data={
                        CONF_CODE: code,
                        CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(DEFAULT_CODE, DEFAULT_SCAN_INTERVAL),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return XrasOptionsFlow()


class XrasOptionsFlow(OptionsFlow):
    """Изменение настроек после установки."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                code, _ = await _resolve(self.hass, str(user_input[CONF_CODE]))
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    data={**user_input, CONF_CODE: code}
                )

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(
                current.get(CONF_CODE, DEFAULT_CODE),
                current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ),
            errors=errors,
        )
