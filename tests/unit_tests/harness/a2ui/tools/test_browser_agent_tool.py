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
from unittest.mock import AsyncMock, MagicMock, patch

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
    BrowserOptionSelectionInterruptRail,
    BrowserOptionSelectionRequest,
    _build_credential_request_output,
    _build_option_selection_output,
    browser_handoff_to_checkout,
    request_login_credentials,
    request_option_selection,
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
    bat._PENDING_OPTION_SELECTION_REQUESTS.clear()
    yield
    bat._PENDING_CREDENTIAL_REQUESTS.clear()
    bat._PENDING_OPTION_SELECTION_REQUESTS.clear()


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
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=MagicMock())),
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
        assert set(tool_schema["properties"]) == {"task", "stop_at_seat_selection"}

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
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=MagicMock())),
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
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=MagicMock())),
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
            AsyncMock(side_effect=asyncio.CancelledError) if cancel_during_startup else AsyncMock(return_value=MagicMock())
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
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=MagicMock())),
            patch.object(bat.Runner, "run_agent_streaming") as mock_stream,
        ):
            tool = BrowserAgentTool()
            result = await tool.invoke({"resume_token": "does-not-exist", "credentials": {"username": "alice"}})

        assert result.success is False
        assert "no longer active" in result.error
        mock_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_task_and_resume_token_errors(self):
        with patch.object(bat, "_get_browser_agent", AsyncMock(return_value=MagicMock())):
            tool = BrowserAgentTool()
            result = await tool.invoke({})

        assert result.success is False
        assert "task" in result.error


def _fake_page(select_options=None, pay_now_at_point=False):
    page = MagicMock()
    page.viewport_size = {"width": 360, "height": 640}

    async def evaluate(script, *args):
        if script == bat._PAY_NOW_AT_POINT_JS:
            return pay_now_at_point
        return select_options

    page.evaluate = AsyncMock(side_effect=evaluate)
    page.keyboard = MagicMock(type=AsyncMock())
    page.mouse = MagicMock(click=AsyncMock(), move=AsyncMock(), wheel=AsyncMock())
    page.screenshot = AsyncMock(return_value=b"JPEGBYTES")
    return page


class TestResumeIterationBudget:
    @pytest.mark.asyncio
    async def test_resume_raises_the_cap_then_restores_it(self):
        bat._PENDING_OPTION_SELECTION_REQUESTS["budget-tok"] = {"inner_session_id": "s", "inner_id": "tc"}
        agent = MagicMock()

        async def fake_stream(*args, **kwargs):
            yield _chunk("answer", {"output": "done"})

        with (
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=agent)),
            patch.object(bat.Runner, "run_agent_streaming", side_effect=fake_stream),
        ):
            await BrowserAgentTool().invoke({"resume_token": "budget-tok", "credentials": {"selected_label": "x"}})

        calls = [call.args[0] for call in agent.config.configure_max_iterations.call_args_list]
        assert calls == [bat._MAX_INNER_ITERATIONS + bat._RESUME_EXTRA_ITERATIONS, bat._MAX_INNER_ITERATIONS]


class TestSeatSelectionHandoff:
    """The seat-map checkout handoff only fires for the specific follow-up
    call the outer agent marks with stop_at_seat_selection -- never on a
    plain search call that happens to end on a page _seat_map_reached()
    would also call true, which previously handed off before the user had
    chosen a departure (see agent.py's departure-choice rule)."""

    async def _run(self, inputs):
        async def fake_stream(*args, **kwargs):
            yield _chunk("answer", {"output": "Here is the seat map."})

        with (
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=MagicMock())),
            patch.object(bat.Runner, "run_agent_streaming", side_effect=fake_stream),
            patch.object(bat, "_checkout_reached", AsyncMock(return_value=False)),
            patch.object(bat, "_seat_map_reached", AsyncMock(return_value=True)),
            patch.object(bat, "_direct_page", AsyncMock(return_value=MagicMock())),
            patch.object(bat, "_checkout_handoff", AsyncMock(return_value={"url": "https://x", "cookies": []})),
        ):
            return await BrowserAgentTool().invoke(inputs)

    @pytest.mark.asyncio
    async def test_hands_off_when_the_flag_is_set_and_the_seat_map_is_reached(self):
        result = await self._run({"task": "select the 07:15 707 Inc departure", "stop_at_seat_selection": True})
        assert result.data["checkout"] == {"url": "https://x", "cookies": []}

    @pytest.mark.asyncio
    async def test_does_not_hand_off_without_the_flag_even_if_it_looks_like_a_seat_map(self):
        result = await self._run({"task": "search Singapore to Melaka buses"})
        assert "checkout" not in result.data


