# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import json
from typing import Any, Optional

from pydantic import BaseModel


def serialize_tool_call_arguments(arguments: Any) -> str:
    """Return tool arguments in the JSON-string form required on the wire."""
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, BaseModel):
        arguments = arguments.model_dump(mode="json")
    return json.dumps(arguments, ensure_ascii=False)


class ToolCall(BaseModel):
    """
    Tool Call
    Attributes:
        id: Tool call ID
        type: Tool call type
        name: Tool name
        arguments: Tool arguments
        index: Tool call index, used to distinguish multiple tool calls
        response_item_id: Optional provider response item ID for protocols
            that distinguish response item IDs from tool call IDs.
    """
    id: Optional[str]
    type: str
    name: str
    arguments: str
    index: Optional[int] = None
    response_item_id: Optional[str] = None
