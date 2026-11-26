# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Phase 2 end-to-end tests: persist / restore, wake-on-request, gateway fallback,
persistent gateway store, CPU template and checkpoint pairing.

Needs a running FcSessionManager (``FC_MANAGER_URL``); the restart test also
needs the Docker socket (``FC_E2E_DOCKER=1`` in run_e2e.sh).
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.sys_operation.config import GatewayConfig, GatewayInvokeRequest, GatewayStoreConfig
from openjiuwen.core.sys_operation.sandbox.gateway.gateway import SandboxGateway
from openjiuwen.extensions.sys_operation.sandbox.launchers import (
    FirecrackerLauncher,
    build_firecracker_gateway_config,
    get_session_info,
)
from tests.test_logger import logger as test_logger
from tests.system_tests.extensions.sys_operation.sandbox.fc_helpers import (
    BROWSER_PROFILE,
    DOCKER_SOCKET,
    MANAGER_TOKEN,
    MANAGER_URL,
    SUCCESS,
    in_session,
    manager,
    restart_manager_container,
    sh,
    wait_for_state,
)

pytestmark = pytest.mark.skipif(not MANAGER_URL, reason="needs a running FcSessionManager (FC_MANAGER_URL)")

BACKGROUND_PROCESS = (
    "nohup python3 -c 'import os, random, time\n"
    "v = random.random()\n"
    "while True:\n"
    "    open(\"/tmp/alive\", \"w\").write(f\"{os.getpid()} {v} {time.time()}\")\n"
    "    time.sleep(0.2)' >/dev/null 2>&1 & sleep 1; cat /tmp/alive"
)


def _alive(text: str) -> tuple[int, str, float]:
    pid, value, ts = text.split()
    return int(pid), value, float(ts)


@pytest.mark.skipif(not DOCKER_SOCKET, reason="needs the Docker socket to restart the manager (FC_E2E_DOCKER=1)")
@pytest.mark.asyncio
async def test_persisted_session_survives_manager_restart(fc):
    """2 GiB guest: persist -> restart FcSessionManager -> resume; the process and its memory survive."""
    profile = {"vcpus": 2, "mem_mib": 2048}
    sid, op = fc.open("alice", profile=profile)
    before = _alive((await sh(op, sid, BACKGROUND_PROCESS)).stdout)
    _, info_before = await manager("GET", f"/v1/sessions/{sid}", "alice")

    started = time.monotonic()
    status, info = await manager("POST", f"/v1/sessions/{sid}:persist", "alice", {"checkpoint_ref": "ck-1"})
    persist_s = time.monotonic() - started
    assert status == 200 and info["state"] == "PERSISTED", info
    _, dbg = await manager("GET", f"/v1/sessions/{sid}/debug", "alice")
    assert dbg["snapshot"]["snapshot_type"] == "Full" and dbg["snapshot"]["cpu_template"] == "T2"
    assert dbg["snapshot_files"]["mem"] == 2048 * 1024 * 1024
    assert dbg["pid"] is None, "a persisted session has no VM process"

    await restart_manager_container()
    _, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert info["state"] == "PERSISTED", "the snapshot must survive a manager restart"

    started = time.monotonic()
    status, info = await manager("POST", f"/v1/sessions/{sid}:resume", "alice", {"expected_checkpoint_ref": "ck-1"})
    resume_s = time.monotonic() - started
    assert status == 200 and info["state"] == "RUNNING", info
    assert info["base_url"] == info_before["base_url"], "the session keeps its endpoint across persist/restore"

    after = _alive((await sh(op, sid, "cat /tmp/alive")).stdout)
    later = _alive((await sh(op, sid, "sleep 1; cat /tmp/alive")).stdout)
    assert after[0] == before[0] and after[1] == before[1], "same process, same in-memory value"
    assert later[2] > after[2], "the process keeps running after restore"
    test_logger.info("2 GiB guest: persist %.1fs, resume %.1fs", persist_s, resume_s)


@pytest.mark.asyncio
async def test_idle_session_is_persisted_then_woken_by_the_next_tool_call(fc):
    profile = {"vcpus": 2, "mem_mib": 1024, "ttl_pause_s": 2, "ttl_persist_s": 3}
    sid, op = fc.open("alice", profile=profile)
    write = await in_session(sid, lambda: op.fs().write_file("/workspace/state.txt", "kept", prepend_newline=False))
    assert write.code == SUCCESS, write.message

    await wait_for_state("alice", sid, "PAUSED", timeout=60)
    await wait_for_state("alice", sid, "PERSISTED", timeout=60)

    # Same SysOperation, nothing done in agent-core: the proxy wakes the VM.
    read = await in_session(sid, lambda: op.fs().read_file("/workspace/state.txt"))
    assert read.code == SUCCESS and read.data.content == "kept", read.message
    _, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert info["state"] == "RUNNING" and info["snapshot_gen"] == 1


