# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Multi-step autonomous browser agent tool, with live step streaming.

Wraps the framework's Playwright MCP browser runtime
(``openjiuwen.harness.tools.browser_move``) behind a small internal
``ReActAgent`` that can navigate/click/fill/extract across several real
pages in one call -- e.g. actually searching a real bus-ticket site for
routes and prices. Unlike ``browser_tools.browser_inspect_page``
(single-shot, read-only, never clicks/fills/submits anything), this tool
can submit search/filter forms to retrieve real results, but it must never
complete a real purchase/booking/reservation -- see ``_INNER_SYSTEM_PROMPT``.

Each of the inner agent's own tool calls is relayed live to the *outer*
a2ui session via ``session.write_stream`` (``type="browser_agent_step"``),
so the client's action-log panel can show them as they happen. This only
works from a hand-written ``Tool`` subclass, not the ``@tool`` decorator
(``LocalFunction.invoke`` drops the ``session`` kwarg and can't stream
mid-call) -- see ``A2uiToolEventRail``, the same mechanism reused here to
capture the inner agent's own tool_call/tool_result/tool_error chunks.

After each finished step, a screenshot of the current page is captured
directly (bypassing the inner model -- ``browser_take_screenshot`` is a
"vision"-tier tool this agent isn't given, so this calls it straight
through the runtime's own Playwright MCP client) and attached to that
step's event as base64, so the client can show a live view of the actual
page next to the action log, mirroring the reference product's split
chat-log/live-browser layout.
"""

import asyncio
import base64
import json
import os
import re
import secrets
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolOutput, tool
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.prompts.builder import PromptSection
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.rails.context_engineer import ContextProcessorRail
from openjiuwen.harness.rails.interrupt.interrupt_base import BaseInterruptRail, InterruptDecision
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_capabilities import (
    resolve_browser_capabilities,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_logging import (
    browser_agent_log_info,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_state_context_processor import (
    BrowserStateContextProcessorConfig,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_working_context_processor import (
    BrowserWorkingContextProcessorConfig,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    build_browser_guardrails,
    build_playwright_mcp_config,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.runtime import (
    BrowserAgentRuntime,
    BrowserRuntimeRail,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.runtime_tools import (
    build_browser_runtime_tools,
)

from ..core import config as app_config
from ..core import genui
from ..core.rails import A2uiToolEventRail

_INPUT_PARAMS = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": (
                "A specific, self-contained browser task in natural language, e.g. "
                "'Search a real bus ticket site for buses from Singapore to Melaka on "
                "2026-10-10 and list the available operators, departure times, and "
                "prices.' Include every constraint the user already gave (route, dates, "
                "passenger count, etc.) -- the browser agent only sees this string, not "
                "the rest of the conversation."
            ),
        },
    },
    "required": ["task"],
}

# Real booking sites often need a long exploratory chain (probe, navigate,
# retry past a date-picker/anti-bot check, probe again...) before reaching a
# genuinely confirmed set of results -- 30 wasn't always enough headroom left
# over to also pause for request_option_selection/request_login_credentials
# once it got there, observed live against real sites.
_MAX_INNER_ITERATIONS = 45
# A paused run resumes its own iteration numbering, so resuming shares the
# original cap. A pause always happens within the first _MAX_INNER_ITERATIONS
# steps, so raising the cap by this much on resume leaves a full allowance for
# the steps after the user's pick (seat selection, etc.).
_RESUME_EXTRA_ITERATIONS = 45

# TODO(frontend-secure-credentials): replace this hidden generic-A2UI context
# value with a dedicated browser.credentials.submit message emitted by a
# native, masked credential form.  It is deliberately non-secret: it only
# identifies server-side pending state and is bound to one conversation.
BROWSER_LOGIN_FLOW_CONTEXT_KEY = "__browser_login_flow_id"
_LOGIN_FLOW_TTL_SECONDS = 5 * 60

# Same hidden-context-binding trick as BROWSER_LOGIN_FLOW_CONTEXT_KEY above,
# for the (non-sensitive) option-selection resume -- see ws_session.py.
BROWSER_OPTION_SELECTION_FLOW_CONTEXT_KEY = "__browser_option_selection_flow_id"

_INNER_SYSTEM_PROMPT = """You are a browser automation agent that carries out one real, \
multi-step web task per call: navigating, scrolling, clicking, filling in forms, and \
extracting information from real pages using the available browser tools.

You MAY click "Search"/"Submit"/"Apply"/"Filter"-style controls freely whenever doing so \
retrieves information -- e.g. submitting a route/date search form on a bus, flight, or \
hotel site to see the list of available operators, times, and prices -- and, once the real \
user has picked a specific option (see request_option_selection below), you may also \
select that option and proceed on its behalf (choosing a seat, confirming the route, \
logging in via request_login_credentials if needed, up to the passenger details page). \
All of that is expected, normal, core behavior for this task, not a boundary violation. \
The passenger details page and everything after it (passenger details, payment method, \
the pay button) belong to the user -- never fill, select, or click anything there.

You must NEVER click, tap, or submit anything that would complete or finalize the real \
transaction itself -- this includes, in any language, final actions like "Pay", "Place \
order", "Confirm payment", "Confirm booking", or "Pay now", and entering real payment/card \
details. The instant the next action would actually charge money or finalize the booking, \
STOP and report back exactly where you are (including that page's URL) instead of \
proceeding -- the user always completes that final step themselves, on the real site, \
after this call hands them off right before it.

MANDATORY STEP, not optional: the moment you have two or more genuinely distinct, \
comparable results for a booking-intent task (different bus/flight/hotel departures, \
operators, times, rooms, prices, etc.), your very next tool call MUST be \
request_option_selection -- before any summary, before ending the turn, regardless of how \
many steps you have already used or how tempting it is to just report what you found \
instead. Reporting a list of real options as your final text answer, without first calling \
request_option_selection, is a mistake -- never do that for a booking-intent task once you \
have 2+ comparable results. Pass the REAL options you found as `options` (each `label` \
describing it fully and accurately: operator, time, price, etc., exactly as shown on the \
page) and a short `prompt` describing the choice. That call pauses you here; the real \
user's pick comes back as request_option_selection's own result, telling you exactly which \
option they chose. Then actually select that option on the page (e.g. click its \
"Select"/"Choose"/"Book" control) and continue the task from there, up to the passenger \
details page for that specific option -- then stop there and hand off to the user. The only \
exceptions: there is \
genuinely only one result, or the task is explicitly asking for information/comparison \
only rather than to proceed with booking one -- skip straight to acting on it or reporting \
it in those two cases only.

For a bus-ticket task specifically, prefer https://www.easybook.com as the first site to \
try over any other operator or aggregator site, unless the task names a different concrete \
site to use instead -- a standing account is on file for it, which lets you actually log \
in and proceed through seat selection (not just report a login wall as a blocker).

If a page requires you to log in (username/password, or similar) before you can proceed, \
and you do not already have credentials for it, call request_login_credentials with the \
specific fields the page's own form actually asks for and a short reason -- never guess, \
invent, or attempt to bypass a login. On easybook.com this frequently resolves immediately \
with that standing account's values instead of pausing -- treat that exactly like a real \
user's own answer (use the returned values to actually log in on the page) and continue \
the task, now picking a seat if the flow offers one. Otherwise, that call pauses you here; \
once the real user provides their credentials, they come back as request_login_credentials's \
own result -- \
use them to actually log in on the page, then continue the task from there.

Use browser_probe_interactives to see a page's controls and browser_probe_cards for \
repeated results/listings before deciding what to click or fill; use browser_navigate \
directly to a known or constructed results URL when that is faster than clicking \
through. Prefer browser_batch_interact once two or more actions in a row are already \
decided. Once you have real, concrete results, have reached a real payment wall, or hit a \
real, specific blocker, stop browsing -- but remember the mandatory step above: if what you \
have is 2+ comparable results for a booking-intent task, stopping means calling \
request_option_selection, not ending your turn with a text summary. Only write a final \
text summary once that does not apply (a single result, an info-only task, a real \
blocker, or you already reached a payment wall/checkout page). End with a concise, \
factual summary of exactly what you found or where you stopped (real operator/ \
flight/hotel names, times, prices, and the current page's URL) -- never invent or guess a \
detail you did not actually see on a page."""


class BrowserCredentialRequest(InterruptRequest):
    """Interrupt payload for request_login_credentials -- see
    BrowserCredentialInterruptRail below."""

    fields: List[str] = []
    reason: str = ""


class BrowserOptionSelectionRequest(InterruptRequest):
    """Interrupt payload for request_option_selection -- see
    BrowserOptionSelectionInterruptRail below."""

    options: List[Dict[str, str]] = []
    prompt: str = ""


def _parse_tool_call_args(tool_call: Optional[ToolCall]) -> Dict[str, Any]:
    if tool_call is None:
        return {}
    args = tool_call.arguments
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(args, dict):
        return args
    return {}


_EASYBOOK_URL_MARKER = "easybook.com"
_EASYBOOK_USERNAME_FIELD_TOKENS = ("email", "user", "mobile", "phone", "login", "account")


async def _current_page_url() -> str:
    """The live page URL. The cached page state is the fast path; it can be
    empty at a login wall, so fall back to asking the page itself."""
    runtime = _browser_runtime
    if runtime is None:
        return ""
    cached = str(runtime.export_page_state().get("url") or "")
    if cached:
        return cached
    try:
        evaluate = await runtime._get_playwright_mcp_tool("browser_evaluate")
        result = await evaluate.invoke({"function": "() => location.href"})
    except Exception:  # noqa: BLE001 -- a missing URL just means no auto-login
        return ""
    text = str(result.get("result", "")) if isinstance(result, dict) else ""
    match = re.search(r"https?://[^\s\"'`]+", text)
    return match.group(0) if match else ""


def _easybook_auto_credentials(fields: List[str], current_url: str) -> Optional[Dict[str, str]]:
    """The standing easybook.com login, if configured and actually on that
    site right now -- scoped strictly by the live page URL so these
    credentials are never typed into some other site's login form the
    browser agent happens to hit. See config.py's EASYBOOK_USERNAME/
    EASYBOOK_PASSWORD for where these come from and the exposure note.

    Maps each requested field name heuristically (anything mentioning
    "pass" is the password; anything mentioning email/user/mobile/phone/
    login/account is the username) -- if any field can't be confidently
    mapped this way, returns None rather than guessing, falling back to the
    normal pause-and-ask-the-user flow.
    """
    username = str(app_config.get("EASYBOOK_USERNAME") or "").strip()
    password = str(app_config.get("EASYBOOK_PASSWORD") or "").strip()
    if not username or not password:
        return None
    if _EASYBOOK_URL_MARKER not in current_url.lower():
        return None
    mapped: Dict[str, str] = {}
    for field in fields:
        lowered = field.lower()
        if "pass" in lowered:
            mapped[field] = password
        elif any(token in lowered for token in _EASYBOOK_USERNAME_FIELD_TOKENS):
            mapped[field] = username
    if not mapped or len(mapped) != len(fields):
        return None
    return mapped


class BrowserCredentialInterruptRail(BaseInterruptRail):
    """Pauses the inner browser agent at a login wall and waits for the real
    user's credentials.

    Registered on the *inner* browser agent only (see _get_browser_agent) --
    entirely invisible to the outer a2ui agent/session, which never sees an
    "interrupt" as a wire-level concept. BrowserAgentTool.invoke() detects
    the pause directly in its own tool_call/tool_result streaming loop (the
    interrupt request arrives as an ordinary chunk in that same stream, see
    INTERACTION handling there), builds a real A2UI login form from it, and
    later resumes this same paused tool call by re-invoking
    Runner.run_agent_streaming with an InteractiveInput -- mirroring
    AskUserRail's design (ask a question, wait, inject the answer as this
    tool's own result) but for a form instead of free-text Q&A.
    """

    def __init__(self) -> None:
        super().__init__(tool_names=["request_login_credentials"])

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[ToolCall],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ) -> InterruptDecision:
        del ctx, auto_confirm_config
        if isinstance(user_input, dict) and user_input:
            summary = ", ".join(f"{key}={value!r}" for key, value in user_input.items())
            return self.reject(tool_result=f"The user provided: {summary}. You may now use these to log in.")
        args = _parse_tool_call_args(tool_call)
        raw_fields = args.get("fields")
        fields = [str(f) for f in raw_fields] if isinstance(raw_fields, list) else []
        fields = fields or ["Username", "Password"]
        current_url = await _current_page_url()
        auto_credentials = _easybook_auto_credentials(fields, current_url)
        logger.info(
            f"[browser-login] url={current_url!r} fields={fields} "
            f"account_configured={bool(app_config.get('EASYBOOK_USERNAME'))} "
            f"auto_login={auto_credentials is not None}"
        )
        if auto_credentials is not None:
            summary = ", ".join(f"{key}={value!r}" for key, value in auto_credentials.items())
            return self.reject(tool_result=f"A standing account for this site is on file: {summary}. Use these to log in now.")
        return self.interrupt(
            BrowserCredentialRequest(
                message="Login required",
                fields=fields,
                reason=str(args.get("reason") or ""),
            )
        )


class BrowserOptionSelectionInterruptRail(BaseInterruptRail):
    """Pauses the inner browser agent when it has found several real,
    comparable options and needs the real user to pick exactly one before it
    continues (e.g. several bus departures) -- same pause/resume mechanism as
    BrowserCredentialInterruptRail above, just for a choice instead of a
    login form. See that class's own docstring for the general design.
    """

    def __init__(self) -> None:
        super().__init__(tool_names=["request_option_selection"])

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[ToolCall],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ) -> InterruptDecision:
        del ctx, auto_confirm_config
        if isinstance(user_input, dict) and user_input:
            raw_selected = user_input.get("selected_label")
            # ChoicePicker's data-model value is structurally a list (even
            # for a single-select "mutuallyExclusive" field) -- accept either
            # shape rather than depend on exactly how it unwraps on submit.
            if isinstance(raw_selected, list):
                selected_label = str(raw_selected[0]) if raw_selected else ""
            else:
                selected_label = str(raw_selected or "")
            return self.reject(
                tool_result=f"The user selected: {selected_label!r}. Select that exact option on the page and continue."
            )
        args = _parse_tool_call_args(tool_call)
        raw_options = args.get("options")
        options = [dict(o) for o in raw_options if isinstance(o, dict)] if isinstance(raw_options, list) else []
        return self.interrupt(
            BrowserOptionSelectionRequest(
                message="Selection required",
                options=options,
                prompt=str(args.get("prompt") or ""),
            )
        )


@tool(
    description=(
        "Call this when the current page requires login credentials you don't have, and "
        "there is no way to proceed with the task without logging in. Do NOT guess, "
        "invent, or attempt to bypass login -- pause here and let the real user provide "
        "their own credentials. `fields` should name exactly what the page's own form "
        "asks for (e.g. ['Username', 'Password'], or ['Email', 'Password']) and `reason` "
        "should say which site/page this is for, in one short sentence."
    )
)
def request_login_credentials(fields: list[str], reason: str) -> dict[str, Any]:
    del fields, reason
    # Never actually reached: BrowserCredentialInterruptRail.before_tool_call
    # intercepts this tool's name and always interrupts or rejects instead
    # of approving real execution -- this body is an unreachable fallback.
    return {"error": "request_login_credentials should never execute directly."}


_PENDING_CREDENTIAL_REQUESTS: Dict[str, Dict[str, Any]] = {}


@tool(
    description=(
        "Call this when you've found two or more real, comparable options on the page "
        "(different departures, operators, times, prices, rooms, etc.) and need the real "
        "user to pick exactly one before you proceed. Never guess which one they want. "
        "Each item in `options` needs a `label` that fully and accurately describes it "
        "(operator, time, price, etc.) exactly as shown on the page -- these are shown to "
        "the user verbatim. `prompt` is a short description of the choice being made (e.g. "
        "'Choose a bus departure for Singapore -> Melaka on 2026-10-10')."
    )
)
def request_option_selection(options: list[dict[str, str]], prompt: str) -> dict[str, Any]:
    del options, prompt
    # Never actually reached: BrowserOptionSelectionInterruptRail.before_tool_call
    # intercepts this tool's name and always interrupts or rejects instead
    # of approving real execution -- this body is an unreachable fallback.
    return {"error": "request_option_selection should never execute directly."}


_PENDING_OPTION_SELECTION_REQUESTS: Dict[str, Dict[str, Any]] = {}


def _slugify_field_name(name: str, index: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or f"field_{index}"


def _build_credential_request_output(
    inner_session_id: str,
    inner_id: str,
    request: Any,
    *,
    conversation_id: str = "",
) -> ToolOutput:
    """Build the A2UI login form for a paused request_login_credentials call.

    Stashes the inner session/call under a fresh one-time flow ID and binds it
    into the compatibility form's hidden context. The WebSocket handler uses
    that ID to resume this exact paused call without routing credentials or
    the flow ID through the outer model.
    """
    raw_fields = getattr(request, "fields", None) or []
    fields = [str(f) for f in raw_fields] or ["Username", "Password"]
    reason = str(getattr(request, "reason", "") or "")

    now = time.monotonic()
    for stale_token, pending in list(_PENDING_CREDENTIAL_REQUESTS.items()):
        if float(pending.get("expires_at") or 0) <= now:
            _PENDING_CREDENTIAL_REQUESTS.pop(stale_token, None)

    resume_token = secrets.token_urlsafe(32)
    _PENDING_CREDENTIAL_REQUESTS[resume_token] = {
        "inner_session_id": inner_session_id,
        "inner_id": inner_id,
        "conversation_id": conversation_id,
        "credential_keys": [],
        "expires_at": now + _LOGIN_FLOW_TTL_SECONDS,
        "resuming": False,
    }

    surface_id = genui.new_surface_id("browser-login")
    field_groups: list[tuple[str, list[dict[str, Any]]]] = []
    field_paths: dict[str, str] = {}
    field_defaults: dict[str, Any] = {}
    for index, name in enumerate(fields):
        field_id = _slugify_field_name(name, index)
        # No masked/password TextField variant is confirmed to exist in the
        # AGenUI basic catalog, so this renders as plain text like every
        # other text_field in this app -- transport is still TLS-encrypted
        # end to end same as the rest of this session; only on-screen
        # masking is missing.
        field_groups.append((name, [genui.text_field(field_id, label=name, value="")]))
        field_paths[field_id] = f"/{field_id}/value"
        field_defaults[field_id] = ""
        _PENDING_CREDENTIAL_REQUESTS[resume_token]["credential_keys"].append(field_id)

    # TODO(frontend-secure-credentials): this hidden data-model binding is the
    # compatibility bridge for clients that can only submit generic uiActions.
    # A native secure form should send the flow ID in its dedicated envelope
    # instead, never through a chat-form context.
    field_paths[BROWSER_LOGIN_FLOW_CONTEXT_KEY] = f"/{BROWSER_LOGIN_FLOW_CONTEXT_KEY}/value"
    field_defaults[BROWSER_LOGIN_FLOW_CONTEXT_KEY] = resume_token

    messages = genui.form(
        surface_id,
        title="Log in to continue",
        fields=[field for _, group in field_groups for field in group],
        field_groups=field_groups,
        submit_label="Continue",
        action_name="submit_browser_credentials",
        field_paths=field_paths,
        field_defaults=field_defaults,
    )

    reason_suffix = f" ({reason})" if reason else ""
    fields_text = ", ".join(fields)
    model_text = (
        f"The browser hit a login wall{reason_suffix} and needs real credentials from the "
        f"user before it can continue. A login form asking for {fields_text} has already "
        "been shown to the user -- do not render your own and do not ask for credentials "
        "in chat. The server will resume the paused browser run directly when the form is "
        "submitted; do not call browser_agent_run again for this login."
    )
    return ToolOutput(
        success=True,
        data={
            "content": model_text,
            "text": f"I need you to log in to continue -- fill in {fields_text} and submit when ready.",
            "genui": messages,
            # TODO(frontend-secure-credentials): the current client drops UI
            # actions while chat processing is true.  Defer this temporary
            # generic form until just after chat.completed; a native secure
            # prompt can manage its own enabled/submitting lifecycle.
            "defer_genui_until_completed": True,
        },
    )


def _build_option_selection_output(inner_session_id: str, inner_id: str, request: Any) -> ToolOutput:
    """Build the A2UI choice card for a paused request_option_selection call.

    Stashes {inner_session_id, inner_id} under a fresh resume_token, same
    general idea as _build_credential_request_output above, just for a
    single-choice ChoicePicker field instead of a login form. Unlike that
    login path, a selection isn't sensitive, so this skips its
    conversation-binding/TTL/single-resume hardening -- but the resume itself
    still never goes through the outer model (see resume_browser_option_selection
    and ws_session.py's submit_browser_option_selection interception): once the
    user submits a pick, the server resumes this exact paused call directly.
    Each option's full label text is used directly as its submitted value (not
    a separate id), so the resumed model gets back exactly the descriptive
    string it needs to find and click the matching element again.
    """
    raw_options = getattr(request, "options", None) or []
    options = [dict(option) for option in raw_options if isinstance(option, dict)]
    prompt = str(getattr(request, "prompt", "") or "") or "Choose an option to continue"

    resume_token = uuid.uuid4().hex[:12]
    _PENDING_OPTION_SELECTION_REQUESTS[resume_token] = {
        "inner_session_id": inner_session_id,
        "inner_id": inner_id,
    }

    surface_id = genui.new_surface_id("browser-select")
    field_id = "selected_label"
    choice_options: list[tuple[str, str]] = []
    for index, option in enumerate(options):
        label = str(option.get("label") or "").strip() or f"Option {index + 1}"
        choice_options.append((label, label))

    # Hidden binding so the WebSocket handler can find this resume_token in
    # the submission's context, same mechanism as the login form's own
    # BROWSER_LOGIN_FLOW_CONTEXT_KEY -- see ws_session.py.
    field_paths = {
        field_id: f"/{field_id}/value",
        BROWSER_OPTION_SELECTION_FLOW_CONTEXT_KEY: f"/{BROWSER_OPTION_SELECTION_FLOW_CONTEXT_KEY}/value",
    }
    messages = genui.form(
        surface_id,
        title=prompt,
        fields=[genui.choice_picker(field_id, choice_options, label=prompt)],
        submit_label="Select",
        action_name="submit_browser_option_selection",
        field_paths=field_paths,
        field_defaults={BROWSER_OPTION_SELECTION_FLOW_CONTEXT_KEY: resume_token},
    )

    model_text = (
        f"A choice of {len(options)} real option(s) has already been shown to the user as "
        f"a selection card titled {prompt!r} -- do not render your own. The server will "
        "resume the paused browser run directly once the user picks and submits; do not "
        "call browser_agent_run again for this selection."
    )
    return ToolOutput(
        success=True,
        data={
            "content": model_text,
            "text": f"I found a few options -- {prompt.lower()} and I'll continue with that one.",
            "genui": messages,
        },
    )


_browser_agent: Optional[ReActAgent] = None
_browser_runtime: Optional[BrowserAgentRuntime] = None
_init_lock = asyncio.Lock()


def _force_headless_mcp_env() -> None:
    """This runs on a headless server with no display -- Playwright MCP is
    headed by default, so force --headless/--no-sandbox unless an operator
    already set PLAYWRIGHT_MCP_ARGS explicitly."""
    os.environ.setdefault(
        "PLAYWRIGHT_MCP_ARGS",
        f"-y @playwright/mcp@0.0.78 --cdp-endpoint {_BROWSER_CDP_ENDPOINT} --viewport-size 360x720",
    )


async def _get_browser_agent() -> ReActAgent:
    global _browser_agent, _browser_runtime
    if _browser_agent is not None:
        return _browser_agent

    async with _init_lock:
        if _browser_agent is not None:
            return _browser_agent

        _force_headless_mcp_env()

        model_client_config = ModelClientConfig(
            client_provider=app_config.get("MODEL_PROVIDER"),
            api_key=app_config.get("API_KEY"),
            api_base=app_config.get("API_BASE"),
            verify_ssl=app_config.get("LLM_SSL_VERIFY"),
        )
        # ``model`` (not the declared ``model_name`` field) matches agent.py's
        # own ModelRequestConfig construction -- forwarded as an extra field
        # straight through to the API call (model allows extras).
        model_request_config = ModelRequestConfig(
            model=app_config.get("MODEL_NAME"),
            temperature=0.2,
        )

        # "core" only: navigate/click/fill/type/select/snapshot/find/evaluate/etc.
        # -- no pdf/vision/devtools/network/storage/testing/unsafe_dev tools.
        capabilities = resolve_browser_capabilities([])
        mcp_cfg = build_playwright_mcp_config()

        runtime = BrowserAgentRuntime(
            provider=model_client_config.client_provider,
            api_key=model_client_config.api_key,
            api_base=model_client_config.api_base,
            model_name=app_config.get("MODEL_NAME"),
            mcp_cfg=mcp_cfg,
            guardrails=build_browser_guardrails(),
            allowed_tool_names=capabilities.allowed_tool_names,
        )
        await runtime.ensure_started()

        card = AgentCard(
            id="a2ui_browser_agent",
            name="browser_agent",
            description="Multi-step browser automation subagent for a2ui.",
        )
        agent_config = ReActAgentConfig(
            model_client_config=model_client_config,
            model_config_obj=model_request_config,
            prompt_template=[{"role": "system", "content": _INNER_SYSTEM_PROMPT}],
            max_iterations=_MAX_INNER_ITERATIONS,
        )
        agent = ReActAgent(card=card).configure(agent_config)
        await agent.register_rail(A2uiToolEventRail())
        await agent.register_rail(BrowserRuntimeRail(runtime))
        await agent.register_rail(BrowserCredentialInterruptRail())
        await agent.register_rail(BrowserOptionSelectionInterruptRail())
        await agent.register_rail(BrowserCheckoutStopRail())
        # Must come after BrowserRuntimeRail (lower priority) so it can undo
        # that rail's blanket tool-strip on the terminal pass.
        await agent.register_rail(BrowserInteractionAvailabilityRail())
        await agent.register_rail(
            ContextProcessorRail(
                processors=[
                    (
                        "BrowserStateContextProcessor",
                        BrowserStateContextProcessorConfig(provider=runtime),
                    ),
                    (
                        "BrowserWorkingContextProcessor",
                        BrowserWorkingContextProcessorConfig(language="en", runtime_projection_only=True),
                    ),
                ],
                preset=False,
            )
        )

        await Runner.resource_mgr.add_mcp_server(mcp_cfg, tag=card.id)
        # Registering the whole McpServerConfig on ability_manager would expose
        # every Playwright MCP tool (~70, including cookie/localStorage/
        # network-mock/tracing/video/unsafe-code-eval) to this agent's model --
        # fetch and add only the resolved "core" subset (navigate/click/fill/
        # type/select/snapshot/find/evaluate/etc.) instead.
        mcp_tools = await Runner.resource_mgr.get_mcp_tool(
            name=list(capabilities.allowed_tool_names),
            server_id=mcp_cfg.server_id,
        )
        for mcp_tool in mcp_tools:
            if mcp_tool is not None:
                agent.ability_manager.add(mcp_tool.card)

        for runtime_tool in build_browser_runtime_tools(runtime, language="en"):
            Runner.resource_mgr.add_tool(runtime_tool)
            agent.ability_manager.add(runtime_tool.card)

        Runner.resource_mgr.add_tool(request_login_credentials)
        agent.ability_manager.add(request_login_credentials.card)
        Runner.resource_mgr.add_tool(request_option_selection)
        agent.ability_manager.add(request_option_selection.card)

        _browser_agent = agent
        _browser_runtime = runtime
        return agent


def _final_summary(raw_text: str) -> str:
    """The inner agent's last turn is often the browser runtime's own
    structured task-completion object (task_id/current_phase/evidence/
    deadline/etc., see BrowserRuntimeRail), not a plain-text answer -- pull
    its ``summary`` field when present instead of handing the outer agent a
    wall of JSON to (re)read."""
    try:
        parsed = json.loads(raw_text)
    except (TypeError, ValueError):
        return raw_text
    if isinstance(parsed, dict):
        browser_result = parsed.get("browser_result")
        if isinstance(browser_result, dict):
            summary = browser_result.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
    return raw_text


def _normalized_tool_name(tool_name: str) -> str:
    """Raw Playwright MCP tools register under a server-qualified name (e.g.
    ``mcp_playwright-official_browser_navigate``); strip that prefix so step
    text matches on the bare ``browser_navigate``-style name either way."""
    name = tool_name or ""
    marker = "_browser_"
    index = name.find(marker)
    if index != -1:
        return name[index + 1 :]
    return name


_SCREENSHOT_LINK_RE = re.compile(r"\]\(([^)]+\.(?:png|jpeg|jpg))\)")


async def _capture_screenshot(runtime: BrowserAgentRuntime) -> Optional[Dict[str, str]]:
    """Grab a small JPEG of the current page and return it as base64.

    Calls ``browser_take_screenshot`` directly through the runtime's own
    Playwright MCP client -- not through the inner agent's tool-calling loop
    -- so this runs unconditionally after every step regardless of whether
    that "vision"-tier tool is one the model itself was given. The MCP tool
    only returns a message naming the file it wrote (under the MCP server's
    own cwd); this reads that file, base64-encodes it, and deletes it. Any
    failure here (tool unavailable, page mid-navigation, file already gone)
    just means no screenshot for this step -- never worth failing the step
    itself over.
    """
    try:
        tool = await runtime._get_playwright_mcp_tool("browser_take_screenshot")
        result = await tool.invoke({"type": "jpeg", "scale": "css", "fullPage": False})
        text = result.get("result", "") if isinstance(result, dict) else ""
        match = _SCREENSHOT_LINK_RE.search(text)
        if not match:
            return None
        rel_path = match.group(1)
        cwd = str(runtime.service.mcp_cfg.params.get("cwd") or "")
        full_path = os.path.join(cwd, rel_path) if cwd else rel_path
        with open(full_path, "rb") as handle:
            image_bytes = handle.read()
        try:
            os.remove(full_path)
        except OSError:
            pass
        mime = "image/jpeg" if rel_path.lower().endswith((".jpeg", ".jpg")) else "image/png"
        return {"mime": mime, "base64": base64.b64encode(image_bytes).decode("ascii")}
    except Exception:  # noqa: BLE001 -- best-effort; never break the step over a missing screenshot
        return None


_BROWSER_INPUT_LOCK = asyncio.Lock()
_TYPED_TEXT_MAX_CHARS = 200
_BROWSER_CDP_ENDPOINT = os.getenv("BROWSER_CDP_ENDPOINT", "http://127.0.0.1:9222")
_CHECKOUT_TEXT_MARKERS = ("Ticket Collector Info", "Passenger Information", "Payment Info")
_direct_playwright: Any = None
_direct_browser: Any = None
_direct_sized_pages: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _clamp(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0.0.0 Mobile Safari/537.36"
)


async def _apply_mobile_emulation(context: Any, page: Any) -> None:
    """Present the page as a phone: a mobile user agent, touch input and
    mobile device metrics, so sites serve their mobile layout instead of the
    desktop one squeezed into the 360 px view."""
    cdp = await context.new_cdp_session(page)
    await cdp.send("Network.setUserAgentOverride", {"userAgent": _MOBILE_USER_AGENT, "platform": "Android"})
    await cdp.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": 360, "height": 720, "deviceScaleFactor": 2, "mobile": True},
    )
    await cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})


async def _direct_page() -> Any:
    """The live page, driven directly over the shared Chrome's DevTools
    endpoint -- not through the browser tool's MCP round-trip, which costs
    about a second per call. The MCP tool attaches to the same Chrome."""
    global _direct_playwright, _direct_browser
    async with _BROWSER_INPUT_LOCK:
        if _direct_browser is None or not _direct_browser.is_connected():
            if _direct_playwright is None:
                from playwright.async_api import async_playwright

                _direct_playwright = await async_playwright().start()
            _direct_browser = await _direct_playwright.chromium.connect_over_cdp(_BROWSER_CDP_ENDPOINT)
        context = _direct_browser.contexts[0]
        open_pages = [page for page in context.pages if not page.is_closed()]
        page = open_pages[-1] if open_pages else await context.new_page()
        if page not in _direct_sized_pages:
            await page.set_viewport_size({"width": 360, "height": 720})
            await _apply_mobile_emulation(context, page)
            _direct_sized_pages.add(page)
        return page


_PAY_NOW_AT_POINT_JS = """([x, y]) => {
    const el = document.elementFromPoint(x, y);
    const control = el ? el.closest('button, a, input[type=submit], input[type=button], [role=button]') : null;
    if (!control) return false;
    const label = (control.innerText || control.value || control.textContent || '').trim();
    return /pay\\s*now/i.test(label);
}"""

_SELECT_AT_POINT_JS = """([x, y]) => {
    const el = document.elementFromPoint(x, y);
    const select = el ? el.closest('select') : null;
    if (!select) return null;
    window.__bridgeSelect = select;
    return Array.from(select.options).map(o => o.text.trim()).filter(t => t.length > 0);
}"""

_FOCUSED_FIELD_JS = """() => {
    const f = document.activeElement;
    if (!f || !['INPUT', 'TEXTAREA'].includes(f.tagName)) return null;
    const r = f.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    return {x: r.left / vw, y: r.top / vh, w: r.width / vw, h: r.height / vh, value: f.value || ''};
}"""

_CHOOSE_OPTION_JS = """(index) => {
    const select = window.__bridgeSelect;
    if (!select) return false;
    select.selectedIndex = index;
    select.dispatchEvent(new Event('input', {bubbles: true}));
    select.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
}"""


_CLOUDFLARE_BOUND_COOKIES = frozenset({"cf_clearance", "__cf_bm"})


def _cookie_belongs_to_host(cookie_domain: str, host: str) -> bool:
    """True when a cookie's domain is the page's host or a parent of it, so
    ``.redbus.sg`` covers ``www.redbus.sg`` but not ``evilredbus.sg``."""
    domain = cookie_domain.lower().lstrip(".")
    return bool(host) and bool(domain) and (host == domain or host.endswith("." + domain))


async def _checkout_handoff(page: Any) -> Dict[str, Any]:
    """What the app needs to continue this checkout in its own web view: the
    current page, the mobile user agent it was rendered with, and the
    Easybook session cookies. Only cookies for the site are sent."""
    host = (urlsplit(page.url).hostname or "").lower()
    cookies = [
        {
            "name": cookie["name"],
            "value": cookie["value"],
            "domain": cookie["domain"],
            "path": cookie.get("path") or "/",
            "secure": bool(cookie.get("secure")),
            "expires": cookie.get("expires", -1),
        }
        for cookie in await page.context.cookies()
        if _cookie_belongs_to_host(str(cookie.get("domain") or ""), host)
        # Cloudflare clearance is bound to the IP and browser that solved the
        # challenge, so it loops on another device's network. Not handed over.
        and cookie["name"] not in _CLOUDFLARE_BOUND_COOKIES
    ]
    return {"url": page.url, "user_agent": _MOBILE_USER_AGENT, "cookies": cookies}


async def perform_browser_input(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Relay one user interaction with the live browser view straight to the
    page -- deliberately not through the LLM, so typed values (including
    payment fields) never enter a model prompt. Returns a fresh screenshot to
    show back, plus the option labels when a native dropdown was tapped (the
    page's own popup never renders into a screenshot, so the app shows the
    choices itself and sends one back as a `choose` action). Typed text is
    never logged here.
    """
    kind = str(event.get("kind") or "")
    if kind not in ("tap", "type", "fill", "choose", "scroll", "refresh", "reset"):
        return None
    if kind == "type" and not str(event.get("text") or ""):
        return None
    if kind == "fill" and not isinstance(event.get("text"), str):
        return None
    page = await _direct_page()
    viewport = page.viewport_size or {"width": 360, "height": 720}
    started = time.monotonic()
    select_options: Optional[List[str]] = None
    field: Optional[Dict[str, Any]] = None
    if kind == "tap":
        x = _clamp(event.get("x"), 0.0, 1.0) * viewport["width"]
        y = _clamp(event.get("y"), 0.0, 1.0) * viewport["height"]
        if await page.evaluate(_PAY_NOW_AT_POINT_JS, [x, y]):
            # Pay Now is not clicked here. The app takes over with the live
            # session, so the payment step happens in its own web view.
            checkout = await _checkout_handoff(page)
            logger.info(f"[browser-checkout] url={checkout['url']} cookies={len(checkout['cookies'])}")
            return {"checkout": checkout, "frame": None, "select_options": None, "field": None}
        select_options = await page.evaluate(_SELECT_AT_POINT_JS, [x, y])
        if not select_options:
            select_options = None
            await page.mouse.click(x, y)
            field = await page.evaluate(_FOCUSED_FIELD_JS)
    elif kind == "type":
        text = str(event.get("text") or "")[:_TYPED_TEXT_MAX_CHARS]
        await page.keyboard.type(text)
    elif kind == "fill":
        # Replaces the focused field's whole value, so clearing the input
        # clears the field too. Values are never logged.
        text = str(event.get("text") or "")[:_TYPED_TEXT_MAX_CHARS]
        await page.locator(":focus").fill(text)
    elif kind == "choose":
        await page.evaluate(_CHOOSE_OPTION_JS, int(_clamp(event.get("index"), 0, 500)))
    elif kind == "refresh":
        pass
    elif kind == "reset":
        await page.goto("about:blank")
    else:
        dy = _clamp(event.get("dy"), -1000, 1000)
        await page.mouse.move(viewport["width"] / 2, viewport["height"] / 2)
        await page.mouse.wheel(0, dy)
    acted = time.monotonic()
    shot = await page.screenshot(type="jpeg", quality=70)
    finished = time.monotonic()
    logger.info(
        f"[browser-input] kind={kind} act_ms={(acted - started) * 1000:.0f} "
        f"screenshot_ms={(finished - acted) * 1000:.0f}"
    )
    frame = {"mime": "image/jpeg", "base64": base64.b64encode(shot).decode("ascii")}
    return {"frame": frame, "select_options": select_options, "field": field}


async def _checkout_reached() -> bool:
    """True once the page shows passenger details or payment. Checked on the
    page text, because the payment section lives on the same URL as seat
    selection, so the URL alone doesn't tell the two apart."""
    current_url = (await _current_page_url()).lower()
    if any(marker in current_url for marker in _CHECKOUT_STOP_URL_MARKERS):
        return True
    try:
        page = await _direct_page()
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:  # noqa: BLE001 -- an unreadable page isn't a checkout page
        return False
    return any(marker in text for marker in _CHECKOUT_TEXT_MARKERS)


_SEAT_MAP_TEXT_MARKERS = (
    "choose seat",
    "choose your seat",
    "select seat",
    "select your seat",
    "seat selection",
    "seat map",
    "seat layout",
    "选座",
    "选择座位",
    "选择坐位",
    "座位图",
)


async def _seat_map_reached() -> bool:
    """True once the page shows a seat picker -- the point a departure-choice
    run is told to stop at (see the outer agent's prompt). Checked on page
    text, the same way as ``_checkout_reached``, since there's no single URL
    or DOM shape shared across booking sites."""
    try:
        page = await _direct_page()
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:  # noqa: BLE001 -- an unreadable page isn't a seat map
        return False
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _SEAT_MAP_TEXT_MARKERS)


