# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structured payloads and result types for ``interact_team``.

The runtime exposes three interaction perspectives:

* **God view** — speak directly to the team's leader DeepAgent. Equivalent
  to the historical ``invoke``/``deliver_to_leader`` channel.
* **Operator view** — speak as the external user, addressing one member
  with ``@member_name`` semantics or the whole team via broadcast.
* **Direct-control view** — speak as a registered human-agent team
  member. Routed through ``HumanAgentInbox`` and gated by HITT.

Each interact call carries one concrete payload. ``DeliverResult``
captures the outcome uniformly so callers do not need to special-case
return types per channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Optional,
    Union,
)


@dataclass(frozen=True, slots=True)
class GodViewMessage:
    """Speak directly to the team's leader DeepAgent."""

    body: str


@dataclass(frozen=True, slots=True)
class OperatorMessage:
    """Speak as the external user.

    Attributes:
        body: Message content.
        target: Member name to address; ``None`` broadcasts to the team.
    """

    body: str
    target: Optional[str] = None


@dataclass(frozen=True, slots=True)
class HumanAgentMessage:
    """Speak as a registered human-agent team member.

    Attributes:
        body: Message content.
        sender: Member name of the human-agent speaking.
        target: Target member; ``None`` broadcasts to the team.
    """

    body: str
    sender: str
    target: Optional[str] = None


InteractPayload = Union[GodViewMessage, OperatorMessage, HumanAgentMessage]
"""Discriminated union of supported interact payload shapes."""


@dataclass(frozen=True, slots=True)
class DeliverResult:
    """Outcome of a single payload delivery.

    On success ``ok`` is True and ``message_id`` carries the assigned
    message id (``None`` for channels that do not produce a message,
    e.g. ``deliver_to_leader``). On failure ``ok`` is False and ``reason``
    carries a short stable token suitable for surfacing to the caller
    (``human_agent_not_enabled``, ``unknown_human_agent``, ``send_failed``,
    ...).
    """

    ok: bool
    message_id: Optional[str] = None
    reason: Optional[str] = None

    @classmethod
    def success(cls, message_id: Optional[str] = None) -> "DeliverResult":
        """Build a success result, optionally carrying a message id."""
        return cls(ok=True, message_id=message_id)

    @classmethod
    def failure(cls, reason: str) -> "DeliverResult":
        """Build a failure result with a short reason token."""
        return cls(ok=False, reason=reason)


__all__ = [
    "DeliverResult",
    "GodViewMessage",
    "HumanAgentMessage",
    "InteractPayload",
    "OperatorMessage",
]
