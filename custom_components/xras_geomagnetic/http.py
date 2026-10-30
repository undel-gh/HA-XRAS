"""Bounded HTTP response helpers for XRAS."""

from __future__ import annotations

import aiohttp

# XRAS API payloads are normally only a few KiB. Keep generous ceilings while
# preventing an upstream/proxy error from making Home Assistant buffer unbounded data.
MAX_API_RESPONSE_BYTES = 256 * 1024
MAX_REGION_RESPONSE_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class XrasResponseTooLarge(Exception):
    """An XRAS response exceeded the configured safety limit."""


async def read_utf8_limited(
    response: aiohttp.ClientResponse, *, limit: int
) -> str:
    """Read and decode a response without buffering more than limit + 1 bytes."""
    content_length = getattr(response, "content_length", None)
    if content_length is not None and content_length > limit:
        raise XrasResponseTooLarge(
            f"XRAS response exceeds {limit} bytes (Content-Length: {content_length})"
        )

    body = bytearray()
    while True:
        remaining = limit + 1 - len(body)
        chunk = await response.content.read(min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            return body.decode("utf-8")

        body.extend(chunk)
        if len(body) > limit:
            raise XrasResponseTooLarge(f"XRAS response exceeds {limit} bytes")
