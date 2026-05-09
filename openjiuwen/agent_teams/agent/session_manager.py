# coding: utf-8
"""Session lifecycle and persistence for TeamAgent."""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Optional,
)

from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.core.session.agent_team import Session as AgentTeamSession

if TYPE_CHECKING:
    from openjiuwen.agent_teams.agent.agent_configurator import AgentConfigurator
    from openjiuwen.agent_teams.agent.recovery_manager import RecoveryManager
    from openjiuwen.agent_teams.agent.state import TeamAgentState


class SessionManager:
    """Manages session lifecycle and persistence.

    Responsibilities:
    - Session ID management
    - Team session persistence
    - Session recovery
    """

    def __init__(
        self,
        *,
        state: "TeamAgentState",
        configurator: AgentConfigurator,
        recovery_manager: RecoveryManager,
    ):
        self._state = state
        self._configurator = configurator
        self._recovery_manager = recovery_manager

    @property
    def session_id(self) -> Optional[str]:
        return self._state.session_id

    @session_id.setter
    def session_id(self, value: Optional[str]) -> None:
        self._state.session_id = value

    @property
    def team_session(self) -> Optional[AgentTeamSession]:
        return self._state.team_session

    @team_session.setter
    def team_session(self, value: Optional[AgentTeamSession]) -> None:
        self._state.team_session = value

    async def bind_session(self, session: Optional[AgentTeamSession]) -> None:
        """Bind this team agent to ``session`` (None clears the binding).

        Wires session_id into the state and the global contextvar,
        creates per-session DB tables, and persists leader config when
        the team is leader-side. Idempotent for ``session=None`` —
        repeated unbinds simply leave the agent unbound.
        """
        from openjiuwen.agent_teams.context import set_session_id

        if session is None:
            self._state.session_id = None
            self._state.team_session = None
            return

        self._state.session_id = session.get_session_id()
        set_session_id(self._state.session_id)
        self._state.team_session = session if isinstance(session, AgentTeamSession) else None

        team_backend = self._configurator.team_backend
        if team_backend:
            await team_backend.db.create_cur_session_tables()

        spec = self._configurator.spec
        if spec and self._configurator.role == TeamRole.LEADER:
            self._recovery_manager.persist_leader_config(session)

    def release_team_session(self) -> None:
        """Drop the live ``AgentTeamSession`` reference.

        Used by coordination tear-down to make sure a paused/stopped
        agent does not keep a stale session pinned.
        """
        self._state.team_session = None

    async def resume_for_new_session(self, session) -> None:
        """Switch to a new session and rebind live teammate runtimes.

        Persistent teams keep team rows and old session data intact across
        sessions; only the live runtime needs rebinding so it picks up the
        new session_id.
        """
        recoverable_members = await self._recovery_manager.collect_live_teammates_for_session_switch()
        await self.bind_session(session)

        team_backend = self._configurator.team_backend
        if self._configurator.role != TeamRole.LEADER or not team_backend:
            return

        await self._recovery_manager.restart_for_session_switch(
            recoverable_members,
            cleanup_first=True,
        )

    async def recover_for_existing_session(self, session) -> None:
        """Rebind to a checkpoint-restored session without cleanup.

        Caller must have already torn down coordination (which already
        cleared the live handles) and validated the checkpoint.
        """
        recoverable_members = await self._recovery_manager.collect_live_teammates_for_session_switch()
        await self.bind_session(session)

        team_backend = self._configurator.team_backend
        if self._configurator.role != TeamRole.LEADER or not team_backend:
            return

        await self._recovery_manager.restart_for_session_switch(
            recoverable_members,
            cleanup_first=False,
        )
