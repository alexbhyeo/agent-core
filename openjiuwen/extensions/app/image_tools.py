# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Image-fetching tool for the ReAct agent.

``fetch_page_image``'s return value feeds ``image_url`` into ``show_card``/
``show_info_list`` (see ``tools.py``) -- see that module's docstring for how
a tool's return value reaches the WebSocket layer.
"""

import asyncio
from typing import Any, Optional
from urllib.parse import urljoin

from openjiuwen.core.foundation.tool import tool
from openjiuwen.harness.tools.web import _http
from openjiuwen.harness.tools.web._common import _REQUEST_HEADERS, _parse_html
from openjiuwen.harness.tools.web._decode import _decode_response_text

MAX_IMAGE_FETCH_ATTEMPTS = 3

_BAD_IMAGE_SRC_HINTS = ("logo", "icon", "sprite", "avatar", "pixel", "spacer", "1x1", "blank.gif")


class _RetryableFetchError(Exception):
    """A page fetch that may succeed on a fresh attempt (network hiccup, 5xx, timeout)."""


async def _fetch_page_once(url: str) -> Optional[str]:
    """One fetch+parse attempt. Raises ``_RetryableFetchError`` for failures worth
    retrying; returns ``None`` (not an error) when the page loaded fine but simply
    has no usable image -- retrying an identical successful fetch would not change
    that, so callers should not retry in that case.
    """
    try:
        async with _http.new_session() as session:
            status, headers, body, final_url, _truncated = await _http.request(
                session, "GET", url, headers=_REQUEST_HEADERS, timeout_seconds=15, max_bytes=3_000_000
            )
    except Exception as exc:  # noqa: BLE001 -- network/timeout errors are retryable
        raise _RetryableFetchError(str(exc)) from exc
    if status >= 500:
        raise _RetryableFetchError(f"HTTP {status}")
    if status >= 400:
        return None

    html = _decode_response_text(body, content_type=headers.get("Content-Type", ""))
    soup = _parse_html(html)

    for selector in ('meta[property="og:image"]', 'meta[property="og:image:url"]', 'meta[name="twitter:image"]'):
        tag = soup.select_one(selector)
        content = tag.get("content") if tag else None
        if content:
            return urljoin(final_url, content.strip())

    for img in soup.select("img"):
        src = img.get("src") or img.get("data-src")
        if not src or src.startswith("data:"):
            continue
        if any(hint in src.lower() for hint in _BAD_IMAGE_SRC_HINTS):
            continue
        return urljoin(final_url, src)
    return None


async def _extract_page_image(url: str) -> Optional[str]:
    """Fetch ``url`` and pull out its Open Graph image, or failing that the
    first plausible content <img> -- reuses the same HTTP transport and HTML
    parser as ``fetch_webpage`` rather than duplicating that logic, but keeps
    the raw markup (fetch_webpage strips it entirely for text extraction, so
    it can never recover a real image URL).

    Retries transient failures (network errors, timeouts, 5xx) up to
    ``MAX_IMAGE_FETCH_ATTEMPTS`` times with a short backoff between attempts,
    so one flaky request doesn't cost the user an image they could have had.
    """
    last_error: Optional[_RetryableFetchError] = None
    for attempt in range(1, MAX_IMAGE_FETCH_ATTEMPTS + 1):
        try:
            return await _fetch_page_once(url)
        except _RetryableFetchError as exc:
            last_error = exc
            if attempt < MAX_IMAGE_FETCH_ATTEMPTS:
                await asyncio.sleep(0.5 * attempt)
    raise last_error  # exhausted all attempts


@tool(
    description=(
        "Fetch a webpage and return the URL of its main/representative image "
        "(its Open Graph image, or the first substantial <img> on the page) -- "
        "use this instead of guessing an image URL from memory when you want "
        "to show a real image in `show_card` or a `show_info_list` item. Pass "
        "a page you already know is relevant (e.g. the Wikipedia article or a "
        "recipe page for that specific dish/topic). Returns `image_url: null` "
        "if no usable image was found on the page -- in that case, don't "
        "invent one, just leave the image out."
    )
)
async def fetch_page_image(url: str) -> dict[str, Any]:
    try:
        image_url = await _extract_page_image(url)
    except Exception as exc:  # noqa: BLE001 -- report the failure, don't crash the tool call
        return {"url": url, "image_url": None, "error": str(exc)}
    return {"url": url, "image_url": image_url}