@pytest.mark.asyncio
async def test_gateway_fallback_restores_when_wake_on_request_is_off(fc):
    profile = {"vcpus": 2, "mem_mib": 1024, "ttl_pause_s": 2, "ttl_persist_s": 3, "wake_on_request": False}
    sid, op = fc.open("alice", profile=profile)
    await in_session(sid, lambda: op.fs().write_file("/workspace/state.txt", "kept", prepend_newline=False))
    await wait_for_state("alice", sid, "PERSISTED", timeout=90)

    # The proxy refuses the connection; the gateway drops its stale record,
    # re-launches (which restores the VM) and retries the call once.
    read = await in_session(sid, lambda: op.fs().read_file("/workspace/state.txt"))
    assert read.code == SUCCESS and read.data.content == "kept", read.message
    _, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert info["state"] == "RUNNING"


@pytest.mark.asyncio
async def test_browser_page_survives_persist_and_reconnect(fc):
    from playwright.async_api import async_playwright

    sid = f"br2-{uuid.uuid4().hex[:6]}"
    fc.sessions.append(("alice", sid))
    info = await get_session_info(fc.config("alice", BROWSER_PROFILE), sid)

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(info["cdp_url"])
        page = await browser.contexts[0].new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")
        await browser.close()

    status, persisted = await manager("POST", f"/v1/sessions/{sid}:persist", "alice")
    assert status == 200 and persisted["state"] == "PERSISTED"

    async with async_playwright() as pw:  # connecting wakes the VM; Chromium comes back with its tabs
        browser = await pw.chromium.connect_over_cdp(info["cdp_url"])
        urls = [p.url for p in browser.contexts[0].pages]
        await browser.close()
    assert any(u.startswith("https://example.com") for u in urls), urls


@pytest.mark.asyncio
async def test_checkpoint_ref_is_paired_with_the_snapshot(fc):
    sid, op = fc.open("alice")
    await sh(op, sid, "true")
    status, info = await manager("POST", f"/v1/sessions/{sid}:persist", "alice", {"checkpoint_ref": "ck-A"})
    assert status == 200 and info["checkpoint_ref"] == "ck-A"
    status, body = await manager("POST", f"/v1/sessions/{sid}:resume", "alice", {"expected_checkpoint_ref": "ck-B"})
    assert status == 409 and "checkpoint mismatch" in body["error"]
    _, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert info["state"] == "PERSISTED", "a refused resume must not touch the snapshot"
    status, info = await manager("POST", f"/v1/sessions/{sid}:resume", "alice", {"expected_checkpoint_ref": "ck-A"})
    assert status == 200 and info["state"] == "RUNNING"


@pytest.mark.asyncio
async def test_default_cpu_template_is_applied(fc):
    sid, op = fc.open("alice")
    guest_flags = set((await sh(op, sid, "grep -m1 '^flags' /proc/cpuinfo")).stdout.split(":", 1)[1].split())
    with open("/proc/cpuinfo", encoding="utf-8") as fh:
        host_flags = set(next(line for line in fh if line.startswith("flags")).split(":", 1)[1].split())
    _, dbg = await manager("GET", f"/v1/sessions/{sid}/debug", "alice")
    assert dbg["cpu_template"] == "T2"
    hidden = {"avx512f", "avx512bw", "avx512vl"} & host_flags
    assert hidden, "this check expects a host CPU with AVX-512"
    assert not hidden & guest_flags, f"T2 must hide AVX-512 from the guest, guest shows {hidden & guest_flags}"
    assert {"sse4_2", "avx2"} <= guest_flags


@pytest.mark.asyncio
async def test_persistent_gateway_store_reattaches_after_agent_core_restart(fc):
    sid = f"gw-{uuid.uuid4().hex[:6]}"
    fc.sessions.append(("alice", sid))
    config = build_firecracker_gateway_config(manager_url=MANAGER_URL, owner_user_id="alice",
                                              api_token=MANAGER_TOKEN, profile={"mem_mib": 1024})
    db = os.path.join(tempfile.mkdtemp(), "sandbox-records.db")
    key = f"user-alice_{sid}"
    request = GatewayInvokeRequest(op_type="shell", method="execute_cmd", params={"command": "echo up"},
                                   isolation_key=key)

    first = SandboxGateway(GatewayConfig(store=GatewayStoreConfig(type="sqlite", sqlite_path=db)))
    result = await in_session(sid, lambda: first.handle_request(config, request))
    assert result.code == StatusCode.SUCCESS.code and "up" in result.data.data.stdout

    # A new agent-core process: no launcher handles in memory, only the SQLite records.
    FirecrackerLauncher._handles.clear()
    second = SandboxGateway(GatewayConfig(store=GatewayStoreConfig(type="sqlite", sqlite_path=db)))
    released = await second.release_sandbox(key, on_stop="pause")
    assert released.code == StatusCode.SUCCESS.code, released.message
    _, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert info["state"] == "PAUSED", "the restarted gateway must still reach the session through the manager"
