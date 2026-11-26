# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Phase 3 end-to-end tests: session integration and AgentTeam.

* Runner.release destroys the session's VM; the idle policy ends in destroy.
* Two users' same-named MCP servers run in their own VMs without colliding.
* Per-session tool credentials reach the guest through MMDS but never disk.
* A 3-member team shares one VM; stop_agent_team keeps it, session switching
  gets a new one, Runner.release leaves nothing behind (LLM-driven).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid

import pytest

from openjiuwen.core.runner import Runner
from openjiuwen.extensions.sys_operation.sandbox.launchers import (
    get_session_info,
    register_session_mcp_server,
    scoped_resource_id,
    set_session_secrets,
)
from tests.test_logger import logger as test_logger
from tests.system_tests.extensions.sys_operation.sandbox.fc_helpers import (
    DOCKER_SOCKET,
    LLM_READY,
    MANAGER_TOKEN,
    MANAGER_URL,
    SUCCESS,
    docker_exec,
    in_session,
    manager,
    sh,
    wait_for_state,
)

pytestmark = pytest.mark.skipif(not MANAGER_URL, reason="needs a running FcSessionManager (FC_MANAGER_URL)")

MCP_COMMAND = "/opt/mcp/bin/python"
MCP_ARGS = ["/opt/fc/demo_mcp_server.py"]


@pytest.mark.asyncio
async def test_runner_release_destroys_the_session_vm(fc):
    sid, op = fc.open("alice")
    await sh(op, sid, "true")
    _, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert info["state"] == "RUNNING"

    await Runner.release(sid)

    status, _ = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert status == 404, "Runner.release must delete the session's microVM"


@pytest.mark.asyncio
async def test_idle_policy_runs_pause_persist_destroy(fc):
    profile = {"vcpus": 1, "mem_mib": 512, "ttl_pause_s": 2, "ttl_persist_s": 2, "ttl_destroy_s": 4}
    sid, op = fc.open("alice", profile=profile)
    await sh(op, sid, "true")
    started = time.monotonic()
    await wait_for_state("alice", sid, "PAUSED", timeout=60)
    await wait_for_state("alice", sid, "PERSISTED", timeout=60)
    deadline = time.monotonic() + 60
    status = 200
    while time.monotonic() < deadline:
        status, _ = await manager("GET", f"/v1/sessions/{sid}", "alice")
        if status == 404:
            break
        await asyncio.sleep(1)
    assert status == 404, "after T3 the persisted session must be destroyed"
    test_logger.info("idle policy reached DESTROYED after %.1fs", time.monotonic() - started)


@pytest.mark.asyncio
async def test_same_named_mcp_servers_of_two_users_run_in_their_own_vms(fc):
    sessions = {"alice": f"mcp-{uuid.uuid4().hex[:6]}", "bob": f"mcp-{uuid.uuid4().hex[:6]}"}
    servers = {}
    for owner, sid in sessions.items():
        fc.sessions.append((owner, sid))
        servers[owner] = await register_session_mcp_server(fc.config(owner), sid, "tools", MCP_COMMAND, MCP_ARGS)

    ids = {owner: s.server_id for owner, s in servers.items()}
    assert ids["alice"] != ids["bob"] and ids["alice"] == scoped_resource_id("alice", sessions["alice"], "tools")
    async def mcp_tool(name: str, server_id: str):
        found = await Runner.resource_mgr.get_mcp_tool(name, server_id=server_id)
        found = found[0] if isinstance(found, list) else found
        assert found is not None, f"{name} tool of {server_id} not found"
        return found

    try:
        for owner, sid in sessions.items():
            text = str(await (await mcp_tool("whoami", ids[owner])).invoke({}))
            assert sid in text and owner in text, f"{owner}'s MCP server answered from the wrong VM: {text}"
            other = sessions["bob" if owner == "alice" else "alice"]
            assert other not in text
            assert "5" in str(await (await mcp_tool("add", ids[owner])).invoke({"a": 2, "b": 3}))
    finally:
        for server in servers.values():
            await Runner.resource_mgr.remove_mcp_server(server_id=server.server_id)

    # The MCP command ran in the VM, not on the agent-core host.
    _, info = await manager("GET", f"/v1/sessions/{sessions['alice']}", "alice")
    assert info["mcp_url"] and servers["alice"].server_path.startswith(info["mcp_url"])
    import aiohttp

    async with aiohttp.ClientSession() as session:  # no token, no access
        async with session.post(info["mcp_url"].split("/t/")[0] + "/servers/tools/mcp", json={}) as resp:
            assert resp.status == 403


