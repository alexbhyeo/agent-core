# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for openjiuwen.extensions.app.ws_session."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openjiuwen.extensions.app.ws_session import (
    ConnectionSession,
    _describe_ui_actions,
    _extract_result,
    _translate,
)


def _chunk(chunk_type, payload=None):
    return SimpleNamespace(type=chunk_type, payload=payload)


class TestDescribeUiActions:
    def test_returns_empty_string_for_no_actions(self):
        assert _describe_ui_actions(None) == ""
        assert _describe_ui_actions([]) == ""

    def test_formats_action_name_and_context(self):
        actions = [{"action": {"name": "submit_prefs", "context": {"level": "Beginner"}}}]
        result = _describe_ui_actions(actions)
        assert "submit_prefs" in result
        assert "Beginner" in result


class TestExtractResult:
    def test_dict_result_returns_text_and_genui(self):
        text, genui_messages = _extract_result({"text": "hi", "genui": [{"a": 1}]})
        assert text == "hi"
        assert genui_messages == [{"a": 1}]

    def test_none_result_returns_empty(self):
        assert _extract_result(None) == ("", None)

    def test_non_dict_result_stringifies(self):
        assert _extract_result(42) == ("42", None)


class TestTranslate:
    def test_llm_output_emits_chat_token(self):
        state = {"streamed_token": False}
        events = _translate(_chunk("llm_output", {"content": "hello"}), state)
        assert events == [("chat.token", {"text": "hello"})]
        assert state["streamed_token"] is True

    def test_answer_is_fallback_only_when_nothing_streamed(self):
        state = {"streamed_token": True}
        events = _translate(_chunk("answer", {"content": "hello"}), state)
        assert events == []

        state = {"streamed_token": False}
        events = _translate(_chunk("answer", {"content": "hello"}), state)
        assert events == [("chat.token", {"text": "hello"})]

    def test_tool_call_emits_tool_started(self):
        state = {"streamed_token": False}
        events = _translate(_chunk("tool_call", {"tool_name": "show_card"}), state)
        assert events == [("tool.started", {"tool": "show_card"})]

    def test_tool_result_emits_finished_output_and_genui(self):
        state = {"streamed_token": False}
        payload = {
            "tool_name": "show_card",
            "tool_result": {"text": "answer", "genui": [{"createSurface": {}}]},
        }
        events = _translate(_chunk("tool_result", payload), state)
        assert events == [
            ("tool.finished", {"tool": "show_card"}),
            ("tool.output", {"text": "answer"}),
            ("genui", {"createSurface": {}}),
        ]

    def test_unhandled_chunk_type_emits_nothing(self):
        state = {"streamed_token": False}
        assert _translate(_chunk("llm_usage", {}), state) == []


class TestConnectionSessionDispatch:
    @pytest.mark.asyncio
    async def test_heartbeat_updates_timestamp_without_reply(self):
        websocket = SimpleNamespace(send_json=AsyncMock())
        session = ConnectionSession(websocket, agent=object(), user_id="u1")
        before = session.last_heartbeat

        await session._dispatch({"type": "heartbeat"})

        websocket.send_json.assert_not_awaited()
        assert session.last_heartbeat >= before

    @pytest.mark.asyncio
    async def test_unknown_message_type_sends_validation_error(self):
        websocket = SimpleNamespace(send_json=AsyncMock())
        session = ConnectionSession(websocket, agent=object(), user_id="u1")

        await session._dispatch({"type": "bogus", "conversationId": "c1"})

        websocket.send_json.assert_awaited_once()
        sent = websocket.send_json.await_args.args[0]
        assert sent["type"] == "error.validation"
        assert "bogus" in sent["payload"]["message"]

    @pytest.mark.asyncio
    async def test_chat_start_runs_agent_and_streams_events(self):
        websocket = SimpleNamespace(send_json=AsyncMock())
        session = ConnectionSession(websocket, agent=object(), user_id="u1")

        async def fake_stream(*args, **kwargs):
            yield _chunk("llm_output", {"content": "hi there"})

        with patch(
            "openjiuwen.extensions.app.ws_session.Runner.run_agent_streaming",
            side_effect=fake_stream,
        ):
            await session._dispatch({"type": "chat.start", "conversationId": "c1", "payload": {"text": "hi"}})
            assert session._active_task is not None
            await session._active_task

        sent_types = [call.args[0]["type"] for call in websocket.send_json.await_args_list]
        assert sent_types == ["chat.accepted", "chat.token", "chat.completed"]

    @pytest.mark.asyncio
    async def test_second_chat_start_while_running_is_rejected(self):
        websocket = SimpleNamespace(send_json=AsyncMock())
        session = ConnectionSession(websocket, agent=object(), user_id="u1")
        session._active_task = SimpleNamespace(done=lambda: False)

        await session._dispatch({"type": "chat.start", "conversationId": "c1", "payload": {"text": "hi"}})

        sent = websocket.send_json.await_args.args[0]
        assert sent["type"] == "error.validation"
        assert "already running" in sent["payload"]["message"]
