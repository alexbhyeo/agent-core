# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Phase 5 end-to-end tests: tiers and rate limits, balloon, CPU templates, egress audit and
allowlist, sealed snapshots, warm pool, metrics / alerts, agent-core LLM-only egress.

The manager must run with (see deploy/firecracker/README.md)::

    FCM_TIERS='{"e2e-slow": {"disk_mbps": 20, "disk_iops": 1000, "net_mbps": 20, "max_sessions": 2}}'
    FCM_USER_TIERS='{"noisy": "e2e-slow", "fenced": "restricted"}'
    FCM_WARM_POOL_SIZE=2
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

import aiohttp
import pytest

from tests.system_tests.extensions.sys_operation.sandbox.fc_helpers import (
    DOCKER_SOCKET,
    MANAGER_TOKEN,
    MANAGER_URL,
    docker_exec,
    docker_run,
    manager,
    sh,
    wait_for_state,
)
from tests.test_logger import logger as test_logger

pytestmark = pytest.mark.skipif(not MANAGER_URL, reason="needs FC_MANAGER_URL (a running FcSessionManager)")
needs_docker = pytest.mark.skipif(not DOCKER_SOCKET, reason="needs FC_E2E_DOCKER=1 (Docker socket)")
REPO = Path(__file__).resolve().parents[5]


async def _metrics_text() -> str:
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{MANAGER_URL}/metrics", headers={"Authorization": f"Bearer {MANAGER_TOKEN}"}) as r:
            assert r.status == 200
            return await r.text()


async def _gauge(name: str) -> float:
    for line in (await _metrics_text()).splitlines():
        if line.startswith(f"fc_{name} "):
            return float(line.split()[1])
    return 0.0


async def _timed_dd(op, sid: str, mib: int) -> float:
    """Write ``mib`` MiB with O_DIRECT to the workspace drive; returns MiB/s."""
    out = await sh(op, sid, f"s=$(date +%s.%N); dd if=/dev/zero of=/workspace/dd.bin bs=1M count={mib} "
                            "oflag=direct status=none; e=$(date +%s.%N); rm -f /workspace/dd.bin; echo $s $e")
    start, end = map(float, out.stdout.split()[-2:])
    return mib / (end - start)


# --------------------------------------------------------------------- tiers / rate limits


@pytest.mark.asyncio
async def test_tier_rate_limits_keep_a_noisy_neighbour_in_check(fc):
    noisy, noisy_op = fc.open("noisy")
    calm, calm_op = fc.open("alice")
    await asyncio.gather(sh(noisy_op, noisy, "true"), sh(calm_op, calm, "true"))
    _, info = await manager("GET", f"/v1/sessions/{noisy}", "noisy")
    assert info["tier"] == "e2e-slow"

    noisy_rate, calm_rate = await asyncio.gather(_timed_dd(noisy_op, noisy, 160), _timed_dd(calm_op, calm, 160))
    test_logger.info("direct writes in parallel: limited VM %.1f MiB/s, neighbour %.1f MiB/s", noisy_rate, calm_rate)
    assert noisy_rate <= 20 * 1.25, f"the 20 MiB/s drive limit was not applied ({noisy_rate:.1f} MiB/s)"
    assert calm_rate > 2 * noisy_rate, "the neighbour must not be held to the noisy VM's limit"

    # The tier allows two kept sessions; a third is refused until one is deleted.
    second = f"e2e-{uuid.uuid4().hex[:8]}"
    fc.sessions.append(("noisy", second))
    status, _ = await manager("POST", "/v1/sessions", "noisy", {"session_id": second})
    assert status == 201
    status, body = await manager("POST", "/v1/sessions", "noisy", {"session_id": f"e2e-{uuid.uuid4().hex[:8]}"})
    assert status == 429, body
    status, body = await manager("POST", "/v1/sessions", "noisy",
                                 {"session_id": f"e2e-{uuid.uuid4().hex[:8]}", "profile": {"vcpus": 8}})
    assert status in (400, 429), body


# --------------------------------------------------------------------------- balloon


