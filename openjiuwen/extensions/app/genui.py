# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""A2UI v0.9 message builders.

Matches the wire schema shipped with the Flutter ``genui`` package
(``genui-0.10.1/assets/schemas/server_to_client.json``): every message
requires ``"version": "v0.9"``; ``createSurface`` requires ``surfaceId`` and
``catalogId``; ``updateComponents`` requires one component with ``id: "root"``.
"""

import uuid
from typing import Any, Optional

from . import config

A2UI_VERSION = "v0.9"


def new_surface_id(prefix: str = "surface") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def create_surface(
    surface_id: str,
    catalog_id: Optional[str] = None,
    theme: Optional[dict[str, Any]] = None,
    send_data_model: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "surfaceId": surface_id,
        "catalogId": catalog_id or config.get("CATALOG_ID"),
    }
    if theme is not None:
        payload["theme"] = theme
    if send_data_model:
        payload["sendDataModel"] = True
    return {"version": A2UI_VERSION, "createSurface": payload}


def update_components(surface_id: str, components: list[dict[str, Any]]) -> dict[str, Any]:
    if not any(c.get("id") == "root" for c in components):
        raise ValueError("updateComponents requires one component with id 'root'")
    return {
        "version": A2UI_VERSION,
        "updateComponents": {"surfaceId": surface_id, "components": components},
    }


def delete_surface(surface_id: str) -> dict[str, Any]:
    return {"version": A2UI_VERSION, "deleteSurface": {"surfaceId": surface_id}}


def update_data_model(surface_id: str, path: str, value: Any) -> dict[str, Any]:
    return {
        "version": A2UI_VERSION,
        "updateDataModel": {"surfaceId": surface_id, "path": path, "value": value},
    }


# ---------------------------------------------------------------------------
# Minimal basic-catalog component helpers (Text / Column / Divider)
# ---------------------------------------------------------------------------


def text(comp_id: str, value: str, variant: str = "body", weight: Optional[int] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": comp_id, "component": "Text", "text": value, "variant": variant}
    if weight is not None:
        payload["weight"] = weight
    return payload


def divider(comp_id: str) -> dict[str, Any]:
    return {"id": comp_id, "component": "Divider"}


def column(
    comp_id: str,
    children: list[str],
    justify: Optional[str] = None,
    align: Optional[str] = None,
    weight: Optional[int] = None,
) -> dict[str, Any]:
    # `weight` only matters when this Column is placed as a child of a Row
    # (or List laid out horizontally): Row only gives a child flexible sizing
    # -- so long text wraps instead of overflowing -- if the child component
    # carries an explicit "weight" property (Column/Text aren't
    # isImplicitlyFlexible like TextField/ChoicePicker are). See
    # genui-0.10.1/lib/src/catalog/basic_catalog_widgets/row.dart.
    payload: dict[str, Any] = {"id": comp_id, "component": "Column", "children": children}
    if justify is not None:
        payload["justify"] = justify
    if align is not None:
        payload["align"] = align
    if weight is not None:
        payload["weight"] = weight
    return payload


def card(comp_id: str, child_id: str) -> dict[str, Any]:
    """A Material card surface (elevation/shadow) wrapping a single child."""
    return {"id": comp_id, "component": "Card", "child": child_id}


def row(
    comp_id: str,
    children: list[str],
    justify: Optional[str] = None,
    align: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": comp_id, "component": "Row", "children": children}
    if justify is not None:
        payload["justify"] = justify
    if align is not None:
        payload["align"] = align
    return payload


def icon(comp_id: str, name: str) -> dict[str, Any]:
    """An icon; ``name`` must be one of ``ICON_NAMES``."""
    return {"id": comp_id, "component": "Icon", "name": name}


def image(comp_id: str, url: str, variant: str = "mediumFeature", fit: Optional[str] = None) -> dict[str, Any]:
    """An image loaded directly by the client via ``Image.network(url)``.

    ``url`` must be a real, publicly reachable http(s) URL -- the client
    fetches it directly, nothing is downloaded/served by this backend. A
    broken/unreachable URL degrades gracefully to a broken-image icon
    client-side, it doesn't fail the surface.

    ``variant``: one of icon/avatar/smallFeature/mediumFeature/largeFeature/header.
    """
    payload: dict[str, Any] = {"id": comp_id, "component": "Image", "url": url, "variant": variant}
    if fit is not None:
        payload["fit"] = fit
    return payload


# Every icon name the client's Icon widget recognizes (genui basic catalog).
# Passing anything outside this set fails client-side schema validation
# silently (see genui-0.10.1/lib/src/catalog/basic_catalog_widgets/icon.dart).
ICON_NAMES = [
    "accountCircle", "add", "arrowBack", "arrowForward", "attachFile",
    "calendarToday", "call", "camera", "check", "close", "delete",
    "download", "edit", "error", "event", "favorite", "favoriteOff",
    "folder", "help", "home", "info", "locationOn", "lock", "lockOpen",
    "mail", "menu", "moreHoriz", "moreVert", "notifications",
    "notificationsOff", "payment", "person", "phone", "photo", "print",
    "refresh", "search", "send", "settings", "share", "shoppingCart",
    "star", "starHalf", "starOff", "upload", "visibility", "visibilityOff",
    "warning",
]


def list_view(
    comp_id: str,
    children: list[str],
    direction: Optional[str] = None,
    align: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": comp_id, "component": "List", "children": children}
    if direction is not None:
        payload["direction"] = direction
    if align is not None:
        payload["align"] = align
    return payload


def tabs(comp_id: str, tab_specs: list[tuple[str, str]], active_tab: int = 0) -> dict[str, Any]:
    """``tab_specs`` is a list of (label, content_component_id) pairs."""
    return {
        "id": comp_id,
        "component": "Tabs",
        "tabs": [{"label": label, "content": content_id} for label, content_id in tab_specs],
        "activeTab": active_tab,
    }


def summary_card(
    surface_id: str,
    title: str,
    body: str,
    icon_name: Optional[str] = None,
    image_url: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build a create+update pair rendering a title/body card in a Material Card surface.

    ``icon_name`` (one of ``ICON_NAMES``) puts a leading icon next to the title.
    ``image_url`` puts a full-width header image above the title.
    """
    header_children = (["headerIcon", "title"] if icon_name else ["title"])
    inner_children = (["image"] if image_url else []) + (["header"] if icon_name else ["title"]) + [
        "divider",
        "body",
    ]
    components = [
        card("root", "content"),
        column("content", inner_children),
        *([image("image", image_url, variant="header")] if image_url else []),
        *([row("header", header_children, align="center"), icon("headerIcon", icon_name)] if icon_name else []),
        text("title", title, variant="h3", weight=1 if icon_name else None),
        divider("divider"),
        text("body", body, variant="body"),
    ]
    return [
        create_surface(surface_id),
        update_components(surface_id, components),
    ]