class TestEnsureAllPagesEmulated:
    """_ensure_all_pages_emulated: the inner agent's own Playwright MCP
    connection can open tabs our side never saw -- this sweeps every page in
    the shared context and emulates whichever ones aren't already tracked in
    _direct_sized_pages (observed live: a trip.com search left extra tabs on
    the default desktop UA after the one page open at the run's start had
    already been emulated)."""

    def setup_method(self):
        bat._direct_sized_pages.clear()

    def teardown_method(self):
        bat._direct_sized_pages.clear()

    @pytest.mark.asyncio
    async def test_emulates_every_untracked_page(self):
        page_a, page_b = MagicMock(), MagicMock()
        page_a.is_closed.return_value = False
        page_b.is_closed.return_value = False
        page_a.set_viewport_size = AsyncMock()
        page_b.set_viewport_size = AsyncMock()
        context = MagicMock()
        context.pages = [page_a, page_b]
        with patch.object(bat, "_apply_mobile_emulation", AsyncMock()) as emulate:
            await bat._ensure_all_pages_emulated(context)
        assert emulate.await_count == 2
        assert page_a in bat._direct_sized_pages
        assert page_b in bat._direct_sized_pages

    @pytest.mark.asyncio
    async def test_skips_already_tracked_and_closed_pages(self):
        tracked, closed, fresh = MagicMock(), MagicMock(), MagicMock()
        tracked.is_closed.return_value = False
        closed.is_closed.return_value = True
        fresh.is_closed.return_value = False
        fresh.set_viewport_size = AsyncMock()
        bat._direct_sized_pages.add(tracked)
        context = MagicMock()
        context.pages = [tracked, closed, fresh]
        with patch.object(bat, "_apply_mobile_emulation", AsyncMock()) as emulate:
            await bat._ensure_all_pages_emulated(context)
        emulate.assert_awaited_once_with(context, fresh)

    @pytest.mark.asyncio
    async def test_a_page_that_fails_to_emulate_does_not_stop_the_sweep(self):
        failing, fresh = MagicMock(), MagicMock()
        failing.is_closed.return_value = False
        fresh.is_closed.return_value = False
        failing.set_viewport_size = AsyncMock(side_effect=RuntimeError("navigating"))
        fresh.set_viewport_size = AsyncMock()
        context = MagicMock()
        context.pages = [failing, fresh]
        with patch.object(bat, "_apply_mobile_emulation", AsyncMock()) as emulate:
            await bat._ensure_all_pages_emulated(context)
        assert failing not in bat._direct_sized_pages
        assert fresh in bat._direct_sized_pages
        emulate.assert_awaited_once_with(context, fresh)


class TestBrowserHandoffToCheckout:
    """browser_handoff_to_checkout: the departure-choice form's follow-up
    call -- hands off the live session as-is, with no browsing of its own
    (see the agent.py rule that replaced driving seat selection itself)."""

    @pytest.mark.asyncio
    async def test_hands_off_the_current_page_with_no_browsing(self):
        page = MagicMock()
        checkout = {"url": "https://redbus.sg/seats", "user_agent": "ua", "cookies": []}
        with (
            patch.object(bat, "_direct_page", AsyncMock(return_value=page)),
            patch.object(bat, "_recover_matching_page", AsyncMock()),
            patch.object(bat, "_checkout_handoff", AsyncMock(return_value=checkout)),
        ):
            result = await browser_handoff_to_checkout.invoke({})
        assert result["checkout"] == checkout
        assert not result["text"].startswith("[ERROR]")

    @pytest.mark.asyncio
    async def test_reports_an_error_when_there_is_no_live_session(self):
        with patch.object(bat, "_direct_page", AsyncMock(side_effect=RuntimeError("no browser"))):
            result = await browser_handoff_to_checkout.invoke({})
        assert "checkout" not in result
        assert result["text"].startswith("[ERROR]")

    @pytest.mark.asyncio
    async def test_selected_label_splits_into_the_operator_and_time_the_checkout_view_filters_on(self):
        checkout = {"url": "https://redbus.sg/seats", "user_agent": "ua", "cookies": []}
        with (
            patch.object(bat, "_direct_page", AsyncMock(return_value=MagicMock())),
            patch.object(bat, "_recover_matching_page", AsyncMock()),
            patch.object(bat, "_checkout_handoff", AsyncMock(return_value=checkout)),
        ):
            result = await browser_handoff_to_checkout.invoke({"selected_label": "707-Inc · 07:15"})
        assert result["checkout"]["filter_operator"] == "707-Inc"
        assert result["checkout"]["filter_time"] == "07:15"

    @pytest.mark.asyncio
    async def test_no_selected_label_leaves_the_filter_fields_empty(self):
        checkout = {"url": "https://redbus.sg/seats", "user_agent": "ua", "cookies": []}
        with (
            patch.object(bat, "_direct_page", AsyncMock(return_value=MagicMock())),
            patch.object(bat, "_recover_matching_page", AsyncMock()),
            patch.object(bat, "_checkout_handoff", AsyncMock(return_value=checkout)),
        ):
            result = await browser_handoff_to_checkout.invoke({})
        assert result["checkout"]["filter_operator"] == ""
        assert result["checkout"]["filter_time"] == ""

    @pytest.mark.asyncio
    async def test_calls_recover_matching_page_with_the_chosen_operator_and_time(self):
        checkout = {"url": "https://trip.com/book", "user_agent": "ua", "cookies": []}
        with (
            patch.object(bat, "_direct_page", AsyncMock(return_value=MagicMock())),
            patch.object(bat, "_recover_matching_page", AsyncMock()) as recover,
            patch.object(bat, "_checkout_handoff", AsyncMock(return_value=checkout)),
        ):
            await browser_handoff_to_checkout.invoke({"selected_label": "G7331 · 06:13"})
        recover.assert_awaited_once()
        assert recover.call_args.args[1] == "G7331"
        assert recover.call_args.args[2] == "06:13"


class TestPageMatchesLabel:
    """_page_matches_label: a case-insensitive substring check of the page's
    own text, used to tell whether the live page still shows the departure
    the user chose before handing it off (see _recover_matching_page)."""

    @pytest.mark.asyncio
    async def test_empty_token_always_matches(self):
        assert await bat._page_matches_label(MagicMock(), "") is True

    @pytest.mark.asyncio
    async def test_case_insensitive_substring_match(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="Shanghai Hongqiao -> Hangzhoudong\nG7331 06:13")
        assert await bat._page_matches_label(page, "g7331") is True

    @pytest.mark.asyncio
    async def test_no_match_when_token_absent(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="Z175 04:08 -> 05:50")
        assert await bat._page_matches_label(page, "G7331") is False

    @pytest.mark.asyncio
    async def test_unreadable_page_does_not_match(self):
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=RuntimeError("detached"))
        assert await bat._page_matches_label(page, "G7331") is False


