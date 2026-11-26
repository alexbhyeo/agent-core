# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared helpers for the Firecracker end-to-end tests (see conftest.py for fixtures)."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.logging.utils import set_session_id
from openjiuwen.core.runner import Runner
from openjiuwen.core.sys_operation import OperationMode, SysOperation, SysOperationCard
from openjiuwen.extensions.sys_operation.sandbox.launchers import (
    FirecrackerLauncherConfig,
    build_firecracker_gateway_config,
)

MANAGER_URL = os.getenv("FC_MANAGER_URL", "")
MANAGER_TOKEN = os.getenv("FCM_API_TOKEN", "")
DOCKER_SOCKET = os.getenv("FC_DOCKER_SOCKET", "")
MANAGER_CONTAINER = os.getenv("FC_MANAGER_CONTAINER", "fc-manager")
LLM_READY = bool(os.getenv("API_KEY") and os.getenv("API_BASE") and os.getenv("MODEL_NAME"))
VM_PROFILE = {"vcpus": 2, "mem_mib": 1024}
BROWSER_PROFILE = {"vcpus": 2, "mem_mib": 1536}
SUCCESS = StatusCode.SUCCESS.code


async def manager(method: str, path: str, owner: str, json: Optional[dict] = None) -> tuple[int, Any]:
    headers = {"Authorization": f"Bearer {MANAGER_TOKEN}", "X-Owner-User-Id": owner}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
        async with session.request(method, MANAGER_URL.rstrip("/") + path, json=json, headers=headers) as resp:
            return resp.status, await resp.json(content_type=None)


async def wait_for_state(owner: str, session_id: str, state: str, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    info: dict = {}
    while time.monotonic() < deadline:
        _, info = await manager("GET", f"/v1/sessions/{session_id}", owner)
        if info.get("state") == state:
            return info
        await asyncio.sleep(1)
    raise AssertionError(f"session {session_id} did not reach {state} within {timeout}s: {info}")


async def restart_manager_container(timeout: float = 300) -> None:
    """Restart the manager container through the Docker Engine API, then wait until healthy."""
    connector = aiohttp.UnixConnector(path=DOCKER_SOCKET)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as docker:
        async with docker.post(f"http://docker/containers/{MANAGER_CONTAINER}/restart?t=300") as resp:
            assert resp.status == 204, await resp.text()
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(f"{MANAGER_URL}/healthz") as resp:
                    if resp.status == 200:
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(1)
    raise AssertionError("manager did not come back after restart")


async def docker_exec(cmd: list[str], container: Optional[str] = None) -> tuple[int, str]:
    """Run a command in the manager container through the Docker Engine API; (exit code, output)."""
    connector = aiohttp.UnixConnector(path=DOCKER_SOCKET)
    target = container or MANAGER_CONTAINER
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=120)) as docker:
        async with docker.post(f"http://docker/containers/{target}/exec",
                               json={"Cmd": cmd, "AttachStdout": True, "AttachStderr": True}) as resp:
            exec_id = (await resp.json())["Id"]
        async with docker.post(f"http://docker/exec/{exec_id}/start", json={"Detach": False, "Tty": False}) as resp:
            raw = await resp.read()
        async with docker.get(f"http://docker/exec/{exec_id}/json") as resp:
            exit_code = (await resp.json())["ExitCode"]
    return exit_code, _demux(raw)


def _demux(raw: bytes) -> str:
    """Docker multiplexed stream: an 8-byte header (stream, 0, 0, 0, size) per frame."""
    out, i = bytearray(), 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out += raw[i + 8:i + 8 + size]
        i += 8 + size
    return out.decode(errors="replace")


async def docker_run(image: str, entrypoint: list[str], cmd: list[str], cap_add: Optional[list[str]] = None,
                     network: str = "fc-net", timeout: float = 180) -> tuple[int, str]:
    """Run a throw-away container through the Docker Engine API; (exit code, output)."""
    connector = aiohttp.UnixConnector(path=DOCKER_SOCKET)
    body = {"Image": image, "Entrypoint": entrypoint, "Cmd": cmd,
            "HostConfig": {"CapAdd": cap_add or [], "NetworkMode": network}}
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as docker:
        async with docker.post("http://docker/containers/create", json=body) as resp:
            assert resp.status == 201, await resp.text()
            cid = (await resp.json())["Id"]
        try:
            async with docker.post(f"http://docker/containers/{cid}/start") as resp:
                assert resp.status == 204, await resp.text()
            async with docker.post(f"http://docker/containers/{cid}/wait") as resp:
                code = (await resp.json())["StatusCode"]
            async with docker.get(f"http://docker/containers/{cid}/logs?stdout=1&stderr=1") as resp:
                output = _demux(await resp.read())
        finally:
            async with docker.delete(f"http://docker/containers/{cid}?force=1"):
                pass
    return code, output


async def in_session(session_id: str, fn: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``fn`` in its own task with the agent session id bound (like Runner does)."""

    async def _task() -> Any:
        set_session_id(session_id)
        return await fn()

    return await asyncio.create_task(_task())


class FcEnv:
    """One SysOperation per user (as agent-core intends); one VM per session; full cleanup."""

    def __init__(self) -> None:
        self.sessions: list[tuple[str, str]] = []
        self.cards: list[str] = []
        self.ops: dict[tuple[str, str], SysOperation] = {}

    def config(self, owner: str, profile: Optional[dict] = None) -> FirecrackerLauncherConfig:
        cfg = build_firecracker_gateway_config(manager_url=MANAGER_URL, owner_user_id=owner,
                                               api_token=MANAGER_TOKEN, profile=profile or VM_PROFILE)
        return cfg.launcher_config

    def user_op(self, owner: str, profile: Optional[dict] = None) -> SysOperation:
        """The user's SysOperation; a distinct profile needs its own SysOperation (and owner prefix)."""
        key = (owner, repr(sorted((profile or VM_PROFILE).items())))
        if key not in self.ops:
            card = SysOperationCard(
                id=f"fc-e2e-{owner}-{uuid.uuid4().hex[:6]}",
                mode=OperationMode.SANDBOX,
                gateway_config=build_firecracker_gateway_config(manager_url=MANAGER_URL, owner_user_id=owner,
                                                                api_token=MANAGER_TOKEN,
                                                                profile=profile or VM_PROFILE),
            )
            if profile is not None and profile != VM_PROFILE:
                card.gateway_config.isolation.prefix += f"-p{len(self.ops)}"
            result = Runner.resource_mgr.add_sys_operation(card)
            assert result.is_ok(), result.msg()
            self.cards.append(card.id)
            self.ops[key] = Runner.resource_mgr.get_sys_operation(card.id)
        return self.ops[key]

    def open(self, owner: str, session_id: Optional[str] = None,
             profile: Optional[dict] = None) -> tuple[str, SysOperation]:
        session_id = session_id or f"e2e-{uuid.uuid4().hex[:8]}"
        self.sessions.append((owner, session_id))
        return session_id, self.user_op(owner, profile)

    async def close(self) -> None:
        for owner, session_id in self.sessions:
            await manager("DELETE", f"/v1/sessions/{session_id}", owner)
        for card_id in self.cards:
            if Runner.resource_mgr.get_sys_operation(card_id) is not None:
                Runner.resource_mgr.remove_sys_operation(card_id)


async def sh(op: SysOperation, session_id: str, command: str, timeout: int = 240):
    result = await in_session(session_id, lambda: op.shell().execute_cmd(command, timeout=timeout))
    assert result.code == SUCCESS, result.message
    return result.data
