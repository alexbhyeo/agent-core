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

from pydantic import BaseModel, Field

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


class FormFieldType(str, Enum):
    choice = "choice"
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
            "choice = single-select radio buttons; slider = numeric range; "
            "text = free text; checkbox = yes/no toggle."
        )
    )
    label: str = Field(description="Label shown above/next to the field.")
    options: Optional[list[FormFieldOption]] = Field(
        default=None, description="Required for type=choice: the selectable options."
    )
    default_option_value: Optional[str] = Field(
        default=None, description="For type=choice: the value of the option pre-selected."
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
            if f.default_option_value:
                field_defaults[f.id] = [f.default_option_value]
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
            if f.default_text:
                field_defaults[f.id] = f.default_text
        elif f.type == FormFieldType.checkbox:
            built_fields.append(genui.check_box(f.id, f.label, value=f.default_checked))
            field_defaults[f.id] = f.default_checked
        field_paths[f.id] = f"{f.id}.value"

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


ALL_TOOLS = (get_current_time, show_card, ask_preferences_form)