@pytest.mark.asyncio
async def test_session_secrets_reach_the_guest_but_never_disk(fc):
    sid, op = fc.open("alice")
    await sh(op, sid, "true")
    secret = f"ghp_{uuid.uuid4().hex}"
    keys = await set_session_secrets(fc.config("alice"), sid, {"GH_TOKEN": secret})
    assert keys == ["GH_TOKEN"]
    read_secret = (
        "T=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-metadata-token-ttl-seconds: 60'); "
        "curl -s -H \"X-metadata-token: $T\" http://169.254.169.254/latest/meta-data/secrets/GH_TOKEN"
    )
    assert (await sh(op, sid, read_secret)).stdout.strip() == secret

    status, info = await manager("POST", f"/v1/sessions/{sid}:persist", "alice")
    assert status == 200 and info["state"] == "PERSISTED"
    assert (await sh(op, sid, read_secret)).stdout.strip() == secret, "secrets are re-delivered after restore"
    _, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert info["secret_keys"] == ["GH_TOKEN"] and secret not in str(info)

    if DOCKER_SOCKET:  # the value must not be in the manager's store or session files
        code, out = await docker_exec(["grep", "-rlF", secret, "/var/lib/fc-sessions/state.db",
                                       f"/var/lib/fc-sessions/sessions/{sid}"])
        assert code == 1 and not out.strip(), f"secret found on disk: {out}"

    assert await set_session_secrets(fc.config("alice"), sid, {"GH_TOKEN": None}) == []
    assert secret not in (await sh(op, sid, read_secret)).stdout, "a removed secret must disappear from MMDS"


# ---------------------------------------------------------------------------- team


def _team_spec(team: str, sysop_id: str, storage_path: str, workspace: str):
    from openjiuwen.agent_teams.schema.blueprint import StorageSpec, TeamAgentSpec, TransportSpec
    from openjiuwen.agent_teams.schema.team import TeamMemberSpec
    from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
    from openjiuwen.core.sys_operation import OperationMode
    from openjiuwen.extensions.sys_operation.sandbox.launchers import build_firecracker_gateway_config
    from openjiuwen.harness.schema.deep_agent_spec import (
        DeepAgentSpec,
        SysOperationSpec,
        TeamModelConfig,
        WorkspaceSpec,
    )

    model = TeamModelConfig(
        model_client_config=ModelClientConfig(client_provider=os.getenv("MODEL_PROVIDER", "OpenAI"),
                                              api_key=os.getenv("API_KEY"), api_base=os.getenv("API_BASE"),
                                              timeout=180),
        model_request_config=ModelRequestConfig(model=os.getenv("MODEL_NAME"), temperature=0.1),
    )
    sysop = SysOperationSpec(
        id=sysop_id, mode=OperationMode.SANDBOX,
        gateway_config=build_firecracker_gateway_config(manager_url=MANAGER_URL, owner_user_id="alice",
                                                        api_token=MANAGER_TOKEN,
                                                        profile={"vcpus": 2, "mem_mib": 1536}),
    )
    member = DeepAgentSpec(model=model, sys_operation=sysop, workspace=WorkspaceSpec(root_path=workspace),
                           max_iterations=12, enable_task_planning=False)
    writers = [TeamMemberSpec(member_name=f"writer-{x}", display_name=f"Writer {x.upper()}",
                              desc="Writes exactly one file with the write_file tool, then completes its task.")
               for x in "abc"]
    return TeamAgentSpec(
        agents={"leader": member.model_copy(deep=True), "teammate": member}, team_name=team,
        spawn_mode="inprocess", transport=TransportSpec(type="inprocess"),
        external_transport=TransportSpec(type="inprocess"), evolution_enabled=False,
        storage=StorageSpec(type="sqlite", params={"connection_string": storage_path}),
        team_mode="predefined", predefined_members=writers,
    )


