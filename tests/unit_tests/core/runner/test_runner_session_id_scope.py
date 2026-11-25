# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Runner.run_agent / run_agent_streaming expose the run's session id via get_session_id().

Session-scoped consumers (log trace ids, SESSION-scoped sandboxes) read the
session id from context. Before this was bound, every single-agent run saw
``default_trace_id`` and all sessions shared one sandbox.
"""

from typing import Any, AsyncIterator, List, Optional

import pytest
import pytest_asyncio

from openjiuwen.core.common.logging.utils import get_session_id, set_session_id
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.stream import StreamMode
from openjiuwen.core.single_agent import AgentCard, BaseAgent, Session


class _SessionProbeAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentCard(id="session_probe_agent"))
        self.seen: list[Optional[str]] = []

    def configure(self, config) -> "_SessionProbeAgent":
        return self

    async def invoke(self, inputs: Any, session: Session | None = None) -> Any:
        self.seen.append(get_session_id())
        return {"session": get_session_id()}

    async def stream(self, inputs: Any, session: Session | None = None,
                     stream_modes: Optional[List[StreamMode]] = None) -> AsyncIterator[Any]:
        self.seen.append(get_session_id())
        yield {"session": get_session_id()}


@pytest_asyncio.fixture(name="runner")
async def runner_fixture():
    await Runner.start()
    try:
        yield Runner
    finally:
        await Runner.stop()


@pytest.mark.asyncio
async def test_run_agent_binds_session_id_and_restores_it(runner):
    set_session_id("outer-context")
    agent = _SessionProbeAgent()

    result = await runner.run_agent(agent, {"query": "hi"}, session="session-A")
    await runner.run_agent(agent, {"query": "hi"}, session="session-B")

    assert result == {"session": "session-A"}
    assert agent.seen == ["session-A", "session-B"]
    assert get_session_id() == "outer-context", "the caller's session id must be restored after the run"


@pytest.mark.asyncio
async def test_run_agent_streaming_binds_session_id(runner):
    set_session_id("outer-context")
    agent = _SessionProbeAgent()

    chunks = [chunk async for chunk in runner.run_agent_streaming(agent, {"query": "hi"}, session="session-S")]

    assert chunks == [{"session": "session-S"}]
    assert agent.seen == ["session-S"]
    assert get_session_id() == "outer-context"
