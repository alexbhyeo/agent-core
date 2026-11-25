# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DeepAgent whose file and shell tools run inside a Firecracker microVM.

Each (user, session) gets its own VM from FcSessionManager. Start the manager
first (``deploy/firecracker/scripts/run_manager.sh``), then set:

    FC_MANAGER_URL   e.g. http://fc-manager:8700
    FCM_API_TOKEN    the manager's service token
    FC_OWNER_USER_ID the end user this agent works for (default: demo-user)
    API_KEY / API_BASE / MODEL_NAME / MODEL_PROVIDER  an OpenAI-compatible LLM

and run ``python examples/firecracker/deep_agent_firecracker.py``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Optional

from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.runner import Runner
from openjiuwen.core.sys_operation import OperationMode, SysOperation, SysOperationCard
from openjiuwen.extensions.sys_operation.sandbox.launchers import build_firecracker_gateway_config
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail

DEFAULT_QUERY = (
    "Use your tools, do not guess. "
    "1) Write the file /workspace/hello.txt with exactly this content: firecracker ok. "
    "2) Run the shell command: cat /workspace/hello.txt && uname -s && cat /proc/sys/kernel/hostname. "
    "3) Reply with the exact command output."
)


def build_model() -> Model:
    return Model(
        model_client_config=ModelClientConfig(
            client_provider=os.getenv("MODEL_PROVIDER", "OpenAI"),
            api_key=os.getenv("API_KEY", "your api key"),
            api_base=os.getenv("API_BASE", "your api base"),
            timeout=int(os.getenv("MODEL_TIMEOUT", "120")),
        ),
        model_config=ModelRequestConfig(model=os.getenv("MODEL_NAME", "your model"), temperature=0.1),
    )


def get_firecracker_sys_operation(owner_user_id: str, profile: Optional[dict[str, Any]] = None) -> SysOperation:
    """One SysOperation per user; each of that user's sessions gets its own microVM."""
    card_id = f"fc-{owner_user_id}"
    existing = Runner.resource_mgr.get_sys_operation(card_id)
    if existing is not None:
        return existing
    card = SysOperationCard(
        id=card_id,
        mode=OperationMode.SANDBOX,
        gateway_config=build_firecracker_gateway_config(
            manager_url=os.getenv("FC_MANAGER_URL", "http://fc-manager:8700"),
            owner_user_id=owner_user_id,
            api_token=os.getenv("FCM_API_TOKEN", ""),
            profile=profile or {"vcpus": 2, "mem_mib": 1024},
        ),
    )
    result = Runner.resource_mgr.add_sys_operation(card)
    if result.is_err():
        raise RuntimeError(f"add_sys_operation failed: {result.msg()}")
    return Runner.resource_mgr.get_sys_operation(card.id)


async def run_demo(owner_user_id: str, session_id: str, query: str = DEFAULT_QUERY,
                   model: Optional[Model] = None) -> dict[str, Any]:
    """Run one DeepAgent turn for ``owner_user_id`` inside ``session_id``'s microVM."""
    sys_operation = get_firecracker_sys_operation(owner_user_id)
    agent = create_deep_agent(
        model=model or build_model(),
        system_prompt="You are a precise assistant. Always use tools for files and commands.",
        rails=[SysOperationRail()],
        enable_task_loop=False,
        max_iterations=10,
        workspace="/workspace",
        sys_operation=sys_operation,
    )
    return await Runner.run_agent(agent, {"query": query}, session=session_id)


async def main() -> None:
    await Runner.start()
    try:
        owner = os.getenv("FC_OWNER_USER_ID", "demo-user")
        session_id = f"demo-{uuid.uuid4().hex[:8]}"
        result = await run_demo(owner, session_id)
        print(f"[{owner}/{session_id}] {result.get('output')}")
    finally:
        await Runner.stop()


if __name__ == "__main__":
    asyncio.run(main())
