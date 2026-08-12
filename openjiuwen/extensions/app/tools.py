# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Tools available to the ReAct agent.

``show_card``'s return value (``{"text": ..., "genui": [...]}``) is how A2UI
messages reach the WebSocket layer: ``rails.A2uiToolEventRail`` captures the
raw dict from ``AFTER_TOOL_CALL`` and ``ws_session._translate`` turns the
``genui`` list into one WebSocket ``genui`` event per message.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlencode, urljoin

from pydantic import BaseModel, Field

from openjiuwen.core.foundation.tool import tool
from openjiuwen.harness.tools.web import _http
from openjiuwen.harness.tools.web._common import _REQUEST_HEADERS, _parse_html
from openjiuwen.harness.tools.web._decode import _decode_response_text

from . import config, genui

MAX_IMAGE_FETCH_ATTEMPTS = 3
MAX_VIDEO_FETCH_ATTEMPTS = 3


@tool(
    description=(
        "Get the current UTC date and time. Call this first if the user's request "
        "depends on knowing what time or day it is."
    )
)
def get_current_time() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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


# ---------------------------------------------------------------------------
# Video clips (show_video_clips)
# ---------------------------------------------------------------------------
#
# genui.video() (the client's native player) can only open a real, direct
# video file/stream URL -- it has no HTML/JS engine, so it can never open a
# YouTube/Vimeo *page*. Those sites don't expose a direct file URL through
# normal means; getting one requires extracting their signed, expiring
# internal stream URLs, which is fragile and against their Terms of Service.
# Instead, YouTube/Vimeo links resolve to that site's own `/embed/<id>` URL
# and render through genui.web() (a WebView) -- playback goes through their
# own embedded player, exactly as if you'd pasted the embed URL into a
# browser tab. That's the only path used for those sites; nothing here ever
# scrapes or reverse-engineers a raw stream URL.


@tool(
    description=(
        "Search YouTube for real videos matching a query, via the YouTube Data "
        "API (a real search API, not scraping a JS-rendered results page) -- "
        "this is the primary way to find clips for `show_video_clips` when the "
        "user wants a video/videos about something. Returns up to `max_results` "
        "real videos, each with `video_id`, `title`, `description`, and a "
        "ready-to-use `embed_url`. Pass that `embed_url` straight into "
        "`show_video_clips` as a clip's `embed_url` with `kind='youtube'` -- "
        "there's no need to call `fetch_video_source` on these results, they're "
        "already resolved. Never invent a video ID or title yourself; only use "
        "what this returns. An `error` in the response (e.g. the API key isn't "
        "configured) means don't fabricate a video for this request."
    )
)
async def search_youtube_videos(query: str, max_results: int = 5) -> dict[str, Any]:
    api_key = config.get("YOUTUBE_API_KEY")
    if not api_key:
        return {"query": query, "videos": [], "error": "YOUTUBE_API_KEY is not configured on the server."}
    max_results = max(1, min(max_results, 10))
    params = {
        "part": "snippet",
        "type": "video",
        "q": query,
        "maxResults": str(max_results),
        "safeSearch": "moderate",
        "key": api_key,
    }
    url = "https://www.googleapis.com/youtube/v3/search?" + urlencode(params)
    try:
        async with _http.new_session() as session:
            status, headers, body, _final_url, _truncated = await _http.request(
                session, "GET", url, headers=_REQUEST_HEADERS, timeout_seconds=15, max_bytes=1_000_000
            )
    except Exception as exc:  # noqa: BLE001 -- report the failure, don't crash the tool call
        return {"query": query, "videos": [], "error": str(exc)}

    text = _decode_response_text(body, content_type=headers.get("Content-Type", ""))
    if status >= 400:
        return {"query": query, "videos": [], "error": f"YouTube API returned HTTP {status}: {text[:300]}"}
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return {"query": query, "videos": [], "error": f"Could not parse YouTube API response: {exc}"}

    videos: list[dict[str, Any]] = []
    for item in data.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet", {})
        videos.append(
            {
                "video_id": video_id,
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "embed_url": _youtube_embed_url(video_id),
            }
        )
    return {"query": query, "videos": videos}


