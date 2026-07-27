# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for openjiuwen.extensions.app.rails.A2uiToolEventRail."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.extensions.app.rails import A2uiToolEventRail


def _make_ctx(session, tool_name="show_card", tool_args=None, tool_result=None):
    return SimpleNamespace(
        session=session,
        inputs=SimpleNamespace(tool_name=tool_name, tool_args=tool_args or {}, tool_result=tool_result),
    )


class TestBeforeToolCall:
    @pytest.mark.asyncio
    async def test_writes_tool_call_chunk(self):
        session = SimpleNamespace(write_stream=AsyncMock())
        rail = A2uiToolEventRail()
        ctx = _make_ctx(session, tool_name="get_current_time", tool_args={"a": 1})

        await rail.before_tool_call(ctx)

        session.write_stream.assert_awaited_once()
        chunk = session.write_stream.await_args.args[0]
        assert chunk.type == "tool_call"
        assert chunk.payload == {"tool_name": "get_current_time", "tool_args": {"a": 1}}

    @pytest.mark.asyncio
    async def test_no_session_is_a_no_op(self):
        rail = A2uiToolEventRail()
        ctx = _make_ctx(None)

        await rail.before_tool_call(ctx)  # should not raise


class TestAfterToolCall:
    @pytest.mark.asyncio
    async def test_keeps_raw_tool_result_unstringified(self):
        session = SimpleNamespace(write_stream=AsyncMock())
        rail = A2uiToolEventRail()
        raw_result = {"text": "hi", "genui": [{"version": "v0.9"}]}
        ctx = _make_ctx(session, tool_result=raw_result)

        await rail.after_tool_call(ctx)

        chunk = session.write_stream.await_args.args[0]
        assert chunk.type == "tool_result"
        assert chunk.payload["tool_result"] is raw_result

    @pytest.mark.asyncio
    async def test_no_session_is_a_no_op(self):
        rail = A2uiToolEventRail()
        ctx = _make_ctx(None)

        await rail.after_tool_call(ctx)  # should not raise