# The inner agent can reach for a raw JS escape hatch (`browser_evaluate` /
# `browser_run_code`). Its step text can't come from the tool name alone --
# "Evaluate…" says nothing about whether it is reading a price, filling a
# field, or clicking a button -- so these helpers describe the script itself.
_SCRIPT_ARG_KEYS = ("function", "expression", "script", "code")
_SCRIPT_WRITE_RE = re.compile(
    r"\.click\s*\(|dispatchEvent|\.value\s*=|setAttribute\s*\(|\.submit\s*\(|"
    r"appendChild|removeChild|innerHTML\s*=|\.focus\s*\(|scrollIntoView",
    re.IGNORECASE,
)
_SCRIPT_READ_RE = re.compile(
    r"textContent|innerText|innerHTML|getAttribute|dataset|document\.title|querySelector",
    re.IGNORECASE,
)
_SELECTOR_RE = re.compile(r"""querySelector(?:All)?\(\s*["'`]([^"'`]+)["'`]""")
_SCRIPT_SELECTORS_LIMIT = 2
_SCRIPT_FIELDS_LIMIT = 4


def _script_text(args: Dict[str, Any]) -> str:
    """The raw script out of whichever parameter name carried it."""
    for key in _SCRIPT_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _coerce_tool_args(tool_args: Any) -> Dict[str, Any]:
    """Normalize one step's tool arguments into a dict.

    ``A2uiToolEventRail`` relays ``ctx.inputs.tool_args`` verbatim, and that
    is ``ToolCall.arguments`` -- the model's raw JSON *string* (see the
    ``ToolCallInputs`` construction in ability_manager), not the parsed dict
    the executor builds internally for its own use. Reading it as if it were
    already a dict silently dropped every argument-derived step text, so
    "Navigating to <url>" degraded to "Navigating…" and a script step could
    never say what it was reading.
    """
    if isinstance(tool_args, dict):
        return tool_args
    if isinstance(tool_args, str) and tool_args.strip():
        try:
            parsed = json.loads(tool_args)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _shorthand_label(script: str, name: str) -> str:
    """Readable label for a shorthand return key such as ``{title}``.

    A bare identifier like ``t`` says nothing, so fall back to what it was
    assigned from: ``const t = document.title`` reads as "title", and
    ``const p = document.querySelector('.price').innerText`` as "price".
    """
    assigned = re.search(rf"(?:const|let|var)\s+{re.escape(name)}\s*=\s*([^;\n]+)", script)
    if not assigned:
        return ""
    expression = assigned.group(1)
    if "document.title" in expression:
        return "title"
    attribute = re.search(r"""getAttribute\(\s*["'`]data-([\w-]+)["'`]""", expression)
    if attribute:
        return attribute.group(1).replace("-", " ")
    selectors = _script_selectors(expression, limit=1)
    if selectors:
        token = re.split(r"[\s>+~]+", selectors[0])[-1].lstrip(".#")
        return token.replace("-", " ").replace("_", " ").strip()
    return ""


