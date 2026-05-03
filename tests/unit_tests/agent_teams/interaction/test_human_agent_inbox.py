# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verify HumanAgentInbox routes input by ``@`` mention vs LLM dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from openjiuwen.agent_teams.context import (
    reset_session_id,
    set_session_id,
)
from openjiuwen.agent_teams.interaction.human_agent_inbox import (
    HumanAgentInbox,
    HumanAgentNotEnabledError,
    UnknownHumanAgentError,
)
from openjiuwen.agent_teams.messager import Messager
from openjiuwen.agent_teams.schema.status import MemberMode
from openjiuwen.agent_teams.schema.team import (
    TeamMemberSpec,
    TeamRole,
)
from openjiuwen.agent_teams.tools.database import (
    DatabaseConfig,
    DatabaseType,
    TeamDatabase,
)
from openjiuwen.agent_teams.tools.team import TeamBackend
from openjiuwen.core.single_agent import AgentCard

HUMAN = "human_alice"
TEAMMATE = "dev_bob"


@pytest_asyncio.fixture
async def db():
    token = set_session_id("inbox_session")
    config = DatabaseConfig(db_type=DatabaseType.SQLITE, connection_string=":memory:")
    database = TeamDatabase(config)
    try:
        await database.initialize()
        await database.team.create_team(
            team_name="hitt_team",
            display_name="HITT",
            leader_member_name="team_leader",
        )
        for name in (HUMAN, TEAMMATE):
            await database.member.create_member(
                member_name=name,
                team_name="hitt_team",
                display_name=name,
                agent_card=AgentCard().model_dump_json(),
                status="READY",
                mode=MemberMode.BUILD_MODE.value,
            )
        yield database
    finally:
        await database.close()
        reset_session_id(token)


@pytest_asyncio.fixture
async def messager():
    yield AsyncMock(spec=Messager)


@pytest_asyncio.fixture
async def team_backend(db, messager):
    backend = TeamBackend(
        team_name="hitt_team",
        member_name="team_leader",
        is_leader=True,
        db=db,
        messager=messager,
        predefined_members=[
            TeamMemberSpec(
                member_name=HUMAN,
                display_name="Alice",
                role_type=TeamRole.HUMAN_AGENT,
                persona="user avatar",
            ),
        ],
    )
    yield backend


@pytest.mark.asyncio
@pytest.mark.level0
async def test_send_no_mention_drives_human_agent(team_backend):
    """No mention prefix → body is fed to the avatar's DeepAgent."""
    avatar = AsyncMock()
    inbox = HumanAgentInbox(
        team_backend,
        team_backend.message_manager,
        agent_lookup=lambda name: avatar if name == HUMAN else None,
    )

    result = await inbox.send("read design.md and summarise it")

    assert result.ok
    assert result.message_id is None
    avatar.deliver_input.assert_awaited_once_with("read design.md and summarise it")


@pytest.mark.asyncio
@pytest.mark.level0
async def test_send_mention_at_member_forwards_direct(team_backend, db):
    """``@<member> body`` forwards to the team via point-to-point send."""
    avatar = AsyncMock()
    inbox = HumanAgentInbox(
        team_backend,
        team_backend.message_manager,
        agent_lookup=lambda name: avatar,
    )

    result = await inbox.send(f"@{TEAMMATE} ping me when done")
    assert result.ok
    assert result.message_id is not None
    avatar.deliver_input.assert_not_called()

    messages = await team_backend.message_manager.get_messages(to_member_name=TEAMMATE)
    assert any(m.from_member_name == HUMAN and "ping me when done" in m.content for m in messages)


@pytest.mark.asyncio
@pytest.mark.level0
async def test_send_mention_at_all_broadcasts(team_backend, db):
    """``@all body`` and ``@* body`` both broadcast as the human member."""
    avatar = AsyncMock()
    inbox = HumanAgentInbox(
        team_backend,
        team_backend.message_manager,
        agent_lookup=lambda name: avatar,
    )

    result = await inbox.send("@all status sync")
    assert result.ok
    avatar.deliver_input.assert_not_called()

    star = await inbox.send("@* heads up")
    assert star.ok

    casts = await team_backend.message_manager.get_team_messages("hitt_team")
    bodies = {m.content for m in casts if m.broadcast and m.from_member_name == HUMAN}
    assert "status sync" in bodies
    assert "heads up" in bodies


@pytest.mark.asyncio
@pytest.mark.level0
async def test_send_mention_unknown_target_returns_unknown_member(team_backend):
    """Mention typo must surface as a stable failure code, not LLM input."""
    avatar = AsyncMock()
    inbox = HumanAgentInbox(
        team_backend,
        team_backend.message_manager,
        agent_lookup=lambda name: avatar,
    )

    result = await inbox.send("@ghost hi")
    assert not result.ok
    assert result.reason == "unknown_member"
    avatar.deliver_input.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.level0
async def test_send_explicit_to_param_bypasses_mention_parsing(team_backend, db):
    """The legacy ``to=...`` Phase-1 surface still routes to the team."""
    avatar = AsyncMock()
    inbox = HumanAgentInbox(
        team_backend,
        team_backend.message_manager,
        agent_lookup=lambda name: avatar,
    )

    result = await inbox.send("just a heads up", to=TEAMMATE)

    assert result.ok
    assert result.message_id is not None
    avatar.deliver_input.assert_not_called()
    messages = await team_backend.message_manager.get_messages(to_member_name=TEAMMATE)
    assert any("just a heads up" in m.content for m in messages)


@pytest.mark.asyncio
@pytest.mark.level0
async def test_send_no_agent_lookup_returns_agent_unavailable(team_backend):
    """No mention + no agent_lookup → the avatar is reported unavailable."""
    inbox = HumanAgentInbox(
        team_backend,
        team_backend.message_manager,
    )

    result = await inbox.send("do the thing")
    assert not result.ok
    assert result.reason == "agent_unavailable"


@pytest.mark.asyncio
@pytest.mark.level0
async def test_send_unknown_sender_raises(team_backend):
    """An unregistered ``sender`` must raise instead of silently routing."""
    avatar = AsyncMock()
    inbox = HumanAgentInbox(
        team_backend,
        team_backend.message_manager,
        agent_lookup=lambda name: avatar,
    )

    with pytest.raises(UnknownHumanAgentError):
        await inbox.send("hi", sender="nope")


@pytest.mark.asyncio
@pytest.mark.level0
async def test_send_no_human_agent_registered_raises(db, messager):
    """An empty human-agent roster makes any send call illegal."""
    backend = TeamBackend(
        team_name="hitt_team",
        member_name="team_leader",
        is_leader=True,
        db=db,
        messager=messager,
    )
    inbox = HumanAgentInbox(backend, backend.message_manager)

    with pytest.raises(HumanAgentNotEnabledError):
        await inbox.send("hello")


@pytest.mark.level0
def test_register_human_agent_inbound_unknown_member_raises(team_backend):
    """Registering against an unknown member must fail loudly."""
    with pytest.raises(KeyError):
        team_backend.register_human_agent_inbound("ghost", lambda evt: None)


@pytest.mark.level0
def test_register_human_agent_inbound_clear(team_backend):
    """Passing ``None`` clears a prior registration."""

    def cb(evt):
        return None

    team_backend.register_human_agent_inbound(HUMAN, cb)
    assert team_backend.get_human_agent_inbound(HUMAN) is cb

    team_backend.register_human_agent_inbound(HUMAN, None)
    assert team_backend.get_human_agent_inbound(HUMAN) is None