@pytest.mark.asyncio
async def test_balloon_returns_idle_memory_and_gives_it_back_before_oom(fc):
    sid, op = fc.open("alice")
    await sh(op, sid, "true")
    deadline, stats = time.monotonic() + 90, {}
    while time.monotonic() < deadline:
        _, dbg = await manager("GET", f"/v1/sessions/{sid}/debug", "alice")
        stats = dbg.get("balloon", {})
        if stats.get("target_mib", 0) >= 256:
            break
        await asyncio.sleep(2)
    assert stats.get("target_mib", 0) >= 256, f"idle guest memory was not reclaimed: {stats}"
    inflated = stats["target_mib"]

    # Allocate and touch more than the guest has left with the balloon inflated.
    hog = ("python3 -c \"b = bytearray(700 * 2**20)\n"
           "for i in range(0, len(b), 4096): b[i] = 1\n"
           "print('allocated', len(b) >> 20)\"; dmesg | grep -ci 'killed process' || true")
    out = (await sh(op, sid, hog, timeout=300)).stdout.split()
    assert out[:2] == ["allocated", "700"], out
    assert out[-1] == "0", "the guest must not OOM-kill while the balloon can deflate"
    _, dbg = await manager("GET", f"/v1/sessions/{sid}/debug", "alice")
    test_logger.info("balloon: inflated to %d MiB while idle, %s MiB right after the 700 MiB allocation",
                     inflated, dbg.get("balloon", {}).get("actual_mib"))


# ---------------------------------------------------------------------- CPU templates


@pytest.mark.asyncio
async def test_session_chooses_a_cpu_template_that_survives_persist(fc):
    status, body = await manager("POST", "/v1/sessions", "alice",
                                 {"session_id": f"e2e-{uuid.uuid4().hex[:8]}", "profile": {"cpu_template": "Z9"}})
    assert status == 400 and "cpu_template" in body["error"]
    sid = f"e2e-{uuid.uuid4().hex[:8]}"
    fc.sessions.append(("alice", sid))
    status, info = await manager("POST", "/v1/sessions", "alice",
                                 {"session_id": sid, "profile": {"vcpus": 2, "mem_mib": 1024, "cpu_template": "T2S"}})
    assert status == 201, info
    assert info["cpu_template"] == "T2S"
    op = fc.user_op("alice")
    assert (await sh(op, sid, "grep -c avx512 /proc/cpuinfo || true")).stdout.strip() == "0"
    await manager("POST", f"/v1/sessions/{sid}:persist", "alice")
    _, dbg = await manager("GET", f"/v1/sessions/{sid}/debug", "alice")
    assert dbg["snapshot"]["cpu_template"] == "T2S"
    assert (await sh(op, sid, "echo back")).stdout.strip() == "back"


# ------------------------------------------------------------------------- egress


@pytest.mark.asyncio
async def test_guest_connections_are_audited(fc):
    sid, op = fc.open("alice")
    await sh(op, sid, "curl -s -o /dev/null -m 20 https://example.com || true")
    await asyncio.sleep(1)
    _, log = await manager("GET", f"/v1/sessions/{sid}/egress", "alice")
    assert log["audit"] is True
    assert any(e["kind"] == "connect" and e["dport"] == 443 for e in log["entries"]), log
    status, _ = await manager("GET", f"/v1/sessions/{sid}/egress", "bob")
    assert status == 403
    if DOCKER_SOCKET:
        code, out = await docker_exec(["sh", "-c", f"grep -h '\"{sid}\"' /var/lib/fc-sessions/audit/*.jsonl | wc -l"])
        assert code == 0 and int(out.strip()) >= 1, out


