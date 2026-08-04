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


def text(
    comp_id: str,
    value: str,
    variant: str = "body",
    weight: Optional[int] = None,
    styles: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": comp_id, "component": "Text", "text": value, "variant": variant}
    if weight is not None:
        payload["weight"] = weight
    if styles is not None:
        payload["styles"] = styles
    return payload


def divider(comp_id: str) -> dict[str, Any]:
    return {"id": comp_id, "component": "Divider"}


def column(
    comp_id: str,
    children: list[str],
    justify: Optional[str] = None,
    align: Optional[str] = None,
    weight: Optional[int] = None,
    styles: Optional[dict[str, Any]] = None,
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
    if styles is not None:
        payload["styles"] = styles
    return payload


# Card's own component spec only sets border-radius (16px); it inherits
# transparent background / 0px border from the universal style baseline.
# Against this app's plain white page that leaves cards with no visible
# boundary at all, so every card() call gets this light, bordered surface
# by default instead of relying on the (invisible) built-in look.
_DEFAULT_CARD_STYLES: dict[str, Any] = {
    "background-color": "#FFFFFF",
    "border-width": "1px",
    "border-color": "#E1E4E9",
    "filter": "drop-shadow(0px 1px 4px rgba(0, 0, 0, 0.08))",
    "padding": "16px",
}


def card(comp_id: str, child_id: str, styles: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """A Material card surface (elevation/shadow) wrapping a single child."""
    payload: dict[str, Any] = {"id": comp_id, "component": "Card", "child": child_id}
    payload["styles"] = {**_DEFAULT_CARD_STYLES, **(styles or {})}
    return payload


def row(
    comp_id: str,
    children: list[str],
    justify: Optional[str] = None,
    align: Optional[str] = None,
    styles: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": comp_id, "component": "Row", "children": children}
    if justify is not None:
        payload["justify"] = justify
    if align is not None:
        payload["align"] = align
    if styles is not None:
        payload["styles"] = styles
    return payload


_MATERIAL_TO_LUCIDE_ICON_NAMES: dict[str, str] = {
    "accountCircle": "circle-user-round",
    "add": "plus",
    "arrowBack": "arrow-left",
    "arrowForward": "arrow-right",
    "attachFile": "paperclip",
    "calendarToday": "calendar-days",
    "call": "phone",
    "camera": "camera",
    "check": "check",
    "close": "x",
    "delete": "trash-2",
    "download": "download",
    "edit": "pencil",
    "error": "circle-alert",
    "event": "calendar",
    "favorite": "heart",
    "favoriteOff": "heart-off",
    "folder": "folder",
    "help": "circle-help",
    "home": "house",
    "info": "info",
    "locationOn": "map-pin",
    "lock": "lock",
    "lockOpen": "lock-open",
    "mail": "mail",
    "menu": "menu",
    "moreHoriz": "ellipsis",
    "moreVert": "ellipsis-vertical",
    "notifications": "bell",
    "notificationsOff": "bell-off",
    "payment": "credit-card",
    "person": "user",
    "phone": "phone",
    "photo": "image",
    "print": "printer",
    "refresh": "refresh-cw",
    "search": "search",
    "send": "send",
    "settings": "settings",
    "share": "share-2",
    "shoppingCart": "shopping-cart",
    "star": "star",
    "starHalf": "star-half",
    "starOff": "star-off",
    "upload": "upload",
    "visibility": "eye",
    "visibilityOff": "eye-off",
    "warning": "triangle-alert",
}


def icon(comp_id: str, name: str) -> dict[str, Any]:
    """Render a supported backend icon through Harmony AGenUI's Lucide mapper."""
    return {
        "id": comp_id,
        "component": "Icon",
        "name": _MATERIAL_TO_LUCIDE_ICON_NAMES.get(name, name),
    }


def image(
    comp_id: str,
    url: str,
    variant: str = "mediumFeature",
    fit: Optional[str] = None,
    styles: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
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
    if styles is not None:
        payload["styles"] = styles
    return payload


# Every Material-style icon name accepted by the tool API. ``icon()`` above
# translates them to AGenUI/Harmony's Lucide names before sending the surface.
ICON_NAMES = [
    "accountCircle",
    "add",
    "arrowBack",
    "arrowForward",
    "attachFile",
    "calendarToday",
    "call",
    "camera",
    "check",
    "close",
    "delete",
    "download",
    "edit",
    "error",
    "event",
    "favorite",
    "favoriteOff",
    "folder",
    "help",
    "home",
    "info",
    "locationOn",
    "lock",
    "lockOpen",
    "mail",
    "menu",
    "moreHoriz",
    "moreVert",
    "notifications",
    "notificationsOff",
    "payment",
    "person",
    "phone",
    "photo",
    "print",
    "refresh",
    "search",
    "send",
    "settings",
    "share",
    "shoppingCart",
    "star",
    "starHalf",
    "starOff",
    "upload",
    "visibility",
    "visibilityOff",
    "warning",
]


def list_view(
    comp_id: str,
    children: list[str],
    direction: Optional[str] = None,
    align: Optional[str] = None,
    styles: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": comp_id, "component": "List", "children": children}
    if direction is not None:
        payload["direction"] = direction
    if align is not None:
        payload["align"] = align
    if styles is not None:
        payload["styles"] = styles
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
    link_url: Optional[str] = None,
    link_label: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build a create+update pair rendering a title/body card in a Material Card surface.

    ``icon_name`` (one of ``ICON_NAMES``) puts a leading icon next to the title.
    ``image_url`` puts a full-width header image above the title.
    ``link_url`` adds a button below the body that opens that URL externally
    (via ``open_url_button``) -- e.g. handing off to a real website for the
    user to finish something (like a booking) there themselves.
    """
    header_children = ["headerIcon", "title"] if icon_name else ["title"]
    inner_children = (
        (["image"] if image_url else [])
        + (["header"] if icon_name else ["title"])
        + ["divider", "body"]
        + (["linkButton"] if link_url else [])
    )
    components = [
        card("root", "content"),
        column("content", inner_children),
        *([image("image", image_url, variant="header")] if image_url else []),
        *([row("header", header_children, align="center"), icon("headerIcon", icon_name)] if icon_name else []),
        text("title", title, variant="h3", weight=1 if icon_name else None),
        divider("divider"),
        text("body", body, variant="body", styles={"line-clamp": 0}),
        *(
            [
                open_url_button("linkButton", "linkText", link_url, styles={"width": "100%"}),
                text(
                    "linkText",
                    link_label or "Continue on site",
                    styles={"color": "#FFFFFF", "width": "100%", "text-align": "center"},
                ),
            ]
            if link_url
            else []
        ),
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
        outer_id = f"item{i}"
        text_col_id = f"{outer_id}TextCol"
        text_id = f"{outer_id}Text"
        subtitle_id = f"{outer_id}Subtitle"
        media_id = f"{outer_id}Media"
        text_col_children = [text_id] + ([subtitle_id] if item_subtitle else [])
        # padding=0 on the card itself (overriding the default 16px): a photo
        # is only a full-bleed "on top of the card" look if nothing insets
        # it. The text block below gets its own padding instead, applied to
        # textColId, so it's still comfortably inset from the card edges.
        item_components.append(card(card_id, outer_id, styles={"padding": "0px"}))
        if item_image_url:
            item_components.append(column(outer_id, [media_id, text_col_id]))
            item_components.append(image(media_id, item_image_url, variant="header", fit="cover"))
            item_components.append(column(text_col_id, text_col_children, styles={"padding": "16px", "gap": "8px"}))
        else:
            # No photo for this item: a leading icon next to the title
            # instead of an empty full-width photo block.
            header_row_id = f"{outer_id}Header"
            item_components.append(
                column(
                    outer_id,
                    [header_row_id, *([subtitle_id] if item_subtitle else [])],
                    styles={"padding": "16px", "gap": "8px"},
                )
            )
            item_components.append(row(header_row_id, [media_id, text_id], align="center", styles={"gap": "12px"}))
            # tools.py's _item_icon() always supplies a fallback icon when
            # there's no image.
            item_components.append(icon(media_id, item_icon or "check"))
        item_components.append(text(text_id, item_title, variant="body"))
        if item_subtitle:
            # line-clamp: the Text component's shared style baseline defaults
            # to a single visible line with a trailing ellipsis; these
            # subtitles are 1-2 sentence descriptions, so that was cutting
            # nearly all of them off after a few words. 0 = unlimited, so the
            # full description always shows regardless of length.
            item_components.append(text(subtitle_id, item_subtitle, variant="caption", styles={"line-clamp": 0}))

    header_children = ["headerIcon", "title"] if icon_name else ["title"]
    root_children = (["header"] if icon_name else ["title"]) + ["list"]
    components = [
        column("root", root_children, styles={"gap": "12px"}),
        *([row("header", header_children, align="center"), icon("headerIcon", icon_name)] if icon_name else []),
        text("title", title, variant="h3", weight=1 if icon_name else None),
        list_view("list", card_ids, styles={"gap": "10px"}),
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
    styles: Optional[dict[str, Any]] = None,
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
    if styles is not None:
        payload["styles"] = styles
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


def date_input(
    comp_id: str,
    value: str,
    label: Optional[str] = None,
    min_date: Optional[str] = None,
    max_date: Optional[str] = None,
) -> dict[str, Any]:
    """A date-only native calendar picker using YYYY-MM-DD values."""
    payload: dict[str, Any] = {
        "id": comp_id,
        "component": "DateTimeInput",
        "value": value,
        "enableDate": True,
        "enableTime": False,
    }
    if label is not None:
        payload["label"] = label
    if min_date is not None:
        payload["min"] = min_date
    if max_date is not None:
        payload["max"] = max_date
    return payload


# The catalog's Button spec only recognizes "default"/"borderless" variants
# ("primary" is not a real enum value there) and its "default" look is a
# near-white background (Color_BG_L5) with a 6%-black hairline border --
# invisible against this app's plain white page. Give every button an
# explicit, unmistakably-visible brand-blue fill instead of relying on that.
_DEFAULT_BUTTON_STYLES: dict[str, Any] = {
    "background-color": "#2273F7",
    "border-width": "0px",
    # Button has no built-in padding at all (its spec never sets one), so
    # the label text sat flush against the button's edges. A generous
    # padding plus a large, fully-rounded radius gives the pill shape a
    # real button needs instead of a bare colored rectangle.
    "padding": "16px 32px",
    "border-radius": "32px",
}


def button(
    comp_id: str,
    child_id: str,
    action_name: str,
    context_paths: Optional[dict[str, str]] = None,
    variant: str = "default",
    styles: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"name": action_name}
    if context_paths:
        event["context"] = {key: {"path": path} for key, path in context_paths.items()}
    return {
        "id": comp_id,
        "component": "Button",
        "child": child_id,
        "variant": variant,
        "styles": {**_DEFAULT_BUTTON_STYLES, **(styles or {})},
        "action": {"event": event},
    }


def open_url_button(
    comp_id: str,
    child_id: str,
    url: str,
    variant: str = "default",
    styles: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """A button that opens ``url`` externally via the client's built-in ``openUrl``
    function, instead of ``button()``'s in-app ``UserActionEvent``.

    Use this to hand off to a real website (e.g. so the user can finish a
    booking/reservation there themselves) -- pressing it never sends anything
    back to this agent, it only opens the URL in the user's browser/handler.
    """
    return {
        "id": comp_id,
        "component": "Button",
        "child": child_id,
        "variant": variant,
        "styles": {**_DEFAULT_BUTTON_STYLES, **(styles or {})},
        "action": {"functionCall": {"call": "openUrl", "args": {"url": url}}},
    }


def form(
    surface_id: str,
    title: str,
    fields: list[dict[str, Any]],
    submit_label: str,
    action_name: str,
    field_paths: dict[str, str],
    field_defaults: Optional[dict[str, Any]] = None,
    field_groups: Optional[list[tuple[str, list[dict[str, Any]]]]] = None,
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
    groups = field_groups or [("Preferences", fields)]
    group_card_ids = [f"group{index}Card" for index in range(len(groups))]
    group_components: list[dict[str, Any]] = []
    for index, (category, group_fields) in enumerate(groups):
        content_id = f"group{index}Content"
        category_id = f"group{index}Title"
        group_components.extend(
            [
                card(group_card_ids[index], content_id),
                column(
                    content_id,
                    [category_id, *[field["id"] for field in group_fields]],
                    align="stretch",
                    styles={"gap": "10px"},
                ),
                text(category_id, category, variant="h4"),
                *group_fields,
            ]
        )
    components = [
        # The submit button lives in its own trailing Card, as a sibling of
        # the fields card rather than nested inside the same Column/Row
        # subtree as every field. Splitting the button out this way keeps
        # each card's own reconciliation small, which has proven far more
        # reliable than one deeply-nested tree for large forms -- big single
        # trees have intermittently dropped the button component entirely
        # during the client's batch-flush pass.
        column("root", ["title", *group_card_ids, "submitCard"], styles={"gap": "12px"}),
        text("title", title, variant="h3"),
        *group_components,
        # The Card wrapper keeps the submit component reliable in the client.
        # Give it the same blue surface as its full-width Button, so the whole
        # visible card is a single obvious submit target rather than a white
        # card containing a smaller button.
        card(
            "submitCard",
            "submit",
            styles={
                "background-color": "#2273F7",
                "border-width": "0px",
                "border-radius": "32px",
                "padding": "0px",
            },
        ),
        button(
            "submit",
            "submitText",
            action_name=action_name,
            context_paths=field_paths,
            styles={"width": "100%", "border-radius": "32px"},
        ),
        text(
            "submitText",
            submit_label,
            styles={"color": "#FFFFFF", "width": "100%", "text-align": "center"},
        ),
    ]
    # updateDataModel must land BEFORE updateComponents. The submit button's
    # action.context binds one data-model path per field; the client
    # evaluates every bound component's data-binding status at the next
    # flush, and if the button's very first flush sees any of those paths
    # still unresolved, it's classified "partially ready", rejected, and
    # orphaned -- permanently, since orphans in this state are never
    # re-attached later (confirmed against the client's virtual_dom source).
    # So every field path must already have a value by the time the button
    # component is first flushed, not after.
    messages = [create_surface(surface_id, send_data_model=True)]
    for field_id, default_value in (field_defaults or {}).items():
        if default_value is None:
            continue
        messages.append(update_data_model(surface_id, field_paths.get(field_id, f"/{field_id}/value"), default_value))
    messages.append(update_components(surface_id, components))
    return messages
