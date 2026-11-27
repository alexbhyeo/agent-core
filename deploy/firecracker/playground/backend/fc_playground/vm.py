# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Access to FcSessionManager and the session VMs on behalf of a Playground user.

Everything returned to the browser goes through :func:`scrub`, which removes
guest tokens from URLs and drops token fields (spec P-1 of the design).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from fc_playground.config import Settings
from fc_playground.events import EventHub
from openjiuwen.core.common.logging.utils import set_session_id
from openjiuwen.core.runner import Runner
from openjiuwen.core.sys_operation import OperationMode, SysOperation, SysOperationCard
from openjiuwen.extensions.sys_operation.sandbox.launchers import build_firecracker_gateway_config

logger = logging.getLogger(__name__)

_GUEST_TOKEN = re.compile(r"/t/[A-Za-z0-9_\-]+")
_FILE_LIMIT = 256 * 1024


class ManagerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def scrub(value: Any) -> Any:
    """Remove guest tokens and token fields before data leaves the backend."""
    if isinstance(value, dict):
        return {k: (_GUEST_TOKEN.sub("/t/***", v) if isinstance(v, str) and k.endswith("_url") else scrub(v))
                for k, v in value.items() if "token" not in k.lower()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


class ManagerClient:
    """REST calls to FcSessionManager with the service credential and the user as owner."""

    def __init__(self, settings: Settings):
        self.url = settings.manager_url
        self._token = settings.manager_token
        self._http: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=330))

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()

    async def call(self, method: str, path: str, owner: Optional[str] = None, body: Any = None,
                   timeout: float = 300) -> Any:
        assert self._http is not None
        headers = {"Authorization": f"Bearer {self._token}"}
        if owner is not None:
            headers["X-Owner-User-Id"] = owner
        try:
            async with self._http.request(method, self.url + path, json=body, headers=headers,
                                          timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                text = await resp.text()
                if resp.content_type == "application/json" or text.startswith(("{", "[")):
                    payload = json.loads(text) if text else {}
                else:
                    payload = text
                if resp.status >= 400:
                    message = payload.get("error", text) if isinstance(payload, dict) else text
                    raise ManagerError(resp.status, str(message))
                return payload
        except aiohttp.ClientError as exc:
            raise ManagerError(503, f"FcSessionManager unreachable: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise ManagerError(504, "FcSessionManager did not answer in time") from exc


# ---------------------------------------------------------------- sys operations


def user_sys_operation(settings: Settings, user: str) -> SysOperation:
    """The user's SysOperation (one per user; each session id gets its own VM)."""
    card_id = f"pg-{user}"
    existing = Runner.resource_mgr.get_sys_operation(card_id)
    if existing is not None:
        return existing
    card = SysOperationCard(id=card_id, mode=OperationMode.SANDBOX,
                            gateway_config=gateway_config(settings, user))
    result = Runner.resource_mgr.add_sys_operation(card)
    op = Runner.resource_mgr.get_sys_operation(card_id)
    if op is None or getattr(op, "mode", None) != OperationMode.SANDBOX:
        raise ManagerError(500, f"cannot register the sandbox sys operation of {user}: {result.msg()}")
    return op


def gateway_config(settings: Settings, user: str, team: bool = False):
    config = build_firecracker_gateway_config(manager_url=settings.manager_url, owner_user_id=user,
                                              api_token=settings.manager_token, profile=settings.default_profile)
    if team:
        # A distinct isolation template, so the team's SysOperation can be registered next to the
        # user's DeepAgent one; the VM is still chosen by the (team) session id.
        config.isolation.prefix += "-team"
    return config


def team_sys_operation(settings: Settings, user: str) -> SysOperation:
    """Register (or re-register) the SANDBOX SysOperation AgentTeam members resolve by id.

    The harness silently falls back to a LOCAL sys operation when a member's
    SysOperationSpec cannot be registered, which would run agent tools inside this
    backend. Registering it here first, and refusing to run otherwise, rules that out.
    """
    card_id = f"pg-team-{user}"
    op = Runner.resource_mgr.get_sys_operation(card_id)
    if op is None:
        card = SysOperationCard(id=card_id, mode=OperationMode.SANDBOX,
                                gateway_config=gateway_config(settings, user, team=True))
        result = Runner.resource_mgr.add_sys_operation(card)
        op = Runner.resource_mgr.get_sys_operation(card_id)
        if op is None:
            raise ManagerError(500, f"cannot register the team sys operation of {user}: {result.msg()}")
    if getattr(op, "mode", None) != OperationMode.SANDBOX:
        raise ManagerError(500, f"sys operation {card_id} is not a sandbox; refusing to run the team")
    return op


async def in_session(session_id: str, fn: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``fn`` in its own task with the agent session id bound (routes to that session's VM)."""

    async def _task() -> Any:
        set_session_id(session_id)
        return await fn()

    return await asyncio.create_task(_task())


async def run_shell(settings: Settings, user: str, session_id: str, command: str, timeout: int = 120) -> dict:
    op = user_sys_operation(settings, user)
    result = await in_session(session_id, lambda: op.shell().execute_cmd(command, timeout=timeout))
    if result.data is None:
        raise ManagerError(502, result.message or "shell call failed")
    return {"exit_code": result.data.exit_code, "stdout": result.data.stdout, "stderr": result.data.stderr}


_LIST_SCRIPT = (
    "import json,os,sys\n"
    "p=sys.argv[1]\n"
    "if os.path.isdir(p):\n"
    " out=[]\n"
    " for n in sorted(os.listdir(p)):\n"
    "  f=os.path.join(p,n)\n"
    "  try: s=os.lstat(f)\n"
    "  except OSError: continue\n"
    "  out.append({'name':n,'dir':os.path.isdir(f),'size':s.st_size,'mtime':int(s.st_mtime)})\n"
    " print(json.dumps({'type':'dir','path':p,'entries':out}))\n"
    "else:\n"
    " b=open(p,'rb').read(%d)\n"
    " print(json.dumps({'type':'file','path':p,'size':os.path.getsize(p),"
    "'text':b.decode('utf-8','replace'),'truncated':os.path.getsize(p)>len(b)}))\n" % _FILE_LIMIT
)


async def browse(settings: Settings, user: str, session_id: str, path: str) -> dict:
    out = await run_shell(settings, user, session_id,
                          f"python3 -c {shlex.quote(_LIST_SCRIPT)} {shlex.quote(path or '/workspace')}")
    if out["exit_code"] != 0:
        raise ManagerError(404, (out["stderr"] or out["stdout"]).strip()[-300:] or f"cannot read {path}")
    return json.loads(out["stdout"].strip().splitlines()[-1])


# ----------------------------------------------------------------- VM messages


class MessageFollower:
    """Long-polls the VM messages of a user's sessions while that user has an open event stream."""

    def __init__(self, manager: ManagerClient, hub: EventHub, sessions_of: Callable[[str], list[str]]):
        self._manager = manager
        self._hub = hub
        self._sessions_of = sessions_of
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._last: dict[str, int] = {}

    def follow_user(self, user: str) -> None:
        for session_id in self._sessions_of(user):
            self.follow(user, session_id)

    def unfollow_user(self, user: str) -> None:
        for key in [k for k in self._tasks if k[0] == user]:
            self._tasks.pop(key).cancel()

    def follow(self, user: str, session_id: str) -> None:
        key = (user, session_id)
        if key not in self._tasks and self._hub.has_subscribers(user):
            self._tasks[key] = asyncio.create_task(self._loop(user, session_id))

    def forget(self, user: str, session_id: str) -> None:
        task = self._tasks.pop((user, session_id), None)
        if task is not None:
            task.cancel()
        self._last.pop(session_id, None)

    async def _loop(self, user: str, session_id: str) -> None:
        while True:
            try:
                after = self._last.get(session_id, 0)
                page = await self._manager.call(
                    "GET", f"/v1/sessions/{session_id}/messages?after={after}&wait=25", user, timeout=60)
                for msg in page.get("messages", []):
                    self._last[session_id] = max(self._last.get(session_id, 0), int(msg["id"]))
                    self._hub.publish(user, "vm.message", session_id, message=scrub(msg))
            except asyncio.CancelledError:
                raise
            except ManagerError as exc:
                if exc.status in (403, 404):
                    self._tasks.pop((user, session_id), None)
                    return
                await asyncio.sleep(5)
            except Exception:  # keep following; one bad page must not stop it
                logger.exception("message follower for %s/%s failed", user, session_id)
                await asyncio.sleep(5)


# ------------------------------------------------------------- manager restart


async def restart_manager(settings: Settings) -> None:
    """Restart the manager container through the Docker Engine API (graceful: it persists VMs)."""
    if settings.docker_socket is None:
        raise ManagerError(404, "manager restart is disabled (set PLAYGROUND_DOCKER=1)")
    connector = aiohttp.UnixConnector(path=settings.docker_socket)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=400)) as docker:
        async with docker.post(f"http://docker/containers/{settings.manager_container}/restart?t=300") as resp:
            if resp.status != 204:
                raise ManagerError(502, f"docker restart failed: {resp.status} {await resp.text()}")