@pytest.mark.asyncio
async def test_restricted_tier_reaches_only_allowlisted_domains(fc):
    status, body = await manager("POST", "/v1/sessions", "fenced",
                                 {"session_id": f"e2e-{uuid.uuid4().hex[:8]}",
                                  "profile": {"egress_allowlist": ["example.com"]}})
    assert status == 400, "a profile cannot widen the tier's allowlist"
    sid, op = fc.open("fenced")
    await sh(op, sid, "true")
    _, info = await manager("GET", f"/v1/sessions/{sid}", "fenced")
    assert info["egress"] == "allowlist" and "pypi.org" in info["egress_allowlist"]

    code_of = "curl -s -o /dev/null -m 20 -w '%{http_code} ' "
    out = (await sh(op, sid, f"{code_of} https://pypi.org/simple/six/; {code_of} https://example.com; "
                             f"{code_of} --noproxy '*' https://1.1.1.1; echo")).stdout.split()
    assert out[0] == "200", f"allowlisted domain through the proxy: {out}"
    assert out[1] == "000", f"non-allowlisted domain must be refused: {out}"
    assert out[2] == "000", f"direct egress must be dropped: {out}"
    pip = await sh(op, sid, "python3 -m venv /tmp/v && /tmp/v/bin/pip install -q six && "
                            "/tmp/v/bin/python -c 'import six; print(\"six-ok\")'", timeout=300)
    assert "six-ok" in pip.stdout, pip.stdout

    _, log = await manager("GET", f"/v1/sessions/{sid}/egress", "fenced")
    proxied = [e for e in log["entries"] if e["kind"] == "proxy"]
    assert any(e["host"] == "pypi.org" and e["allowed"] for e in proxied), proxied
    assert any(e["host"] == "example.com" and not e["allowed"] for e in proxied), proxied

    # The restriction holds across persist / restore (new slot, rules applied again).
    await manager("POST", f"/v1/sessions/{sid}:persist", "fenced")
    out = (await sh(op, sid, f"{code_of} https://pypi.org/simple/six/; {code_of} --noproxy '*' https://1.1.1.1; "
                             "echo")).stdout.split()
    assert out == ["200", "000"], out


# ---------------------------------------------------------------------- sealed snapshots


@needs_docker
@pytest.mark.asyncio
async def test_cold_snapshots_are_compressed_and_encrypted(fc):
    sid = f"e2e-{uuid.uuid4().hex[:8]}"
    fc.sessions.append(("alice", sid))
    status, _ = await manager("POST", "/v1/sessions", "alice",
                              {"session_id": sid, "profile": {"vcpus": 2, "mem_mib": 1024,
                                                              "snapshot_seal_after_s": 8}})
    assert status == 201
    op = fc.user_op("alice")
    marker = f"fc-marker-{uuid.uuid4().hex}"
    await sh(op, sid, f"echo {marker} > /dev/shm/marker")
    await manager("POST", f"/v1/sessions/{sid}:persist", "alice")
    _, dbg = await manager("GET", f"/v1/sessions/{sid}/debug", "alice")
    snap = f"/var/lib/fc-sessions/sessions/{sid}/snapshots/gen-{dbg['snapshot']['generation']}"
    code, out = await docker_exec(["grep", "-c", "-a", marker, f"{snap}/mem"])
    assert code == 0 and int(out.strip()) >= 1, "the clear-text snapshot holds the guest's RAM (test validity)"

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        _, dbg = await manager("GET", f"/v1/sessions/{sid}/debug", "alice")
        files = dbg.get("snapshot_files", {})
        if "mem.sealed" in files and "mem" not in files and "sealed" in dbg.get("snapshot", {}):
            break
        await asyncio.sleep(2)
    files = dbg["snapshot_files"]
    assert "mem.sealed" in files and "mem" not in files, files
    assert dbg["snapshot"]["sealed"]["encrypt"] == "aes-256-ctr"
    assert files["mem.sealed"] < 1024 * 2 ** 20 / 2, "zero pages must compress away"
    code, _ = await docker_exec(["grep", "-c", "-a", marker, f"{snap}/mem.sealed"])
    assert code == 1, "the sealed snapshot must not contain clear text"
    code, out = await docker_exec(["stat", "-c", "%a", "/var/lib/fc-sessions/keys/snapshot.key",
                                   "/var/lib/fc-sessions"])
    assert out.split() == ["600", "700"], out

    assert (await sh(op, sid, "cat /dev/shm/marker")).stdout.strip() == marker  # unsealed on wake
    _, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert info["state"] == "RUNNING"


