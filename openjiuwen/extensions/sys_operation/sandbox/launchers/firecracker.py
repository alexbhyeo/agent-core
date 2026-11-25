# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Firecracker sandbox launcher: one microVM per agent session.

The launcher is a thin client of FcSessionManager (``deploy/firecracker``),
which runs on a Linux/KVM host and owns the VMs. agent-core keeps using the
existing aio provider for fs / shell / code: the guest agent in each VM speaks
the AIO sandbox API, reached through a per-session ``base_url``.

Ownership: every manager call carries ``owner_user_id``. The manager refuses a
session that belongs to someone else, and :func:`build_firecracker_gateway_config`
puts the owner into the isolation key so two users never share a Gateway
record or cached provider, even if they present the same session id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional

import aiohttp
from pydantic import Field

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError, build_error
from openjiuwen.core.common.logging import sys_operation_logger as logger
from openjiuwen.core.common.logging.utils import get_session_id
from openjiuwen.core.sys_operation.config import (
    ContainerScope,
    SandboxGatewayConfig,
    SandboxIsolationConfig,
    SandboxLauncherConfig,
)
from openjiuwen.core.sys_operation.sandbox.gateway.sandbox_store import SandboxStatus
from openjiuwen.core.sys_operation.sandbox.launchers.base import LaunchedSandbox, SandboxLauncher
from openjiuwen.core.sys_operation.sandbox.sandbox_registry import SandboxRegistry
# The guest agent speaks the AIO API, so the aio providers must be registered.
from openjiuwen.extensions.sys_operation.sandbox.providers import aio as _aio  # noqa: F401

LAUNCHER_TYPE = "firecracker"
OWNER_HEADER = "X-Owner-User-Id"
_DEFAULT_SESSION_IDS = {None, "", "default_trace_id", "default_session"}
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_.:@-]+")


class FirecrackerLauncherConfig(SandboxLauncherConfig):
    """Launcher settings for Firecracker-backed sessions."""

    launcher_type: Literal["firecracker"] = "firecracker"
    sandbox_type: str = Field(default="aio", description="Provider type; the guest agent is AIO-compatible")
    manager_url: str = Field(..., description="FcSessionManager base URL, e.g. http://fc-manager:8700")
    owner_user_id: str = Field(..., min_length=1, description="End user that owns the sessions this agent opens")
    api_token: str = Field(default="", description="Bearer token for FcSessionManager (FCM_API_TOKEN)")
    profile: Dict[str, Any] = Field(default_factory=dict, description="VM profile, e.g. {'vcpus': 2, 'mem_mib': 2048}")
    pause_mode: Literal["memory"] = Field(
        default="memory",
        description="What pause() does. Phase 1 supports 'memory' (vCPUs stopped, RAM kept); snapshots come later.",
    )
    request_timeout_seconds: float = Field(default=180.0, description="Timeout for manager calls (boot included)")


@dataclass(frozen=True)
class _Handle:
    manager_url: str
    owner_user_id: str
    api_token: str
    timeout: float


def _launcher_error(operation: str, reason: str, http_status: Optional[int] = None,
                    cause: Optional[BaseException] = None) -> BaseError:
    return build_error(
        StatusCode.SYS_OPERATION_SANDBOX_LAUNCHER_ERROR,
        operation=operation,
        error_msg=reason,
        details={"launcher": LAUNCHER_TYPE, "http_status": http_status},
        cause=cause,
    )


def manager_http_status(exc: BaseException) -> Optional[int]:
    """HTTP status FcSessionManager returned for a failed launcher call, if any."""
    details = getattr(exc, "details", None)
    return details.get("http_status") if isinstance(details, dict) else None


def is_ownership_error(exc: BaseException) -> bool:
    """True when the manager refused a session that belongs to another user."""
    return manager_http_status(exc) == 403


def build_firecracker_gateway_config(
    *,
    manager_url: str,
    owner_user_id: str,
    api_token: str = "",
    profile: Optional[Dict[str, Any]] = None,
    on_stop: Literal["delete", "pause", "keep"] = "keep",
    timeout_seconds: int = 180,
) -> SandboxGatewayConfig:
    """Gateway config for one user: session-scoped, with the owner in the isolation key."""
    return SandboxGatewayConfig(
        isolation=SandboxIsolationConfig(
            container_scope=ContainerScope.SESSION,
            prefix=f"user-{_safe_id(owner_user_id)}",
        ),
        launcher_config=FirecrackerLauncherConfig(
            manager_url=manager_url,
            owner_user_id=owner_user_id,
            api_token=api_token,
            profile=dict(profile or {}),
            on_stop=on_stop,
            request_timeout_seconds=float(timeout_seconds),
        ),
        timeout_seconds=timeout_seconds,
    )


def _safe_id(value: str) -> str:
    return _UNSAFE_ID_CHARS.sub("-", value).strip("-")[:128] or "unknown"


def _session_id_for(isolation_key: Optional[str]) -> str:
    """The agent session id the VM is named after (manager key)."""
    session_id = get_session_id()
    if session_id not in _DEFAULT_SESSION_IDS:
        return _safe_id(str(session_id))
    if isolation_key:
        return _safe_id(isolation_key)
    raise _launcher_error("launch", "no session id: run inside an agent session or pass isolation_key")


