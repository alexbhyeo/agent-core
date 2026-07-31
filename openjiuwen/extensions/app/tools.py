# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Tools available to the ReAct agent.

``show_card``'s return value (``{"text": ..., "genui": [...]}``) is how A2UI
messages reach the WebSocket layer: ``rails.A2uiToolEventRail`` captures the
raw dict from ``AFTER_TOOL_CALL`` and ``ws_session._translate`` turns the
``genui`` list into one WebSocket ``genui`` event per message.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from urllib.parse import urljoin

from pydantic import BaseModel, Field

from openjiuwen.core.foundation.tool import tool
from openjiuwen.harness.tools.web import _http
from openjiuwen.harness.tools.web._common import _REQUEST_HEADERS, _parse_html
from openjiuwen.harness.tools.web._decode import _decode_response_text

from . import genui


@tool(
    description=(
        "Get the current UTC date and time. Call this first if the user's request "
        "depends on knowing what time or day it is."
    )
)
def get_current_time() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


_BAD_IMAGE_SRC_HINTS = ("logo", "icon", "sprite", "avatar", "pixel", "spacer", "1x1", "blank.gif")


async def _extract_page_image(url: str) -> Optional[str]:
    """Fetch ``url`` and pull out its Open Graph image, or failing that the
    first plausible content <img> -- reuses the same HTTP transport and HTML
    parser as ``fetch_webpage`` rather than duplicating that logic, but keeps
    the raw markup (fetch_webpage strips it entirely for text extraction, so
    it can never recover a real image URL).
    """
    async with _http.new_session() as session:
        status, headers, body, final_url, _truncated = await _http.request(
            session, "GET", url, headers=_REQUEST_HEADERS, timeout_seconds=15, max_bytes=3_000_000
        )
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
        "to show as a header image above the title. Get one from `fetch_page_image`; "
        "do not type one from memory, it will almost always be wrong."
    )
)
def show_card(title: str, body: str, icon: Optional[str] = None, image_url: Optional[str] = None) -> dict[str, Any]:
    surface_id = genui.new_surface_id("card")
    return {
        "text": body,
        "genui": genui.summary_card(
            surface_id, title=title, body=body, icon_name=_safe_icon(icon), image_url=image_url
        ),
    }


class InfoListItem(BaseModel):
    title: str = Field(description="The item's main text.")
    subtitle: Optional[str] = Field(default=None, description="Optional smaller secondary text below the title.")
    icon: Optional[str] = Field(default=None, description=f"Optional leading icon, one of: {', '.join(genui.ICON_NAMES)}")
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
    return _safe_icon(item.icon) or _DEFAULT_ITEM_ICON


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
    summary = "\n".join(
        f"- {i.title}" + (f" — {i.subtitle}" if i.subtitle else "") for i in parsed_items
    )
    genui_messages = genui.info_list_card(
        surface_id,
        title=title,
        items=[(_item_icon(i), i.title, i.subtitle, i.image_url) for i in parsed_items],
        icon_name=_safe_icon(icon),
    )
    return {"text": f"{title}\n{summary}", "genui": genui_messages}


class FormFieldType(str, Enum):
    choice = "choice"
    multi_choice = "multi_choice"
    slider = "slider"
    text = "text"
    checkbox = "checkbox"


class FormFieldOption(BaseModel):
    label: str = Field(description="Text shown to the user for this option.")
    value: str = Field(description="Stable value submitted for this option.")


class FormField(BaseModel):
    id: str = Field(description="Unique field id (letters/digits/underscore only), e.g. 'budget'.")
    type: FormFieldType = Field(
        description=(
            "choice = single-select radio buttons (pick exactly one); "
            "multi_choice = checkboxes (pick any number, including zero) -- use this "
            "whenever the user could reasonably want more than one option, e.g. "
            "'which cuisines do you like', 'which amenities matter to you'; "
            "slider = numeric range; text = free text; checkbox = a single yes/no toggle."
        )
    )
    label: str = Field(description="Label shown above/next to the field.")
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
def ask_preferences_form(
    title: str, fields: list[FormField], submit_label: str = "Submit"
) -> dict[str, Any]:
    surface_id = genui.new_surface_id("form")
    # Nested list items arrive as raw dicts, not coerced FormField instances
    # (LocalFunction's schema formatting doesn't recurse into list[BaseModel]).
    parsed_fields = [f if isinstance(f, FormField) else FormField(**f) for f in fields]
    built_fields = []
    field_paths = {}
    field_defaults: dict[str, Any] = {}
    for f in parsed_fields:
        if f.type == FormFieldType.choice:
            built_fields.append(
                genui.choice_picker(
                    f.id,
                    [(opt.label, opt.value) for opt in (f.options or [])],
                    label=f.label,
                    value=[f.default_option_value] if f.default_option_value else None,
                )
            )
            # ChoicePicker's onChanged always writes a list, even for
            # mutually-exclusive single-select -- match that shape here.
            # Always seed the path, even with an empty list: the submit
            # button's action.context binds to every field's path, and a
            # path with no data-model entry at all leaves that binding
            # PartiallyReady forever, which silently orphans the whole
            # button component client-side (never just renders blank --
            # it disappears and never retries).
            field_defaults[f.id] = [f.default_option_value] if f.default_option_value else []
        elif f.type == FormFieldType.multi_choice:
            built_fields.append(
                genui.choice_picker(
                    f.id,
                    [(opt.label, opt.value) for opt in (f.options or [])],
                    label=f.label,
                    value=f.default_option_values,
                    variant="multipleSelection",
                )
            )
            # Same reasoning as the choice case above: always seed the path.
            field_defaults[f.id] = f.default_option_values if f.default_option_values else []
        elif f.type == FormFieldType.slider:
            default_value = f.default_number if f.default_number is not None else (f.min_value or 0)
            built_fields.append(
                genui.slider(
                    f.id,
                    value=default_value,
                    min_value=f.min_value or 0,
                    max_value=f.max_value if f.max_value is not None else 100,
                    label=f.label,
                )
            )
            field_defaults[f.id] = default_value
        elif f.type == FormFieldType.text:
            built_fields.append(genui.text_field(f.id, label=f.label, value=f.default_text))
            # Always seed the path, even with "" -- see the choice case above.
            field_defaults[f.id] = f.default_text
        elif f.type == FormFieldType.checkbox:
            built_fields.append(genui.check_box(f.id, f.label, value=f.default_checked))
            field_defaults[f.id] = f.default_checked
        field_paths[f.id] = f"/{f.id}/value"

    messages = genui.form(
        surface_id,
        title=title,
        fields=built_fields,
        submit_label=submit_label,
        action_name="submit_preferences_form",
        field_paths=field_paths,
        field_defaults=field_defaults,
    )
    return {
        "text": f'I\'ve put a "{title}" form on your screen — fill it in and submit when ready.',
        "genui": messages,
    }


ALL_TOOLS = (get_current_time, show_card, show_info_list, ask_preferences_form, fetch_page_image)
