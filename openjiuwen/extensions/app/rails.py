# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Tool-call lifecycle rail attached to the ReAct agent.

Modeled on ``openjiuwen.harness.cli.rails.tool_tracker.ToolTrackingRail``,
which emits the same ``tool_call``/``tool_result`` OutputSchema chunks but
stringifies ``tool_result`` via ``str(...)`` for CLI text rendering. That
stringification would mangle ``show_card``'s structured return value (a
Python dict embedding A2UI/GenUI messages), so this rail keeps
``tool_result`` as the raw returned object instead -- see ``ws_session.py``,
which reads ``payload["tool_result"]["genui"]`` off of it.
"""

from typing import Any

from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent.rail.base import AgentRail


class A2uiToolEventRail(AgentRail):
    """Emits tool_call / tool_result chunks with untouched payloads."""

    priority = 5  # run after other rails, mirroring ToolTrackingRail

    async def before_tool_call(self, ctx: Any) -> None:
        session = ctx.session
        if session is None:
            return
        await session.write_stream(
            OutputSchema(
                type="tool_call",
                index=0,
                payload={
                    "tool_name": getattr(ctx.inputs, "tool_name", ""),
                    "tool_args": getattr(ctx.inputs, "tool_args", ""),
                },
            )
        )

    async def after_tool_call(self, ctx: Any) -> None:
        session = ctx.session
        if session is None:
            return
        await session.write_stream(
            OutputSchema(
                type="tool_result",
                index=0,
                payload={
                    "tool_name": getattr(ctx.inputs, "tool_name", ""),
                    "tool_args": getattr(ctx.inputs, "tool_args", ""),
                    "tool_result": getattr(ctx.inputs, "tool_result", None),
                },
            )
        )