class TestPageMatchesDeparture:
    """_page_matches_departure: requires BOTH the operator and its exact
    departure time to be on the page -- regression coverage for the
    same-operator-different-time case (see its own docstring)."""

    @pytest.mark.asyncio
    async def test_true_when_both_operator_and_time_are_present(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="707-Inc · 12:30 -> Melaka Sentral")
        assert await bat._page_matches_departure(page, "707-Inc", "12:30") is True

    @pytest.mark.asyncio
    async def test_false_when_operator_matches_but_a_different_time_is_shown(self):
        # Regression test: observed live, the same operator ran more than
        # one departure ("707-Inc" at both 07:15 and 12:30) -- a page for
        # the wrong one of those still has the operator's name on it, so an
        # operator-only check would wrongly accept it.
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="707-Inc · 07:15 -> Melaka Sentral")
        assert await bat._page_matches_departure(page, "707-Inc", "12:30") is False

    @pytest.mark.asyncio
    async def test_false_when_time_matches_but_a_different_operator_is_shown(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="Delima Express · 12:30 -> Melaka Sentral")
        assert await bat._page_matches_departure(page, "707-Inc", "12:30") is False

    @pytest.mark.asyncio
    async def test_empty_operator_and_time_always_matches(self):
        assert await bat._page_matches_departure(MagicMock(), "", "") is True


class _FakeHistoryPage:
    """A minimal stand-in for a Playwright Page that actually models browser
    history as a position in a list, so go_back/go_forward/goto behave like
    the real thing instead of needing a brittle chain of mocked return
    values. `history` is oldest-first; `start` is the current entry's index."""

    def __init__(self, history: list[tuple[str, str]], start: int):
        self._history = list(history)
        self._index = start
        self.go_back_calls = 0
        self.go_forward_calls = 0
        self.goto_urls: list[str] = []

    @property
    def url(self) -> str:
        return self._history[self._index][0]

    async def evaluate(self, _script: str) -> str:
        return self._history[self._index][1]

    async def go_back(self, **_kwargs):
        self.go_back_calls += 1
        if self._index == 0:
            raise RuntimeError("no more history")
        self._index -= 1

    async def go_forward(self, **_kwargs):
        self.go_forward_calls += 1
        self._index += 1

    async def goto(self, url: str, **_kwargs):
        self.goto_urls.append(url)
        for i, (entry_url, _) in enumerate(self._history):
            if entry_url == url:
                self._index = i
                return


class TestRecoverMatchingPage:
    """_recover_matching_page: the search run can leave the live page on a
    *different* result's own page (it often clicks into individual results
    to read exact details) -- this steps back through browser history
    looking for a page that shows the one the user actually chose, by both
    its operator and its exact departure time. It must never follow that
    history past the current site's own origin (see its own docstring for
    the live trip.com/redBus cross-contamination this guards against)."""

    @pytest.mark.asyncio
    async def test_already_matching_page_is_left_alone(self):
        page = _FakeHistoryPage([("https://redbus.sg/x", "707-Inc 12:30")], start=0)
        await bat._recover_matching_page(page, "707-Inc", "12:30")
        assert page.go_back_calls == 0

    @pytest.mark.asyncio
    async def test_steps_back_through_history_until_both_operator_and_time_match(self):
        # The current page has the right operator but the wrong time (a
        # different departure by the same operator) -- an operator-only
        # check would have stopped here wrongly; this must keep going.
        page = _FakeHistoryPage(
            [
                ("https://redbus.sg/listing", "707-Inc 12:30 and other departures"),
                ("https://redbus.sg/detail-0715", "707-Inc 07:15"),
            ],
            start=1,
        )
        await bat._recover_matching_page(page, "707-Inc", "12:30")
        assert page.go_back_calls == 1
        assert page.url == "https://redbus.sg/listing"

    @pytest.mark.asyncio
    async def test_gives_up_and_restores_the_original_page_if_nothing_matches(self):
        history = [
            ("https://redbus.sg/a", "Z175 04:08"),
            ("https://redbus.sg/b", "Z175 04:08"),
            ("https://redbus.sg/c", "Z175 04:08"),
            ("https://redbus.sg/d", "Z175 04:08"),
        ]
        page = _FakeHistoryPage(history, start=3)
        await bat._recover_matching_page(page, "G7331", "06:13")
        assert page.go_back_calls == 3
        # Nothing on-site matched -- restored to where it started, not left
        # on whatever the last go_back happened to land on.
        assert page.url == "https://redbus.sg/d"
        assert page.goto_urls == ["https://redbus.sg/d"]

    @pytest.mark.asyncio
    async def test_stops_if_there_is_no_more_history(self):
        page = _FakeHistoryPage([("https://redbus.sg/a", "Z175 04:08")], start=0)
        await bat._recover_matching_page(page, "G7331", "06:13")
        assert page.go_back_calls == 1
        assert page.goto_urls == []  # never moved, so no restore needed

    @pytest.mark.asyncio
    async def test_never_follows_history_past_the_current_site_into_a_different_one(self):
        # Regression test: observed live, a shared long-lived browser tab's
        # history reached back through `about:blank` (the boundary a prior
        # `reset` left behind) into a leftover trip.com train search from an
        # unrelated earlier test run, and the checkout handoff landed there.
        history = [
            ("https://www.trip.com/trains/list", "G7331 06:13 Shanghai -> Hangzhou"),
            ("about:blank", ""),
            ("https://www.redbus.sg/", "redBus home page, no departure info"),
        ]
        page = _FakeHistoryPage(history, start=2)
        await bat._recover_matching_page(page, "707-Inc", "07:45")
        assert page.go_back_calls == 1  # stepped into about:blank, then stopped
        assert page.go_forward_calls == 1  # undid that one step
        assert page.url == "https://www.redbus.sg/"  # never reached trip.com
        assert page.goto_urls == []  # go_forward already restored it