async def _call(handle: _Handle, operation: str, method: str, path: str,
                json: Optional[dict] = None) -> dict[str, Any]:
    headers = {OWNER_HEADER: handle.owner_user_id}
    if handle.api_token:
        headers["Authorization"] = f"Bearer {handle.api_token}"
    timeout = aiohttp.ClientTimeout(total=handle.timeout)
    url = handle.manager_url.rstrip("/") + path
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, json=json, headers=headers) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    payload = {"error": await resp.text()}
                if resp.status >= 400:
                    message = payload.get("error", payload) if isinstance(payload, dict) else payload
                    raise _launcher_error(operation, f"FcSessionManager returned {resp.status}: {message}",
                                          http_status=resp.status)
                return payload or {}
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise _launcher_error(operation, f"cannot reach FcSessionManager at {handle.manager_url}: {exc}",
                              cause=exc) from exc


@SandboxRegistry.launcher(LAUNCHER_TYPE)
class FirecrackerLauncher(SandboxLauncher):
    """SandboxLauncher backed by FcSessionManager.

    ``SandboxGateway`` creates launchers without arguments and later calls
    ``pause`` / ``resume`` / ``delete`` / ``check_status`` with only the
    sandbox id, so the manager address and owner recorded at ``launch`` are
    kept in a process-level table keyed by sandbox id.
    """

    _handles: Dict[str, _Handle] = {}

    async def launch(
        self,
        config: SandboxLauncherConfig,
        timeout_seconds: int,
        isolation_key: Optional[str] = None,
    ) -> LaunchedSandbox:
        if not isinstance(config, FirecrackerLauncherConfig):
            raise _launcher_error("launch", "FirecrackerLauncher requires FirecrackerLauncherConfig")
        handle = _Handle(
            manager_url=config.manager_url,
            owner_user_id=config.owner_user_id,
            api_token=config.api_token,
            timeout=max(float(config.request_timeout_seconds), float(timeout_seconds or 0)),
        )
        session_id = _session_id_for(isolation_key)
        info = await _call(handle, "launch", "POST", "/v1/sessions", {
            "session_id": session_id,
            "owner_user_id": config.owner_user_id,
            "profile": config.profile,
        })
        self._handles[session_id] = handle
        logger.info("Firecracker session %s ready for owner %s (state=%s)",
                    session_id, config.owner_user_id, info.get("state"))
        return LaunchedSandbox(base_url=info["base_url"], sandbox_id=session_id)

    def _handle(self, operation: str, sandbox_id: str) -> _Handle:
        handle = self._handles.get(sandbox_id)
        if handle is None:
            raise _launcher_error(operation, f"no manager handle for sandbox {sandbox_id}; launch() it first",
                                  http_status=404)
        return handle

    async def pause(self, sandbox_id: str) -> None:
        await _call(self._handle("pause", sandbox_id), "pause", "POST", f"/v1/sessions/{sandbox_id}:pause")

    async def resume(self, sandbox_id: str) -> None:
        await _call(self._handle("resume", sandbox_id), "resume", "POST", f"/v1/sessions/{sandbox_id}:resume")

    async def delete(self, sandbox_id: str, **kwargs) -> None:
        handle = self._handles.get(sandbox_id)
        if handle is None:
            return
        try:
            await _call(handle, "delete", "DELETE", f"/v1/sessions/{sandbox_id}")
        except BaseError as exc:
            if manager_http_status(exc) != 404:
                raise
        self._handles.pop(sandbox_id, None)

    async def check_status(self, sandbox_id: str) -> SandboxStatus:
        try:
            info = await _call(self._handle("check_status", sandbox_id), "check_status", "GET",
                               f"/v1/sessions/{sandbox_id}")
        except BaseError as exc:
            if manager_http_status(exc) == 404:
                return SandboxStatus.KILLED
            raise
        state = info.get("state")
        if state == "RUNNING":
            return SandboxStatus.RUNNING
        if state in ("PAUSED", "PERSISTED"):
            return SandboxStatus.PAUSED
        return SandboxStatus.KILLED


async def get_session_info(config: FirecrackerLauncherConfig, session_id: str,
                           ensure: bool = True) -> dict[str, Any]:
    """Session details from the manager; with ``ensure`` the VM is created if missing."""
    handle = _Handle(config.manager_url, config.owner_user_id, config.api_token,
                     float(config.request_timeout_seconds))
    safe = _safe_id(session_id)
    if ensure:
        info = await _call(handle, "get_session_info", "POST", "/v1/sessions", {
            "session_id": safe, "owner_user_id": config.owner_user_id, "profile": config.profile,
        })
        FirecrackerLauncher._handles[safe] = handle
        return info
    return await _call(handle, "get_session_info", "GET", f"/v1/sessions/{safe}")


async def build_browser_instance(config: FirecrackerLauncherConfig, session_id: str) -> Dict[str, str]:
    """``browser_instance`` for ``create_browser_agent``: this session's in-VM Chromium.

    The key includes owner and session, so each session gets its own Playwright
    MCP server and nothing (cookies, task queue) is shared between sessions.
    """
    info = await get_session_info(config, session_id, ensure=True)
    return {
        "key": f"{_safe_id(config.owner_user_id)}--{_safe_id(session_id)}",
        "driver_mode": "remote",
        "cdp_url": info["cdp_url"],
    }


__all__ = [
    "FirecrackerLauncher",
    "FirecrackerLauncherConfig",
    "LAUNCHER_TYPE",
    "build_browser_instance",
    "build_firecracker_gateway_config",
    "get_session_info",
    "is_ownership_error",
    "manager_http_status",
]
