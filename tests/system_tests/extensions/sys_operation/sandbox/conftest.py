# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Fixtures for the Firecracker end-to-end tests."""

import pytest_asyncio

from openjiuwen.core.runner import Runner

# Register the browser agent's context processors at collection time. tests/conftest.py restores
# ContextEngine._PROCESSOR_MAP after every test, so a processor first imported inside an earlier
# test is dropped and never re-registered, breaking later browser-agent tests in the same run.
from openjiuwen.harness.tools.browser_move.playwright_runtime import (  # noqa: F401  pylint: disable=unused-import
    browser_state_context_processor,
    browser_working_context_processor,
)
from tests.system_tests.extensions.sys_operation.sandbox.fc_helpers import FcEnv


@pytest_asyncio.fixture(name="fc")
async def fc_fixture():
    await Runner.start()
    env = FcEnv()
    try:
        yield env
    finally:
        await env.close()
        await Runner.stop()
