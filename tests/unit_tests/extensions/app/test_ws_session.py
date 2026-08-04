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
    _unsent_suffix,
)


def _chunk(chunk_type, payload=None):
    return SimpleNamespace(type=chunk_type, payload=payload)


def _new_state():
    return {"last_llm_text": ""}


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


class TestUnsentSuffix:
    def test_empty_final_text_returns_empty(self):
        assert _unsent_suffix("", "anything") == ""

    def test_identical_text_returns_empty(self):
        assert _unsent_suffix("hello", "hello") == ""

    def test_streamed_text_already_contains_final_returns_empty(self):
        assert _unsent_suffix("world", "hello wonderful world") == ""

    def test_returns_unsent_suffix_when_final_extends_streamed(self):
        assert _unsent_suffix("hello world", "hello ") == "world"

    def test_returns_full_text_when_unrelated_to_streamed(self):
        assert _unsent_suffix("a distinct final answer", "hello ") == "a distinct final answer"


class TestTranslate:
    def test_llm_output_emits_chat_token_and_accumulates_state(self):
        state = _new_state()
        events = _translate(_chunk("llm_output", {"content": "hello"}), state)
        assert events == [("chat.token", {"text": "hello"})]
        assert state["last_llm_text"] == "hello"

        events = _translate(_chunk("llm_output", {"content": " world"}), state)
        assert events == [("chat.token", {"text": " world"})]
        assert state["last_llm_text"] == "hello world"

    def test_answer_emits_only_unsent_suffix(self):
        state = _new_state()
        _translate(_chunk("llm_output", {"content": "hello "}), state)
        events = _translate(_chunk("answer", {"output": "hello world"}), state)
        assert events == [("chat.token", {"text": "world"})]

    def test_answer_emits_nothing_when_fully_streamed(self):
        state = _new_state()
        _translate(_chunk("llm_output", {"content": "hello"}), state)
        events = _translate(_chunk("answer", {"output": "hello"}), state)
        assert events == []

    def test_tool_call_emits_tool_started_and_resets_streamed_text(self):
        state = _new_state()
        state["last_llm_text"] = "some preceding text"
        events = _translate(_chunk("tool_call", {"tool_name": "show_card", "tool_call_id": "c1"}), state)
        assert events == [("tool.started", {"tool": "show_card", "callId": "c1"})]
        assert state["last_llm_text"] == ""

    def test_tool_result_emits_finished_output_and_genui(self):
        state = _new_state()
        payload = {
            "tool_name": "show_card",
            "tool_call_id": "c1",
            "tool_result": {"text": "answer", "genui": [{"createSurface": {}}]},
        }
        events = _translate(_chunk("tool_result", payload), state)
        assert events == [
            ("tool.finished", {"tool": "show_card", "callId": "c1"}),
            ("tool.output", {"tool": "show_card", "callId": "c1", "text": "answer"}),
            ("genui", {"createSurface": {}}),
        ]

    def test_tool_result_with_error_prefix_emits_error_tool_instead_of_output(self):
        state = _new_state()
        payload = {
            "tool_name": "fetch_webpage",
            "tool_call_id": "c2",
            "tool_result": "[ERROR] could not reach host",
        }
        events = _translate(_chunk("tool_result", payload), state)
        assert events == [
            ("tool.finished", {"tool": "fetch_webpage", "callId": "c2"}),
            ("error.tool", {"tool": "fetch_webpage", "callId": "c2", "message": "[ERROR] could not reach host"}),
        ]

    def test_tool_error_emits_finished_then_error_tool(self):
        state = _new_state()
        payload = {"tool_name": "browser_inspect_page", "tool_call_id": "c3", "message": "nav timeout"}
        events = _translate(_chunk("tool_error", payload), state)
        assert events == [
            ("tool.finished", {"tool": "browser_inspect_page", "callId": "c3"}),
            ("error.tool", {"tool": "browser_inspect_page", "callId": "c3", "message": "nav timeout"}),
        ]

    def test_unhandled_chunk_type_emits_nothing(self):
        state = _new_state()
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
