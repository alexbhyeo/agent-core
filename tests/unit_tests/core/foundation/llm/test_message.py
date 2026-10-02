# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import json

from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall


def test_assistant_message_dump_repairs_mapping_tool_arguments() -> None:
    tool_call = ToolCall(
        id="call-1",
        type="function",
        name="browser_navigate",
        arguments='{"url": "https://example.com"}',
    )
    # Tool rails can mutate an already validated ToolCall while normalizing a
    # browser action.  Reproduce the invalid history entry seen in production.
    tool_call.arguments = {"url": "#collapseOne"}  # type: ignore[assignment]

    dumped = AssistantMessage(content="", tool_calls=[tool_call]).model_dump()
    arguments = dumped["tool_calls"][0]["function"]["arguments"]

    assert isinstance(arguments, str)
    assert json.loads(arguments) == {"url": "#collapseOne"}
