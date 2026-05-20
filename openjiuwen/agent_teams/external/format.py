# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure-function rendering of inbound team context for external agents.

These helpers turn raw team models (messages, tasks) into the same
human-readable text an in-process member would receive through its
coordination handlers, reusing the shared ``i18n`` strings so an external
agent sees identical wording. No I/O, no LLM calls — fully deterministic
and unit-testable.
"""

from __future__ import annotations

from typing import Protocol

from openjiuwen.agent_teams.i18n import t

# Task statuses that should not appear on the actionable task board.
_TERMINAL_TASK_STATUSES = frozenset({"completed", "cancelled"})


class _MessageLike(Protocol):
    """Structural view of a team message row used for rendering."""

    message_id: str
    from_member_name: str
    content: str
    broadcast: bool


class _TaskLike(Protocol):
    """Structural view of a team task row used for rendering."""

    task_id: str
    title: str
    content: str
    status: str
    assignee: str | None


def render_message(message: _MessageLike) -> str:
    """Render one inbound message exactly like the in-process dispatcher.

    Args:
        message: A team message row (direct or broadcast).

    Returns:
        Localised text mirroring ``dispatcher.msg_received``.
    """
    msg_type = t("dispatcher.msg_type_broadcast") if message.broadcast else t("dispatcher.msg_type_direct")
    return t(
        "dispatcher.msg_received",
        msg_type=msg_type,
        message_id=message.message_id,
        sender=message.from_member_name,
        content=message.content,
    )


def render_messages(messages: list[_MessageLike]) -> str:
    """Render a batch of inbound messages, newest-handling left to caller."""
    return "\n\n".join(render_message(m) for m in messages)


def render_task_board(tasks: list[_TaskLike], *, is_leader: bool) -> str:
    """Render the actionable task board for an idle member.

    Mirrors ``TaskBoardHandler._nudge_idle_agent``: a role-specific header
    followed by one line per non-terminal task.

    Args:
        tasks: All team tasks; terminal ones are filtered out here.
        is_leader: Whether the viewer is the leader (changes the header).

    Returns:
        Localised task-board text, or an empty string when nothing is
        actionable.
    """
    incomplete = [task for task in tasks if task.status not in _TERMINAL_TASK_STATUSES]
    if not incomplete:
        return ""

    header = t("dispatcher.leader_task_board") if is_leader else t("dispatcher.teammate_task_list")
    lines = [header]
    for task in incomplete:
        if task.assignee:
            assignee = f" → {task.assignee}"
        else:
            assignee = t("dispatcher.task_unassigned_marker")
        lines.append(f"- [{task.task_id}] [{task.status}] {task.title}: {task.content}{assignee}")
    return "\n".join(lines)


__all__ = [
    "render_message",
    "render_messages",
    "render_task_board",
]