class TestPerformBrowserInput:
    """perform_browser_input relays user taps/typing straight to the page."""

    @pytest.mark.asyncio
    async def test_type_sends_text_to_the_focused_field_and_returns_frame(self):
        page = _fake_page()
        with patch.object(bat, "_direct_page", AsyncMock(return_value=page)):
            result = await bat.perform_browser_input({"kind": "type", "text": "4111 1111"})

        page.keyboard.type.assert_awaited_once_with("4111 1111")
        assert result["frame"]["base64"] == "SlBFR0JZVEVT"
        assert result["select_options"] is None

    @pytest.mark.asyncio
    async def test_tap_clicks_at_the_scaled_viewport_point(self):
        page = _fake_page()
        with patch.object(bat, "_direct_page", AsyncMock(return_value=page)):
            await bat.perform_browser_input({"kind": "tap", "x": 0.5, "y": 0.25})

        page.mouse.click.assert_awaited_once_with(180.0, 160.0)

    @pytest.mark.asyncio
    async def test_tap_on_a_dropdown_returns_its_options_instead_of_clicking(self):
        page = _fake_page(select_options=["Gender", "Male", "Female"])
        with patch.object(bat, "_direct_page", AsyncMock(return_value=page)):
            result = await bat.perform_browser_input({"kind": "tap", "x": 0.5, "y": 0.5})

        assert result["select_options"] == ["Gender", "Male", "Female"]
        page.mouse.click.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tap_on_pay_now_hands_the_session_off_instead_of_clicking(self):
        page = _fake_page(pay_now_at_point=True)
        page.url = "https://www.redbus.sg/checkout/passengers"
        page.context = MagicMock()
        page.context.cookies = AsyncMock(return_value=[
            {"name": "sid", "value": "abc", "domain": ".redbus.sg", "path": "/", "secure": True, "expires": -1},
            {"name": "ad", "value": "x", "domain": ".example.org", "path": "/", "expires": -1},
            {"name": "evil", "value": "z", "domain": "evilredbus.sg", "path": "/", "expires": -1},
            {"name": "cf_clearance", "value": "y", "domain": ".redbus.sg", "path": "/", "expires": -1},
        ])
        with patch.object(bat, "_direct_page", AsyncMock(return_value=page)):
            result = await bat.perform_browser_input({"kind": "tap", "x": 0.5, "y": 0.5})

        page.mouse.click.assert_not_awaited()
        assert result["checkout"]["url"] == "https://www.redbus.sg/checkout/passengers"
        assert result["checkout"]["user_agent"] == bat._MOBILE_USER_AGENT
        assert [c["name"] for c in result["checkout"]["cookies"]] == ["sid"]

    @pytest.mark.asyncio
    async def test_scroll_wheels_at_the_centre_of_the_viewport(self):
        page = _fake_page()
        with patch.object(bat, "_direct_page", AsyncMock(return_value=page)):
            await bat.perform_browser_input({"kind": "scroll", "dy": 120})

        page.mouse.move.assert_awaited_once_with(180.0, 320.0)
        page.mouse.wheel.assert_awaited_once_with(0, 120.0)

    @pytest.mark.asyncio
    async def test_unreachable_browser_raises_for_the_caller_to_report(self):
        with patch.object(bat, "_direct_page", AsyncMock(side_effect=RuntimeError("no browser"))):
            with pytest.raises(RuntimeError):
                await bat.perform_browser_input({"kind": "tap", "x": 0.5, "y": 0.5})

    @pytest.mark.asyncio
    async def test_reset_closes_every_other_open_tab_and_blanks_the_current_one(self):
        # Regression test: tabs opened by past searches (clicking into an
        # individual result opens a new one) were never closed, so "New"
        # only blanking the one page _direct_page() happened to resolve to
        # left the others accumulating indefinitely -- observed live, five
        # tabs deep, with the live view ending up on a stale one instead of
        # whatever the next search actually used.
        page = _fake_page()
        page.goto = AsyncMock()
        still_open = MagicMock()
        still_open.is_closed.return_value = False
        still_open.close = AsyncMock()
        already_closed = MagicMock()
        already_closed.is_closed.return_value = True
        already_closed.close = AsyncMock()
        context = MagicMock()
        context.pages = [still_open, page, already_closed]
        page.context = context

        with patch.object(bat, "_direct_page", AsyncMock(return_value=page)):
            await bat.perform_browser_input({"kind": "reset"})

        still_open.close.assert_awaited_once()
        already_closed.close.assert_not_awaited()
        page.goto.assert_awaited_once_with("about:blank")

    @pytest.mark.asyncio
    async def test_reset_tolerates_a_tab_that_fails_to_close(self):
        page = _fake_page()
        page.goto = AsyncMock()
        stubborn = MagicMock()
        stubborn.is_closed.return_value = False
        stubborn.close = AsyncMock(side_effect=RuntimeError("target already navigating"))
        context = MagicMock()
        context.pages = [page, stubborn]
        page.context = context

        with patch.object(bat, "_direct_page", AsyncMock(return_value=page)):
            await bat.perform_browser_input({"kind": "reset"})

        page.goto.assert_awaited_once_with("about:blank")


