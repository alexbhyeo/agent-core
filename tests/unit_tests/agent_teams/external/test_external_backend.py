# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for TeamBackend.spawn_external_cli_agent registration."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.messager import Messager
from openjiuwen.agent_teams.tools.database import DatabaseConfig, DatabaseType, TeamDatabase
from openjiuwen.agent_teams.tools.team import TeamBackend

_TEAM = "ext_cli_team"


@pytest_asyncio.fixture
async def backend():
    token = set_session_id("sess")
    db = TeamDatabase(DatabaseConfig(db_type=DatabaseType.SQLITE, connection_string=":memory:"))
    await db.initialize()
    await db.team.create_team(team_name=_TEAM, display_name="Ext CLI", leader_member_name="leader")
    messager = AsyncMock(spec=Messager)
    yield TeamBackend(team_name=_TEAM, member_name="leader", db=db, messager=messager, is_leader=True)
    reset_session_id(token)
    await db.close()


@pytest.mark.asyncio
@pytest.mark.level0
async def test_spawn_external_cli_agent_registers_member(backend):
    result = await backend.spawn_external_cli_agent(
        member_name="cli-1",
        display_name="CLI One",
        cli_agent="claude",
        persona="senior reviewer",
    )
    assert result.ok, result.reason
    assert backend.is_external_cli_agent("cli-1")
    assert backend.get_external_cli_agent("cli-1") == "claude"
    assert "cli-1" in backend.external_cli_agent_names()

    member = await backend.get_member("cli-1")
    assert member is not None
    assert member.role == "teammate"


@pytest.mark.asyncio
@pytest.mark.level1
async def test_spawn_external_cli_agent_unknown_adapter_fails(backend):
    result = await backend.spawn_external_cli_agent(
        member_name="cli-2",
        display_name="CLI Two",
        cli_agent="not-a-real-cli",
        persona="x",
    )
    assert not result.ok
    assert not backend.is_external_cli_agent("cli-2")


@pytest.mark.asyncio
@pytest.mark.level1
async def test_spawn_external_cli_agent_requires_persona(backend):
    result = await backend.spawn_external_cli_agent(
        member_name="cli-3",
        display_name="CLI Three",
        cli_agent="claude",
        persona="",
    )
    assert not result.ok
    assert not backend.is_external_cli_agent("cli-3")


@pytest.mark.asyncio
@pytest.mark.level0
async def test_non_external_member_returns_none(backend):
    assert backend.get_external_cli_agent("nobody") is None
    assert not backend.is_external_cli_agent("nobody")