def _returned_field_names(script: str, limit: int = _SCRIPT_FIELDS_LIMIT) -> List[str]:
    """Keys of the object literal a script returns, in source order.

    ``return {title, price: el.textContent}`` yields ``title``/``price``, so
    the log can name what is being read. Comma splitting is brace/bracket
    aware so a nested value (``{meta: {a: 1}}``) doesn't leak its own keys,
    and shorthand keys (``{title}``) are resolved through their assignment
    rather than reported as the raw variable name.
    """
    match = re.search(r"return\s*\(?\s*\{", script) or re.search(r"=>\s*\(\s*\{", script)
    if not match:
        return []
    start = script.index("{", match.start())
    depth = 0
    end = -1
    for index in range(start, len(script)):
        char = script[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end == -1:
        return []

    names: List[str] = []
    depth = 0
    token = ""
    for char in script[start + 1 : end] + ",":
        if char in "{([":
            depth += 1
        elif char in "})]":
            depth -= 1
        if char == "," and depth == 0:
            key = re.match(r"""\s*["'`]?([A-Za-z_$][\w$\-]*)["'`]?\s*(:?)""", token)
            if key:
                name = key.group(1)
                if not key.group(2):
                    name = _shorthand_label(script, name) or name
                names.append(name)
            token = ""
            if len(names) >= limit:
                break
        else:
            token += char
    return names


def _script_selectors(script: str, limit: int = _SCRIPT_SELECTORS_LIMIT) -> List[str]:
    """Distinct selectors the script queries, shortest-first, de-duplicated."""
    selectors: List[str] = []
    for match in _SELECTOR_RE.finditer(script):
        selector = re.sub(r"\s+", " ", match.group(1).strip())
        if selector and selector not in selectors:
            selectors.append(selector[:48])
    selectors.sort(key=len)
    return selectors[:limit]


def _script_step_text(tool_name: str, args: Dict[str, Any]) -> str:
    """Describe a script-injection step by what the script does to the page."""
    script = _script_text(args)
    target = str(args.get("element") or args.get("target") or args.get("ref") or "").strip()
    if not script:
        return "Running a script on the page…"

    if _SCRIPT_WRITE_RE.search(script):
        suffix = f" ({target})" if target and target.lower() != "page" else ""
        return f"Changing the page with a script{suffix}…"

    if _SCRIPT_READ_RE.search(script):
        fields = _returned_field_names(script)
        if fields:
            return f"Reading page data ({', '.join(fields)})…"
        selectors = _script_selectors(script)
        if selectors:
            return f"Reading data from the page ({', '.join(selectors)})…"
        return "Reading data from the page…"

    fields = _returned_field_names(script)
    if fields:
        return f"Running a script that returns {', '.join(fields)}…"
    if tool_name != "browser_evaluate":
        return "Running a script on the page…"
    return "Checking the page with a script…"


def _step_text(tool_name: str, tool_args: Any) -> str:
    """Human-readable line for one inner browser action, shown live in the
    client's action-log panel (see browser.step in ws_session.py)."""
    args = _coerce_tool_args(tool_args)
    tool_name = _normalized_tool_name(tool_name)
    if tool_name in ("browser_evaluate", "browser_run_code", "browser_run_code_unsafe"):
        return _script_step_text(tool_name, args)
    if tool_name == "browser_navigate":
        url = args.get("url")
        return f"Navigating to {url}" if url else "Navigating…"
    if tool_name == "browser_click":
        target = args.get("element") or args.get("ref") or ""
        return f"Clicking {target}".strip() if target else "Clicking…"
    if tool_name in ("browser_type", "browser_fill_form"):
        return "Filling in a field…"
    if tool_name == "browser_select_option":
        return "Choosing an option…"
    if tool_name == "browser_probe_interactives":
        return "Looking at what's on the page…"
    if tool_name == "browser_probe_cards":
        return "Reading the results on the page…"
    if tool_name == "browser_batch_interact":
        return "Carrying out a sequence of actions…"
    if tool_name == "browser_snapshot":
        return "Taking a look at the current page…"
    if tool_name:
        readable = tool_name.replace("browser_", "").replace("_", " ").strip()
        return (readable[0].upper() + readable[1:] + "…") if readable else "Working…"
    return "Working…"


# BrowserRuntimeRail._prepare_terminal_synthesis clears every tool as soon as
# the browser state goes terminal. Higher priority runs first, so this rail
# sits below BrowserRuntimeRail (default 50) to run *after* that clear.
_BROWSER_INTERACTION_RAIL_PRIORITY = 40

# Outranks that same rail's "browser_terminal_synthesis" section (priority
# 100), which is what tells the model not to call tools.
_BROWSER_INTERACTION_SECTION_PRIORITY = 120

# The only tools worth having once browsing is over: hand a real choice, or a
# login wall, back to the user. Resuming afterwards never goes through the
# outer agent either way -- see resume_browser_option_selection/
# resume_browser_login and ws_session.py's submit_* interception.
_BROWSER_INTERACTION_TOOL_NAMES = ("request_option_selection", "request_login_credentials")


def _tool_call_name(tool_call: Any) -> str:
    """The bare tool name off a response's tool call.

    Providers wrap it differently -- sometimes ``name`` on the call itself,
    sometimes nested under a ``function`` object -- so accept either.
    """
    function = getattr(tool_call, "function", tool_call)
    return str(getattr(function, "name", getattr(tool_call, "name", "")) or "").strip()


_CHECKOUT_STOP_URL_MARKERS = ("/passengerdetails", "/payment", "/checkout")
_PAGE_ACTION_TOOL_NAMES = (
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_batch_interact",
    "browser_run_code",
    "browser_run_code_unsafe",
    "browser_evaluate",
)


class BrowserCheckoutStopRail(BaseInterruptRail):
    """Stops the inner browser agent at the passenger-details / payment step.

    Seat selection is the agent's job; anything past it (passenger details,
    payment method, the pay button) is the user's to complete themselves, in
    the live view. The prompt asks for that, but a prompt alone wasn't holding
    -- a live run went straight through to the payment-method section -- so
    this enforces it: once the page URL is at a checkout-stop marker, every
    further page action is rejected with a stop instruction instead of run.
    """

    def __init__(self) -> None:
        super().__init__(tool_names=list(_PAGE_ACTION_TOOL_NAMES))

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[ToolCall],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ) -> InterruptDecision:
        del ctx, tool_call, user_input, auto_confirm_config
        if not await _checkout_reached():
            return self.approve()
        browser_agent_log_info("[BROWSER_SUBAGENT] reached the checkout stop point; rejecting further page actions")
        return self.reject(
            tool_result=(
                "STOP: the browser is now on the passenger details / payment step. Do not click, "
                "type, or submit anything on it, and do not call any more browser tools. Your final "
                "reply must say the user now enters their passenger details and payment themselves "
                "on the real site, and must include the current page URL."
            )
        )