class TestBrowserCheckoutStopRail:
    """Page actions are refused once the page shows passenger details or
    payment, and allowed everywhere before it."""

    @pytest.mark.asyncio
    async def test_rejects_actions_once_checkout_is_reached(self):
        with patch.object(bat, "_checkout_reached", AsyncMock(return_value=True)):
            decision = await bat.BrowserCheckoutStopRail().resolve_interrupt(None, None, None)
        assert "STOP" in str(decision.tool_result)

    @pytest.mark.asyncio
    async def test_allows_actions_before_checkout(self):
        with patch.object(bat, "_checkout_reached", AsyncMock(return_value=False)):
            decision = await bat.BrowserCheckoutStopRail().resolve_interrupt(None, None, None)
        assert type(decision).__name__ == "ApproveResult"


class TestSeatMapReached:
    """_seat_map_reached: true once the page's own text shows a seat picker,
    in English or Chinese -- the point a departure-choice run is told to
    stop at and hand off to the app's checkout view."""

    @pytest.mark.asyncio
    async def test_true_for_an_english_seat_picker(self):
        page = _fake_page()
        page.evaluate = AsyncMock(return_value="Choose Seats for Departing Trip\n14A 14B 14C")
        with patch.object(bat, "_direct_page", AsyncMock(return_value=page)):
            assert await bat._seat_map_reached() is True

    @pytest.mark.asyncio
    async def test_true_for_a_chinese_seat_picker(self):
        page = _fake_page()
        page.evaluate = AsyncMock(return_value="请选择座位\n选座图")
        with patch.object(bat, "_direct_page", AsyncMock(return_value=page)):
            assert await bat._seat_map_reached() is True

    @pytest.mark.asyncio
    async def test_false_for_an_ordinary_results_page(self):
        page = _fake_page()
        page.evaluate = AsyncMock(return_value="Singapore to Malacca -- 376 trips found")
        with patch.object(bat, "_direct_page", AsyncMock(return_value=page)):
            assert await bat._seat_map_reached() is False

    @pytest.mark.asyncio
    async def test_false_when_the_page_cannot_be_read(self):
        with patch.object(bat, "_direct_page", AsyncMock(side_effect=RuntimeError("no browser"))):
            assert await bat._seat_map_reached() is False


class TestEasybookAutoCredentials:
    """_easybook_auto_credentials: the standing easybook.com login, scoped
    strictly to the live page URL so it's never typed into an unrelated
    site's login form (see BrowserCredentialInterruptRail.resolve_interrupt,
    which calls this before pausing for the real user)."""

    def setup_method(self):
        bat.app_config.set_value("EASYBOOK_USERNAME", "agent@example.com")
        bat.app_config.set_value("EASYBOOK_PASSWORD", "s3cr3t-pw")

    def teardown_method(self):
        bat.app_config.set_value("EASYBOOK_USERNAME", "")
        bat.app_config.set_value("EASYBOOK_PASSWORD", "")

    def test_maps_fields_when_on_easybook(self):
        mapped = bat._easybook_auto_credentials(
            ["Email", "Password"], "https://www.easybook.com/en-sg/bus/login"
        )

        assert mapped == {"Email": "agent@example.com", "Password": "s3cr3t-pw"}

    def test_none_when_not_on_easybook(self):
        assert bat._easybook_auto_credentials(
            ["Email", "Password"], "https://www.some-other-bus-site.com/login"
        ) is None

    def test_none_when_credentials_not_configured(self):
        bat.app_config.set_value("EASYBOOK_USERNAME", "")

        assert bat._easybook_auto_credentials(
            ["Email", "Password"], "https://www.easybook.com/en-sg/bus/login"
        ) is None

    def test_none_when_a_field_cannot_be_mapped(self):
        assert bat._easybook_auto_credentials(
            ["Email", "Password", "Captcha"], "https://www.easybook.com/en-sg/bus/login"
        ) is None

    def test_none_when_url_unknown(self):
        assert bat._easybook_auto_credentials(["Email", "Password"], "") is None


