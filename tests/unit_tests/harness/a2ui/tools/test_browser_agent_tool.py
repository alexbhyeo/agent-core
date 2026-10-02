# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the browser_agent_tool login-credential-form flow.

Covers the pause-at-login-wall -> A2UI form -> resume-with-credentials
chain end to end at the tool level, with Runner.run_agent_streaming and
_get_browser_agent mocked out (no real browser/LLM involved).
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.a2ui.tools import browser_agent_tool as bat
from openjiuwen.harness.a2ui.tools.browser_agent_tool import (
    BROWSER_LOGIN_FLOW_CONTEXT_KEY,
    BrowserAgentTool,
    BrowserCredentialInterruptRail,
    BrowserCredentialRequest,
    _build_credential_request_output,
    request_login_credentials,
)
from tests.unit_tests.fixtures.mock_llm import (
    MockLLMModel,
    create_text_response,
    create_tool_call_response,
)


def _chunk(chunk_type, payload=None):
    return SimpleNamespace(type=chunk_type, payload=payload)


@pytest.fixture(autouse=True)
def _clear_pending_requests():
    bat._PENDING_CREDENTIAL_REQUESTS.clear()
    yield
    bat._PENDING_CREDENTIAL_REQUESTS.clear()


class TestBrowserAgentToolInterrupt:
    @pytest.mark.asyncio
    async def test_login_wall_returns_credential_form(self):
        async def fake_stream(*args, **kwargs):
            yield _chunk(
                INTERACTION,
                SimpleNamespace(
                    id="tc-1",
                    value=BrowserCredentialRequest(
                        message="Login required",
                        fields=["Username", "Password"],
                        reason="example.com",
                    ),
                ),
            )

        with (
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=object())),
            patch.object(bat.Runner, "run_agent_streaming", side_effect=fake_stream),
        ):
            tool = BrowserAgentTool()
            result = await tool.invoke({"task": "log in to example.com and check my orders"})

        assert result.success is True
        assert "resume_token" not in result.data["content"]
        assert result.data["genui"]
        assert len(bat._PENDING_CREDENTIAL_REQUESTS) == 1
        token = next(iter(bat._PENDING_CREDENTIAL_REQUESTS))
        assert len(token) >= 32
        components = result.data["genui"][-1]["updateComponents"]["components"]
        submit = next(component for component in components if component["id"] == "submit")
        assert submit["action"]["event"]["context"][BROWSER_LOGIN_FLOW_CONTEXT_KEY] == {
            "path": f"/{BROWSER_LOGIN_FLOW_CONTEXT_KEY}/value"
        }
        seeded_values = [
            message["updateDataModel"]
            for message in result.data["genui"]
            if "updateDataModel" in message
        ]
        assert any(value["value"] == token for value in seeded_values)

        tool_schema = BrowserAgentTool().card.input_params
        assert set(tool_schema["properties"]) == {"task"}

    @pytest.mark.asyncio
    async def test_resume_with_credentials_uses_interactive_input(self):
        bat._PENDING_CREDENTIAL_REQUESTS["tok-123"] = {
            "inner_session_id": "browser-agent-abc",
            "inner_id": "tc-1",
            "conversation_id": "",
            "credential_keys": ["username", "password"],
            "expires_at": bat.time.monotonic() + 60,
            "resuming": False,
        }
        captured = {}

        async def fake_stream(agent, run_input, session=None):
            del agent
            captured["run_input"] = run_input
            captured["session"] = session
            yield _chunk("answer", {"output": "Logged in and found 3 orders."})

        with (
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=object())),
            patch.object(bat.Runner, "run_agent_streaming", side_effect=fake_stream),
        ):
            tool = BrowserAgentTool()
            result = await tool.invoke(
                {"resume_token": "tok-123", "credentials": {"username": "alice", "password": "s3cr3t"}}
            )

        assert result.success is True
        assert "3 orders" in result.data["content"]
        assert captured["session"] == "browser-agent-abc"
        interactive_input = captured["run_input"]["query"]
        assert isinstance(interactive_input, InteractiveInput)
        assert interactive_input.user_inputs["tc-1"] == {"username": "alice", "password": "s3cr3t"}
        # The token is consumed on use.
        assert "tok-123" not in bat._PENDING_CREDENTIAL_REQUESTS

    @pytest.mark.asyncio
    async def test_resume_is_bound_to_conversation_and_keeps_token(self):
        bat._PENDING_CREDENTIAL_REQUESTS["tok-123"] = {
            "inner_session_id": "browser-agent-abc",
            "inner_id": "tc-1",
            "conversation_id": "expected-conversation",
            "credential_keys": ["username", "password"],
            "expires_at": bat.time.monotonic() + 60,
            "resuming": False,
        }

        tool = BrowserAgentTool()
        result = await tool._invoke_direct(
            {"resume_token": "tok-123", "credentials": {"username": "alice", "password": "secret"}},
            expected_conversation_id="other-conversation",
        )

        assert result.success is False
        assert "does not belong" in result.error
        assert "tok-123" in bat._PENDING_CREDENTIAL_REQUESTS

    @pytest.mark.asyncio
    async def test_resume_failure_keeps_token_retryable(self):
        bat._PENDING_CREDENTIAL_REQUESTS["tok-123"] = {
            "inner_session_id": "browser-agent-abc",
            "inner_id": "tc-1",
            "conversation_id": "c1",
            "credential_keys": ["username", "password"],
            "expires_at": bat.time.monotonic() + 60,
            "resuming": False,
        }

        async def failing_stream(*args, **kwargs):
            raise RuntimeError("temporary failure")
            yield  # pragma: no cover

        with (
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=object())),
            patch.object(bat.Runner, "run_agent_streaming", side_effect=failing_stream),
        ):
            result = await BrowserAgentTool()._invoke_direct(
                {"resume_token": "tok-123", "credentials": {"username": "alice", "password": "secret"}},
                expected_conversation_id="c1",
            )

        assert result.success is False
        assert bat._PENDING_CREDENTIAL_REQUESTS["tok-123"]["resuming"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cancel_during_startup", [False, True])
    async def test_cancelled_resume_keeps_token_retryable(self, cancel_during_startup):
        bat._PENDING_CREDENTIAL_REQUESTS["tok-123"] = {
            "inner_session_id": "browser-agent-abc",
            "inner_id": "tc-1",
            "conversation_id": "c1",
            "credential_keys": ["username", "password"],
            "expires_at": bat.time.monotonic() + 60,
            "resuming": False,
        }

        async def cancelled_stream(*args, **kwargs):
            raise asyncio.CancelledError
            yield  # pragma: no cover

        get_agent = (
            AsyncMock(side_effect=asyncio.CancelledError) if cancel_during_startup else AsyncMock(return_value=object())
        )
        with (
            patch.object(bat, "_get_browser_agent", get_agent),
            patch.object(bat.Runner, "run_agent_streaming", side_effect=cancelled_stream),
            pytest.raises(asyncio.CancelledError),
        ):
            await BrowserAgentTool()._invoke_direct(
                {"resume_token": "tok-123", "credentials": {"username": "alice", "password": "secret"}},
                expected_conversation_id="c1",
            )

        assert bat._PENDING_CREDENTIAL_REQUESTS["tok-123"]["resuming"] is False

    @pytest.mark.asyncio
    async def test_unknown_resume_token_errors_without_running(self):
        with (
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=object())),
            patch.object(bat.Runner, "run_agent_streaming") as mock_stream,
        ):
            tool = BrowserAgentTool()
            result = await tool.invoke({"resume_token": "does-not-exist", "credentials": {"username": "alice"}})

        assert result.success is False
        assert "no longer active" in result.error
        mock_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_task_and_resume_token_errors(self):
        with patch.object(bat, "_get_browser_agent", AsyncMock(return_value=object())):
            tool = BrowserAgentTool()
            result = await tool.invoke({})

        assert result.success is False
        assert "task" in result.error


