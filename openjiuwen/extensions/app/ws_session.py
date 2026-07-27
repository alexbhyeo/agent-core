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
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

from openjiuwen.core.common.logging import logger
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent import ReActAgent

from .models import make_envelope


class ConnectionSession:
    """Owns one accepted WebSocket for its lifetime."""

    def __init__(self, websocket: WebSocket, agent: ReActAgent, user_id: str):
        self.websocket = websocket
        self.agent = agent
        self.user_id = user_id
        self._active_task: Optional[asyncio.Task] = None
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
        text = payload.get("text", "") or _describe_ui_actions(payload.get("uiActions"))
        await self.send("chat.accepted", {}, conversation_id)

        state = {"streamed_token": False}
        try:
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

        await self.send("chat.completed", {}, conversation_id)


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


def _translate(chunk: Any, state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Map one OutputSchema chunk from the ReAct agent to zero or more wire events."""
    chunk_type = getattr(chunk, "type", None)
    payload = getattr(chunk, "payload", None)
    events: list[tuple[str, dict[str, Any]]] = []

    if chunk_type == "llm_output":
        text = _as_text(payload)
        if text:
            state["streamed_token"] = True
            events.append(("chat.token", {"text": text}))
        return events

    if chunk_type == "answer" and not state["streamed_token"]:
        # Fallback for non-streaming LLM calls that only emit a single final
        # chunk rather than incremental tokens.
        text = _as_text(payload)
        if text:
            events.append(("chat.token", {"text": text}))
        return events

    if chunk_type == "tool_call":
        tool_name = (payload or {}).get("tool_name", "")
        events.append(("tool.started", {"tool": tool_name}))
        return events

    if chunk_type == "tool_result":
        tool_name = (payload or {}).get("tool_name", "")
        tool_result = (payload or {}).get("tool_result")
        events.append(("tool.finished", {"tool": tool_name}))

        result_text, genui_messages = _extract_result(tool_result)
        if result_text:
            events.append(("tool.output", {"text": result_text}))
        for message in genui_messages or []:
            events.append(("genui", message))
        return events

    # llm_reasoning / llm_usage / anything else: internal, not surfaced.
    return events


def _as_text(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("content", "") or "")
    if payload is None:
        return ""
    return str(payload)


def _extract_result(tool_result: Any) -> tuple[str, Optional[list[dict[str, Any]]]]:
    if isinstance(tool_result, dict):
        return str(tool_result.get("text", "") or ""), tool_result.get("genui")
    if tool_result is None:
        return "", None
    return str(tool_result), None
