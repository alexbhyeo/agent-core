# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Phase 4 end-to-end tests: fork, diff snapshots and workspace hot-swap.

Needs a running FcSessionManager (see deploy/firecracker/scripts/run_e2e.sh).
"""

from __future__ import annotations

import aiohttp
import pytest

from openjiuwen.extensions.sys_operation.sandbox.launchers import fork_session

from tests.system_tests.extensions.sys_operation.sandbox.fc_helpers import MANAGER_URL, manager, sh
from tests.test_logger import logger as test_logger

pytestmark = pytest.mark.skipif(not MANAGER_URL, reason="needs FC_MANAGER_URL (a running FcSessionManager)")

READ_MMDS_SESSION = (
    "T=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-metadata-token-ttl-seconds: 60'); "
    "curl -s -H \"X-metadata-token: $T\" http://169.254.169.254/latest/meta-data/session_id"
)


async def _snapshot_meta(owner: str, session_id: str) -> dict:
    status, info = await manager("GET", f"/v1/sessions/{session_id}/debug", owner)
    assert status == 200 and "snapshot" in info, info
    return info["snapshot"]


# ---------------------------------------------------------------------------- fork


@pytest.mark.asyncio
async def test_three_forks_share_the_past_but_not_the_future(fc):
    src, op = fc.open("alice")
    # Memory-only state (tmpfs), a running process and a workspace file before the fork.
    await sh(op, src, "echo 42 > /dev/shm/mark && echo base > /workspace/shared.txt && "
                      "(sleep 100000 >/dev/null 2>&1 &) && sync")
    sleeper = (await sh(op, src, "pgrep -x sleep")).stdout.split()[0]

    fork_ids = [f"{src}-f{i}" for i in range(1, 4)]
    fc.sessions += [("alice", fid) for fid in fork_ids]
    forks = await fork_session(fc.config("alice"), src, fork_ids)
    assert [f["session_id"] for f in forks] == fork_ids
    assert all(f["state"] == "RUNNING" and f["forked_from"] == src for f in forks)
    _, src_info = await manager("GET", f"/v1/sessions/{src}", "alice")
    tokens = {f["base_url"].rsplit("/t/", 1)[1] for f in forks} | {src_info["base_url"].rsplit("/t/", 1)[1]}
    assert len(tokens) == 4, "every fork must get its own guest token"

    entropy = set()
    for fid in [src] + fork_ids:
        out = (await sh(op, fid, "cat /dev/shm/mark; cat /workspace/shared.txt; "
                                 f"pgrep -x sleep; {READ_MMDS_SESSION}; echo; "
                                 "head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \\n'")).stdout.split()
        assert out[0] == "42", f"{fid} lost the memory image: {out}"
        assert out[1] == "base", f"{fid} lost the disk image: {out}"
        assert out[2] == sleeper, f"{fid} lost the running process: {out}"
        assert out[3] == fid, f"{fid} must carry its own identity in MMDS: {out}"
        entropy.add(out[4])
    assert len(entropy) == 4, "forks must not produce the same random bytes"

    # After the fork each VM writes the same path without affecting the others.
    for fid in [src] + fork_ids:
        await sh(op, fid, f"echo {fid} > /workspace/shared.txt")
    for fid in [src] + fork_ids:
        assert (await sh(op, fid, "cat /workspace/shared.txt")).stdout.strip() == fid

    # The source's guest token does not open a fork, and other users cannot fork.
    src_token = src_info["base_url"].rsplit("/t/", 1)[1]
    fork_base = forks[0]["base_url"].rsplit("/t/", 1)[0]
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{fork_base}/t/{src_token}/fc/identity") as resp:
            assert resp.status == 403
    status, _ = await manager("POST", f"/v1/sessions/{src}:fork", "bob", {"new_session_ids": [f"{src}-bob"]})
    assert status == 403


@pytest.mark.asyncio
async def test_fork_of_a_persisted_session_keeps_the_source_snapshot(fc):
    src, op = fc.open("alice")
    await sh(op, src, "echo persisted > /dev/shm/mark")
    status, _ = await manager("POST", f"/v1/sessions/{src}:persist", "alice")
    assert status == 200
    fid = f"{src}-pf"
    fc.sessions.append(("alice", fid))
    status, body = await manager("POST", f"/v1/sessions/{src}:fork", "alice", {"new_session_ids": [fid]})
    assert status == 201, body
    _, info = await manager("GET", f"/v1/sessions/{src}", "alice")
    assert info["state"] == "PERSISTED", "forking a persisted session leaves it persisted"
    assert (await sh(op, fid, "cat /dev/shm/mark")).stdout.strip() == "persisted"
    assert (await sh(op, src, "cat /dev/shm/mark")).stdout.strip() == "persisted"


# -------------------------------------------------------------------- diff snapshots


@pytest.mark.asyncio
async def test_persist_after_restore_writes_a_diff_snapshot(fc):
    sid, op = fc.open("alice")
    await sh(op, sid, "echo one > /dev/shm/a")
    await manager("POST", f"/v1/sessions/{sid}:persist", "alice")
    full = await _snapshot_meta("alice", sid)
    assert full["snapshot_type"] == "Full"

    assert (await sh(op, sid, "cat /dev/shm/a")).stdout.strip() == "one"  # wakes (restore)
    await sh(op, sid, "echo two > /dev/shm/b")
    await manager("POST", f"/v1/sessions/{sid}:persist", "alice")
    diff = await _snapshot_meta("alice", sid)
    assert diff["snapshot_type"] == "Diff"
    assert diff["mem_bytes_written"] < full["mem_bytes_written"] / 2, (full, diff)
    test_logger.info("persist Full: %ss, %.1f MiB written; Diff: %ss (snapshot %ss + rebase %ss), %.1f MiB written",
                     full["persist_s"], full["mem_bytes_written"] / 2 ** 20, diff["persist_s"], diff["snapshot_s"],
                     diff["merge_s"], diff["mem_bytes_written"] / 2 ** 20)

    # The rebased memory is a complete image: a second restore and Diff still hold everything.
    assert (await sh(op, sid, "cat /dev/shm/a /dev/shm/b")).stdout.split() == ["one", "two"]
    await sh(op, sid, "echo three > /dev/shm/c")
    await manager("POST", f"/v1/sessions/{sid}:persist", "alice")
    assert (await _snapshot_meta("alice", sid))["snapshot_type"] == "Diff"
    assert (await sh(op, sid, "cat /dev/shm/a /dev/shm/b /dev/shm/c")).stdout.split() == ["one", "two", "three"]


# ------------------------------------------------------------------ workspace swap


@pytest.mark.asyncio
async def test_workspace_hot_swap_without_reboot(fc):
    sid, op = fc.open("alice")
    out = await sh(op, sid, "findmnt -n -o SOURCE /workspace; cat /proc/sys/kernel/random/boot_id")
    device, boot_id = out.stdout.split()
    assert device == "/dev/vdb", "the workspace is its own drive"
    await sh(op, sid, "echo v1 > /workspace/v.txt")

    status, body = await manager("POST", f"/v1/sessions/{sid}/workspace/checkpoints", "alice", {"name": "v1"})
    assert status == 201 and body["workspace_checkpoints"] == ["v1"], body
    await sh(op, sid, "echo v2 > /workspace/v.txt && echo x > /workspace/extra.txt")

    status, info = await manager("POST", f"/v1/sessions/{sid}/workspace:swap", "alice", {"checkpoint": "v1"})
    assert status == 200 and info["state"] == "RUNNING", info
    out = await sh(op, sid, "cat /workspace/v.txt; ls /workspace; cat /proc/sys/kernel/random/boot_id")
    assert out.stdout.split() == ["v1", "v.txt", boot_id], out.stdout

    # The swapped drive survives persist and restore.
    await manager("POST", f"/v1/sessions/{sid}:persist", "alice")
    assert (await sh(op, sid, "echo v3 >> /workspace/v.txt; cat /workspace/v.txt")).stdout.split() == ["v1", "v3"]
    status, _ = await manager("POST", f"/v1/sessions/{sid}/workspace:swap", "bob", {"checkpoint": "v1"})
    assert status == 403
    status, _ = await manager("POST", f"/v1/sessions/{sid}/workspace:swap", "alice", {"checkpoint": "nope"})
    assert status == 404
