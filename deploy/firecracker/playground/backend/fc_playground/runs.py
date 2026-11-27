# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Query runs: one asyncio task per running session, streaming normalised events.

A ``deepagent`` session keeps one DeepAgent instance in this process, so follow-up
questions continue the conversation. A ``team`` session runs an AgentTeam whose
members share the session's VM (same SysOperationSpec id, team session id).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Optional

from fc_playground.config import Settings
from fc_playground.events import EventHub
from fc_playground.store import PlaygroundStore
from fc_playground.vm import gateway_config, team_sys_operation, user_sys_operation

from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.stream.base import OutputSchema, TraceSchema
from openjiuwen.core.sys_operation.cwd import init_cwd

logger = logging.getLogger(__name__)

VM_GUIDE = (
    "Your file and shell tools run inside this session's own Linux microVM; keep work under /workspace. "
    "The VM has internet access (pip, git). Tasks you start in the VM can talk to the user: "
    "`fc-notify TEXT` sends the user a message and `fc-ask TEXT` asks the user a question and prints the "
    "answer (exit code 2 if unanswered). When you start a background command, do not end the command with "
    "`&`; end it with `& echo started` instead."
)
SYSTEM_PROMPT = "You are a helpful engineering assistant. Always use your tools for files and commands. " + VM_GUIDE
TEAM_NOTE = "\n\n(Environment note for every team member: " + VM_GUIDE + ")"
_SKIP_TYPES = {"context.usage", "llm_usage"}
VM_WORKSPACE = "/workspace"


class Busy(Exception):
    """The session already has a running query (HTTP 409)."""


class Saturated(Exception):
    """Too many runs in the backend (HTTP 429)."""


def build_model() -> Model:
    return Model(
        model_client_config=ModelClientConfig(
            client_provider=os.getenv("MODEL_PROVIDER", "OpenAI"), api_key=os.getenv("API_KEY", ""),
            api_base=os.getenv("API_BASE", ""), timeout=int(os.getenv("MODEL_TIMEOUT", "180"))),
        model_config=ModelRequestConfig(model=os.getenv("MODEL_NAME", ""), temperature=0.2),
    )