def info_list_card(
    surface_id: str,
    title: str,
    items: list[tuple[Optional[str], str, Optional[str], Optional[str]]],
    icon_name: Optional[str] = None,
) -> list[dict[str, Any]]:
    """A titled heading followed by a vertical list of individually-carded items.

    ``items`` is a list of (item_icon_name_or_None, item_title,
    item_subtitle_or_None, item_image_url_or_None). If both an icon and an
    image are given for an item, the image wins (it's shown, not the icon).
    Each item renders in its own Material Card (its own elevated surface,
    not one shared list inside a single outer card) -- good for itineraries,
    step-by-step guides, medication schedules, feature lists -- anything
    that's naturally several discrete entries rather than one paragraph.
    """
    card_ids = [f"item{i}Card" for i in range(len(items))]
    item_components: list[dict[str, Any]] = []
    for i, (item_icon, item_title, item_subtitle, item_image_url) in enumerate(items):
        card_id = card_ids[i]
        row_id = f"item{i}"
        text_col_id = f"{row_id}TextCol"
        text_id = f"{row_id}Text"
        subtitle_id = f"{row_id}Subtitle"
        media_id = f"{row_id}Media"
        text_col_children = [text_id] + ([subtitle_id] if item_subtitle else [])
        item_components.append(card(card_id, row_id))
        item_components.append(row(row_id, [media_id, text_col_id], align="center"))
        if item_image_url:
            # "avatar" (32px circle) rather than "smallFeature" (50px square):
            # the Icon component has no size property to match against, so
            # keeping the image small keeps the icon/image fallback visually
            # consistent across items instead of some rows having a much
            # bigger leading element than others.
            item_components.append(image(media_id, item_image_url, variant="avatar", fit="cover"))
        else:
            # tools.py's _item_icon() always supplies a fallback icon when
            # there's no image, so every row has the same 2-child shape and
            # all text columns line up -- a row with only a text child (no
            # leading element) would sit flush against the card edge while
            # its siblings are indented past the icon column.
            item_components.append(icon(media_id, item_icon or "check"))
        item_components.append(column(text_col_id, text_col_children, weight=1))
        item_components.append(text(text_id, item_title, variant="body"))
        if item_subtitle:
            item_components.append(text(subtitle_id, item_subtitle, variant="caption"))

    header_children = (["headerIcon", "title"] if icon_name else ["title"])
    root_children = (["header"] if icon_name else ["title"]) + ["list"]
    components = [
        column("root", root_children),
        *([row("header", header_children, align="center"), icon("headerIcon", icon_name)] if icon_name else []),
        text("title", title, variant="h3", weight=1 if icon_name else None),
        list_view("list", card_ids),
        *item_components,
    ]
    return [
        create_surface(surface_id),
        update_components(surface_id, components),
    ]


