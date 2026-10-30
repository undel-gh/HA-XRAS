"""XRAS region catalog loading, caching, and lookup."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from time import monotonic

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, REGION_CODE_RE, REQUEST_TIMEOUT, USER_AGENT
from .http import XrasResponseTooLarge, read_utf8_limited

_LOGGER = logging.getLogger(__name__)

REGION_INDEX_URL = "https://xras.ru/js/regions"
REGION_INDEX_CACHE_TTL = 3600
_MAX_REGION_INDEX_BYTES = 128 * 1024
_REGION_INDEX_MARKER_RE = re.compile(r"\bvar\s+reglist\s*=")
_REGION_SLUG_RE = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")
_NORMALIZE_RE = re.compile(r"[a-z0-9а-я]+")
_CACHE_KEY = f"{DOMAIN}_region_index_cache"


class RegionIndexError(Exception):
    """The XRAS region index could not be loaded or parsed."""


@dataclass(frozen=True, slots=True)
class Region:
    """One XRAS region-index row."""

    code: str
    name: str
    slug: str
    group: str


class RegionIndex:
    """Validated XRAS region index with normalized name/slug lookup."""

    def __init__(self, regions: tuple[Region, ...]) -> None:
        self.regions = regions
        lookup: dict[str, list[Region]] = {}
        for region in regions:
            for value in (region.name, region.slug):
                key = normalize_region_query(value)
                if not key:
                    continue
                bucket = lookup.setdefault(key, [])
                if region not in bucket:
                    bucket.append(region)
        self._lookup = {key: tuple(value) for key, value in lookup.items()}

    def find(self, value: str) -> tuple[Region, ...]:
        """Return all regions whose localized name or slug matches value."""
        return self._lookup.get(normalize_region_query(value), ())


@dataclass(slots=True)
class _RegionIndexCache:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    index: RegionIndex | None = None
    fetched_at: float = 0.0


def normalize_region_query(value: str) -> str:
    """Normalize names/slugs while preserving meaningful alphanumeric tokens."""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(_NORMALIZE_RE.findall(value))


def parse_region_index(text: str) -> RegionIndex:
    """Parse and validate the JSON array embedded in XRAS /js/regions."""
    marker = _REGION_INDEX_MARKER_RE.search(text)
    if marker is None:
        raise RegionIndexError("XRAS region index does not contain reglist")

    payload = text[marker.end() :].lstrip()
    try:
        rows, _end = json.JSONDecoder().raw_decode(payload)
    except json.JSONDecodeError as err:
        raise RegionIndexError(
            "XRAS region index contains invalid reglist JSON"
        ) from err

    if not isinstance(rows, list) or not rows:
        raise RegionIndexError("XRAS region index reglist is empty or invalid")

    regions: list[Region] = []
    codes: set[str] = set()
    slugs: set[str] = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 4:
            raise RegionIndexError("XRAS region index contains an invalid row")
        code, name, slug, group = row
        if not all(isinstance(item, str) for item in row):
            raise RegionIndexError("XRAS region index row contains non-string fields")

        code = code.upper()
        name = name.strip()
        slug = slug.strip().lower()
        group = group.strip().lower()
        if not REGION_CODE_RE.fullmatch(code):
            raise RegionIndexError(f"XRAS region index contains invalid code {code!r}")
        if not name:
            raise RegionIndexError(f"XRAS region {code} has an empty name")
        if not _REGION_SLUG_RE.fullmatch(slug):
            raise RegionIndexError(f"XRAS region {code} has invalid slug {slug!r}")
        if not group:
            raise RegionIndexError(f"XRAS region {code} has an empty group")
        if code in codes:
            raise RegionIndexError(f"XRAS region index contains duplicate code {code}")
        if slug in slugs:
            raise RegionIndexError(f"XRAS region index contains duplicate slug {slug}")

        codes.add(code)
        slugs.add(slug)
        regions.append(Region(code=code, name=name, slug=slug, group=group))

    return RegionIndex(tuple(regions))


async def _async_fetch_region_index(hass: HomeAssistant) -> RegionIndex:
    """Download and parse the current XRAS region index."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            REGION_INDEX_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as response:
            response.raise_for_status()
            text = await read_utf8_limited(response, limit=_MAX_REGION_INDEX_BYTES)
        return parse_region_index(text)
    except (
        aiohttp.ClientError,
        TimeoutError,
        UnicodeDecodeError,
        XrasResponseTooLarge,
    ) as err:
        raise RegionIndexError("could not load XRAS region index") from err


async def async_get_region_index(hass: HomeAssistant) -> RegionIndex:
    """Return the XRAS region index, cached for one hour with stale fallback."""
    state = hass.data.get(_CACHE_KEY)
    if not isinstance(state, _RegionIndexCache):
        state = _RegionIndexCache()
        hass.data[_CACHE_KEY] = state

    now = monotonic()
    if state.index is not None and now - state.fetched_at < REGION_INDEX_CACHE_TTL:
        return state.index

    async with state.lock:
        now = monotonic()
        if state.index is not None and now - state.fetched_at < REGION_INDEX_CACHE_TTL:
            return state.index
        try:
            index = await _async_fetch_region_index(hass)
        except RegionIndexError:
            if state.index is not None:
                _LOGGER.debug(
                    "XRAS region-index refresh failed; using stale cached index",
                    exc_info=True,
                )
                return state.index
            raise

        state.index = index
        state.fetched_at = monotonic()
        return index
