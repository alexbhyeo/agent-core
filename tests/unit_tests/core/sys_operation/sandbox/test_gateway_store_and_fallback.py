# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SandboxGateway: persistent SQLite store, launcher re-attachment and the
one-shot retry when a sandbox endpoint went away."""

import time
import uuid

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.sys_operation.config import (
    GatewayConfig,
    GatewayInvokeRequest,
    GatewayStoreConfig,
    SandboxGatewayConfig,
    SandboxLauncherConfig,
)
from openjiuwen.core.sys_operation.sandbox.gateway.gateway import SandboxGateway
from openjiuwen.core.sys_operation.sandbox.gateway.sandbox_store import (
    InMemorySandboxStore,
    SandboxRecord,
    SandboxStatus,
    SqliteSandboxStore,
    build_sandbox_store,
)
from openjiuwen.core.sys_operation.sandbox.launchers.base import LaunchedSandbox, SandboxLauncher
from openjiuwen.core.sys_operation.sandbox.sandbox_registry import SandboxRegistry

TYPE = "unit-fallback"


class _Result:
    def __init__(self, code: int, message: str = "") -> None:
        self.code = code
        self.message = message


class _Launcher(SandboxLauncher):
    launches = 0
    restored: list = []
    paused: list = []

    async def launch(self, config, timeout_seconds, isolation_key=None):
        type(self).launches += 1
        return LaunchedSandbox(base_url=f"http://sb/{type(self).launches}", sandbox_id="sb-1",
                               metadata={"where": "manager-a"})

    def restore_handle(self, sandbox_id, metadata):
        type(self).restored.append((sandbox_id, metadata))

    async def pause(self, sandbox_id):
        type(self).paused.append(sandbox_id)


class _Provider:
    script: list = []

    def __init__(self, endpoint, config=None):
        self.endpoint = endpoint

    async def read_file(self, path):
        return type(self).script.pop(0)


@pytest.fixture(name="registered")
def registered_fixture():
    SandboxRegistry.register_launcher(TYPE, _Launcher)
    SandboxRegistry.register_provider(TYPE, "fs", _Provider)
    _Launcher.launches, _Launcher.restored, _Launcher.paused = 0, [], []
    yield
    SandboxRegistry.unregister_launcher(TYPE)
    SandboxRegistry.unregister_provider(TYPE, "fs")


def _config() -> SandboxGatewayConfig:
    return SandboxGatewayConfig(launcher_config=SandboxLauncherConfig(launcher_type=TYPE, sandbox_type=TYPE))


def _request() -> GatewayInvokeRequest:
    return GatewayInvokeRequest(op_type="fs", method="read_file", params={"path": "/a"}, isolation_key="key-1")


@pytest.mark.asyncio
async def test_sqlite_store_round_trip(tmp_path):
    store = SqliteSandboxStore(str(tmp_path / "records.db"))
    record = SandboxRecord(sandbox_id="s1", base_url="http://x", status=SandboxStatus.PAUSED,
                           launcher_type="t", sandbox_type="aio", container_config_hash="h",
                           last_used_ts=time.time() - 100, metadata={"launcher": {"a": 1}})
    await store.set("k1", record)

    reopened = SqliteSandboxStore(str(tmp_path / "records.db"))  # e.g. after a restart
    loaded = await reopened.get("k1")
    assert loaded == record
    assert [r.sandbox_id for r in await reopened.evict_expired(10, time.time())] == ["s1"]
    assert await reopened.get("k1") is None
    await reopened.set("k2", record)
    assert (await reopened.hdel("k2")).sandbox_id == "s1"
    await reopened.set("k3", record)
    assert len(await reopened.flushdb()) == 1


def test_build_sandbox_store_selects_backend(tmp_path):
    assert isinstance(build_sandbox_store("memory"), InMemorySandboxStore)
    assert isinstance(build_sandbox_store("sqlite", str(tmp_path / "s.db")), SqliteSandboxStore)
    with pytest.raises(BaseError):
        build_sandbox_store("sqlite")
    with pytest.raises(BaseError):
        build_sandbox_store("redis")


@pytest.mark.asyncio
async def test_connection_failure_is_retried_once_after_relaunch(registered):
    gateway = SandboxGateway(GatewayConfig())
    _Provider.script = [_Result(StatusCode.SYS_OPERATION_FS_EXECUTION_ERROR.code,
                                "fs error: Server disconnected without sending a response."),
                        _Result(StatusCode.SUCCESS.code, "ok")]

    response = await gateway.handle_request(_config(), _request())

    assert response.code == StatusCode.SUCCESS.code
    assert response.data.message == "ok"
    assert _Launcher.launches == 2, "the stale record must be dropped and the sandbox resolved again"


@pytest.mark.asyncio
async def test_other_errors_are_not_retried(registered):
    gateway = SandboxGateway(GatewayConfig())
    failure = _Result(StatusCode.SYS_OPERATION_FS_EXECUTION_ERROR.code, "fs error: No such file")
    _Provider.script = [failure]

    response = await gateway.handle_request(_config(), _request())

    assert response.code == StatusCode.SUCCESS.code and response.data is failure
    assert _Launcher.launches == 1


@pytest.mark.asyncio
async def test_release_session_deletes_only_that_sessions_sandboxes(registered, monkeypatch):
    from openjiuwen.core.common.logging.utils import set_session_id

    deleted: list = []

    async def _delete(self, sandbox_id, **kwargs):
        deleted.append(kwargs.get("isolation_key"))

    monkeypatch.setattr(_Launcher, "delete", _delete, raising=False)
    gateway = SandboxGateway(GatewayConfig())
    for session_id, key in (("sess-a", "key-a"), ("sess-b", "key-b")):
        set_session_id(session_id)
        _Provider.script = [_Result(StatusCode.SUCCESS.code, "ok")]
        request = GatewayInvokeRequest(op_type="fs", method="read_file", params={"path": "/a"}, isolation_key=key)
        await gateway.handle_request(_config(), request)

    assert await gateway.release_session("sess-a") == 1
    assert deleted == ["key-a"]
    assert await gateway._store.get("key-a") is None
    assert await gateway._store.get("key-b") is not None
    assert await gateway.release_session("") == 0


@pytest.mark.asyncio
async def test_records_carry_launcher_metadata_and_reattach(registered, tmp_path):
    db = str(tmp_path / f"gw-{uuid.uuid4().hex}.db")
    gateway = SandboxGateway(GatewayConfig(store=GatewayStoreConfig(type="sqlite", sqlite_path=db)))
    _Provider.script = [_Result(StatusCode.SUCCESS.code, "ok")]
    await gateway.handle_request(_config(), _request())

    restarted = SandboxGateway(GatewayConfig(store=GatewayStoreConfig(type="sqlite", sqlite_path=db)))
    response = await restarted.release_sandbox("key-1", on_stop="pause")

    assert response.code == StatusCode.SUCCESS.code
    assert _Launcher.restored == [("sb-1", {"where": "manager-a"})]
    assert _Launcher.paused == ["sb-1"]
