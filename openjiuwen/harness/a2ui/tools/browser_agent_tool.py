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
import uuid
from typing import Any, Dict, Optional

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolOutput
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.rails.context_engineer import ContextProcessorRail
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

Use browser_probe_interactives to see a page's controls and browser_probe_cards for \
repeated results/listings before deciding what to click or fill; use browser_navigate \
directly to a known or constructed results URL when that is faster than clicking \
through. Prefer browser_batch_interact once two or more actions in a row are already \
decided. Stop as soon as you have real, concrete results (or a real, specific blocker) \
for the task -- do not keep browsing "to be thorough" once you already have enough to \
answer it. End with a concise, factual summary of exactly what you found (real operator/ \
flight/hotel names, times, prices, and the page URL) -- never invent or guess a detail \
you did not actually see on a page."""

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
                    "self-contained instruction."
                ),
                input_params=_INPUT_PARAMS,
            )
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> ToolOutput:
        outer_session = kwargs.get("session")
        task = ""
        if isinstance(inputs, dict):
            task = str(inputs.get("task") or "").strip()
        if not task:
            return ToolOutput(success=False, error="'task' is required.")

        try:
            agent = await _get_browser_agent()
        except Exception as exc:  # noqa: BLE001 -- report startup failure, don't crash the outer turn
            return ToolOutput(success=False, error=f"Browser agent unavailable: {exc}")

        async def _emit(payload: dict[str, Any]) -> None:
            if outer_session is None:
                return
            await outer_session.write_stream(
                OutputSchema(type="browser_agent_step", index=0, payload=payload)
            )

        final_text = ""
        inner_session_id = f"browser-agent-{uuid.uuid4().hex}"
        try:
            async for chunk in Runner.run_agent_streaming(agent, {"query": task}, session=inner_session_id):
                chunk_type = getattr(chunk, "type", None)
                payload = getattr(chunk, "payload", None) or {}
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
        except Exception as exc:  # noqa: BLE001 -- report the failure as a tool result, don't crash the outer turn
            await _emit({"status": "error", "tool": "", "text": f"Browser agent run failed: {exc}"})
            return ToolOutput(success=False, error=f"Browser agent run failed: {exc}")

        if not final_text:
            final_text = "The browser agent finished without a final summary."
        return ToolOutput(success=True, data={"content": _final_summary(final_text)})

    async def stream(self, inputs: Any, **kwargs: Any):
        del inputs, kwargs
        if False:  # pragma: no cover -- satisfies Tool's abstract stream(); invoke() is used instead
            yield None


__all__ = ["BrowserAgentTool"]