_YOUTUBE_ID_RE = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")

_VIDEO_FILE_EXTENSIONS = (".mp4", ".webm", ".mov", ".m3u8", ".ogv")


def _extract_youtube_id(url: str) -> Optional[str]:
    match = _YOUTUBE_ID_RE.search(url)
    return match.group(1) if match else None


def _youtube_embed_url(video_id: str) -> str:
    # youtube-nocookie.com (YouTube's own privacy-enhanced embed domain) has a
    # more lenient anonymous-embed policy than youtube.com/embed -- the client
    # WebView navigates straight to this URL as a top-level page (there's no
    # real hosting page/Referer for YouTube's player to validate against
    # here), which youtube.com/embed's stricter check rejects with
    # "Error 153: Video player configuration error" for most real videos.
    # rel=0 keeps end-of-video suggestions restricted to the same channel.
    return f"https://www.youtube-nocookie.com/embed/{video_id}?rel=0"


async def _fetch_page_video_once(url: str) -> Optional[str]:
    """One fetch+parse attempt for a direct video source. Mirrors
    ``_fetch_page_once``'s retry/error split: raises for failures worth
    retrying, returns ``None`` when the page loaded fine but has no direct
    <video> (true of most pages -- they embed YouTube/Vimeo instead, which
    this never returns a URL for; see ``_extract_youtube_id`` for that path).
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

    for selector in ('meta[property="og:video:secure_url"]', 'meta[property="og:video:url"]', 'meta[property="og:video"]'):
        tag = soup.select_one(selector)
        content = tag.get("content") if tag else None
        if content:
            resolved = urljoin(final_url, content.strip())
            if resolved.lower().split("?")[0].endswith(_VIDEO_FILE_EXTENSIONS):
                return resolved

    for video_tag in soup.select("video"):
        src = video_tag.get("src")
        if src and not src.startswith("data:"):
            return urljoin(final_url, src)
        for source_tag in video_tag.select("source"):
            src = source_tag.get("src")
            if src and not src.startswith("data:"):
                return urljoin(final_url, src)
    return None


async def _extract_page_video(url: str) -> Optional[str]:
    """Fetch ``url`` and pull out a real, direct playable video URL from an
    HTML5 ``<video>``/``<source>`` element or an ``og:video`` meta tag (a
    Wikimedia Commons file page or similar self-hosted video). Retries
    transient failures like ``_extract_page_image`` does.
    """
    last_error: Optional[_RetryableFetchError] = None
    for attempt in range(1, MAX_VIDEO_FETCH_ATTEMPTS + 1):
        try:
            return await _fetch_page_video_once(url)
        except _RetryableFetchError as exc:
            last_error = exc
            if attempt < MAX_VIDEO_FETCH_ATTEMPTS:
                await asyncio.sleep(0.5 * attempt)
    raise last_error  # exhausted all attempts


@tool(
    description=(
        "Resolve a playable video source for `show_video_clips` from a URL you "
        "found via `free_search` -- never invent a video ID or file URL yourself. "
        "If the URL is a YouTube (or youtu.be) link, returns `kind: \"youtube\"` "
        "and an `embed_url` that plays through YouTube's own embedded player. "
        "Otherwise fetches the page and looks for a real, direct playable video "
        "file (an HTML5 <video> source -- e.g. a Wikimedia Commons file page or "
        "other self-hosted video) and returns `kind: \"direct\"` with a `video_url`. "
        "Returns `kind: null` if neither was found -- in that case don't invent a "
        "video for this URL, try a different search result instead."
    )
)
async def fetch_video_source(url: str) -> dict[str, Any]:
    youtube_id = _extract_youtube_id(url)
    if youtube_id:
        return {"url": url, "kind": "youtube", "embed_url": _youtube_embed_url(youtube_id)}
    try:
        video_url = await _extract_page_video(url)
    except Exception as exc:  # noqa: BLE001 -- report the failure, don't crash the tool call
        return {"url": url, "kind": None, "video_url": None, "error": str(exc)}
    if video_url is None:
        return {"url": url, "kind": None, "video_url": None}
    return {"url": url, "kind": "direct", "video_url": video_url}


class VideoClip(BaseModel):
    caption: str = Field(description="Short caption/title shown below this clip.")
    kind: str = Field(description="Exactly the `kind` value `fetch_video_source` returned for this clip: 'youtube' or 'direct'.")
    embed_url: Optional[str] = Field(
        default=None, description="Required when kind='youtube': the `embed_url` `fetch_video_source` returned."
    )
    video_url: Optional[str] = Field(
        default=None, description="Required when kind='direct': the `video_url` `fetch_video_source` returned."
    )


@tool(
    description=(
        "Render one or more playable video clips as an A2UI surface, each in its "
        "own card with a caption. Every clip's `embed_url`/`video_url` must come "
        "from a prior `fetch_video_source` call on a real URL found via "
        "`free_search` -- never invent a video ID or file URL. Resolve every clip "
        "you want to show first, then call this once with all of them; don't call "
        "it once per clip."
    )
)
def show_video_clips(title: str, clips: list[VideoClip]) -> dict[str, Any]:
    surface_id = genui.new_surface_id("video")
    parsed_clips = [c if isinstance(c, VideoClip) else VideoClip(**c) for c in clips]
    items: list[tuple[str, str, str]] = []
    for c in parsed_clips:
        if c.kind == "youtube" and c.embed_url:
            items.append(("youtube", c.embed_url, c.caption))
        elif c.kind == "direct" and c.video_url:
            items.append(("direct", c.video_url, c.caption))
    if not items:
        return {"text": "I couldn't find a playable video for that.", "genui": []}
    summary = "\n".join(f"- {c.caption}" for c in parsed_clips)
    return {
        "text": f"{title}\n{summary}",
        "genui": genui.video_gallery_card(surface_id, title, items),
    }


def _safe_icon(name: Optional[str]) -> Optional[str]:
    """Drop an icon name the LLM hallucinated outside ``genui.ICON_NAMES``.

    The client validates every component against its catalog schema and
    silently drops ones that fail (logs a warning, no error surfaced) --
    see genui's SurfaceController._handleCoreMessage. An invalid icon name
    would otherwise take the whole card/list down with it; dropping just
    the icon here degrades gracefully instead.
    """
    if name in genui.ICON_NAMES:
        return name
    return None


_ICON_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("hotel", "room", "accommodation", "stay", "guesthouse", "resort"), "home"),
    (("restaurant", "food", "dish", "meal", "cuisine", "cafe", "hawker"), "favorite"),
    (("location", "place", "address", "area", "near", "distance", "map"), "locationOn"),
    (("date", "day", "time", "schedule", "check-in", "check-out", "booking"), "calendarToday"),
    (("price", "budget", "cost", "payment", "fee", "rm"), "payment"),
    (("warning", "caution", "risk", "alert"), "warning"),
    (("phone", "call", "contact"), "phone"),
    (("photo", "image", "picture"), "photo"),
    (("step", "tip", "recommend", "highlight", "must-see"), "star"),
)


def _suggest_icon(*texts: Optional[str]) -> Optional[str]:
    """Choose a supported visual cue from the content when the model omits one."""
    searchable = " ".join(text for text in texts if text).lower()
    for keywords, icon_name in _ICON_HINTS:
        if any(keyword in searchable for keyword in keywords):
            return icon_name
    return None


@tool(
    description=(
        "Render a small titled card of information as an A2UI surface on the "
        "user's screen. Call this once you have something worth showing — a "
        "summary, an answer, a short paragraph of prose. For several discrete "
        "items (a list, steps, an itinerary) use `show_info_list` instead -- it "
        "reads far better than one long paragraph. The card is shown alongside "
        "your text reply, it does not replace it. Do not call this more than "
        "once per user request. `icon` is optional -- one of: " + ", ".join(genui.ICON_NAMES) + ". "
        "`image_url` is optional -- a real, publicly reachable http(s) image URL "
        "to show as a header image above the title. Get one from `fetch_page_image` or "
        "`browser_inspect_page`; do not type one from memory, it will almost always be wrong. "
        "`link_url`/`link_label` are optional -- add these to hand the user off to a real "
        "website (opens externally, in their own browser) to finish something there "
        "themselves, e.g. completing a booking on the real site you inspected with "
        "`browser_inspect_page`. Never fabricate a `link_url`; only use a URL you actually "
        "navigated to."
    )
)
def show_card(
    title: str,
    body: str,
    icon: Optional[str] = None,
    image_url: Optional[str] = None,
    link_url: Optional[str] = None,
    link_label: Optional[str] = None,
) -> dict[str, Any]:
    surface_id = genui.new_surface_id("card")
    return {
        "text": body,
        "genui": genui.summary_card(
            surface_id,
            title=title,
            body=body,
            icon_name=_safe_icon(icon) or _suggest_icon(title, body),
            image_url=image_url,
            link_url=link_url,
            link_label=link_label,
        ),
    }


class InfoListItem(BaseModel):
    title: str = Field(description="The item's main text.")
    subtitle: Optional[str] = Field(default=None, description="Optional smaller secondary text below the title.")
    icon: Optional[str] = Field(
        default=None, description=f"Optional leading icon, one of: {', '.join(genui.ICON_NAMES)}"
    )
    image_url: Optional[str] = Field(
        default=None,
        description=(
            "Optional real, publicly reachable http(s) image URL shown instead of "
            "the icon for this item. Get one from `fetch_page_image`; do not type "
            "one from memory, it will almost always be wrong."
        ),
    )


_DEFAULT_ITEM_ICON = "check"


def _item_icon(item: InfoListItem) -> Optional[str]:
    if item.image_url:
        return None  # image takes precedence -- info_list_card ignores icon when set
    # Every item needs *some* leading element, or its Row has one fewer
    # child than its siblings and its text starts flush against the card
    # edge instead of aligned with the icon column -- a hallucinated or
    # omitted icon must still fall back to something, not disappear.
    return _safe_icon(item.icon) or _suggest_icon(item.title, item.subtitle) or _DEFAULT_ITEM_ICON


@tool(
    description=(
        "Render a titled card containing a clean vertical list of items (each with "
        "an optional icon or image, a title, and an optional subtitle) as an A2UI "
        "surface. Use this instead of `show_card` whenever the content is naturally "
        "several discrete entries -- an itinerary, a step-by-step guide, a "
        "medication schedule, a feature/amenity list -- rather than one paragraph "
        "of prose. Do not call this more than once per user request."
    )
)
def show_info_list(title: str, items: list[InfoListItem], icon: Optional[str] = None) -> dict[str, Any]:
    surface_id = genui.new_surface_id("list")
    parsed_items = [i if isinstance(i, InfoListItem) else InfoListItem(**i) for i in items]
    summary = "\n".join(f"- {i.title}" + (f" — {i.subtitle}" if i.subtitle else "") for i in parsed_items)
    genui_messages = genui.info_list_card(
        surface_id,
        title=title,
        items=[(_item_icon(i), i.title, i.subtitle, i.image_url) for i in parsed_items],
        icon_name=_safe_icon(icon) or _suggest_icon(title),
    )
    return {"text": f"{title}\n{summary}", "genui": genui_messages}


class FormFieldType(str, Enum):
    choice = "choice"
    multi_choice = "multi_choice"
    slider = "slider"
    text = "text"
    checkbox = "checkbox"
    date = "date"


class FormFieldOption(BaseModel):
    label: str = Field(description="Text shown to the user for this option.")
    value: str = Field(description="Stable value submitted for this option.")


class FormField(BaseModel):
    id: str = Field(description="Unique field id (letters/digits/underscore only), e.g. 'budget'.")
    category: Optional[str] = Field(
        default=None,
        description=(
            "Short section heading for this field, e.g. 'Dietary needs', "
            "'Taste preferences', or 'Trip details'. Fields in the same category "
            "are shown together in their own card."
        ),
    )
    type: FormFieldType = Field(
        description=(
            "choice = single-select radio buttons (pick exactly one); "
            "multi_choice = checkboxes (pick any number, including zero) -- use this "
            "whenever the user could reasonably want more than one option, e.g. "
            "'which cuisines do you like', 'which amenities matter to you'; "
            "slider = numeric range; text = free text; checkbox = a single yes/no toggle."
            "date = date-only calendar picker (use for check-in, check-out, booking, or travel dates)."
        )
    )
    label: str = Field(description="Label shown above/next to the field.")
    help_text: Optional[str] = Field(
        default=None,
        description=(
            "Short explanation shown below the field. Required for sliders: say "
            "what the setting controls and what low versus high values mean."
        ),
    )
    options: Optional[list[FormFieldOption]] = Field(
        default=None, description="Required for type=choice/multi_choice: the selectable options."
    )
    default_option_value: Optional[str] = Field(
        default=None, description="For type=choice: the value of the option pre-selected."
    )
    default_option_values: Optional[list[str]] = Field(
        default=None, description="For type=multi_choice: the values pre-selected."
    )
    min_value: Optional[float] = Field(default=None, description="For type=slider: minimum value.")
    max_value: Optional[float] = Field(default=None, description="For type=slider: maximum value.")
    default_number: Optional[float] = Field(default=None, description="For type=slider: starting value.")
    default_text: str = Field(default="", description="For type=text: starting value.")
    default_checked: bool = Field(default=False, description="For type=checkbox: starting checked state.")
    default_date: Optional[str] = Field(default=None, description="For type=date: starting date in YYYY-MM-DD format.")
    min_date: Optional[str] = Field(
        default=None, description="For type=date: earliest selectable date in YYYY-MM-DD format."
    )
    max_date: Optional[str] = Field(
        default=None, description="For type=date: latest selectable date in YYYY-MM-DD format."
    )


@tool(
    description=(
        "Show an interactive preferences form on the user's screen, instead of asking "
        "several questions one at a time in plain text. Not specific to any topic -- use "
        "it any time you need a handful of structured inputs before you can give a good "
        "answer (choosing/buying something, planning something, configuring something, "
        "etc.). You decide the title and the fields yourself, based on what information "
        "you actually need for the request at hand. The user's answers come back later as "
        "a new message describing a submitted UI form -- respond to that by calling "
        "`show_card` with an answer that references their actual submitted values."
    )
)
def ask_preferences_form(title: str, fields: list[FormField], submit_label: str = "Submit") -> dict[str, Any]:
    surface_id = genui.new_surface_id("form")
    # Nested list items arrive as raw dicts, not coerced FormField instances
    # (LocalFunction's schema formatting doesn't recurse into list[BaseModel]).
    parsed_fields = [f if isinstance(f, FormField) else FormField(**f) for f in fields]
    is_hotel_form = any(term in title.lower() for term in ("hotel", "accommodation", "stay", "booking"))
    if is_hotel_form:
        today = datetime.now(timezone.utc).date()
        default_check_in = today.isoformat()
        default_check_out = (today + timedelta(days=1)).isoformat()
        field_by_id = {field.id: field for field in parsed_fields}
        for field_id, label, default_date in (
            ("check_in", "Check-in date", default_check_in),
            ("check_out", "Check-out date", default_check_out),
        ):
            existing = field_by_id.get(field_id)
            if existing is None:
                parsed_fields.insert(
                    0,
                    FormField(
                        id=field_id,
                        type=FormFieldType.date,
                        label=label,
                        category="Stay dates",
                        default_date=default_date,
                        min_date=default_check_in,
                    ),
                )
            else:
                existing.type = FormFieldType.date
                existing.category = existing.category or "Stay dates"
                existing.label = label
                existing.default_date = existing.default_date or default_date
                existing.min_date = existing.min_date or default_check_in
    built_groups: dict[str, list[dict[str, Any]]] = {}
    field_paths = {}
    field_defaults: dict[str, Any] = {}
    for f in parsed_fields:
        category = f.category.strip() if f.category and f.category.strip() else "Preferences"
        built_fields = built_groups.setdefault(category, [])
        if f.type == FormFieldType.choice:
            options = [(opt.label, opt.value) for opt in (f.options or [])]
            default_value = [f.default_option_value] if f.default_option_value else []
            built_fields.append(
                genui.choice_picker(
                    f.id,
                    options,
                    label=f.label,
                    value=default_value,
                    variant="mutuallyExclusive",
                )
            )
            field_defaults[f.id] = default_value
            field_paths[f.id] = f"/{f.id}/value"
        elif f.type == FormFieldType.multi_choice:
            options = [(opt.label, opt.value) for opt in (f.options or [])]
            default_values = f.default_option_values if f.default_option_values else []
            built_fields.append(
                genui.choice_picker(
                    f.id,
                    options,
                    label=f.label,
                    value=default_values,
                    variant="multipleSelection",
                )
            )
            field_defaults[f.id] = default_values
            field_paths[f.id] = f"/{f.id}/value"
        elif f.type == FormFieldType.slider:
            default_value = f.default_number if f.default_number is not None else (f.min_value or 0)
            min_value = f.min_value or 0
            max_value = f.max_value if f.max_value is not None else 100
            built_fields.append(
                genui.slider(
                    f.id,
                    value=default_value,
                    min_value=min_value,
                    max_value=max_value,
                    label=f.label,
                )
            )
            slider_help = f.help_text or (
                f"Choose a value from {min_value:g} to {max_value:g}. Current value: {default_value:g}."
            )
            built_fields.append(
                genui.text(
                    f"{f.id}_help",
                    slider_help,
                    variant="caption",
                    styles={"line-clamp": 0},
                )
            )
            field_defaults[f.id] = default_value
            field_paths[f.id] = f"/{f.id}/value"
        elif f.type == FormFieldType.text:
            built_fields.append(genui.text_field(f.id, label=f.label, value=f.default_text))
            field_defaults[f.id] = f.default_text
            field_paths[f.id] = f"/{f.id}/value"
        elif f.type == FormFieldType.checkbox:
            built_fields.append(genui.check_box(f.id, f.label, value=f.default_checked))
            field_defaults[f.id] = f.default_checked
            field_paths[f.id] = f"/{f.id}/value"
        elif f.type == FormFieldType.date:
            default_value = f.default_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            built_fields.append(
                genui.date_input(
                    f.id,
                    value=default_value,
                    label=f.label,
                    min_date=f.min_date,
                    max_date=f.max_date,
                )
            )
            field_defaults[f.id] = default_value
            field_paths[f.id] = f"/{f.id}/value"

    messages = genui.form(
        surface_id,
        title=title,
        fields=[field for group in built_groups.values() for field in group],
        field_groups=list(built_groups.items()),
        submit_label=submit_label,
        action_name="submit_preferences_form",
        field_paths=field_paths,
        field_defaults=field_defaults,
    )
    return {
        "text": f'I\'ve put a "{title}" form on your screen — fill it in and submit when ready.',
        "genui": messages,
    }


ALL_TOOLS = (
    get_current_time,
    show_card,
    show_info_list,
    ask_preferences_form,
    fetch_page_image,
    search_youtube_videos,
    fetch_video_source,
    show_video_clips,
)
