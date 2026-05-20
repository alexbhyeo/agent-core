# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the team-member MCP server tool surface."""

import pytest

from openjiuwen.agent_teams.external import ExternalTeamClient
from openjiuwen.agent_teams.mcp.server import build_server
from openjiuwen.agent_teams.schema.status import TaskStatus

_EXPECTED_TOOLS = {
    "read_inbox",
    "send_message",
    "list_tasks",
    "claimable_tasks",
    "get_task",
    "claim_task",
    "complete_task",
    "update_task",
    "list_members",
}


def _factory(make_descriptor, member: str = "dev-1", role: str = "teammate"):
    async def _connect() -> ExternalTeamClient:
        client = ExternalTeamClient(make_descriptor(member=member, role=role))
        await client.connect()
        return client

    return _connect


@pytest.mark.asyncio
@pytest.mark.level0
async def test_server_registers_expected_tools(team_db, make_descriptor):
    server = build_server(_factory(make_descriptor))
    names = {tool.name for tool in await server.list_tools()}
    assert _EXPECTED_TOOLS <= names


@pytest.mark.asyncio
@pytest.mark.level0
async def test_send_message_tool_delivers(team_db, make_descriptor):
    server = build_server(_factory(make_descriptor, member="dev-1"))
    await server.call_tool("send_message", {"to": "leader", "content": "hi via mcp"})

    async with ExternalTeamClient(make_descriptor(member="leader", role="leader")) as leader:
        inbox = await leader.fetch_inbox()
        assert any(m.content == "hi via mcp" for m in inbox.messages)


@pytest.mark.asyncio
@pytest.mark.level0
async def test_claim_task_tool_effect(team_db, make_descriptor):
    await team_db.task.create_task(
        task_id="t1",
        team_name="ext_team",
        title="Do X",
        content="details",
        status=TaskStatus.PENDING.value,
    )
    server = build_server(_factory(make_descriptor, member="dev-1"))
    await server.call_tool("claim_task", {"task_id": "t1"})

    task = await team_db.task.get_task("t1")
    assert task.assignee == "dev-1"
    assert task.status == TaskStatus.CLAIMED.value
