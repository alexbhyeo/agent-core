# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A SESSION-scoped sandbox operation must route each session to its own key.

Regression: the gateway client used to be cached with the isolation key of the
first call, so every later session of the same SysOperation reused the first
session's sandbox.
"""

import asyncio

import pytest

from openjiuwen.core.common.logging.utils import set_session_id
from openjiuwen.core.sys_operation.config import PreDeployLauncherConfig, SandboxGatewayConfig
from openjiuwen.core.sys_operation.sandbox.run_config import SandboxRunConfig
from openjiuwen.core.sys_operation.sandbox.sandbox_mixin import BaseSandboxMixin


class _Op(BaseSandboxMixin):
    def __init__(self) -> None:
        config = SandboxGatewayConfig(launcher_config=PreDeployLauncherConfig(base_url="http://sandbox"))
        self._init_sandbox_context(SandboxRunConfig(config=config, isolation_key_template="u1_{session_id}"), "fs")


async def _key_in_session(op: _Op, session_id: str):
    async def _task():
        set_session_id(session_id)
        client = await op._get_gateway_client()
        return client, client._isolation_key

    return await asyncio.create_task(_task())


@pytest.mark.asyncio
async def test_each_session_gets_its_own_gateway_client():
    op = _Op()
    client_a, key_a = await _key_in_session(op, "session-a")
    client_b, key_b = await _key_in_session(op, "session-b")
    client_a2, key_a2 = await _key_in_session(op, "session-a")

    assert (key_a, key_b, key_a2) == ("u1_session-a", "u1_session-b", "u1_session-a")
    assert client_a is not client_b
    assert client_a is client_a2, "clients are reused per isolation key"
