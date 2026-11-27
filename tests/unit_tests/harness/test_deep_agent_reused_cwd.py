# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A reused DeepAgent binds its cwd again in every new asyncio Task.

The cwd lives in a per-Task ContextVar that DeepAgent set only on first
initialization. A host that keeps one agent instance per conversation runs later
turns in new Tasks; those turns used to fall back to the host process's
``os.getcwd()`` (meaningless inside a sandbox).
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from openjiuwen.core.runner.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperationCard
from openjiuwen.core.sys_operation.cwd import get_cwd, get_workspace, init_cwd, is_cwd_initialized
from openjiuwen.harness import Workspace
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.config import DeepAgentConfig


class _DummyModel:
    model_client_config = None
    model_config = None

    async def invoke(self, *args: Any, **kwargs: Any) -> None:  # never called: no run, only init
        raise AssertionError("the model must not be called")


async def _in_fresh_task(fn: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``fn`` in a new Task with an empty context, like a later turn of a host."""
    return await asyncio.get_running_loop().create_task(fn(), context=contextvars.Context())


@pytest.fixture
def agent_and_workspace(tmp_path: Path):
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    card = SysOperationCard(id=f"reused_cwd_sysop_{int(time.time() * 1000)}", mode=OperationMode.LOCAL,
                            work_config=LocalWorkConfig())
    Runner.resource_mgr.add_sys_operation(card)
    agent = DeepAgent(AgentCard(name="reused_cwd", description="test"))
    agent.configure(DeepAgentConfig(model=_DummyModel(), workspace=Workspace(root_path=str(workspace_dir)),
                                    sys_operation=Runner.resource_mgr.get_sys_operation(card.id),
                                    auto_create_workspace=False, enable_task_loop=False))
    yield agent, str(workspace_dir.resolve())
    Runner.resource_mgr.remove_sys_operation(card.id)


@pytest.mark.asyncio
async def test_reused_agent_binds_its_cwd_in_a_new_task(agent_and_workspace):
    agent, workspace = agent_and_workspace

    async def turn() -> tuple[str, str | None]:
        await agent.ensure_initialized()
        return get_cwd(), get_workspace()

    first = await _in_fresh_task(turn)
    second = await _in_fresh_task(turn)  # the instance is now initialized: the reuse path
    assert first == (workspace, workspace)
    assert second == (workspace, workspace), "a later turn must not fall back to os.getcwd()"
    assert second[0] != os.getcwd()


@pytest.mark.asyncio
async def test_reuse_keeps_a_cwd_the_host_already_bound(agent_and_workspace, tmp_path: Path):
    agent, _ = agent_and_workspace
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    await _in_fresh_task(agent.ensure_initialized)

    async def turn_in_bound_task() -> str:
        init_cwd(str(elsewhere))  # e.g. a parent agent or host chose the directory for this Task
        await agent.ensure_initialized()
        return get_cwd()

    assert await _in_fresh_task(turn_in_bound_task) == str(elsewhere.resolve())


@pytest.mark.asyncio
async def test_is_cwd_initialized_ignores_the_lazy_fallback(tmp_path: Path):
    async def probe() -> list[bool]:
        seen = [is_cwd_initialized()]
        get_cwd()  # creates an empty state lazily; still nothing bound
        seen.append(is_cwd_initialized())
        init_cwd(str(tmp_path))
        seen.append(is_cwd_initialized())
        return seen

    assert await _in_fresh_task(probe) == [False, False, True]
