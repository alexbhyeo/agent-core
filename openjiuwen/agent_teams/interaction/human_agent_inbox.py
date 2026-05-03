# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Human-agent-side inbox: route a user's input to the team or to the
human agent's own DeepAgent.

Phase-2 HITT splits the inbox into two explicit dispatch paths driven
by the body itself:

* ``@<member> body`` (or ``@all body`` / ``@* body``) → forward to the
  team's message bus exactly like Phase 1 did, with the human-agent
  member as the sender. Mirrors how the user has always addressed the
  team.
* anything else → feed the body straight into the corresponding human
  agent's DeepAgent via ``deliver_input``. The agent uses its tool
  surface (file ops, ``member_complete_task``, workspace meta, etc.)
  to act on the user's behalf.

Explicit routing keeps one message doing one thing: the user knows
whether they are talking to the team or instructing their avatar, the
runtime never speculatively burns LLM tokens on chat traffic, and
mention typos surface as ``unknown_member`` errors instead of
silently falling through to the LLM.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Optional,
)

from openjiuwen.agent_teams.constants import HUMAN_AGENT_MEMBER_NAME
from openjiuwen.agent_teams.interaction.payload import (
    DeliverResult,
    HumanAgentInboundEvent,
)
from openjiuwen.agent_teams.interaction.router import parse_mention
from openjiuwen.core.common.logging import team_logger

if TYPE_CHECKING:
    from openjiuwen.agent_teams.agent.team_agent import TeamAgent
    from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager
    from openjiuwen.agent_teams.tools.team import TeamBackend


_BROADCAST_TARGETS: frozenset[str] = frozenset({"all", "*"})


AgentLookup = Callable[[str], Optional["TeamAgent"]]
"""Resolve a human-agent ``member_name`` to its live ``TeamAgent``
runtime. Returns ``None`` when no live runtime is bound to that
member (cold start, before spawn, or after shutdown)."""


OnInbound = Callable[[HumanAgentInboundEvent], Awaitable[None]]
"""Callback fired when the runtime detects an inbound team-side
message addressed to a human agent. The hook is owned by the SDK /
business layer that wraps the runtime; the inbox itself does not
register subscriptions."""


class HumanAgentNotEnabledError(RuntimeError):
    """Raised when a caller tries to speak as a human agent on a
    team that has no human-agent member registered at all.
    """


class UnknownHumanAgentError(RuntimeError):
    """Raised when ``sender`` is not a registered human-agent member.

    Distinct from ``HumanAgentNotEnabledError``: the team has HITT on,
    but the specific sender the caller picked does not exist.
    """