class BrowserInteractionAvailabilityRail(AgentRail):
    """Keep the user-interaction tools callable on the browser terminal pass.

    ``BrowserRuntimeRail._prepare_terminal_synthesis`` wipes the tool list
    (``inputs.tools = []``) and injects a "do not call tools, summarise the
    runtime result" section the moment the browser state reaches a terminal
    status -- ``completed``, ``partial`` or ``blocked``.

    That is right for a plain read-only task, but it silently disabled the
    entire booking hand-off: ``request_option_selection`` and
    ``request_login_credentials`` are the *only* way a choice or a login wall
    ever reaches the user, and they were being removed at precisely the moment
    the run finally had real options to offer. Observed live against a real
    bus site: a run that had scraped five genuine departures wrote them out as
    plain chat text because at that iteration ``tool_count`` was 0, so the
    model had nothing left to call -- no matter that its own system prompt made
    that call mandatory.

    So when the tools have been stripped, this puts back *only* those two.
    The model still cannot browse further; it can hand the choice or the login
    back to the user, or write its summary. That is exactly the decision the
    terminal pass exists for -- the resulting card is resumed straight through
    the server afterwards (see resume_browser_option_selection/
    resume_browser_login), without the outer agent ever seeing it again.
    """

    priority = _BROWSER_INTERACTION_RAIL_PRIORITY

    def __init__(self) -> None:
        super().__init__()
        # Set only when this rail put the tools back for the call now in
        # flight, so after_model_call never overrides a finish for a turn it
        # did not enable.
        self._restored_this_turn = False

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        self._restored_this_turn = False
        inputs = getattr(ctx, "inputs", None)
        if inputs is None or getattr(inputs, "tools", None):
            # Tools are still on offer -- an ordinary exploratory iteration.
            return

        restored = await self._interaction_tool_infos(ctx)
        if not restored:
            return

        inputs.tools = restored
        self._restored_this_turn = True
        self._add_override_section(ctx)
        browser_agent_log_info(
            "[BROWSER_SUBAGENT] terminal pass: restored %s so the run can still hand a "
            "choice or a login back to the user",
            ", ".join(str(getattr(tool, "name", "?")) for tool in restored),
        )

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        """Let a restored interaction call actually run.

        ``BrowserRuntimeRail.after_model_call`` force-finishes whenever the
        browser state is terminal and the response contains *any* tool call --
        it never looks at which tool was called. For a restored
        ``request_option_selection`` that is precisely wrong: the loop consumes
        the finish before executing the call, so the interrupt never fires, no
        card is ever built, and the run ends reporting
        ``browser_task_incomplete`` instead of pausing for the user.

        Dropping the finish here lets the call run; the interrupt then pauses
        the run immediately, so the runtime's terminal state still governs
        every later step.
        """
        if not self._restored_this_turn:
            return
        response = getattr(getattr(ctx, "inputs", None), "response", None)
        interaction_calls = [
            _tool_call_name(tool_call)
            for tool_call in (getattr(response, "tool_calls", None) or [])
            if _tool_call_name(tool_call) in _BROWSER_INTERACTION_TOOL_NAMES
        ]
        if not interaction_calls:
            return
        if ctx.consume_force_finish() is None:
            return
        browser_agent_log_info(
            "[BROWSER_SUBAGENT] held the terminal force-finish so %s can run and pause "
            "for the user",
            ", ".join(interaction_calls),
        )

    @staticmethod
    async def _interaction_tool_infos(ctx: AgentCallbackContext) -> List[Any]:
        """The interaction tools' tool definitions, re-read from the agent."""
        ability_manager = getattr(ctx.agent, "ability_manager", None)
        list_tool_info = getattr(ability_manager, "list_tool_info", None)
        if not callable(list_tool_info):
            return []
        try:
            tools = await list_tool_info(list(_BROWSER_INTERACTION_TOOL_NAMES))
        except Exception:  # noqa: BLE001 -- a missing tool must not break the turn
            return []
        return list(tools or [])

    @staticmethod
    def _add_override_section(ctx: AgentCallbackContext) -> None:
        """Tell the model what it may still do, over the "no tools" section."""
        builder = getattr(ctx.agent, "system_prompt_builder", None)
        if builder is None:
            return
        builder.add_section(
            PromptSection(
                name="browser_interaction_override",
                content={
                    "en": (
                        "Browsing has ended, but one hand-off may still be owed -- this "
                        "overrides any instruction above telling you not to call tools. If "
                        "you have two or more genuinely comparable real options the user "
                        "must choose between (different departures, operators, times, "
                        "prices, rooms...), call request_option_selection now with those "
                        "real options, before writing anything else. If the site blocked "
                        "you behind a login you have no credentials for, call "
                        "request_login_credentials instead. Only if neither applies, write "
                        "your final summary."
                    ),
                    "cn": (
                        "浏览已结束，但仍可能欠用户一次交接——此说明覆盖上文任何“不要调用工具”的"
                        "指示。如果你已获得两个及以上真实且可比较的选项（不同的班次、运营商、时间、"
                        "价格、房型等），请先调用 request_option_selection 并传入这些真实选项，再"
                        "输出其他内容。若页面要求登录而你没有凭据，请改调 "
                        "request_login_credentials。若两者都不适用，再输出最终总结。"
                    ),
                },
                priority=_BROWSER_INTERACTION_SECTION_PRIORITY,
            )
        )