class TestBrowserAgentToolOptionSelection:
    """Mirrors TestBrowserAgentToolInterrupt above, for the
    request_option_selection pause/resume path instead of the login one."""

    @pytest.mark.asyncio
    async def test_multiple_options_returns_selection_card(self):
        options = [
            {"label": "707 Inc - 07:15 AM Ban San Street -> Melaka Sentral - USD 24.03"},
            {"label": "Delima Express - 08:30 Golden Mile Tower -> Melaka Sentral - USD 30.00"},
        ]

        async def fake_stream(*args, **kwargs):
            yield _chunk(
                INTERACTION,
                SimpleNamespace(
                    id="tc-2",
                    value=BrowserOptionSelectionRequest(
                        message="Selection required",
                        options=options,
                        prompt="Choose a bus departure for Singapore -> Melaka on 2026-10-10",
                    ),
                ),
            )

        with (
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=MagicMock())),
            patch.object(bat.Runner, "run_agent_streaming", side_effect=fake_stream),
        ):
            tool = BrowserAgentTool()
            result = await tool.invoke({"task": "find bus tickets from Singapore to Melaka on 2026-10-10"})

        assert result.success is True
        # The server resumes this directly (see resume_browser_option_selection);
        # the model is never told a resume_token to echo back.
        assert "resume_token" not in result.data["content"]
        assert result.data["genui"]
        assert len(bat._PENDING_OPTION_SELECTION_REQUESTS) == 1
        # No leakage into the login-request bookkeeping.
        assert len(bat._PENDING_CREDENTIAL_REQUESTS) == 0

    @pytest.mark.asyncio
    async def test_resume_with_selected_option_uses_interactive_input(self):
        bat._PENDING_OPTION_SELECTION_REQUESTS["sel-tok-1"] = {
            "inner_session_id": "browser-agent-xyz",
            "inner_id": "tc-2",
        }
        captured = {}

        async def fake_stream(agent, run_input, session=None):
            del agent
            captured["run_input"] = run_input
            captured["session"] = session
            yield _chunk("answer", {"output": "Selected 707 Inc and reached the checkout page."})

        with (
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=MagicMock())),
            patch.object(bat.Runner, "run_agent_streaming", side_effect=fake_stream),
        ):
            tool = BrowserAgentTool()
            result = await tool.invoke(
                {
                    "resume_token": "sel-tok-1",
                    "credentials": {"selected_label": "707 Inc - 07:15 AM Ban San Street -> Melaka Sentral"},
                }
            )

        assert result.success is True
        assert "checkout" in result.data["content"]
        assert captured["session"] == "browser-agent-xyz"
        interactive_input = captured["run_input"]["query"]
        assert isinstance(interactive_input, InteractiveInput)
        assert interactive_input.user_inputs["tc-2"] == {
            "selected_label": "707 Inc - 07:15 AM Ban San Street -> Melaka Sentral"
        }
        # The token is consumed on use.
        assert "sel-tok-1" not in bat._PENDING_OPTION_SELECTION_REQUESTS

    @pytest.mark.asyncio
    async def test_unknown_selection_resume_token_errors_without_running(self):
        with (
            patch.object(bat, "_get_browser_agent", AsyncMock(return_value=MagicMock())),
            patch.object(bat.Runner, "run_agent_streaming") as mock_stream,
        ):
            tool = BrowserAgentTool()
            result = await tool.invoke(
                {"resume_token": "does-not-exist", "credentials": {"selected_label": "anything"}}
            )

        assert result.success is False
        assert "no longer active" in result.error
        mock_stream.assert_not_called()

    def test_build_option_selection_output_stores_pending_and_renders_choices(self):
        request = BrowserOptionSelectionRequest(
            message="Selection required",
            options=[
                {"label": "Option A"},
                {"label": "Option B"},
            ],
            prompt="Choose one",
        )

        output = _build_option_selection_output("browser-agent-abc", "tc-3", request)

        assert output.success is True
        assert "resume_token" not in output.data["content"]
        assert output.data["genui"]
        assert len(bat._PENDING_OPTION_SELECTION_REQUESTS) == 1
        resume_token = next(iter(bat._PENDING_OPTION_SELECTION_REQUESTS))
        assert bat._PENDING_OPTION_SELECTION_REQUESTS[resume_token] == {
            "inner_session_id": "browser-agent-abc",
            "inner_id": "tc-3",
        }


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


class TestBrowserOptionSelectionInterruptRailEndToEnd:
    """Mirrors TestBrowserCredentialInterruptRailEndToEnd above, for
    BrowserOptionSelectionInterruptRail/request_option_selection -- drives
    the real framework interrupt machinery with only the LLM mocked."""

    @pytest.mark.asyncio
    async def test_option_wall_pauses_then_resumes_with_real_rail(self):
        os.environ.setdefault("LLM_SSL_VERIFY", "false")
        await Runner.start()
        try:
            agent = ReActAgent(card=AgentCard(id="browser_option_e2e_agent"))
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

            Runner.resource_mgr.add_tool(request_option_selection)
            agent.ability_manager.add(request_option_selection.card)
            await agent.register_rail(BrowserOptionSelectionInterruptRail())

            create_agent_session(session_id="browser_option_e2e_test", card=AgentCard(id="browser_option_e2e_agent"))

            mock_llm = MockLLMModel()
            mock_llm.set_responses(
                [
                    create_tool_call_response(
                        "request_option_selection",
                        '{"options": ['
                        '{"label": "707 Inc - 07:15 AM -> Melaka Sentral - USD 24.03"}, '
                        '{"label": "Delima Express - 08:30 -> Melaka Sentral - USD 30.00"}'
                        '], "prompt": "Choose a bus departure"}',
                    ),
                    create_text_response("Selected 707 Inc and reached the checkout page."),
                ]
            )

            with (
                patch("openjiuwen.core.foundation.llm.model.Model.stream", side_effect=mock_llm.stream),
                patch("openjiuwen.core.foundation.llm.model.Model.invoke", side_effect=mock_llm.invoke),
            ):
                pending_interrupt = None
                async for output in Runner.run_agent_streaming(
                    agent=agent,
                    inputs={"query": "find and book a bus ticket", "conversation_id": "browser_option_e2e_test"},
                    session="browser_option_e2e_test",
                ):
                    if output.type == INTERACTION:
                        pending_interrupt = (output.payload.id, output.payload.value)

                assert pending_interrupt is not None, "expected multiple options to raise an interrupt"
                inner_id, request = pending_interrupt
                assert len(request.options) == 2
                assert request.prompt == "Choose a bus departure"

                # This is the exact call BrowserAgentTool.invoke() makes once it
                # observes the INTERACTION chunk -- proves the real interrupt
                # payload is shaped as _build_option_selection_output expects.
                card_output = _build_option_selection_output("browser_option_e2e_test", inner_id, request)
                assert card_output.success is True
                assert card_output.data["genui"]
                assert len(bat._PENDING_OPTION_SELECTION_REQUESTS) == 1
                resume_token = next(iter(bat._PENDING_OPTION_SELECTION_REQUESTS))
                pending = bat._PENDING_OPTION_SELECTION_REQUESTS.pop(resume_token)

                interactive_input = InteractiveInput()
                interactive_input.update(
                    pending["inner_id"], {"selected_label": "707 Inc - 07:15 AM -> Melaka Sentral - USD 24.03"}
                )

                final_text = ""
                async for output in Runner.run_agent_streaming(
                    agent=agent,
                    inputs={"query": interactive_input},
                    session=pending["inner_session_id"],
                ):
                    if output.type == "answer":
                        final_text = output.payload.get("output") or output.payload.get("content") or final_text

                assert "checkout" in final_text
        finally:
            bat._PENDING_OPTION_SELECTION_REQUESTS.clear()
            await Runner.stop()