def _text_of(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("content", "output", "text", "message"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return payload if isinstance(payload, str) else ""


def _short(value: Any, limit: int = 2000) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + " …"


def normalize_chunk(chunk: Any) -> list[dict[str, Any]]:
    """Turn a DeepAgent / AgentTeam stream chunk into UI events ``{member, kind, text, data}``."""
    member = getattr(chunk, "source_member", None)
    if isinstance(chunk, TraceSchema):
        payload = chunk.payload if isinstance(chunk.payload, dict) else {}
        name = payload.get("name") or payload.get("invokeType") or payload.get("componentName") or "tool"
        inputs = (payload.get("inputs") or {}).get("inputs", payload.get("inputs"))
        invoke_id = payload.get("invokeId")  # pairs a result with its call (parallel calls finish out of order)
        if payload.get("outputs") is None and payload.get("error") is None:
            return [{"member": member, "kind": "tool_call", "text": str(name),
                     "data": {"inputs": _short(inputs), "invoke_id": invoke_id}}]
        return [{"member": member, "kind": "tool_result", "text": str(name),
                 "data": {"outputs": _short(payload.get("outputs")), "error": _short(payload.get("error"))
                          if payload.get("error") else None, "invoke_id": invoke_id}}]
    if not isinstance(chunk, OutputSchema):
        return [{"member": member, "kind": "other", "text": _short(chunk, 500), "data": None}]
    payload = chunk.payload
    if chunk.type in _SKIP_TYPES:
        return []
    if isinstance(payload, dict) and str(payload.get("event_type", "")).startswith("team."):
        return [{"member": member, "kind": "team_event", "text": str(payload.get("event_type")),
                 "data": {k: _short(v, 300) for k, v in payload.items() if k != "event_type"}}]
    if chunk.type == "llm_output":
        return [{"member": member, "kind": "delta", "text": _text_of(payload), "data": None}]
    if chunk.type == "llm_reasoning":
        return [{"member": member, "kind": "reasoning", "text": _text_of(payload), "data": None}]
    if chunk.type == "answer":
        return [{"member": member, "kind": "answer", "text": _text_of(payload), "data": None}]
    return [{"member": member, "kind": "other", "text": f"{chunk.type}: {_short(payload, 500)}", "data": None}]


class RunManager:
    def __init__(self, settings: Settings, store: PlaygroundStore, hub: EventHub):
        self._settings = settings
        self._store = store
        self._hub = hub
        self._runs: dict[str, tuple[str, asyncio.Task]] = {}
        self._agents: dict[str, Any] = {}
        self._teams: dict[str, Any] = {}

    def count(self) -> int:
        return len(self._runs)

    def running(self, session_id: str) -> Optional[str]:
        entry = self._runs.get(session_id)
        return entry[0] if entry else None

    def start(self, user: str, session: dict[str, Any], text: str) -> str:
        session_id = session["session_id"]
        if session_id in self._runs:
            raise Busy(f"session {session_id} is already running a query")
        if len(self._runs) >= self._settings.max_runs:
            raise Saturated(f"{len(self._runs)} queries are running (limit {self._settings.max_runs})")
        run_id = uuid.uuid4().hex[:12]
        task = asyncio.create_task(self._run(user, session, text, run_id))
        self._runs[session_id] = (run_id, task)
        return run_id

    def cancel(self, session_id: str) -> bool:
        entry = self._runs.get(session_id)
        if entry is None:
            return False
        entry[1].cancel()
        return True

    def forget(self, session_id: str) -> None:
        self.cancel(session_id)
        self._agents.pop(session_id, None)
        self._teams.pop(session_id, None)

    # ----------------------------------------------------------------- agents

    def _deep_agent(self, user: str, session_id: str) -> Any:
        agent = self._agents.get(session_id)
        if agent is None:
            from openjiuwen.harness import create_deep_agent
            from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail

            agent = create_deep_agent(
                model=build_model(), system_prompt=SYSTEM_PROMPT, rails=[SysOperationRail()],
                enable_task_loop=False, max_iterations=self._settings.max_iterations, workspace=VM_WORKSPACE,
                sys_operation=user_sys_operation(self._settings, user),
            )
            self._agents[session_id] = agent
        return agent

    def _team_spec(self, user: str, session: dict[str, Any]) -> Any:
        session_id = session["session_id"]
        spec = self._teams.get(session_id)
        if spec is not None:
            return spec
        from openjiuwen.agent_teams.schema.blueprint import StorageSpec, TeamAgentSpec, TransportSpec
        from openjiuwen.agent_teams.schema.team import TeamMemberSpec
        from openjiuwen.core.sys_operation import OperationMode
        from openjiuwen.harness.schema.deep_agent_spec import (
            DeepAgentSpec,
            SysOperationSpec,
            TeamModelConfig,
            WorkspaceSpec,
        )

        team_dir = self._settings.data_dir / "teams" / session_id
        team_dir.mkdir(parents=True, exist_ok=True)
        model = TeamModelConfig(
            model_client_config=ModelClientConfig(client_provider=os.getenv("MODEL_PROVIDER", "OpenAI"),
                                                  api_key=os.getenv("API_KEY", ""), api_base=os.getenv("API_BASE", ""),
                                                  timeout=int(os.getenv("MODEL_TIMEOUT", "180"))),
            model_request_config=ModelRequestConfig(model=os.getenv("MODEL_NAME", ""), temperature=0.2),
        )
        sysop = SysOperationSpec(id=f"pg-team-{user}", mode=OperationMode.SANDBOX,
                                 gateway_config=gateway_config(self._settings, user, team=True))
        # cwd / workspace are paths inside the session VM (the members' tools run there).
        member = DeepAgentSpec(model=model, sys_operation=sysop, workspace=WorkspaceSpec(root_path=VM_WORKSPACE),
                               cwd=VM_WORKSPACE, project_root=VM_WORKSPACE,
                               max_iterations=self._settings.max_iterations, enable_task_planning=False)
        size = max(1, min(int(session.get("team_size") or 3), 5))
        members = [TeamMemberSpec(member_name=f"member-{i}", display_name=f"Member {i}",
                                  desc="General engineer: writes code and files, runs commands in the shared VM, "
                                       "and completes the tasks assigned to it.")
                   for i in range(1, size + 1)]
        spec = TeamAgentSpec(
            agents={"leader": member.model_copy(deep=True), "teammate": member}, team_name=f"pg{session_id}",
            spawn_mode="inprocess", transport=TransportSpec(type="inprocess"),
            external_transport=TransportSpec(type="inprocess"), evolution_enabled=False,
            storage=StorageSpec(type="sqlite", params={"connection_string": str(team_dir / "team.sqlite")}),
            team_mode="predefined", predefined_members=members,
        )
        self._teams[session_id] = spec
        return spec

    # -------------------------------------------------------------------- run

    async def _run(self, user: str, session: dict[str, Any], text: str, run_id: str) -> None:
        session_id = session["session_id"]
        started = time.monotonic()
        self._store.add_entry(session_id, run_id, "user", "text", text)
        self._store.update_session(session_id, last_query_ts=time.time())
        self._hub.publish(user, "run.started", session_id, run_id=run_id, text=text)
        buffers: dict[Optional[str], list[str]] = {}
        status, error = "ok", None

        def flush(member: Optional[str]) -> None:
            parts = buffers.pop(member, None)
            if parts and "".join(parts).strip():
                self._store.add_entry(session_id, run_id, "assistant", "answer", "".join(parts), member)

        try:
            if session["kind"] == "team":
                # Members resolve this id; it must be the sandbox one (the team runtime removes it
                # when a run ends, so it is registered again before every run).
                team_sys_operation(self._settings, user)
                stream = Runner.run_agent_team_streaming(agent_team=self._team_spec(user, session),
                                                         inputs={"query": text + TEAM_NOTE}, session=session_id)
            else:
                # The agent instance is reused across runs (conversation memory), but its shell cwd lives in a
                # per-task ContextVar set only on first init; without this, later runs fall back to this
                # process's os.getcwd() and the tools try to cd into a backend path inside the VM.
                init_cwd(VM_WORKSPACE, project_root=VM_WORKSPACE, workspace=VM_WORKSPACE)
                stream = Runner.run_agent_streaming(self._deep_agent(user, session_id), {"query": text},
                                                    session=session_id)
            async for chunk in stream:
                for event in normalize_chunk(chunk):
                    member, kind = event["member"], event["kind"]
                    if kind == "delta":
                        buffers.setdefault(member, []).append(event["text"])
                    elif kind == "answer":
                        buffers[member] = [event["text"]]
                        flush(member)
                    elif kind in ("tool_call", "tool_result", "team_event"):
                        self._store.add_entry(session_id, run_id, "event", kind,
                                              event["text"] + ("" if not event["data"] else f" {event['data']}"),
                                              member)
                    self._hub.publish(user, "run.chunk", session_id, run_id=run_id, **event)
                    if kind == "team_event" and event["text"] in ("team.completed", "team.idle"):
                        break
                else:
                    continue
                break
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as exc:  # report to the user; the backend keeps serving
            logger.exception("run %s of session %s failed", run_id, session_id)
            status, error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            for member in list(buffers):
                flush(member)
            if error:
                self._store.add_entry(session_id, run_id, "event", "error", error)
            self._runs.pop(session_id, None)
            self._hub.publish(user, "run.finished", session_id, run_id=run_id, status=status, error=error,
                              duration_s=round(time.monotonic() - started, 2))