class BrowserAgentTool(Tool):
    """Runs a real, multi-step browser task and streams its actions live."""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                name="browser_agent_run",
                description=(
                    "Run a real, autonomous, multi-step browser agent for tasks that need "
                    "actually navigating and interacting with a live site -- not just "
                    "reading one page (`browser_inspect_page`) or searching for one "
                    "(`free_search`). Use this when the information can only be gathered by "
                    "actually using a real site's own search/filter UI -- e.g. 'find real bus "
                    "tickets from Singapore to Melaka on 2026-10-10 and list the operators, "
                    "times, and prices' when there is no dedicated search tool for that "
                    "transport type. It can navigate, scroll, click, fill in forms, submit "
                    "search/filter forms, select a specific real result the user picked, and "
                    "proceed through ordinary checkout steps toward a real payment page -- but "
                    "it will never complete an actual purchase or payment itself; it always "
                    "stops right before that and hands the user off to the real site via "
                    "`show_card`'s `link_url` to finish there themselves. While it runs, the "
                    "user can watch its individual actions (navigate/click/fill/extract) live "
                    "in an expandable panel, so it's fine for this to take several steps and a "
                    "bit longer than your other tools -- do not avoid it just because it's "
                    "slower. Give `task` every constraint the user already gave (route, dates, "
                    "passenger count, contact details if given, etc.), written as a single, "
                    "specific, self-contained instruction.\n"
                    "This tool can pause itself mid-task and show the user its own card -- "
                    "either a selection card (when it found several real, comparable options "
                    "and needs the user to pick one) or a login form (when the site requires "
                    "credentials you don't have). Either way, the server resumes that same "
                    "paused run directly once the user responds -- never ask for the choice or "
                    "credentials in chat, never render your own card for either, and never call "
                    "this tool again for that pause or start a fresh `task` call for it."
                ),
                input_params=_INPUT_PARAMS,
                # The outer ability_manager's own default tool-call timeout (300s,
                # DEFAULT_TOOL_CALL_TIMEOUT) was killing real multi-step runs --
                # search, log in, reach seat selection -- well before
                # _MAX_INNER_ITERATIONS or the browser phase budgets would ever
                # stop them on their own. Declared here, per-tool, so no other
                # tool's timeout changes.
                properties={"resilience": {"timeout_s": 900}},
            )
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> ToolOutput:
        return await self._invoke_direct(inputs, **kwargs)

    async def _invoke_direct(self, inputs: Any, **kwargs: Any) -> ToolOutput:
        """Run without requiring callers to enter the model-facing tool path.

        The WebSocket credential compatibility bridge calls this private path
        so submitted secrets do not become an outer-agent tool call, trace, or
        ToolMessage.  The inner agent still receives the values in this
        backend-only transition; a native frontend plus a runtime secret-fill
        primitive should replace that remaining hop.
        """
        outer_session = kwargs.get("session")
        step_callback: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = kwargs.get("step_callback")
        expected_conversation_id = str(kwargs.get("expected_conversation_id") or "")
        task = ""
        resume_token = ""
        credentials: Dict[str, str] = {}
        if isinstance(inputs, dict):
            task = str(inputs.get("task") or "").strip()
            resume_token = str(inputs.get("resume_token") or "").strip()
            raw_credentials = inputs.get("credentials")
            if isinstance(raw_credentials, dict):
                credentials = {str(key): str(value) for key, value in raw_credentials.items()}

        pending: Optional[Dict[str, Any]] = None
        if resume_token:
            pending = _PENDING_CREDENTIAL_REQUESTS.get(resume_token)
            if pending is not None:
                if float(pending.get("expires_at") or 0) <= time.monotonic():
                    _PENDING_CREDENTIAL_REQUESTS.pop(resume_token, None)
                    return ToolOutput(success=False, error="This login request expired -- ask the user to try again.")
                bound_conversation_id = str(pending.get("conversation_id") or "")
                if bound_conversation_id and expected_conversation_id != bound_conversation_id:
                    return ToolOutput(
                        success=False, error="This login request does not belong to this conversation."
                    )
                if pending.get("resuming"):
                    return ToolOutput(success=False, error="This login request is already being resumed.")
                expected_keys = set(pending.get("credential_keys") or [])
                if not credentials or set(credentials) != expected_keys:
                    return ToolOutput(success=False, error="The submitted login fields did not match this request.")
                pending["resuming"] = True
            else:
                # Not a sensitive login flow -- option-selection resume skips the
                # hardening above (see _build_option_selection_output) and is
                # consumed eagerly here instead.
                pending = _PENDING_OPTION_SELECTION_REQUESTS.pop(resume_token, None)
                if pending is None:
                    return ToolOutput(
                        success=False,
                        error="This paused browser request is no longer active -- ask the user to try again.",
                    )
            inner_session_id = pending["inner_session_id"]
            interactive_input = InteractiveInput()
            interactive_input.update(pending["inner_id"], dict(credentials))
            # Runner._prepare_agent calls inputs.get(...) whenever session is a
            # plain string (our case) -- an InteractiveInput passed directly as
            # `inputs` has no .get() and blows up before the agent ever runs.
            # Wrapping it under "query" is the same convention the framework's
            # own resume tests use (see test_interrupt_stream.py).
            run_input: Any = {"query": interactive_input}
        elif task:
            inner_session_id = f"browser-agent-{uuid.uuid4().hex}"
            run_input = {"query": task}
        else:
            return ToolOutput(success=False, error="'task' is required.")

        try:
            agent = await _get_browser_agent()
        except asyncio.CancelledError:
            if pending is not None:
                pending["resuming"] = False
            raise
        except Exception as exc:  # noqa: BLE001 -- report startup failure, don't crash the outer turn
            if pending is not None:
                pending["resuming"] = False
            return ToolOutput(success=False, error=f"Browser agent unavailable: {exc}")

        async def _emit(payload: dict[str, Any]) -> None:
            if step_callback is not None:
                await step_callback(payload)
            if outer_session is None:
                return
            await outer_session.write_stream(OutputSchema(type="browser_agent_step", index=0, payload=payload))

        final_text = ""
        pending_interrupt: Optional[tuple[str, Any]] = None
        if pending is not None:
            agent.config.configure_max_iterations(_MAX_INNER_ITERATIONS + _RESUME_EXTRA_ITERATIONS)
        try:
            async for chunk in Runner.run_agent_streaming(agent, run_input, session=inner_session_id):
                chunk_type = getattr(chunk, "type", None)
                payload = getattr(chunk, "payload", None) or {}
                if chunk_type == INTERACTION:
                    pending_interrupt = (getattr(payload, "id", ""), getattr(payload, "value", None))
                    continue
                if chunk_type == "tool_call":
                    tool_name = _normalized_tool_name(payload.get("tool_name", ""))
                    await _emit(
                        {
                            "status": "started",
                            "tool": tool_name,
                            "text": _step_text(tool_name, payload.get("tool_args")),
                        }
                    )
                elif chunk_type == "tool_result":
                    finished_payload: dict[str, Any] = {
                        "status": "finished",
                        "tool": _normalized_tool_name(payload.get("tool_name", "")),
                    }
                    runtime = _browser_runtime
                    if runtime is not None:
                        screenshot = await _capture_screenshot(runtime)
                        if screenshot is not None:
                            finished_payload["screenshot_base64"] = screenshot["base64"]
                            finished_payload["screenshot_mime"] = screenshot["mime"]
                    await _emit(finished_payload)
                elif chunk_type == "tool_error":
                    await _emit(
                        {
                            "status": "error",
                            "tool": _normalized_tool_name(payload.get("tool_name", "")),
                            "text": str(payload.get("message") or "That action failed."),
                        }
                    )
                elif chunk_type == "answer":
                    content = payload.get("output") or payload.get("content") or ""
                    if content:
                        final_text = str(content)
        except asyncio.CancelledError:
            if pending is not None:
                pending["resuming"] = False
            raise
        except Exception as exc:  # noqa: BLE001 -- report the failure as a tool result, don't crash the outer turn
            if pending is not None:
                pending["resuming"] = False
            await _emit({"status": "error", "tool": "", "text": f"Browser agent run failed: {exc}"})
            return ToolOutput(success=False, error=f"Browser agent run failed: {exc}")
        finally:
            agent.config.configure_max_iterations(_MAX_INNER_ITERATIONS)

        if pending is not None:
            # Harmless no-op for a selection token: it was already popped
            # eagerly above, and never lived in this dict to begin with.
            _PENDING_CREDENTIAL_REQUESTS.pop(resume_token, None)

        if pending_interrupt is not None:
            inner_id, request = pending_interrupt
            # The framework wraps each pause in ToolCallInterruptRequest, so the
            # original request type is gone; the wrapper keeps the tool name and
            # the request's own fields, which is what tells the two pauses apart.
            tool_name = str(getattr(request, "tool_name", "") or "")
            logger.info(f"[browser-pause] tool={tool_name or type(request).__name__}")
            if isinstance(request, BrowserOptionSelectionRequest) or tool_name == "request_option_selection":
                return _build_option_selection_output(inner_session_id, inner_id, request)
            if isinstance(request, BrowserCredentialRequest) or tool_name == "request_login_credentials":
                conversation_id = expected_conversation_id
                if not conversation_id and outer_session is not None:
                    get_session_id = getattr(outer_session, "get_session_id", None)
                    if callable(get_session_id):
                        conversation_id = str(get_session_id() or "")
                return _build_credential_request_output(
                    inner_session_id,
                    inner_id,
                    request,
                    conversation_id=conversation_id,
                )
            return ToolOutput(success=False, error="The browser paused for a step this tool can't resume.")

        if not final_text:
            final_text = "The browser agent finished without a final summary."
        data: Dict[str, Any] = {"content": _final_summary(final_text)}
        # The run stopped right on a seat picker (the prompt tells a
        # departure-choice run to do exactly that) -- hand the live session
        # straight to the app's own checkout view instead of leaving the
        # user to continue in the cramped live-view relay.
        try:
            if not await _checkout_reached() and await _seat_map_reached():
                page = await _direct_page()
                data["checkout"] = await _checkout_handoff(page)
                logger.info(f"[browser-checkout] auto url={data['checkout']['url']} cookies={len(data['checkout']['cookies'])}")
        except Exception:  # noqa: BLE001 -- the text summary still goes out even if the handoff fails
            pass
        return ToolOutput(success=True, data=data)

    async def stream(self, inputs: Any, **kwargs: Any):
        del inputs, kwargs
        if False:  # pragma: no cover -- satisfies Tool's abstract stream(); invoke() is used instead
            yield None


