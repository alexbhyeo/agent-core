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


# ---------------------------------------------------------------------------
# Minimal basic-catalog component helpers (Text / Column / Divider)
# ---------------------------------------------------------------------------


def text(comp_id: str, value: str, variant: str = "body") -> dict[str, Any]:
    return {"id": comp_id, "component": "Text", "text": value, "variant": variant}


def divider(comp_id: str) -> dict[str, Any]:
    return {"id": comp_id, "component": "Divider"}


def column(
    comp_id: str,
    children: list[str],
    justify: Optional[str] = None,
    align: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": comp_id, "component": "Column", "children": children}
    if justify is not None:
        payload["justify"] = justify
    if align is not None:
        payload["align"] = align
    return payload


def summary_card(surface_id: str, title: str, body: str) -> list[dict[str, Any]]:
    """Build a create+update pair rendering a simple title/body summary card."""
    components = [
        column("root", ["title", "divider", "body"]),
        text("title", title, variant="h3"),
        divider("divider"),
        text("body", body, variant="body"),
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
) -> list[dict[str, Any]]:
    """Build a create+update pair for a titled form with a submit button.

    ``field_paths`` maps context keys to "<field_id>.value" data-model paths;
    the submit button reads these off the data model when pressed, so its
    resulting UserActionEvent's ``context`` carries whatever the user entered.
    """
    field_ids = [f["id"] for f in fields]
    components = [
        column("root", ["title", *field_ids, "submit"]),
        text("title", title, variant="h3"),
        *fields,
        button("submit", "submitText", action_name=action_name, context_paths=field_paths),
        text("submitText", submit_label),
    ]
    return [
        create_surface(surface_id, send_data_model=True),
        update_components(surface_id, components),
    ]
