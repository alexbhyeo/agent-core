# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Tools available to the ReAct agent.

``show_card``'s return value (``{"text": ..., "genui": [...]}``) is how A2UI
messages reach the WebSocket layer: ``rails.A2uiToolEventRail`` captures the
raw dict from ``AFTER_TOOL_CALL`` and ``ws_session._translate`` turns the
``genui`` list into one WebSocket ``genui`` event per message.
"""

from datetime import datetime, timezone
from typing import Any

from openjiuwen.core.foundation.tool import tool

from . import genui


@tool(
    description=(
        "Get the current UTC date and time. Call this first if the user's request "
        "depends on knowing what time or day it is."
    )
)
def get_current_time() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@tool(
    description=(
        "Render a small titled card of information as an A2UI surface on the "
        "user's screen. Call this once you have something worth showing — a "
        "summary, an answer, a short list of facts. The card is shown alongside "
        "your text reply, it does not replace it. Do not call this more than "
        "once per user request."
    )
)
def show_card(title: str, body: str) -> dict[str, Any]:
    surface_id = genui.new_surface_id("card")
    return {"text": body, "genui": genui.summary_card(surface_id, title=title, body=body)}


@tool(
    description=(
        "Show an interactive preferences form for buying a badminton racket, instead of "
        "asking the questions one at a time in plain text. Call this as soon as the user "
        "expresses interest in buying/choosing a badminton racket. The user's answers come "
        "back later as a new message describing a submitted UI form -- respond to that by "
        "calling `show_card` with a recommendation."
    )
)
def ask_badminton_preferences() -> dict[str, Any]:
    surface_id = genui.new_surface_id("form")
    fields = [
        genui.choice_picker(
            "level",
            [
                ("Beginner", "Beginner"),
                ("Intermediate", "Intermediate"),
                ("Advanced", "Advanced"),
                ("Professional", "Professional"),
            ],
            label="Playing level",
            value=["Intermediate"],
        ),
        genui.slider("budget", value=150, min_value=30, max_value=500, label="Budget (SGD)"),
        genui.text_field("brand", label="Preferred brand (optional)"),
        genui.check_box("junior", "This racket is for a junior/beginner player"),
    ]
    messages = genui.form(
        surface_id,
        title="Tell us your racket preferences",
        fields=fields,
        submit_label="Get my racket recommendation",
        action_name="submit_badminton_preferences",
        field_paths={
            "level": "level.value",
            "budget": "budget.value",
            "brand": "brand.value",
            "junior": "junior.value",
        },
    )
    return {
        "text": "I've put a preferences form on your screen — fill it in and submit when ready.",
        "genui": messages,
    }


ALL_TOOLS = (get_current_time, show_card, ask_badminton_preferences)