class HumanAgentInbox:
    """Route human-agent input.

    Holds a reference to ``TeamBackend`` so the entry points can verify
    the team has at least one human agent registered and that the
    chosen ``sender`` is one of them. Unregistered senders raise
    ``UnknownHumanAgentError`` instead of silently injecting a rogue
    identity into the message log.

    Construction-time hooks:

    * ``agent_lookup(sender) -> TeamAgent | None`` — resolves the
      live human-agent runtime so non-mention input drives its LLM.
      Required for the LLM-driven path; ``None`` falls back to a
      ``DeliverResult.failure("agent_unavailable")`` so the caller
      learns the avatar is not running yet.
    * ``on_inbound(HumanAgentInboundEvent)`` — passed through to the
      runtime that wires team→user notifications. ``HumanAgentInbox``
      itself does not call it; ``TeamRuntimeManager`` subscribes to
      ``TeamTopic.MESSAGE`` and forwards events here.
    """

    def __init__(
        self,
        team: "TeamBackend",
        message_manager: "TeamMessageManager",
        *,
        agent_lookup: Optional[AgentLookup] = None,
        on_inbound: Optional[OnInbound] = None,
    ):
        self._team = team
        self._mm = message_manager
        self._agent_lookup = agent_lookup
        self._on_inbound = on_inbound

    @property
    def on_inbound(self) -> Optional[OnInbound]:
        """Return the registered team→user notification callback, if any."""
        return self._on_inbound

    def _resolve_sender(self, sender: Optional[str]) -> str:
        """Pick a sender and verify it is a registered human-agent member.

        Defaults to the first registered human agent when ``sender`` is
        omitted so single-human teams keep the minimal call form
        ``inbox.send(body)`` working.
        """
        names = self._team.human_agent_names()
        if not names:
            raise HumanAgentNotEnabledError(
                "No human-agent member is registered on this team; "
                "create the team with enable_hitt=True or declare "
                "TeamMemberSpec(role_type=TeamRole.HUMAN_AGENT, ...) "
                "entries in predefined_members"
            )
        if sender is None:
            # Deterministic default: the reserved name if present,
            # otherwise the lexicographically first registered one.
            if HUMAN_AGENT_MEMBER_NAME in names:
                return HUMAN_AGENT_MEMBER_NAME
            return sorted(names)[0]
        if sender not in names:
            raise UnknownHumanAgentError(
                f"'{sender}' is not a registered human-agent member; registered members: {sorted(names)}"
            )
        return sender

    async def send(
        self,
        body: str,
        to: Optional[str] = None,
        *,
        sender: Optional[str] = None,
    ) -> DeliverResult:
        """Dispatch one human-agent input.

        Routing rules (in order):

        1. ``to`` is provided → forward to the team via point-to-point
           ``send_message`` (Phase 1 contract preserved for callers
           that bypass mention parsing).
        2. body starts with ``@<target> body`` → forward to the team:
           ``@all`` / ``@*`` broadcast; otherwise single-member
           direct message. Unknown member names return
           ``DeliverResult.failure("unknown_member")``.
        3. otherwise → feed the body to the human agent's DeepAgent
           via ``deliver_input``. Returns
           ``DeliverResult.failure("agent_unavailable")`` when no
           ``agent_lookup`` is wired or the avatar is not running.

        Args:
            body: Message content (may include ``@<target>`` prefix).
            to: Optional explicit point-to-point target. Bypasses
                mention parsing when set.
            sender: Member name of the human agent speaking. Optional
                on single-human teams; required when the team declares
                multiple human-agent members.

        Raises:
            HumanAgentNotEnabledError: When the team has no registered
                human-agent member at all.
            UnknownHumanAgentError: When ``sender`` does not match any
                registered human-agent member.
        """
        resolved_sender = self._resolve_sender(sender)
        team_logger.debug(
            "HumanAgentInbox: sender=%s, to=%s, body_len=%d",
            resolved_sender,
            to or "<auto>",
            len(body or ""),
        )

        if to is not None:
            return await self._forward_direct(body, target=to, sender=resolved_sender)

        mention = parse_mention(body)
        if mention is not None:
            target, stripped = mention
            if target in _BROADCAST_TARGETS:
                return await self._forward_broadcast(stripped, sender=resolved_sender)
            return await self._forward_direct(stripped, target=target, sender=resolved_sender)

        return await self._drive_agent(body, sender=resolved_sender)

    # ------------------------------------------------------------------
    # Routing primitives
    # ------------------------------------------------------------------

    async def _forward_broadcast(self, body: str, *, sender: str) -> DeliverResult:
        msg_id = await self._mm.broadcast_message(content=body, from_member_name=sender)
        if msg_id is None:
            return DeliverResult.failure("send_failed")
        return DeliverResult.success(msg_id)

    async def _forward_direct(self, body: str, *, target: str, sender: str) -> DeliverResult:
        # Validate target against the live roster — typos or stale member
        # names should fail loudly here rather than producing an orphan
        # message row that nobody consumes.
        member = await self._team.get_member(target)
        if member is None:
            team_logger.warning(
                "HumanAgentInbox: unknown mention target '%s' from sender '%s'",
                target,
                sender,
            )
            return DeliverResult.failure("unknown_member")
        msg_id = await self._mm.send_message(
            content=body,
            to_member_name=target,
            from_member_name=sender,
        )
        if msg_id is None:
            return DeliverResult.failure("send_failed")
        return DeliverResult.success(msg_id)

    async def _drive_agent(self, body: str, *, sender: str) -> DeliverResult:
        if self._agent_lookup is None:
            team_logger.warning(
                "HumanAgentInbox: no agent_lookup wired; cannot deliver input to human agent %s",
                sender,
            )
            return DeliverResult.failure("agent_unavailable")
        agent = self._agent_lookup(sender)
        if agent is None:
            team_logger.warning("HumanAgentInbox: human agent %s has no live runtime", sender)
            return DeliverResult.failure("agent_unavailable")
        await agent.deliver_input(body)
        return DeliverResult.success(None)


__all__ = [
    "AgentLookup",
    "HumanAgentInbox",
    "HumanAgentInboundEvent",
    "HumanAgentNotEnabledError",
    "OnInbound",
    "UnknownHumanAgentError",
]
