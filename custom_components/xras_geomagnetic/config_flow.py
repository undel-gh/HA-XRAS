"""Настройка интеграции через интерфейс."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BASE_URL,
    CONF_CODE,
    CONF_SCAN_INTERVAL,
    DEFAULT_CODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    REGION_CODE_PATTERN,
    REGION_CODE_RE,
    REQUEST_TIMEOUT,
    USER_AGENT,
)
from .http import (
    MAX_API_RESPONSE_BYTES,
    MAX_REGION_RESPONSE_BYTES,
    XrasResponseTooLarge,
    read_utf8_limited,
)
from .parser import XrasParseError, parse_json, parse_txt

_LOGGER = logging.getLogger(__name__)

# Ссылки вида /txt/kp_RAL5.json или /txt/kpm_RAL5_202506.json.
_CODE_IN_PAGE = re.compile(
    rf"/txt/(?:kpfl|kpf|kpm|kp)_({REGION_CODE_PATTERN})(?:_\d{{6}})?\.(?:json|txt)"
)

# Русские названия -> слаг страницы региона на сайте.
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
    "норильск": "norilsk",
    "ташкент": "tashkent",
}


class CannotConnect(Exception):
    """Источник недоступен или данные не удалось разобрать."""


class RegionNotFound(Exception):
    """Регион не найден по введённому названию или слагу."""


class _CodeNotFound(Exception):
    """Прямой код API отсутствует и значение можно попробовать как регион."""


def _user_schema(code: str, interval: int) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CODE, default=code): str,
            vol.Required(CONF_SCAN_INTERVAL, default=interval): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
            ),
        }
    )


def _options_schema(interval: int) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SCAN_INTERVAL, default=interval): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
            )
        }
    )


async def _validate(hass, code: str) -> str:
    """Проверить, что файл kp_<code> существует и разбирается."""
    code = code.strip().upper()
    session = async_get_clientsession(hass)
    headers = {"User-Agent": USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    last: Exception | None = None
    not_found = 0

    for suffix in ("json", "txt"):
        url = f"{BASE_URL}/kp_{code}.{suffix}"
        try:
            async with session.get(url, headers=headers, timeout=timeout) as response:
                response.raise_for_status()
                raw = await read_utf8_limited(
                    response, limit=MAX_API_RESPONSE_BYTES
                )
            dataset = (
                parse_json(json.loads(raw), code)
                if suffix == "json"
                else parse_txt(raw, code)
            )
            return dataset.tzone or code
        except aiohttp.ClientResponseError as err:
            last = err
            if err.status == 404:
                not_found += 1
            _LOGGER.debug("XRAS validation failed for %s", url, exc_info=True)
        except (
            aiohttp.ClientError,
            TimeoutError,
            UnicodeDecodeError,
            XrasResponseTooLarge,
            json.JSONDecodeError,
            XrasParseError,
        ) as err:
            last = err
            _LOGGER.debug("XRAS validation failed for %s", url, exc_info=True)

    if not_found == 2:
        raise _CodeNotFound(code)
    if last is None:
        raise CannotConnect(f"не удалось проверить код {code}")
    raise CannotConnect(f"не удалось проверить код {code}: {last}") from last


def _slug(value: str) -> str:
    """«Санкт-Петербург» или «Saint Petersburg» -> saint_petersburg."""
    text = value.strip().lower()
    if text in _ALIASES:
        return _ALIASES[text]
    return re.sub(r"\s+", "_", text)


async def _code_from_region(hass, value: str) -> str:
    """Достать код из страницы региона по ссылке «API: JSON»."""
    slug = _slug(value)
    if not re.fullmatch(r"[a-z0-9_\-]+", slug):
        raise RegionNotFound(value)

    session = async_get_clientsession(hass)
    url = f"https://xras.ru/magnetic_storms.html/{slug}/"
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            allow_redirects=False,
        ) as response:
            if 300 <= response.status < 400 or response.status == 404:
                raise RegionNotFound(value)
            response.raise_for_status()
            page = await read_utf8_limited(
                response, limit=MAX_REGION_RESPONSE_BYTES
            )
    except RegionNotFound:
        raise
    except (
        aiohttp.ClientError,
        TimeoutError,
        UnicodeDecodeError,
        XrasResponseTooLarge,
    ) as err:
        _LOGGER.debug("XRAS region lookup failed for %s", url, exc_info=True)
        raise CannotConnect(f"не удалось открыть страницу региона {value!r}") from err

    codes = {code.upper() for code in _CODE_IN_PAGE.findall(page)}
    if len(codes) != 1:
        _LOGGER.debug(
            "XRAS region page %s returned %d distinct API codes: %s",
            url,
            len(codes),
            sorted(codes),
        )
        raise RegionNotFound(value)
    return codes.pop()


async def _resolve(hass, value: str) -> tuple[str, str]:
    """Вернуть (код, часовая зона). Принимает и код, и название региона."""
    value = value.strip()

    # Кириллица, пробелы и URL-слаги не являются кодами XRAS, поэтому не делаем
    # перед поиском региона два заведомо лишних запроса к kp_<value>.*.
    if REGION_CODE_RE.fullmatch(value):
        code = value.upper()
        try:
            return code, await _validate(hass, code)
        except _CodeNotFound:
            # ASCII-имя вроде "moscow" выглядит как код, но может быть slug.
            pass

    code = await _code_from_region(hass, value)
    try:
        return code, await _validate(hass, code)
    except _CodeNotFound as err:
        # Страница региона сослалась на отсутствующий API-код: это уже сбой
        # источника, а не пользовательская ошибка написания региона.
        raise CannotConnect(
            f"регион {value!r} ссылается на недоступный код {code}"
        ) from err


class XrasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Первичная настройка."""

    VERSION = 3

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        code_value = DEFAULT_CODE
        interval_value = DEFAULT_SCAN_INTERVAL

        if user_input is not None:
            code_value = str(user_input[CONF_CODE])
            interval_value = int(user_input[CONF_SCAN_INTERVAL])
            try:
                code, _tzone = await _resolve(self.hass, code_value)
            except RegionNotFound:
                errors["base"] = "invalid_region"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(code)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"XRAS — {code}",
                    data={
                        CONF_CODE: code,
                        CONF_SCAN_INTERVAL: interval_value,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(code_value, interval_value),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return XrasOptionsFlow()


class XrasOptionsFlow(OptionsFlow):
    """Изменение настроек после установки."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )

        interval = int(
            self.config_entry.options.get(
                CONF_SCAN_INTERVAL,
                self.config_entry.data.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                ),
            )
        )
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(interval),
        )
