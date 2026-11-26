# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""FirecrackerLauncher against a fake FcSessionManager (loopback aiohttp server).

No KVM or Firecracker is needed: the fake manager records every request and
answers like the real REST API, including ownership refusals.
"""

from __future__ import annotations

import importlib.util
import uuid
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.common.logging.utils import set_session_id
from openjiuwen.core.sys_operation.config import ContainerScope, SandboxGatewayConfig
from openjiuwen.core.sys_operation.sandbox.gateway.gateway import SandboxGateway
from openjiuwen.core.sys_operation.sandbox.gateway.sandbox_store import SandboxStatus
from openjiuwen.core.sys_operation.sandbox.sandbox_registry import SandboxRegistry
from openjiuwen.core.sys_operation.sys_operation import generate_isolation_key_template
from openjiuwen.core.sys_operation.config import GatewayInvokeRequest
from openjiuwen.extensions.sys_operation.sandbox.launchers import (
    FirecrackerLauncher,
    FirecrackerLauncherConfig,
    build_browser_instance,
    build_firecracker_gateway_config,
    fork_session,
    get_session_info,
    get_session_messages,
    is_ownership_error,
    manager_http_status,
    register_session_mcp_server,
    reply_to_session_message,
    scoped_resource_id,
    set_session_secrets,
)

TOKEN = "test-manager-token"
GUEST_TOKEN = "guest-tok"


class FakeManager:
    """Minimal FcSessionManager: sessions keyed by id, owner-checked."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.files: dict[str, str] = {}
        self.base = ""

    def _info(self, sid: str) -> dict[str, Any]:
        s = self.sessions[sid]
        return {
            "session_id": sid,
            "owner_user_id": s["owner"],
            "state": s["state"],
            "base_url": f"{self.base}/t/{GUEST_TOKEN}",
            "cdp_url": f"{self.base}/cdp/t/{GUEST_TOKEN}",
        }

    def _record(self, request: web.Request, body: Any = None) -> None:
        self.requests.append({
            "method": request.method,
            "path": request.path,
            "owner": request.headers.get("X-Owner-User-Id"),
            "auth": request.headers.get("Authorization"),
            "body": body,
        })

    def _owned(self, request: web.Request) -> str:
        sid = request.match_info["sid"]
        if sid not in self.sessions:
            raise web.HTTPNotFound(text='{"error": "not found"}', content_type="application/json")
        if self.sessions[sid]["owner"] != request.headers.get("X-Owner-User-Id"):
            raise web.HTTPForbidden(text='{"error": "belongs to another user"}', content_type="application/json")
        return sid

    async def create(self, request: web.Request) -> web.Response:
        body = await request.json()
        self._record(request, body)
        sid, owner = body["session_id"], request.headers.get("X-Owner-User-Id")
        if sid in self.sessions and self.sessions[sid]["owner"] != owner:
            return web.json_response({"error": "belongs to another user"}, status=403)
        created = sid not in self.sessions
        self.sessions.setdefault(sid, {"owner": owner, "state": "RUNNING", "profile": body.get("profile")})
        return web.json_response(self._info(sid), status=201 if created else 200)

    async def get(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.json_response(self._info(self._owned(request)))

    async def pause(self, request: web.Request) -> web.Response:
        self._record(request)
        sid = self._owned(request)
        self.sessions[sid]["state"] = "PAUSED"
        return web.json_response(self._info(sid))

    async def resume(self, request: web.Request) -> web.Response:
        self._record(request)
        sid = self._owned(request)
        self.sessions[sid]["state"] = "RUNNING"
        return web.json_response(self._info(sid))

    async def mcp_servers(self, request: web.Request) -> web.Response:
        body = await request.json()
        self._record(request, body)
        sid = self._owned(request)
        return web.json_response({"name": body["name"], "url": f"{self.base}/mcp/{sid}/servers/{body['name']}/mcp",
                                  "servers": [body["name"]]})

    async def secrets(self, request: web.Request) -> web.Response:
        body = await request.json()
        self._record(request, body)
        sid = self._owned(request)
        keys = self.sessions[sid].setdefault("secret_keys", set())
        for key, value in body.items():
            (keys.discard if value is None else keys.add)(key)
        return web.json_response({"session_id": sid, "secret_keys": sorted(keys)})

    async def fork(self, request: web.Request) -> web.Response:
        body = await request.json()
        self._record(request, body)
        sid = self._owned(request)
        for new_id in body["new_session_ids"]:
            self.sessions[new_id] = {"owner": self.sessions[sid]["owner"], "state": "RUNNING", "forked_from": sid}
        return web.json_response({"sessions": [self._info(n) for n in body["new_session_ids"]]}, status=201)

    async def messages(self, request: web.Request) -> web.Response:
        self._record(request)
        sid = self._owned(request)
        after = int(request.query.get("after", "0"))
        msgs = [m for m in self.sessions[sid].setdefault("messages", []) if m["id"] > after]
        return web.json_response({"session_id": sid, "state": "RUNNING", "messages": msgs,
                                  "last_id": max([m["id"] for m in self.sessions[sid]["messages"]], default=0),
                                  "wait": request.query.get("wait")})

    async def reply(self, request: web.Request) -> web.Response:
        body = await request.json()
        self._record(request, body)
        sid = self._owned(request)
        mid = int(request.match_info["mid"])
        msg = next((m for m in self.sessions[sid].get("messages", []) if m["id"] == mid), None)
        if msg is None:
            return web.json_response({"error": "no question"}, status=404)
        msg.update(status="answered", answer=body["text"])
        return web.json_response(msg)

    async def persist(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        self._record(request, body)
        sid = self._owned(request)
        self.sessions[sid]["state"] = "PERSISTED"
        return web.json_response(self._info(sid))

    async def delete(self, request: web.Request) -> web.Response:
        self._record(request)
        sid = self._owned(request)
        del self.sessions[sid]
        return web.json_response({"deleted": True})

    # -- a tiny AIO-compatible guest, enough for the provider round trip --
    async def guest_write(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.files[body["file"]] = body["content"]
        return web.json_response({"success": True, "data": {"file": body["file"], "bytes_written": 1}})

    async def guest_read(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body["file"] not in self.files:
            return web.json_response({"success": False, "message": "not found"}, status=404)
        return web.json_response({"success": True, "data": {"file": body["file"], "content": self.files[body["file"]]}})

    def app(self) -> web.Application:
        @web.middleware
        async def auth(request: web.Request, handler):
            if request.path.startswith("/v1/") and request.headers.get("Authorization") != f"Bearer {TOKEN}":
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)

        app = web.Application(middlewares=[auth])
        sid = "{sid:[^/:]+}"
        app.router.add_post("/v1/sessions", self.create)
        app.router.add_get(f"/v1/sessions/{sid}", self.get)
        app.router.add_post(f"/v1/sessions/{sid}:pause", self.pause)
        app.router.add_post(f"/v1/sessions/{sid}:resume", self.resume)
        app.router.add_post(f"/v1/sessions/{sid}:persist", self.persist)
        app.router.add_post(f"/v1/sessions/{sid}/mcp-servers", self.mcp_servers)
        app.router.add_put(f"/v1/sessions/{sid}/secrets", self.secrets)
        app.router.add_post(f"/v1/sessions/{sid}:fork", self.fork)
        app.router.add_get(f"/v1/sessions/{sid}/messages", self.messages)
        app.router.add_post(f"/v1/sessions/{sid}/messages/{{mid:\\d+}}/reply", self.reply)
        app.router.add_delete(f"/v1/sessions/{sid}", self.delete)
        app.router.add_post(f"/t/{GUEST_TOKEN}/v1/file/write", self.guest_write)
        app.router.add_post(f"/t/{GUEST_TOKEN}/v1/file/read", self.guest_read)
        return app


@pytest_asyncio.fixture(name="manager")
async def manager_fixture():
    fake = FakeManager()
    runner = web.AppRunner(fake.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001 - test-only access to the bound port
    fake.base = f"http://127.0.0.1:{port}"
    FirecrackerLauncher._handles.clear()
    try:
        yield fake
    finally:
        FirecrackerLauncher._handles.clear()
        await runner.cleanup()


def _config(manager: FakeManager, owner: str = "alice", **kwargs: Any) -> FirecrackerLauncherConfig:
    return FirecrackerLauncherConfig(manager_url=manager.base, owner_user_id=owner, api_token=TOKEN,
                                     profile={"vcpus": 1, "mem_mib": 512}, **kwargs)


def test_launcher_is_registered():
    assert SandboxRegistry.get_launcher_cls("firecracker") is FirecrackerLauncher


@pytest.mark.asyncio
async def test_launch_creates_session_for_current_agent_session(manager):
    set_session_id("sess-1")
    launched = await FirecrackerLauncher().launch(_config(manager), timeout_seconds=30, isolation_key="ignored")

    assert launched.sandbox_id == "sess-1"
    assert launched.base_url == f"{manager.base}/t/{GUEST_TOKEN}"
    req = manager.requests[-1]
    assert (req["method"], req["path"]) == ("POST", "/v1/sessions")
    assert req["owner"] == "alice"
    assert req["auth"] == f"Bearer {TOKEN}"
    assert req["body"] == {"session_id": "sess-1", "owner_user_id": "alice", "profile": {"vcpus": 1, "mem_mib": 512}}


@pytest.mark.asyncio
async def test_launch_without_session_context_uses_isolation_key(manager):
    set_session_id("default_trace_id")
    launched = await FirecrackerLauncher().launch(_config(manager), timeout_seconds=30,
                                                  isolation_key="session_firecracker_aio_user-alice_x y")
    assert launched.sandbox_id == "session_firecracker_aio_user-alice_x-y"


@pytest.mark.asyncio
async def test_launch_rejects_foreign_config():
    from openjiuwen.core.sys_operation.config import PreDeployLauncherConfig

    with pytest.raises(BaseError) as exc_info:
        await FirecrackerLauncher().launch(PreDeployLauncherConfig(base_url="http://x"), timeout_seconds=5)
    assert "FirecrackerLauncherConfig" in str(exc_info.value)


@pytest.mark.asyncio
async def test_launch_of_another_users_session_is_refused(manager):
    set_session_id("shared-id")
    await FirecrackerLauncher().launch(_config(manager, owner="alice"), timeout_seconds=30)

    with pytest.raises(BaseError) as exc_info:
        await FirecrackerLauncher().launch(_config(manager, owner="bob"), timeout_seconds=30)
    assert is_ownership_error(exc_info.value)
    assert manager.sessions["shared-id"]["owner"] == "alice"


@pytest.mark.asyncio
async def test_pause_resume_and_check_status(manager):
    set_session_id("sess-2")
    launcher = FirecrackerLauncher()
    await launcher.launch(_config(manager), timeout_seconds=30)

    assert await launcher.check_status("sess-2") == SandboxStatus.RUNNING
    await launcher.pause("sess-2")
    assert manager.requests[-1]["path"] == "/v1/sessions/sess-2:pause"
    assert manager.requests[-1]["owner"] == "alice"
    assert await launcher.check_status("sess-2") == SandboxStatus.PAUSED
    await launcher.resume("sess-2")
    assert manager.requests[-1]["path"] == "/v1/sessions/sess-2:resume"
    assert await launcher.check_status("sess-2") == SandboxStatus.RUNNING


@pytest.mark.asyncio
async def test_snapshot_pause_mode_persists_and_carries_checkpoint_ref(manager):
    set_session_id("sess-snap")
    launcher = FirecrackerLauncher()
    await launcher.launch(_config(manager, pause_mode="snapshot"), timeout_seconds=30)

    await launcher.pause("sess-snap")
    assert manager.requests[-1]["path"] == "/v1/sessions/sess-snap:persist"
    assert await launcher.check_status("sess-snap") == SandboxStatus.PAUSED
    await launcher.persist("sess-snap", checkpoint_ref="ck-7")
    assert manager.requests[-1]["body"] == {"checkpoint_ref": "ck-7"}


@pytest.mark.asyncio
async def test_launch_metadata_has_no_secret(manager):
    set_session_id("sess-meta")
    launched = await FirecrackerLauncher().launch(_config(manager), timeout_seconds=30)
    assert launched.metadata == {"manager_url": manager.base, "owner_user_id": "alice",
                                 "timeout": 180.0, "pause_mode": "memory"}
    assert TOKEN not in str(launched.metadata)


@pytest.mark.asyncio
async def test_restore_handle_reattaches_after_process_restart(manager, monkeypatch):
    set_session_id("sess-restart")
    launched = await FirecrackerLauncher().launch(_config(manager), timeout_seconds=30)
    FirecrackerLauncher._handles.clear()  # simulate a new agent-core process
    monkeypatch.setenv("FCM_API_TOKEN", TOKEN)

    launcher = FirecrackerLauncher()
    launcher.restore_handle("sess-restart", launched.metadata)
    await launcher.pause("sess-restart")
    assert manager.requests[-1]["path"] == "/v1/sessions/sess-restart:pause"
    assert manager.requests[-1]["auth"] == f"Bearer {TOKEN}"
    assert manager.requests[-1]["owner"] == "alice"
    launcher.restore_handle("unknown", None)  # nothing saved: stays unknown
    assert "unknown" not in FirecrackerLauncher._handles


@pytest.mark.asyncio
async def test_check_status_maps_persisted_and_faulted_and_missing(manager):
    set_session_id("sess-3")
    launcher = FirecrackerLauncher()
    await launcher.launch(_config(manager), timeout_seconds=30)

    manager.sessions["sess-3"]["state"] = "PERSISTED"
    assert await launcher.check_status("sess-3") == SandboxStatus.PAUSED
    manager.sessions["sess-3"]["state"] = "FAULTED"
    assert await launcher.check_status("sess-3") == SandboxStatus.KILLED
    del manager.sessions["sess-3"]
    assert await launcher.check_status("sess-3") == SandboxStatus.KILLED


@pytest.mark.asyncio
async def test_delete_removes_vm_and_handle(manager):
    set_session_id("sess-4")
    launcher = FirecrackerLauncher()
    await launcher.launch(_config(manager), timeout_seconds=30)

    await launcher.delete("sess-4", isolation_key="k")
    assert "sess-4" not in manager.sessions
    assert manager.requests[-1]["method"] == "DELETE"
    assert "sess-4" not in FirecrackerLauncher._handles
    await launcher.delete("sess-4")  # unknown handle: no-op, no request
    assert manager.requests[-1]["method"] == "DELETE"


@pytest.mark.asyncio
async def test_delete_tolerates_already_gone_session(manager):
    set_session_id("sess-5")
    launcher = FirecrackerLauncher()
    await launcher.launch(_config(manager), timeout_seconds=30)
    del manager.sessions["sess-5"]
    await launcher.delete("sess-5")
    assert "sess-5" not in FirecrackerLauncher._handles


@pytest.mark.asyncio
async def test_calls_without_launch_handle_fail_clearly():
    with pytest.raises(BaseError) as exc_info:
        await FirecrackerLauncher().pause("never-launched")
    assert manager_http_status(exc_info.value) == 404


@pytest.mark.asyncio
async def test_unreachable_manager_is_wrapped():
    set_session_id("sess-6")
    config = FirecrackerLauncherConfig(manager_url="http://127.0.0.1:9", owner_user_id="alice",
                                       request_timeout_seconds=2)
    with pytest.raises(BaseError) as exc_info:
        await FirecrackerLauncher().launch(config, timeout_seconds=2)
    assert "cannot reach FcSessionManager" in str(exc_info.value)
    assert exc_info.value.cause is not None


@pytest.mark.asyncio
async def test_wrong_service_token_is_reported(manager):
    set_session_id("sess-7")
    config = FirecrackerLauncherConfig(manager_url=manager.base, owner_user_id="alice", api_token="wrong")
    with pytest.raises(BaseError) as exc_info:
        await FirecrackerLauncher().launch(config, timeout_seconds=5)
    assert manager_http_status(exc_info.value) == 401


def test_gateway_config_puts_owner_in_isolation_key():
    cfg_a = build_firecracker_gateway_config(manager_url="http://m:8700", owner_user_id="alice", api_token="t")
    cfg_b = build_firecracker_gateway_config(manager_url="http://m:8700", owner_user_id="bob/x", api_token="t")

    assert isinstance(cfg_a, SandboxGatewayConfig)
    assert cfg_a.isolation.container_scope == ContainerScope.SESSION
    assert isinstance(cfg_a.launcher_config, FirecrackerLauncherConfig)
    assert cfg_a.launcher_config.on_stop == "keep"
    key_a = generate_isolation_key_template(cfg_a.isolation.prefix, cfg_a.isolation.container_scope, None,
                                            "firecracker", "aio")
    key_b = generate_isolation_key_template(cfg_b.isolation.prefix, cfg_b.isolation.container_scope, None,
                                            "firecracker", "aio")
    assert key_a != key_b
    assert "user-alice" in key_a and "user-bob-x" in key_b


@pytest.mark.asyncio
async def test_browser_instance_is_per_owner_and_session(manager):
    instance = await build_browser_instance(_config(manager), "sess-8")
    assert instance == {
        "key": "alice--sess-8",
        "driver_mode": "remote",
        "cdp_url": f"{manager.base}/cdp/t/{GUEST_TOKEN}",
    }
    info = await get_session_info(_config(manager), "sess-8", ensure=False)
    assert info["state"] == "RUNNING"


def test_scoped_resource_ids_never_collide_across_users():
    assert scoped_resource_id("alice", "s1", "github") == "alice--s1--github"
    assert scoped_resource_id("alice", "s1", "github") != scoped_resource_id("bob", "s1", "github")
    assert scoped_resource_id("a/b", "s 1", "x") == "a-b--s-1--x"


@pytest.mark.asyncio
async def test_register_session_mcp_server_builds_scoped_config(manager):
    server = await register_session_mcp_server(_config(manager), "sess-mcp", "tools", "python3",
                                               ["-m", "srv"], {"K": "v"}, register=False)
    assert server.server_id == "alice--sess-mcp--tools"
    assert server.client_type == "streamable-http"
    assert server.server_path == f"{manager.base}/mcp/sess-mcp/servers/tools/mcp"
    req = manager.requests[-1]
    assert req["path"] == "/v1/sessions/sess-mcp/mcp-servers"
    assert req["body"] == {"name": "tools", "command": "python3", "args": ["-m", "srv"], "env": {"K": "v"}}


@pytest.mark.asyncio
async def test_set_session_secrets_merges_and_removes(manager):
    await get_session_info(_config(manager), "sess-sec")
    assert await set_session_secrets(_config(manager), "sess-sec", {"GH_TOKEN": "x", "OTHER": "y"}) == \
        ["GH_TOKEN", "OTHER"]
    assert await set_session_secrets(_config(manager), "sess-sec", {"OTHER": None}) == ["GH_TOKEN"]
    assert manager.requests[-1]["owner"] == "alice"


@pytest.mark.asyncio
async def test_fork_session_creates_forks_for_the_same_owner(manager):
    await get_session_info(_config(manager), "sess-src")
    forks = await fork_session(_config(manager), "sess-src", ["sess-f1", "sess f2"])
    assert [f["session_id"] for f in forks] == ["sess-f1", "sess-f2"]
    req = manager.requests[-1]
    assert req["path"] == "/v1/sessions/sess-src:fork" and req["owner"] == "alice"
    assert req["body"] == {"new_session_ids": ["sess-f1", "sess-f2"]}


@pytest.mark.asyncio
async def test_session_messages_round_trip(manager):
    await get_session_info(_config(manager), "sess-msg")
    manager.sessions["sess-msg"]["messages"] = [
        {"id": 1, "type": "notify", "text": "building"},
        {"id": 2, "type": "question", "text": "deploy?", "status": "pending"},
    ]
    page = await get_session_messages(_config(manager), "sess-msg", after=1, wait_seconds=120)
    assert [m["id"] for m in page["messages"]] == [2] and page["last_id"] == 2
    assert manager.requests[-1]["path"] == "/v1/sessions/sess-msg/messages"
    assert page["wait"] == "60", "the long-poll wait is capped at 60 seconds"
    answered = await reply_to_session_message(_config(manager), "sess-msg", 2, "yes")
    assert answered["status"] == "answered" and answered["answer"] == "yes"
    assert manager.requests[-1]["body"] == {"text": "yes"}
    with pytest.raises(BaseError) as exc:
        await reply_to_session_message(_config(manager), "sess-msg", 9, "no")
    assert manager_http_status(exc.value) == 404


@pytest.mark.asyncio
async def test_session_messages_of_another_user_are_rejected(manager):
    await get_session_info(_config(manager), "sess-msg-a")
    with pytest.raises(BaseError) as exc:
        await get_session_messages(_config(manager, owner="mallory"), "sess-msg-a")
    assert is_ownership_error(exc.value)
    with pytest.raises(BaseError) as exc:
        await reply_to_session_message(_config(manager, owner="mallory"), "sess-msg-a", 1, "x")
    assert is_ownership_error(exc.value)


@pytest.mark.asyncio
async def test_fork_session_of_another_users_session_is_rejected(manager):
    await get_session_info(_config(manager), "sess-bob")
    with pytest.raises(BaseError) as exc:
        await fork_session(_config(manager, owner="mallory"), "sess-bob", ["x"])
    assert manager_http_status(exc.value) == 403


@pytest.mark.skipif(importlib.util.find_spec("agent_sandbox") is None, reason="needs the 'sandbox' extra")
@pytest.mark.asyncio
async def test_gateway_round_trip_through_aio_provider(manager):
    """Gateway -> FirecrackerLauncher -> aio provider -> guest URL with the token prefix."""
    set_session_id(f"sess-gw-{uuid.uuid4().hex[:6]}")
    gateway = SandboxGateway()
    config = build_firecracker_gateway_config(manager_url=manager.base, owner_user_id="alice", api_token=TOKEN)
    key = f"user-alice-{uuid.uuid4().hex[:6]}"

    write = await gateway.handle_request(config, GatewayInvokeRequest(
        op_type="fs", method="write_file", params={"path": "/workspace/a.txt", "content": "hi"},
        isolation_key=key))
    assert write.code == StatusCode.SUCCESS.code, write.message
    read = await gateway.handle_request(config, GatewayInvokeRequest(
        op_type="fs", method="read_file", params={"path": "/workspace/a.txt"}, isolation_key=key))
    assert read.code == StatusCode.SUCCESS.code, read.message
    assert read.data.data.content.endswith("hi")
