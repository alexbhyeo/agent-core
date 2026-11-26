# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""End-to-end tests: agent-core <-> FcSessionManager <-> Firecracker microVMs.

Requires a running FcSessionManager (deploy/firecracker) reachable at
``FC_MANAGER_URL`` with ``FCM_API_TOKEN``; skipped otherwise. LLM-driven tests
additionally need ``API_KEY`` / ``API_BASE`` / ``MODEL_NAME``.

Run inside the e2e runner container (see deploy/firecracker/README.md):
    pytest tests/system_tests/extensions/sys_operation/sandbox/test_firecracker_e2e.py
"""

from __future__ import annotations

import asyncio
import socket
import time
import uuid

import aiohttp
import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.runner import Runner
from openjiuwen.extensions.sys_operation.sandbox.launchers import build_browser_instance, get_session_info
from tests.test_logger import logger as test_logger
from tests.system_tests.extensions.sys_operation.sandbox.fc_helpers import (
    BROWSER_PROFILE,
    LLM_READY,
    MANAGER_URL,
    SUCCESS,
    in_session,
    manager,
    sh,
)

pytestmark = pytest.mark.skipif(not MANAGER_URL, reason="needs a running FcSessionManager (FC_MANAGER_URL)")


# --------------------------------------------------------------------------- basics


@pytest.mark.asyncio
async def test_file_and_shell_round_trip(fc):
    sid, op = fc.open("alice")
    write = await in_session(sid, lambda: op.fs().write_file("/workspace/notes/hello.txt", "hello from alice",
                                                             prepend_newline=False))
    assert write.code == SUCCESS, write.message
    read = await in_session(sid, lambda: op.fs().read_file("/workspace/notes/hello.txt"))
    assert read.code == SUCCESS, read.message
    assert read.data.content == "hello from alice"

    listing = await in_session(sid, lambda: op.fs().list_files("/workspace/notes"))
    assert listing.code == SUCCESS, listing.message
    assert [f.name for f in listing.data.list_items] == ["hello.txt"]

    out = await sh(op, sid, "echo $((6*7)); uname -s; cat /workspace/notes/hello.txt")
    assert out.exit_code == 0
    assert out.stdout.split() == ["42", "Linux", "hello", "from", "alice"]

    failing = await sh(op, sid, "ls /does-not-exist")
    assert failing.exit_code != 0
    assert "No such file" in failing.stderr

    status, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert status == 200 and info["state"] == "RUNNING" and info["owner_user_id"] == "alice"


@pytest.mark.asyncio
async def test_vm_process_runs_inside_the_jailer(fc):
    sid, op = fc.open("alice")
    await sh(op, sid, "true")
    status, dbg = await manager("GET", f"/v1/sessions/{sid}/debug", "alice")
    assert status == 200, dbg
    # What the VM process sees as "/" is the jailer chroot: only its own files, no host tree.
    entries = set(dbg["root_entries"])
    assert {"firecracker", "api.sock", "v.sock", "vmlinux", "rootfs.ext4", "dev"} <= entries, entries
    assert not entries & {"etc", "usr", "home", "var", "opt"}, f"host directories visible in chroot: {entries}"
    assert dbg["mntns_inode"] != dbg["manager_mntns_inode"], "VM must have its own mount namespace"
    assert dbg["exe"] == "/firecracker", "binary must be the jailer's copy inside the chroot"
    assert dbg["uid"] >= 10000, "Firecracker must drop root"
    assert dbg["netns_inode"] != dbg["manager_netns_inode"], "VM must have its own network namespace"
    assert f"/fc/{dbg['vm_id']}" in dbg["cgroup"], "VM must be in its own cgroup"
    assert dbg["seccomp"] == "2", "Firecracker's seccomp filter must be active"


@pytest.mark.asyncio
async def test_guest_reads_its_session_id_from_mmds(fc):
    sid, op = fc.open("alice")
    out = await sh(op, sid, (
        "T=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-metadata-token-ttl-seconds: 60'); "
        "curl -s -H \"X-metadata-token: $T\" http://169.254.169.254/latest/meta-data/session_id; echo; "
        "curl -s -o /dev/null -w '%{http_code}' http://169.254.169.254/latest/meta-data/session_id"
    ))
    lines = out.stdout.split()
    assert lines[0] == sid, "MMDS V2 must return this VM's session id"
    assert lines[1] == "401", "MMDS V2 must refuse requests without a token"


@pytest.mark.asyncio
async def test_guest_has_internet_but_not_host_or_private_ranges(fc):
    sid, op = fc.open("alice")
    out = await sh(op, sid, (
        "set -e; python3 -m venv /tmp/venv && /tmp/venv/bin/pip install -q six && "
        "/tmp/venv/bin/python -c 'import six; print(\"pip-ok\", six.__version__)'; "
        "git clone -q --depth 1 https://github.com/octocat/Hello-World.git /tmp/hw && test -f /tmp/hw/README && "
        "echo git-ok"
    ), timeout=280)
    assert out.exit_code == 0, out.stdout + out.stderr
    assert "pip-ok" in out.stdout and "git-ok" in out.stdout

    _, dbg = await manager("GET", f"/v1/sessions/{sid}/debug", "alice")
    index = int(dbg["netns"].removeprefix("fc"))
    manager_ip = socket.gethostbyname(MANAGER_URL.split("//")[1].split(":")[0])
    targets = {
        "manager_container": f"http://{manager_ip}:8700/healthz",
        "host_side_of_vm_link": f"http://10.200.{index}.1:8700/healthz",
        "tap_gateway": "http://172.16.0.1:22/",
        "private_10": "http://10.0.0.1/",
        "private_192_168": "http://192.168.65.1/",
        "private_172_16": "http://172.17.0.1/",
        "public_pypi": "https://pypi.org/simple/six/",
    }
    # One line per target: "<name> <http_code> <curl exit code>".
    script = "; ".join(
        f"echo {name} $(curl -s -o /dev/null -m 8 -w '%{{http_code}} ' {url}; echo $?)"
        for name, url in targets.items()
    )
    out = await sh(op, sid, script)
    results = {}
    for line in out.stdout.strip().splitlines():
        name, code, rc = line.split()
        results[name] = (code, int(rc))
    assert set(results) == set(targets), out.stdout
    for name in targets:
        if name == "public_pypi":
            assert results[name] == ("200", 0), results[name]
        else:
            assert results[name][0] == "000" and results[name][1] != 0, f"{name} must be unreachable: {results[name]}"


# ------------------------------------------------------------------- multi-user


@pytest.mark.asyncio
async def test_another_users_session_id_is_rejected(fc):
    sid, alice_op = fc.open("alice")
    await in_session(sid, lambda: alice_op.fs().write_file("/workspace/secret.txt", "alice-secret",
                                                           prepend_newline=False))

    # Bob presents Alice's session id through his own agent: the manager refuses it.
    bob_op = fc.user_op("bob")
    with pytest.raises(BaseError) as refused:
        await in_session(sid, lambda: bob_op.fs().read_file("/workspace/secret.txt"))
    assert "403" in str(refused.value) and "another user" in str(refused.value)

    # Direct manager calls as Bob are refused too, and Bob cannot see the session.
    for method, path in (("GET", f"/v1/sessions/{sid}"), ("POST", f"/v1/sessions/{sid}:pause"),
                         ("DELETE", f"/v1/sessions/{sid}"), ("GET", f"/v1/sessions/{sid}/debug")):
        status, body = await manager(method, path, "bob")
        assert status == 403, (method, path, body)
    status, body = await manager("POST", "/v1/sessions", "bob", {"session_id": sid})
    assert status == 403
    status, body = await manager("GET", "/v1/sessions?owner=alice", "bob")
    assert status == 403
    _, listing = await manager("GET", "/v1/sessions", "bob")
    assert sid not in [s["session_id"] for s in listing["sessions"]]

    # Wrong service token: rejected before anything else.
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{MANAGER_URL}/v1/sessions/{sid}",
                               headers={"Authorization": "Bearer nope", "X-Owner-User-Id": "alice"}) as resp:
            assert resp.status == 401

    # Alice's VM is untouched.
    read = await in_session(sid, lambda: alice_op.fs().read_file("/workspace/secret.txt"))
    assert read.code == SUCCESS and read.data.content == "alice-secret"


@pytest.mark.asyncio
async def test_three_users_run_in_parallel_in_separate_vms(fc):
    users = ["alice", "bob", "carol"]
    opened = {user: fc.open(user) for user in users}
    # Alice also has a second session, which must be isolated from her first one.
    opened["alice-2"] = fc.open("alice")

    async def work(user: str) -> dict[str, str]:
        sid, op = opened[user]
        write = await in_session(sid, lambda: op.fs().write_file("/workspace/whoami.txt", f"{user}:{sid}",
                                                                 prepend_newline=False))
        assert write.code == SUCCESS, write.message
        out = await sh(op, sid, (
            "T=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-metadata-token-ttl-seconds: 60'); "
            "echo mmds=$(curl -s -H \"X-metadata-token: $T\" http://169.254.169.254/latest/meta-data/session_id); "
            "echo files=$(ls /workspace | tr '\\n' ','); echo boot=$(cat /proc/sys/kernel/random/boot_id); "
            "python3 -c 'print(\"sum=\", sum(i*i for i in range(2_000_000)))'"
        ))
        values = dict(line.split("=", 1) for line in out.stdout.strip().splitlines())
        read = await in_session(sid, lambda: op.fs().read_file("/workspace/whoami.txt"))
        values["whoami"] = read.data.content
        return values

    started = time.monotonic()
    results = dict(zip(opened, await asyncio.gather(*(work(u) for u in opened))))
    elapsed = time.monotonic() - started

    for user, (sid, _) in opened.items():
        assert results[user]["mmds"] == sid
        assert results[user]["whoami"] == f"{user}:{sid}"
        assert results[user]["files"] == "whoami.txt,", "a VM must only see its own files"
    assert len({r["boot"] for r in results.values()}) == len(opened), "every session must be its own VM"

    for user, (sid, _) in opened.items():
        owner = user.split("-")[0]
        _, listing = await manager("GET", "/v1/sessions", owner)
        assert all(s["owner_user_id"] == owner for s in listing["sessions"])
        assert sid in [s["session_id"] for s in listing["sessions"]]
    _, alice_list = await manager("GET", "/v1/sessions", "alice")
    assert {opened["alice"][0], opened["alice-2"][0]} <= {s["session_id"] for s in alice_list["sessions"]}
    test_logger.info("4 sessions for 3 users booted and worked in parallel in %.1fs", elapsed)


@pytest.mark.asyncio
async def test_pause_freezes_and_resume_continues(fc):
    sid, op = fc.open("alice")
    await sh(op, sid, "nohup bash -c 'i=0; while true; do i=$((i+1)); echo $i > /tmp/counter; sleep 0.1; done' "
                      ">/dev/null 2>&1 & sleep 1")
    before = int((await sh(op, sid, "cat /tmp/counter")).stdout)

    status, info = await manager("POST", f"/v1/sessions/{sid}:pause", "alice")
    assert status == 200 and info["state"] == "PAUSED"
    await asyncio.sleep(3)
    status, info = await manager("POST", f"/v1/sessions/{sid}:resume", "alice")
    assert status == 200 and info["state"] == "RUNNING"

    after = int((await sh(op, sid, "cat /tmp/counter")).stdout)
    assert after - before < 20, f"counter advanced {after - before} while paused for 3s (~30 if not frozen)"
    await asyncio.sleep(1)
    later = int((await sh(op, sid, "cat /tmp/counter")).stdout)
    assert later > after, "the background process must keep running after resume"


@pytest.mark.asyncio
async def test_destroy_removes_the_vm(fc):
    sid, op = fc.open("alice")
    await sh(op, sid, "true")
    status, _ = await manager("DELETE", f"/v1/sessions/{sid}", "alice")
    assert status == 200
    status, _ = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert status == 404
    _, listing = await manager("GET", "/v1/sessions", "alice")
    assert sid not in [s["session_id"] for s in listing["sessions"]]


# ----------------------------------------------------------------------- browser


@pytest.mark.asyncio
async def test_each_session_has_its_own_browser(fc):
    from playwright.async_api import async_playwright

    sessions = {"alice": f"br-{uuid.uuid4().hex[:6]}", "bob": f"br-{uuid.uuid4().hex[:6]}"}
    for owner, sid in sessions.items():
        fc.sessions.append((owner, sid))
    infos = dict(zip(sessions, await asyncio.gather(*(
        get_session_info(fc.config(owner, BROWSER_PROFILE), sid) for owner, sid in sessions.items()))))

    async def use_browser(owner: str) -> str:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(infos[owner]["cdp_url"])
            context = browser.contexts[0]
            page = await context.new_page()
            await page.goto("https://example.com", wait_until="domcontentloaded")
            await context.add_cookies([{"name": "owner", "value": owner, "domain": "example.com", "path": "/"}])
            title = await page.title()
            await browser.close()
            return title

    titles = await asyncio.gather(*(use_browser(owner) for owner in sessions))
    assert all("Example Domain" in t for t in titles)

    async def cookies(owner: str) -> list[dict]:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(infos[owner]["cdp_url"])
            found = await browser.contexts[0].cookies("https://example.com")
            await browser.close()
            return found

    for owner in sessions:
        values = [c["value"] for c in await cookies(owner) if c["name"] == "owner"]
        assert values == [owner], f"{owner}'s browser must only hold its own cookie, got {values}"

    # The CDP endpoint is useless without the session token.
    tokenless = infos["alice"]["cdp_url"].split("/t/")[0] + "/json/version"
    async with aiohttp.ClientSession() as session:
        async with session.get(tokenless) as resp:
            assert resp.status == 403


# ------------------------------------------------------------- LLM-driven (agents)


@pytest.mark.skipif(not LLM_READY, reason="needs API_KEY / API_BASE / MODEL_NAME")
@pytest.mark.asyncio
async def test_example_deep_agent_works_in_its_vm(fc):
    from examples.firecracker import deep_agent_firecracker as example

    sid = f"agent-{uuid.uuid4().hex[:8]}"
    fc.sessions.append(("alice", sid))
    fc.cards.append("fc-alice")  # the example registers its per-user SysOperation under this id
    result = await example.run_demo("alice", sid)
    assert result.get("result_type") == "answer", result
    output = str(result.get("output"))
    assert "firecracker ok" in output and "Linux" in output and "fc-session" in output, output

    # Verify the side effect independently: the file really is in this session's VM.
    status, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert status == 200 and info["owner_user_id"] == "alice"
    op = Runner.resource_mgr.get_sys_operation("fc-alice")
    read = await in_session(sid, lambda: op.fs().read_file("/workspace/hello.txt"))
    assert read.code == SUCCESS and read.data.content.strip() == "firecracker ok"


@pytest.mark.skipif(not LLM_READY, reason="needs API_KEY / API_BASE / MODEL_NAME")
@pytest.mark.asyncio
async def test_two_users_browser_agents_run_in_parallel_with_separate_cookies(fc):
    from playwright.async_api import async_playwright

    from examples.firecracker.deep_agent_firecracker import build_model
    from openjiuwen.harness.subagents import create_browser_agent

    runs = {"alice": f"bra-{uuid.uuid4().hex[:6]}", "bob": f"bra-{uuid.uuid4().hex[:6]}"}
    for owner, sid in runs.items():
        fc.sessions.append((owner, sid))

    async def run(owner: str) -> tuple[dict, str]:
        config = fc.config(owner, BROWSER_PROFILE)
        instance = await build_browser_instance(config, runs[owner])
        agent = create_browser_agent(model=build_model(), browser_instance=instance, max_iterations=12)
        query = (f"Open https://httpbin.org/cookies/set?owner={owner} in the browser. "
                 "Then report the JSON text shown on the resulting page.")
        result = await Runner.run_agent(agent, {"query": query}, session=runs[owner])
        return result, instance["cdp_url"]

    started = time.monotonic()
    (alice_result, alice_cdp), (bob_result, bob_cdp) = await asyncio.gather(run("alice"), run("bob"))
    elapsed = time.monotonic() - started
    assert "alice" in str(alice_result.get("output")), alice_result
    assert "bob" in str(bob_result.get("output")), bob_result

    async def owner_cookie(cdp_url: str) -> list[str]:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            found = await browser.contexts[0].cookies("https://httpbin.org")
            await browser.close()
            return [c["value"] for c in found if c["name"] == "owner"]

    assert await owner_cookie(alice_cdp) == ["alice"]
    assert await owner_cookie(bob_cdp) == ["bob"]
    test_logger.info("two browser agents finished in parallel in %.1fs", elapsed)
