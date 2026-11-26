# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Runner.release(session_id) also deletes the sandboxes created under that session."""

import pytest

from openjiuwen.core.runner import Runner
from openjiuwen.core.sys_operation.sandbox.gateway.gateway import SandboxGateway


class _Gateway:
    def __init__(self) -> None:
        self.calls: list = []

    async def release_session(self, session_id, on_stop="delete"):
        self.calls.append((session_id, on_stop))
        return 1


@pytest.mark.asyncio
async def test_release_deletes_session_sandboxes(monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(SandboxGateway, "_instance", gateway)
    await Runner.start()
    try:
        await Runner.release("session-x")
    finally:
        await Runner.stop()
    assert gateway.calls == [("session-x", "delete")]


@pytest.mark.asyncio
async def test_release_without_gateway_creates_none(monkeypatch):
    monkeypatch.setattr(SandboxGateway, "_instance", None)
    await Runner.start()
    try:
        await Runner.release("session-y")
    finally:
        await Runner.stop()
    assert SandboxGateway._instance is None