class TestBrowserStepText:
    """``_step_text`` feeds the client's live action log (see ``browser.step``
    in ws_session.py), in Chinese, and only for the handful of tool names
    worth a line -- everything else (clicks, raw DOM probes/snapshots, the
    script-injection escape hatch, key presses, ...) used to get one too,
    dozens per search, drowning out the few that told the user something
    real (see _STEP_TEXT_BUILDERS)."""

    @pytest.mark.parametrize(
        "tool, args, expected",
        [
            ("browser_navigate", {"url": "https://example.com"}, "正在前往 https://example.com…"),
            ("browser_navigate", {}, "正在跳转…"),
            ("browser_type", {"element": "From"}, "正在填写表单…"),
            ("browser_fill_form", {}, "正在填写表单…"),
            ("browser_select_option", {}, "正在选择选项…"),
            ("browser_probe_cards", {}, "正在读取页面上的车次信息…"),
        ],
    )
    def test_the_important_tools_get_chinese_text(self, tool, args, expected):
        assert bat._step_text(tool, args) == expected

    @pytest.mark.parametrize(
        "tool",
        [
            "browser_click",
            "browser_snapshot",
            "browser_probe_interactives",
            "browser_batch_interact",
            "browser_evaluate",
            "browser_run_code",
            "browser_run_code_unsafe",
            "browser_press_key",
            "some_unknown_tool",
            "",
        ],
    )
    def test_every_other_tool_produces_no_log_line(self, tool):
        assert bat._step_text(tool, {}) == ""

    def test_server_qualified_tool_name_is_normalized_before_matching(self):
        text = bat._step_text("mcp_playwright-official_browser_navigate", {"url": "https://example.com"})

        assert text == "正在前往 https://example.com…"


class TestBrowserStepTextToolArgsCoercion:
    """A2uiToolEventRail relays ``ctx.inputs.tool_args`` verbatim, which is
    ``ToolCall.arguments`` -- a raw JSON string, not a dict. Step text that
    read it as a dict silently lost every argument it needed."""

    def test_json_string_args_are_parsed(self):
        raw_args = '{"url": "https://www.easybook.com/en-my/bus/booking"}'
        assert bat._step_text("browser_navigate", raw_args) == "正在前往 https://www.easybook.com/en-my/bus/booking…"

    @pytest.mark.parametrize("raw_args", ["", "   ", "not json", '["a", "b"]', None, 42])
    def test_malformed_or_non_object_args_degrade_gracefully(self, raw_args):
        assert bat._step_text("browser_navigate", raw_args) == "正在跳转…"

    def test_dict_args_still_work(self):
        assert bat._step_text("browser_navigate", {"url": "https://example.com"}) == "正在前往 https://example.com…"


def _interaction_ctx(*, tools, available):
    """A minimal AgentCallbackContext stand-in for the rail under test."""
    ability_manager = SimpleNamespace(list_tool_info=AsyncMock(return_value=list(available)))
    builder = SimpleNamespace(add_section=MagicMock())
    agent = SimpleNamespace(ability_manager=ability_manager, system_prompt_builder=builder)
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=tools))
    return ctx, ability_manager, builder