async def _run_team(spec, query: str, session: str, timeout: float = 900) -> None:
    from openjiuwen.agent_teams.schema.stream import is_team_event_marker

    async def _consume() -> None:
        async for chunk in Runner.run_agent_team_streaming(agent_team=spec, inputs={"query": query},
                                                           session=session):
            if is_team_event_marker(chunk):
                event = getattr(chunk, "payload", {}) or {}
                if str(event.get("event_type", "")) in ("team.completed", "team.idle"):
                    return

    await asyncio.wait_for(_consume(), timeout)


@pytest.mark.skipif(not LLM_READY, reason="needs API_KEY / API_BASE / MODEL_NAME")
@pytest.mark.asyncio
async def test_three_member_team_shares_one_vm_and_leaves_nothing_behind(fc):
    team = f"fcteam{uuid.uuid4().hex[:6]}"
    sid, sid2 = f"{team}-s1", f"{team}-s2"
    tmp = tempfile.mkdtemp(prefix="fcteam-")
    sysop_id = f"fc-team-{team}"
    fc.cards.append(sysop_id)
    fc.sessions += [("alice", sid), ("alice", sid2)]
    spec = _team_spec(team, sysop_id, os.path.join(tmp, "team.sqlite"), os.path.join(tmp, "ws"))
    query = ("Create exactly one task for each teammate and assign it: writer-a must use write_file to create "
             "/workspace/team/a.txt containing exactly 'A'; writer-b the same for /workspace/team/b.txt with 'B'; "
             "writer-c the same for /workspace/team/c.txt with 'C'. Wait until all three tasks are completed, "
             "then reply DONE.")
    await _run_team(spec, query, sid)

    _, listing = await manager("GET", "/v1/sessions", "alice")
    team_vms = [s for s in listing["sessions"] if s["session_id"].startswith(team)]
    assert [s["session_id"] for s in team_vms] == [sid], f"the team must share one VM: {team_vms}"
    # Resolve the members' SysOperation the way a member harness does (get-or-create by spec id).
    op = spec.agents["teammate"].sys_operation.resolve()
    listing_files = await in_session(sid, lambda: op.shell().execute_cmd("ls /workspace/team && cat /workspace/team/*"))
    assert listing_files.code == SUCCESS, listing_files.message
    assert listing_files.data.stdout.split() == ["a.txt", "b.txt", "c.txt", "ABC"] or \
        set(listing_files.data.stdout.split()[:3]) == {"a.txt", "b.txt", "c.txt"}, listing_files.data.stdout

    # Stopping the team runtime (False when it already wound down after completing) keeps the
    # session, and its VM, for a later resume.
    await Runner.stop_agent_team(team_name=team, session_id=sid)
    _, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert info["state"] in ("RUNNING", "PAUSED", "PERSISTED"), info

    # Switching to another session gets that session its own VM.
    await _run_team(spec, "Ask writer-a to create /workspace/team/switch.txt containing 'S' with write_file, "
                          "then reply DONE.", sid2)
    _, listing = await manager("GET", "/v1/sessions", "alice")
    assert {s["session_id"] for s in listing["sessions"] if s["session_id"].startswith(team)} == {sid, sid2}
    other = await in_session(sid2, lambda: op.shell().execute_cmd("ls /workspace/team"))
    assert "a.txt" not in other.data.stdout, "a new session must not see the first session's files"

    # Releasing both sessions leaves no VM behind.
    await Runner.release(sid, force=True)
    await Runner.release(sid2, force=True)
    _, listing = await manager("GET", "/v1/sessions", "alice")
    assert not [s for s in listing["sessions"] if s["session_id"].startswith(team)], listing