# ---------------------------------------------------------------------------
# Form input components (ChoicePicker / Slider / TextField / CheckBox / Button)
#
# Each input auto-binds to a data-model path of "<comp_id>.value" unless an
# explicit {"path": ...} is given for its value -- see genui's TextField /
# ChoicePicker / Slider / CheckBox widget builders. Button.action's "context"
# map is resolved against those same paths when pressed, so a submit button
# collects whatever the user has entered into its sibling fields.
# ---------------------------------------------------------------------------


def choice_picker(
    comp_id: str,
    options: list[tuple[str, str]],
    label: Optional[str] = None,
    value: Optional[list[str]] = None,
    variant: str = "mutuallyExclusive",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": comp_id,
        "component": "ChoicePicker",
        "options": [{"label": opt_label, "value": opt_value} for opt_label, opt_value in options],
        "value": value or [],
        "variant": variant,
    }
    if label is not None:
        payload["label"] = label
    return payload


def slider(
    comp_id: str,
    value: float,
    min_value: float = 0,
    max_value: float = 1,
    label: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": comp_id,
        "component": "Slider",
        "value": value,
        "min": min_value,
        "max": max_value,
    }
    if label is not None:
        payload["label"] = label
    return payload


def text_field(
    comp_id: str,
    label: Optional[str] = None,
    value: str = "",
    variant: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": comp_id, "component": "TextField", "value": value}
    if label is not None:
        payload["label"] = label
    if variant is not None:
        payload["variant"] = variant
    return payload


def check_box(comp_id: str, label: str, value: bool = False) -> dict[str, Any]:
    return {"id": comp_id, "component": "CheckBox", "label": label, "value": value}


def button(
    comp_id: str,
    child_id: str,
    action_name: str,
    context_paths: Optional[dict[str, str]] = None,
    variant: str = "primary",
) -> dict[str, Any]:
    event: dict[str, Any] = {"name": action_name}
    if context_paths:
        event["context"] = {key: {"path": path} for key, path in context_paths.items()}
    return {
        "id": comp_id,
        "component": "Button",
        "child": child_id,
        "variant": variant,
        "action": {"event": event},
    }


def form(
    surface_id: str,
    title: str,
    fields: list[dict[str, Any]],
    submit_label: str,
    action_name: str,
    field_paths: dict[str, str],
    field_defaults: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Build a create+update(+updateDataModel) sequence for a titled form.

    ``field_paths`` maps context keys to "<field_id>.value" data-model paths;
    the submit button reads these off the data model when pressed, so its
    resulting UserActionEvent's ``context`` carries whatever the user entered.

    A field's ``value`` in its component JSON only sets its *visual* initial
    state -- it is not written into the data model until the user interacts
    with the widget (see e.g. ChoicePicker/Slider/TextField/CheckBox widget
    builders: unset paths fall back to that literal for display only). A
    field left untouched at its pre-selected default would therefore submit
    as empty. ``field_defaults`` (field id -> default value) seeds the data
    model directly via ``updateDataModel`` so defaults are real, submittable
    values even if the user never touches that field.
    """
    field_ids = [f["id"] for f in fields]
    components = [
        card("root", "content"),
        # align="stretch": Row uses MainAxisSize.min (fixed client-side, not
        # configurable from here), so justify="center" on submitRow only has
        # room to center the button if the Row itself is stretched to the
        # Column's full width -- otherwise it just hugs the button's own
        # width and "centering" is a no-op.
        column("content", ["title", *field_ids, "submitRow"], align="stretch"),
        row("submitRow", ["submit"], justify="center"),
        text("title", title, variant="h3"),
        *fields,
        button("submit", "submitText", action_name=action_name, context_paths=field_paths),
        text("submitText", submit_label),
    ]
    messages = [
        create_surface(surface_id, send_data_model=True),
        update_components(surface_id, components),
    ]
    for field_id, default_value in (field_defaults or {}).items():
        if default_value is None:
            continue
        messages.append(update_data_model(surface_id, field_paths.get(field_id, f"{field_id}.value"), default_value))
    return messages
