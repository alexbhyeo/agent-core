# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""One WebSocket connection per client, running the single A2UI ReAct agent.

``chat.start`` payloads carry either free text (``{"text": "..."}``) or a
form/button submission (``{"text": "", "uiActions": [...]}``, one entry per
``UiInteractionPart`` -- see the Flutter client's ``ChatBridge._onSend``).
"""

import asyncio
import json
import time
import uuid
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent import ReActAgent

from ..tools.browser_agent_tool import (
    BROWSER_LOGIN_FLOW_CONTEXT_KEY,
    BROWSER_OPTION_SELECTION_FLOW_CONTEXT_KEY,
    perform_browser_input,
    resume_browser_login,
    resume_browser_option_selection,
)
from .models import make_envelope


class ConnectionSession:
    """Owns one accepted WebSocket for its lifetime."""

    def __init__(self, websocket: WebSocket, agent: ReActAgent, user_id: str):
        self.websocket = websocket
        self.agent = agent
        self.user_id = user_id
        self._active_task: Optional[asyncio.Task] = None
        self._scroll_task: Optional[asyncio.Task] = None
        self._pending_scroll_dy: float = 0.0
        self._pending_scroll_conversation_id: Optional[str] = None
        self.last_heartbeat = time.time()

    async def send(
        self,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        envelope = make_envelope(event_type, payload, conversation_id)
        await self.websocket.send_json(envelope.model_dump())

    async def run(self) -> None:
        await self.websocket.accept()
        try:
            while True:
                raw = await self.websocket.receive_json()
                await self._dispatch(raw)
        except WebSocketDisconnect:
            logger.info(f"[a2ui-react-agent] websocket disconnected user={self.user_id}")
        finally:
            if self._active_task is not None and not self._active_task.done():
                self._active_task.cancel()

    async def _dispatch(self, raw: dict[str, Any]) -> None:
        msg_type = raw.get("type")
        conversation_id = raw.get("conversationId")
        payload = raw.get("payload") or {}

        if msg_type == "heartbeat":
            self.last_heartbeat = time.time()
            return

        if msg_type == "browser.input":
            if payload.get("kind") == "scroll":
                self._queue_browser_scroll(conversation_id, payload)
            else:
                asyncio.create_task(self._run_browser_input(conversation_id, payload))
            return

        if msg_type == "chat.cancel":
            if self._active_task is not None and not self._active_task.done():
                self._active_task.cancel()
            return

        if msg_type == "chat.start":
            if self._active_task is not None and not self._active_task.done():
                await self.send(
                    "error.validation",
                    {"message": "A chat is already running on this connection."},
                    conversation_id,
                )
                return
            self._active_task = asyncio.create_task(self._run_chat(conversation_id, payload))
            return

        await self.send("error.validation", {"message": f"Unknown message type: {msg_type}"}, conversation_id)

    async def _run_chat(self, conversation_id: Optional[str], payload: dict[str, Any]) -> None:
        await self.send("chat.accepted", {}, conversation_id)

        # ``llm_output`` is token-streamed for each model turn. ``answer``
        # follows the run and normally repeats the most recent turn, but is
        # also the only text source for non-streaming model calls.
        # ``any_text_sent``/``last_presentation_text`` (unlike ``last_llm_text``,
        # never reset mid-run) track whether the model said *anything* of its
        # own across the whole run, so a fallback bubble can be synthesized
        # from the last presentation tool's own text (see ``_translate``) if
        # not -- the model usually adds a trailing turn of its own after a
        # `show_card`/`show_info_list`/etc. call, but occasionally doesn't,
        # which otherwise leaves the user with a card and no reply bubble.
        # ``bubble_open`` tracks whether the client's current text bubble is
        # still appendable (no genui card has broken it yet), to decide
        # whether a new model turn's text needs a separator before it.
        state = {
            "last_llm_text": "",
            "any_text_sent": False,
            "last_presentation_text": "",
            "bubble_open": False,
            "geocode_pending": 0,
            "deferred_genui": [],
        }
        try:
            credential_submission = _extract_browser_credential_submission(payload.get("uiActions"))
            if credential_submission is not None:
                flow_id, credentials = credential_submission
                await self._resume_browser_login(conversation_id, flow_id, credentials)
                return

            selection_submission = _extract_browser_option_selection_submission(payload.get("uiActions"))
            if selection_submission is not None:
                flow_id, selected_label = selection_submission
                await self._resume_browser_option_selection(conversation_id, flow_id, selected_label)
                return

            # Inside the try, not computed above chat.accepted -- a malformed
            # uiActions entry (e.g. a client sending "action" as a bare string
            # instead of the documented {"name": ..., ...} object) used to
            # raise here uncaught, since this ran before the try existed.
            # _run_chat is wrapped in asyncio.create_task with nothing
            # awaiting it, so that exception just vanished into "Task
            # exception was never retrieved" -- no error.* event, no
            # chat.completed, the client hangs forever on a turn that
            # already silently died server-side.
            text = payload.get("text", "") or _describe_ui_actions(payload.get("uiActions"))
            async for chunk in Runner.run_agent_streaming(self.agent, {"query": text}, session=conversation_id):
                for event_type, event_payload in _translate(chunk, state):
                    await self.send(event_type, event_payload, conversation_id)
        except asyncio.CancelledError:
            await self.send("chat.cancelled", {}, conversation_id)
            return
        except Exception as exc:  # noqa: BLE001 -- surface any agent-run failure to the client
            logger.exception("[a2ui-react-agent] agent run failed")
            await self.send("error.agent", {"message": str(exc)}, conversation_id)
            return

        if not state["any_text_sent"] and state["last_presentation_text"]:
            await self.send("chat.token", {"text": state["last_presentation_text"]}, conversation_id)

        await self.send("chat.completed", {}, conversation_id)
        # TODO(frontend-secure-credentials): the compiled client ignores form
        # taps while processing. Show only the temporary login form after the
        # completion event has cleared that guard; a native secure form removes
        # this ordering shim.
        for message in state["deferred_genui"]:
            await self.send("genui", message, conversation_id)

    def _queue_browser_scroll(self, conversation_id: Optional[str], payload: dict[str, Any]) -> None:
        # A drag sends many small scrolls. Each one costs a page round-trip plus
        # a screenshot, so they are summed here and drained in one pass -- the
        # frame always shows the page as far as the drag has gone so far.
        self._pending_scroll_dy += _scroll_dy(payload)
        self._pending_scroll_conversation_id = conversation_id
        if self._scroll_task is None or self._scroll_task.done():
            self._scroll_task = asyncio.create_task(self._drain_browser_scroll())

    async def _drain_browser_scroll(self) -> None:
        while abs(self._pending_scroll_dy) >= 1:
            dy = self._pending_scroll_dy
            self._pending_scroll_dy = 0.0
            conversation_id = self._pending_scroll_conversation_id
            await self._run_browser_input(conversation_id, {"kind": "scroll", "dy": dy})

    async def _run_browser_input(self, conversation_id: Optional[str], payload: dict[str, Any]) -> None:
        try:
            outcome = await perform_browser_input(payload)
        except Exception:  # noqa: BLE001 -- never echo the submitted values back in an error
            await self.send("error.tool", {"tool": "browser_agent_run", "message": "The browser view could not be updated."}, conversation_id)
            return
        if outcome is None:
            return
        frame = outcome.get("frame")
        if frame is not None:
            await self.send(
                "browser.frame",
                {"screenshot_base64": frame["base64"], "screenshot_mime": frame["mime"]},
                conversation_id,
            )
        if outcome.get("select_options"):
            await self.send("browser.select", {"options": outcome["select_options"]}, conversation_id)
        if "field" in outcome:
            await self.send("browser.field", {"field": outcome["field"]}, conversation_id)

    async def _resume_browser_login(
        self,
        conversation_id: Optional[str],
        flow_id: str,
        credentials: dict[str, str],
    ) -> None:
        """Compatibility route that keeps submitted credentials out of chat.

        TODO(frontend-secure-credentials): replace the generic ``chat.start``
        uiAction with a dedicated credential envelope and a native masked form.
        This backend entry point and conversation binding can remain.
        """
        if not conversation_id:
            await self.send("error.validation", {"message": "A conversation is required for login."})
            return

        call_id = f"browser-login-{uuid.uuid4().hex[:8]}"
        await self.send(
            "tool.started",
            {"tool": "browser_agent_run", "callId": call_id, "text": "Signing in and continuing…"},
            conversation_id,
        )

        async def _send_step(payload: dict[str, Any]) -> None:
            await self.send("browser.step", payload, conversation_id)

        try:
            result = await resume_browser_login(
                flow_id,
                credentials,
                conversation_id=conversation_id,
                step_callback=_send_step,
            )
        finally:
            # Drop the only WebSocket-layer reference before any subsequent
            # model or presentation work. Do not log/cache it for retry.
            credentials.clear()
        await self.send(
            "tool.finished",
            {"tool": "browser_agent_run", "callId": call_id},
            conversation_id,
        )

        if not result.success:
            await self.send(
                "error.tool",
                {"tool": "browser_agent_run", "callId": call_id, "message": result.error or "Login failed."},
                conversation_id,
            )
            return

        data = result.data if isinstance(result.data, dict) else {}
        messages = data.get("genui") or []
        defer_genui = data.get("defer_genui_until_completed") is True
        if not defer_genui:
            for message in messages:
                await self.send("genui", message, conversation_id)
        display_text = str(data.get("text") or data.get("content") or "The browser task resumed.")
        await self.send("chat.token", {"text": display_text}, conversation_id)
        await self.send("chat.completed", {}, conversation_id)
        if defer_genui:
            for message in messages:
                await self.send("genui", message, conversation_id)

    async def _resume_browser_option_selection(
        self,
        conversation_id: Optional[str],
        flow_id: str,
        selected_label: str,
    ) -> None:
        """Resume a paused option-selection pick without a model round-trip.

        A selection isn't sensitive like a login's credentials (see
        _resume_browser_login above), but it still never goes through the
        outer model -- resume_token/credentials were removed from
        browser_agent_run's own model-facing schema, so this is the only way
        a pick ever reaches the paused inner browser run.
        """
        if not conversation_id:
            await self.send("error.validation", {"message": "A conversation is required to continue."})
            return

        call_id = f"browser-select-{uuid.uuid4().hex[:8]}"
        await self.send(
            "tool.started",
            {"tool": "browser_agent_run", "callId": call_id, "text": "Continuing with your selection…"},
            conversation_id,
        )

        async def _send_step(payload: dict[str, Any]) -> None:
            await self.send("browser.step", payload, conversation_id)

        result = await resume_browser_option_selection(
            flow_id,
            selected_label,
            step_callback=_send_step,
        )
        await self.send(
            "tool.finished",
            {"tool": "browser_agent_run", "callId": call_id},
            conversation_id,
        )

        if not result.success:
            await self.send(
                "error.tool",
                {"tool": "browser_agent_run", "callId": call_id, "message": result.error or "Selection failed."},
                conversation_id,
            )
            return

        data = result.data if isinstance(result.data, dict) else {}
        messages = data.get("genui") or []
        defer_genui = data.get("defer_genui_until_completed") is True
        if not defer_genui:
            for message in messages:
                await self.send("genui", message, conversation_id)
        display_text = str(data.get("text") or data.get("content") or "The browser task resumed.")
        await self.send("chat.token", {"text": display_text}, conversation_id)
        await self.send("chat.completed", {}, conversation_id)
        if defer_genui:
            for message in messages:
                await self.send("genui", message, conversation_id)


def _scroll_dy(payload: dict[str, Any]) -> float:
    try:
        return float(payload.get("dy") or 0)
    except (TypeError, ValueError):
        return 0.0


def _extract_browser_credential_submission(
    ui_actions: Optional[list[dict[str, Any]]],
) -> Optional[tuple[str, dict[str, str]]]:
    """Extract the transitional generic-A2UI login submission.

    Matching submissions are handled before ``_describe_ui_actions`` so their
    values can never become an outer-agent user message.  Malformed matching
    submissions raise a value error that is surfaced by ``_run_chat`` without
    echoing any submitted value.
    """
    if not ui_actions:
        return None
    for entry in ui_actions:
        if not isinstance(entry, dict):
            continue
        action = entry.get("action", entry)
        if not isinstance(action, dict) or action.get("name") != "submit_browser_credentials":
            continue
        context = action.get("context")
        if not isinstance(context, dict):
            raise ValueError("Malformed browser credential submission.")
        flow_id = str(context.get(BROWSER_LOGIN_FLOW_CONTEXT_KEY) or "").strip()
        if not flow_id:
            raise ValueError("Browser credential submission is missing its flow ID.")
        credentials = {
            str(key): str(value)
            for key, value in context.items()
            if key != BROWSER_LOGIN_FLOW_CONTEXT_KEY
        }
        if not credentials:
            raise ValueError("Browser credential submission did not contain any fields.")
        return flow_id, credentials
    return None


def _extract_browser_option_selection_submission(
    ui_actions: Optional[list[dict[str, Any]]],
) -> Optional[tuple[str, str]]:
    """Extract the transitional generic-A2UI option-selection submission.

    Same shape as _extract_browser_credential_submission above, for
    submit_browser_option_selection instead -- see that function's docstring.
    A selection isn't sensitive, but it still can't reach the outer model:
    resume_token/credentials were removed from browser_agent_run's own
    model-facing schema (see browser_agent_tool._INPUT_PARAMS), so this is
    the only path left to resume that paused call.
    """
    if not ui_actions:
        return None
    for entry in ui_actions:
        if not isinstance(entry, dict):
            continue
        action = entry.get("action", entry)
        if not isinstance(action, dict) or action.get("name") != "submit_browser_option_selection":
            continue
        context = action.get("context")
        if not isinstance(context, dict):
            raise ValueError("Malformed browser option-selection submission.")
        flow_id = str(context.get(BROWSER_OPTION_SELECTION_FLOW_CONTEXT_KEY) or "").strip()
        if not flow_id:
            raise ValueError("Browser option-selection submission is missing its flow ID.")
        # ChoicePicker's data-model value is structurally a list even for a
        # single-select field (see BrowserOptionSelectionInterruptRail's own
        # resolve_interrupt), so accept either shape here too.
        raw_selected = context.get("selected_label")
        selected_label = str(raw_selected[0]) if isinstance(raw_selected, list) and raw_selected else str(raw_selected or "")
        if not selected_label:
            raise ValueError("Browser option-selection submission did not contain a selected option.")
        return flow_id, selected_label
    return None


def _describe_ui_actions(ui_actions: Optional[list[dict[str, Any]]]) -> str:
    """Turn a submitted form/button's UI action(s) into a query for the LLM.

    Each entry is a decoded ``UiInteractionPart`` (see chat_bridge.dart's
    ``_onSend``): ``{"version": "v0.9", "action": {"name", "sourceComponentId",
    "context", "timestamp"}}``. ``context`` holds whatever the button's
    ``action.event.context`` path bindings resolved to on the data model --
    i.e. the user's field values (see ``genui.form``/``genui.button``).
    """
    if not ui_actions:
        return ""
    parts = []
    for entry in ui_actions:
        action = entry.get("action", entry)
        name = action.get("name", "ui_action")
        context = action.get("context", {})
        parts.append(f"[UI action: {name}] submitted values: {json.dumps(context)}")
    return "\n".join(parts)


def _tool_progress_text(tool_name: str, tool_args: Any) -> str:
    """Human-readable status line for a tool.started event's "text" field.

    Surfaced in the client's "Working..." row while a tool call is in
    flight (see Index.ets's kind === 'working' rendering) so a slow
    multi-tool-call turn -- e.g. show_map's caller geocoding several places
    one at a time before the map itself renders -- reads as visible
    progress instead of one static "Working..." the whole time.
    """
    args = tool_args if isinstance(tool_args, dict) else {}
    if tool_name == "geocode_place":
        query = args.get("query")
        return f"Looking up {query}…" if query else "Looking up a place…"
    if tool_name == "show_map":
        title = args.get("title")
        return f"Building your map: {title}…" if title else "Building your map…"
    if tool_name:
        # Generic fallback for every other tool: "search_flights" -> "Search flights…".
        readable = tool_name.replace("_", " ").strip()
        if readable:
            return readable[0].upper() + readable[1:] + "…"
    return "Working…"


def _translate(chunk: Any, state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Map one OutputSchema chunk from the ReAct agent to zero or more wire events."""
    chunk_type = getattr(chunk, "type", None)
    payload = getattr(chunk, "payload", None)
    events: list[tuple[str, dict[str, Any]]] = []

    if chunk_type == "llm_output":
        text = _as_text(payload)
        if text:
            if not state["last_llm_text"] and state["bubble_open"]:
                # First chunk of a new model turn, and the client's current
                # text bubble is still open (no genui surface has appeared
                # since text last flowed into it -- see the tool_result
                # branch) -- e.g. a lead-in sentence before an earlier batch
                # of data-only tool calls like `search_images`, followed by
                # a closing sentence once they return. Without a separator
                # this would run straight into that prior text mid-sentence,
                # since the client appends same-turn-or-not chat.token text
                # into one transcript entry as long as it's stayed a text
                # bubble (see Index.ets's appendAssistantText). Sent as its
                # own event so ``last_llm_text`` keeps accumulating only the
                # model's actual raw output, not this injected separator --
                # that raw text is what ``_unsent_suffix`` diffs the final
                # ``answer`` chunk against. Once a presentation tool's genui
                # actually renders a card, the client starts a fresh bubble
                # on its own (a `createSurface` message breaks the "append to
                # last text row" chain there) -- see ``bubble_open`` below.
                events.append(("chat.token", {"text": "\n\n"}))
            state["last_llm_text"] += text
            state["any_text_sent"] = True
            state["bubble_open"] = True
            events.append(("chat.token", {"text": text}))
        return events

    if chunk_type == "answer":
        # A final answer uses ``output`` (not ``content``). Emit it if the
        # preceding model turn was non-streaming; otherwise emit only the
        # part that was not already delivered as ``llm_output`` tokens.
        text = _as_text(payload)
        unsent_text = _unsent_suffix(text, state["last_llm_text"])
        if unsent_text:
            state["any_text_sent"] = True
            state["bubble_open"] = True
            events.append(("chat.token", {"text": unsent_text}))
        return events

    if chunk_type == "tool_call":
        # A tool call starts a new model turn. Do not use text streamed before
        # the tool when deduplicating the final answer after the tool result.
        state["last_llm_text"] = ""
        tool_name = (payload or {}).get("tool_name", "")
        tool_args = (payload or {}).get("tool_args")
        call_id = (payload or {}).get("tool_call_id")
        if tool_name == "geocode_place":
            # geocode_place is called once per place, often several at once
            # in one parallel batch (e.g. 7 calls for a 7-place map) -- track
            # how many are still outstanding so the batch's *completion* can
            # also report progress (see tool_result below). Without this, the
            # "Working..." row freezes on whichever place happened to be
            # looked up last for the entire stretch afterward where the model
            # is silently reasoning over all the results to build the next
            # (usually single, much larger) show_map call -- often the
            # longest, least-explained part of the whole request.
            state["geocode_pending"] = state.get("geocode_pending", 0) + 1
        events.append((
            "tool.started",
            {"tool": tool_name, "callId": call_id, "text": _tool_progress_text(tool_name, tool_args)},
        ))
        return events

    if chunk_type == "tool_result":
        state["last_llm_text"] = ""
        tool_name = (payload or {}).get("tool_name", "")
        call_id = (payload or {}).get("tool_call_id")
        tool_result = (payload or {}).get("tool_result")
        finished_payload: dict[str, Any] = {"tool": tool_name, "callId": call_id}
        if tool_name == "geocode_place":
            state["geocode_pending"] = max(0, state.get("geocode_pending", 0) - 1)
            if state["geocode_pending"] == 0:
                # Last outstanding lookup in the batch just came back. The
                # model still has to reason over every result and produce the
                # (larger, single) show_map call next -- report that instead
                # of leaving the row on a stale "Looking up ..." place name.
                finished_payload["text"] = "Got them all — planning your map…"
        events.append(("tool.finished", finished_payload))

        result_text, genui_messages = _extract_result(tool_result)
        if genui_messages and result_text:
            # Only "presentation" tools (show_card/show_info_list/etc.) return
            # both -- a data tool like search_images has no genui, so it never
            # overwrites this. Keeps the *last* one, matching whichever card
            # actually ended up on screen.
            state["last_presentation_text"] = result_text
        if result_text.startswith("[ERROR]"):
            # Some tools (e.g. fetch_webpage) catch their own failures and
            # return an "[ERROR]: ..." string instead of raising -- so this
            # never reaches on_tool_exception. Surface it the same way a
            # real exception does.
            events.append(("error.tool", {"tool": tool_name, "callId": call_id, "message": result_text}))
        elif result_text:
            events.append(("tool.output", {"tool": tool_name, "callId": call_id, "text": result_text}))
        defer_genui = (
            isinstance(tool_result, ToolOutput)
            and isinstance(tool_result.data, dict)
            and tool_result.data.get("defer_genui_until_completed") is True
        )
        if defer_genui:
            state.setdefault("deferred_genui", []).extend(genui_messages or [])
        else:
            for message in genui_messages or []:
                events.append(("genui", message))
        if genui_messages:
            # A rendered card breaks the client's current text bubble (a
            # `createSurface` message ends the "append to last text row"
            # chain -- see Index.ets's appendAssistantText/onCreateSurface).
            # Whatever text comes next starts a brand new bubble there on its
            # own, so no leading separator is needed for it.
            state["bubble_open"] = False
        return events

    if chunk_type == "tool_error":
        state["last_llm_text"] = ""
        # A2uiToolEventRail.on_tool_exception -- the tool actually raised.
        tool_name = (payload or {}).get("tool_name", "")
        call_id = (payload or {}).get("tool_call_id")
        message = (payload or {}).get("message", "Tool execution failed.")
        events.append(("tool.finished", {"tool": tool_name, "callId": call_id}))
        events.append(("error.tool", {"tool": tool_name, "callId": call_id, "message": message}))
        return events

    if chunk_type == "browser_agent_step":
        # Emitted by BrowserAgentTool.invoke while its inner browser subagent
        # runs, one per navigate/click/fill/probe/etc. -- see
        # browser_agent_tool.py. Passed through mostly as-is for the client's
        # expandable action-log panel (see ChatBridge.onBrowserAgentStep).
        events.append(("browser.step", dict(payload or {})))
        return events

    # llm_reasoning / llm_usage / anything else: internal, not surfaced.
    return events


def _as_text(payload: Any) -> str:
    if isinstance(payload, dict):
        # Stream chunks use ``content``; the run's final ``answer`` chunk
        # uses ``output``. Supporting both prevents a non-streaming final
        # turn from silently disappearing.
        return str(payload.get("content", payload.get("output", "")) or "")
    if payload is None:
        return ""
    return str(payload)


def _unsent_suffix(final_text: str, streamed_text: str) -> str:
    """Return only final-answer text that has not already been streamed.

    The framework normally repeats a model turn as its terminal ``answer``.
    If the provider supplied only part of that turn as tokens, preserve the
    remaining suffix. A distinct final answer is intentionally returned in
    full: it belongs after the tool result rather than being dropped.
    """
    if not final_text or final_text == streamed_text or streamed_text.endswith(final_text):
        return ""
    if final_text.startswith(streamed_text):
        return final_text[len(streamed_text):]
    return final_text


def _extract_result(tool_result: Any) -> tuple[str, Optional[list[dict[str, Any]]]]:
    if isinstance(tool_result, dict):
        return str(tool_result.get("text", "") or ""), tool_result.get("genui")
    if isinstance(tool_result, ToolOutput):
        # Hand-written Tool subclasses (e.g. BrowserAgentTool) return this
        # instead of the plain {"text": ..., "genui": [...]} dict @tool
        # functions do -- AbilityManager hands the raw ToolOutput straight
        # through to ctx.inputs.tool_result, so without this branch its
        # genui (if any) silently never reaches the client at all.
        data = tool_result.data if isinstance(tool_result.data, dict) else {}
        return str(data.get("text", "") or ""), data.get("genui")
    if tool_result is None:
        return "", None
    return str(tool_result), None