class TestBrowserInteractionAvailabilityRail:
    """BrowserRuntimeRail wipes ``inputs.tools`` and tells the model not to call
    tools once the browser state goes terminal. That silently disabled the
    booking hand-off, so this rail puts back only the two tools that can still
    hand a choice or a login wall back to the user."""

    INTERACTION_TOOLS = ["request_option_selection", "request_login_credentials"]

    @pytest.mark.asyncio
    async def test_restores_the_interaction_tools_when_stripped(self):
        ctx, ability_manager, builder = _interaction_ctx(tools=[], available=self.INTERACTION_TOOLS)

        await bat.BrowserInteractionAvailabilityRail().before_model_call(ctx)

        assert ctx.inputs.tools == self.INTERACTION_TOOLS
        ability_manager.list_tool_info.assert_awaited_once_with(list(bat._BROWSER_INTERACTION_TOOL_NAMES))
        assert len(builder.add_section.call_args_list) == 1

    @pytest.mark.asyncio
    async def test_left_alone_on_an_ordinary_iteration(self):
        existing = ["browser_navigate", "browser_click"]
        ctx, ability_manager, builder = _interaction_ctx(tools=list(existing), available=self.INTERACTION_TOOLS)

        await bat.BrowserInteractionAvailabilityRail().before_model_call(ctx)

        assert ctx.inputs.tools == existing
        ability_manager.list_tool_info.assert_not_awaited()
        builder.add_section.assert_not_called()

    @pytest.mark.asyncio
    async def test_stays_empty_when_the_tools_are_not_registered(self):
        ctx, _ability_manager, builder = _interaction_ctx(tools=[], available=[])

        await bat.BrowserInteractionAvailabilityRail().before_model_call(ctx)

        assert ctx.inputs.tools == []
        builder.add_section.assert_not_called()

    @pytest.mark.asyncio
    async def test_survives_a_missing_ability_manager(self):
        ctx = SimpleNamespace(agent=SimpleNamespace(), inputs=SimpleNamespace(tools=[]))

        await bat.BrowserInteractionAvailabilityRail().before_model_call(ctx)

        assert ctx.inputs.tools == []

    def test_outranks_the_runtime_terminal_synthesis_it_undoes(self):
        rail_priority = bat.BrowserInteractionAvailabilityRail.priority
        runtime_rail_priority = 50  # AgentRail default, as BrowserRuntimeRail uses
        terminal_synthesis_priority = 100  # "do not call tools" section

        # Higher runs first, so the rail must sort after the runtime rail...
        assert rail_priority < runtime_rail_priority
        # ...and its prompt section must outrank the "do not call tools" one.
        assert bat._BROWSER_INTERACTION_SECTION_PRIORITY > terminal_synthesis_priority

    def test_override_section_tells_the_model_it_may_still_pause(self):
        _ctx, _ability_manager, builder = _interaction_ctx(tools=[], available=self.INTERACTION_TOOLS)

        bat.BrowserInteractionAvailabilityRail._add_override_section(_ctx)

        section = builder.add_section.call_args.args[0]
        assert section.name == "browser_interaction_override"
        assert section.priority == bat._BROWSER_INTERACTION_SECTION_PRIORITY
        assert "request_option_selection" in section.content["en"]
        assert "request_login_credentials" in section.content["en"]


class _FakeRailContext:
    """AgentCallbackContext stand-in that also models force-finish requests."""

    def __init__(self, *, tools, available, tool_calls=()):
        self.inputs = SimpleNamespace(
            tools=tools,
            response=SimpleNamespace(tool_calls=list(tool_calls)),
        )
        self.agent = SimpleNamespace(
            ability_manager=SimpleNamespace(list_tool_info=AsyncMock(return_value=list(available))),
            system_prompt_builder=SimpleNamespace(add_section=MagicMock()),
        )
        self._force_finish = None

    def request_force_finish(self, result):
        self._force_finish = result

    def consume_force_finish(self):
        request, self._force_finish = self._force_finish, None
        return request


class TestBrowserInteractionRailHoldsTheForceFinish:
    """BrowserRuntimeRail.after_model_call force-finishes on *any* tool call
    made while the state is terminal. For a restored interaction call that
    discarded it before it could run, so no card was ever built."""

    INTERACTION_CALL = SimpleNamespace(name="request_option_selection")

    @staticmethod
    def _terminal_finish(ctx):
        ctx.request_force_finish({"error": "browser_task_incomplete"})

    @pytest.mark.asyncio
    async def test_holds_the_finish_so_a_restored_interaction_call_can_run(self):
        ctx = _FakeRailContext(
            tools=[], available=["request_option_selection"], tool_calls=[self.INTERACTION_CALL]
        )
        rail = bat.BrowserInteractionAvailabilityRail()
        await rail.before_model_call(ctx)
        self._terminal_finish(ctx)

        await rail.after_model_call(ctx)

        # Cleared, so the loop proceeds to execute the call and the interrupt
        # rail can pause the run to show the card.
        assert ctx.consume_force_finish() is None

    @pytest.mark.asyncio
    async def test_leaves_the_finish_alone_for_a_browser_tool_call(self):
        ctx = _FakeRailContext(
            tools=[], available=["request_option_selection"], tool_calls=[SimpleNamespace(name="browser_click")]
        )
        rail = bat.BrowserInteractionAvailabilityRail()
        await rail.before_model_call(ctx)
        self._terminal_finish(ctx)

        await rail.after_model_call(ctx)

        assert ctx.consume_force_finish() is not None

    @pytest.mark.asyncio
    async def test_ignores_a_finish_on_a_turn_it_did_not_enable(self):
        ctx = _FakeRailContext(
            tools=["browser_click"], available=["request_option_selection"], tool_calls=[self.INTERACTION_CALL]
        )
        rail = bat.BrowserInteractionAvailabilityRail()
        await rail.before_model_call(ctx)  # tools were present -> nothing restored
        self._terminal_finish(ctx)

        await rail.after_model_call(ctx)

        assert ctx.consume_force_finish() is not None

    @pytest.mark.asyncio
    async def test_an_ordinary_turn_never_touches_the_finish(self):
        ctx = _FakeRailContext(
            tools=[], available=["request_option_selection"], tool_calls=[self.INTERACTION_CALL]
        )
        rail = bat.BrowserInteractionAvailabilityRail()
        await rail.before_model_call(ctx)

        await rail.after_model_call(ctx)  # no finish was ever requested

        assert ctx.consume_force_finish() is None

    @pytest.mark.parametrize(
        "tool_call, expected",
        [
            (SimpleNamespace(name="request_option_selection"), "request_option_selection"),
            (SimpleNamespace(function=SimpleNamespace(name="request_login_credentials")), "request_login_credentials"),
            (SimpleNamespace(), ""),
        ],
    )
    def test_tool_call_name_reads_both_wrapper_shapes(self, tool_call, expected):
        assert bat._tool_call_name(tool_call) == expected