async def resume_browser_login(
    flow_id: str,
    credentials: Dict[str, str],
    *,
    conversation_id: str,
    step_callback: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
) -> ToolOutput:
    """Resume a pending login without exposing credentials to the outer LLM.

    TODO(frontend-secure-credentials): retain this server-side entry point when
    replacing the generic A2UI form, but call it from a dedicated credential
    WebSocket message and pass secrets to a runtime-only fill primitive rather
    than through the inner agent's model context.
    """
    tool_instance = BrowserAgentTool()
    return await tool_instance._invoke_direct(
        {"resume_token": flow_id, "credentials": credentials},
        expected_conversation_id=conversation_id,
        step_callback=step_callback,
    )


async def resume_browser_option_selection(
    flow_id: str,
    selected_label: str,
    *,
    step_callback: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
) -> ToolOutput:
    """Resume a pending option-selection pick without a model round-trip.

    Mirrors resume_browser_login above, minus the conversation-binding checks
    that one needs for sensitive credentials -- a selection is just which
    real option the user picked, with no secrecy requirement.
    """
    tool_instance = BrowserAgentTool()
    return await tool_instance._invoke_direct(
        {"resume_token": flow_id, "credentials": {"selected_label": selected_label}},
        step_callback=step_callback,
    )


__all__ = [
    "BROWSER_LOGIN_FLOW_CONTEXT_KEY",
    "BROWSER_OPTION_SELECTION_FLOW_CONTEXT_KEY",
    "BrowserAgentTool",
    "resume_browser_login",
    "resume_browser_option_selection",
]