# ------------------------------------------------------------------------ warm pool


@pytest.mark.asyncio
async def test_warm_pool_makes_creates_take_under_a_second(fc):
    op = fc.user_op("alice")
    durations, boot_ids = [], set()
    for _ in range(5):
        deadline = time.monotonic() + 120
        while await _gauge("warm_pool_ready") < 1 and time.monotonic() < deadline:
            await asyncio.sleep(1)
        sid = f"e2e-{uuid.uuid4().hex[:8]}"
        fc.sessions.append(("alice", sid))
        started = time.monotonic()
        status, info = await manager("POST", "/v1/sessions", "alice",
                                     {"session_id": sid, "profile": {"vcpus": 2, "mem_mib": 1024}})
        durations.append(time.monotonic() - started)
        assert status == 201, info
        read_id = ("T=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-metadata-token-ttl-seconds: 60');"
                   " curl -s -H \"X-metadata-token: $T\" http://169.254.169.254/latest/meta-data/session_id; echo;"
                   " cat /proc/sys/kernel/random/boot_id")
        out = (await sh(op, sid, read_id)).stdout.split()
        assert out[0] == sid, "a pooled VM must take on the new session's identity"
        boot_ids.add(out[1])
    durations.sort()
    p95 = durations[int(round(0.95 * (len(durations) - 1)))]
    test_logger.info("create with warm pool: %s s (P95 %.2f s)", [round(d, 2) for d in durations], p95)
    assert p95 < 1.0
    assert len(boot_ids) == 5, "every session gets its own VM"
    assert 'fc_sessions_created_total{source="warm_pool"}' in await _metrics_text()


# ------------------------------------------------------------------- metrics / alerts


@pytest.mark.asyncio
async def test_metrics_and_alerts_are_exposed_to_the_operator(fc):
    sid, op = fc.open("alice")
    await sh(op, sid, "true")
    text = await _metrics_text()
    for name in ('fc_session_create_seconds{quantile="0.95"}', "fc_vm_slots_used", "fc_host_mem_available_mib",
                 'fc_sessions{state="RUNNING"}', "fc_warm_pool_target"):
        assert name in text, name
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{MANAGER_URL}/metrics") as resp:
            assert resp.status == 401
    status, body = await manager("GET", "/v1/alerts", "alice")
    assert status == 200 and isinstance(body["alerts"], list)
    for alert in body["alerts"]:
        assert {"name", "severity", "message"} <= set(alert)


# -------------------------------------------------------------- agent-core egress


@needs_docker
@pytest.mark.skipif(not os.getenv("API_BASE"), reason="needs API_BASE (the LLM endpoint)")
@pytest.mark.asyncio
async def test_agent_core_container_reaches_only_the_llm_and_the_manager():
    llm_host = os.environ["API_BASE"].split("://", 1)[-1].split("/", 1)[0]
    script = (REPO / "deploy/firecracker/scripts/agent_core_egress.sh").read_text()
    checks = (
        "\nset +e\n"
        "echo llm=$(curl -s -o /dev/null -m 20 -w '%{http_code}' https://" + llm_host + "/)\n"
        "echo other=$(curl -s -o /dev/null -m 10 -w '%{http_code}' https://example.com/)\n"
        "echo manager=$(curl -s -o /dev/null -m 10 -w '%{http_code}' http://fc-manager:8700/healthz)\n"
    )
    code, out = await docker_run("fc-manager:dev", ["bash", "-c"], [script + checks, "egress", llm_host],
                                 cap_add=["NET_ADMIN"])
    test_logger.info("agent-core egress check: %s", out.strip().splitlines()[-3:])
    results = dict(line.split("=", 1) for line in out.splitlines() if line.startswith(("llm=", "other=", "manager=")))
    assert code == 0, out
    assert results["llm"] not in ("", "000"), out
    assert results["other"] == "000", out
    assert results["manager"] == "200", out
