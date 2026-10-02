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
from collections.abc import Awaitable, Callable
from typing import Any, Dict, List, Optional

from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolOutput, tool
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.rails.context_engineer import ContextProcessorRail
from openjiuwen.harness.rails.interrupt.interrupt_base import BaseInterruptRail, InterruptDecision
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_capabilities import (
    resolve_browser_capabilities,
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

_MAX_INNER_ITERATIONS = 30

# TODO(frontend-secure-credentials): replace this hidden generic-A2UI context
# value with a dedicated browser.credentials.submit message emitted by a
# native, masked credential form.  It is deliberately non-secret: it only
# identifies server-side pending state and is bound to one conversation.
BROWSER_LOGIN_FLOW_CONTEXT_KEY = "__browser_login_flow_id"
_LOGIN_FLOW_TTL_SECONDS = 5 * 60

_INNER_SYSTEM_PROMPT = """You are a browser automation agent that carries out one real, \
multi-step web task per call: navigating, scrolling, clicking, filling in forms, and \
extracting information from real pages using the available browser tools.

You MAY click "Search"/"Submit"/"Apply"/"Filter"-style controls freely whenever doing so \
retrieves information -- e.g. submitting a route/date search form on a bus, flight, or \
hotel site to see the list of available operators, times, and prices. That is expected, \
normal, core behavior for this task, not a boundary violation.

You must NEVER click, tap, or submit anything that would complete or finalize a real \
transaction on the user's behalf -- this includes, in any language, "Buy", "Pay", \
"Purchase", "Confirm order", "Place order", "Checkout", "Book now", "Confirm booking", or \
entering real payment/card/passenger-identity details. The instant a page's next action \
would be one of those, STOP and report back what you already found (including that \
page's URL) instead of proceeding -- the user always completes the actual purchase \
themselves, on the real site, after this call hands the options back to them.

If a page requires you to log in (username/password, or similar) before you can proceed, \
and you do not already have credentials for it, call request_login_credentials with the \
specific fields the page's own form actually asks for and a short reason -- never guess, \
invent, or attempt to bypass a login. That call pauses you here; once the real user \
provides their credentials, they come back as request_login_credentials's own result -- \
use them to actually log in on the page, then continue the task from there.

Use browser_probe_interactives to see a page's controls and browser_probe_cards for \
repeated results/listings before deciding what to click or fill; use browser_navigate \
directly to a known or constructed results URL when that is faster than clicking \
through. Prefer browser_batch_interact once two or more actions in a row are already \
decided. Stop as soon as you have real, concrete results (or a real, specific blocker) \
for the task -- do not keep browsing "to be thorough" once you already have enough to \
answer it. End with a concise, factual summary of exactly what you found (real operator/ \
flight/hotel names, times, prices, and the page URL) -- never invent or guess a detail \
you did not actually see on a page."""


class BrowserCredentialRequest(InterruptRequest):
    """Interrupt payload for request_login_credentials -- see
    BrowserCredentialInterruptRail below."""

    fields: List[str] = []
    reason: str = ""


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
        return self.interrupt(
            BrowserCredentialRequest(
                message="Login required",
                fields=fields or ["Username", "Password"],
                reason=str(args.get("reason") or ""),
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


_browser_agent: Optional[ReActAgent] = None
_browser_runtime: Optional[BrowserAgentRuntime] = None
_init_lock = asyncio.Lock()


def _force_headless_mcp_env() -> None:
    """This runs on a headless server with no display -- Playwright MCP is
    headed by default, so force --headless/--no-sandbox unless an operator
    already set PLAYWRIGHT_MCP_ARGS explicitly."""
    os.environ.setdefault(
        "PLAYWRIGHT_MCP_ARGS",
        "-y @playwright/mcp@0.0.78 --headless --no-sandbox",
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


def _step_text(tool_name: str, tool_args: Any) -> str:
    """Human-readable line for one inner browser action, shown live in the
    client's action-log panel (see browser.step in ws_session.py)."""
    args = tool_args if isinstance(tool_args, dict) else {}
    tool_name = _normalized_tool_name(tool_name)
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
                    "transport type. It can navigate, scroll, click, fill in forms, and "
                    "submit search/filter forms to retrieve real results -- but it will never "
                    "complete an actual purchase, booking, or payment; it always stops and "
                    "reports back once it has real results (or a real blocker) for you to "
                    "present to the user, the same way `browser_inspect_page` does -- you "
                    "still hand the user off to the real site via `show_card`'s `link_url` "
                    "to actually finish there themselves. While it runs, the user can watch "
                    "its individual actions (navigate/click/fill/extract) live in an "
                    "expandable panel, so it's fine for this to take several steps and a bit "
                    "longer than your other tools -- do not avoid it just because it's "
                    "slower. Give `task` every constraint the user already gave (route, "
                    "dates, passenger count, etc.), written as a single, specific, "
                    "self-contained instruction. If the site hits a real login wall, this "
                    "tool pauses itself and shows its own login form. The server resumes that "
                    "run directly after submission; never ask for credentials in chat and "
                    "never start a fresh task for that login."
                ),
                input_params=_INPUT_PARAMS,
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
            if pending is None:
                return ToolOutput(
                    success=False,
                    error="This login request is no longer active -- ask the user to try again.",
                )
            if float(pending.get("expires_at") or 0) <= time.monotonic():
                _PENDING_CREDENTIAL_REQUESTS.pop(resume_token, None)
                return ToolOutput(success=False, error="This login request expired -- ask the user to try again.")
            bound_conversation_id = str(pending.get("conversation_id") or "")
            if bound_conversation_id and expected_conversation_id != bound_conversation_id:
                return ToolOutput(success=False, error="This login request does not belong to this conversation.")
            if pending.get("resuming"):
                return ToolOutput(success=False, error="This login request is already being resumed.")
            expected_keys = set(pending.get("credential_keys") or [])
            if not credentials or set(credentials) != expected_keys:
                return ToolOutput(success=False, error="The submitted login fields did not match this request.")
            pending["resuming"] = True
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

        if pending is not None:
            _PENDING_CREDENTIAL_REQUESTS.pop(resume_token, None)

        if pending_interrupt is not None:
            inner_id, request = pending_interrupt
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

        if not final_text:
            final_text = "The browser agent finished without a final summary."
        return ToolOutput(success=True, data={"content": _final_summary(final_text)})

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


__all__ = ["BROWSER_LOGIN_FLOW_CONTEXT_KEY", "BrowserAgentTool", "resume_browser_login"]