class TestBrowserCredentialInterruptRailEndToEnd:
    """Drives the real framework interrupt machinery (Runner, ReActAgent,
    BrowserCredentialInterruptRail, InteractiveInput resume) with only the
    LLM mocked -- proves the pause/resume mechanism BrowserAgentTool.invoke()
    relies on actually works, not just that mocked chunks satisfy the tool's
    own code.
    """

    @pytest.mark.asyncio
    async def test_login_wall_pauses_then_resumes_with_real_rail(self):
        os.environ.setdefault("LLM_SSL_VERIFY", "false")
        await Runner.start()
        try:
            agent = ReActAgent(card=AgentCard(id="browser_e2e_agent"))
            agent_config = ReActAgentConfig()
            agent_config.configure_model_client(
                provider="OpenAI",
                api_key="sk-fake",
                api_base="https://api.openai.com/v1",
                model_name="gpt-3.5-turbo",
                verify_ssl=False,
            )
            agent_config.configure_prompt_template(
                [{"role": "system", "content": "You are a browser automation agent."}]
            )
            agent.configure(agent_config)

            Runner.resource_mgr.add_tool(request_login_credentials)
            agent.ability_manager.add(request_login_credentials.card)
            await agent.register_rail(BrowserCredentialInterruptRail())

            create_agent_session(session_id="browser_e2e_test", card=AgentCard(id="browser_e2e_agent"))

            mock_llm = MockLLMModel()
            mock_llm.set_responses(
                [
                    create_tool_call_response(
                        "request_login_credentials",
                        '{"fields": ["Username", "Password"], "reason": "example.com orders page"}',
                    ),
                    create_text_response("Logged in and found 3 orders."),
                ]
            )

            with (
                patch("openjiuwen.core.foundation.llm.model.Model.stream", side_effect=mock_llm.stream),
                patch("openjiuwen.core.foundation.llm.model.Model.invoke", side_effect=mock_llm.invoke),
            ):
                pending_interrupt = None
                async for output in Runner.run_agent_streaming(
                    agent=agent,
                    inputs={"query": "log in and check my orders", "conversation_id": "browser_e2e_test"},
                    session="browser_e2e_test",
                ):
                    if output.type == INTERACTION:
                        pending_interrupt = (output.payload.id, output.payload.value)

                assert pending_interrupt is not None, "expected the login wall to raise an interrupt"
                inner_id, request = pending_interrupt
                assert request.fields == ["Username", "Password"]
                assert request.reason == "example.com orders page"

                # This is the exact call BrowserAgentTool.invoke() makes once it
                # observes the INTERACTION chunk -- proves the real interrupt
                # payload is shaped as _build_credential_request_output expects.
                form_output = _build_credential_request_output("browser_e2e_test", inner_id, request)
                assert form_output.success is True
                assert form_output.data["genui"]
                assert len(bat._PENDING_CREDENTIAL_REQUESTS) == 1
                resume_token = next(iter(bat._PENDING_CREDENTIAL_REQUESTS))
                pending = bat._PENDING_CREDENTIAL_REQUESTS.pop(resume_token)

                interactive_input = InteractiveInput()
                interactive_input.update(pending["inner_id"], {"Username": "alice", "Password": "s3cr3t"})

                final_text = ""
                # Mirrors BrowserAgentTool.invoke()'s own resume call: Runner._prepare_agent
                # calls inputs.get(...) whenever session is a plain string, so the
                # InteractiveInput must be wrapped under "query", not passed bare.
                async for output in Runner.run_agent_streaming(
                    agent=agent,
                    inputs={"query": interactive_input},
                    session=pending["inner_session_id"],
                ):
                    if output.type == "answer":
                        final_text = output.payload.get("output") or output.payload.get("content") or final_text

                assert "3 orders" in final_text
        finally:
            bat._PENDING_CREDENTIAL_REQUESTS.clear()
            await Runner.stop()
