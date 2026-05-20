# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for pure-function inbound rendering."""

from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.external.format import (
    render_message,
    render_messages,
    render_task_board,
)
from openjiuwen.agent_teams.i18n import get_language, set_language


@pytest.fixture
def lang_en():
    """Pin the process language to English for stable assertions."""
    previous = get_language()
    set_language("en")
    yield
    set_language(previous)


def _message(message_id: str, sender: str, content: str, *, broadcast: bool) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        from_member_name=sender,
        content=content,
        broadcast=broadcast,
    )


def _task(task_id: str, status: str, assignee: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=task_id,
        title=f"title-{task_id}",
        content=f"content-{task_id}",
        status=status,
        assignee=assignee,
    )


@pytest.mark.level0
def test_render_message_direct(lang_en):
    out = render_message(_message("m1", "leader", "hello", broadcast=False))
    assert "m1" in out
    assert "leader" in out
    assert "hello" in out
    assert "direct message" in out


@pytest.mark.level0
def test_render_message_broadcast(lang_en):
    out = render_message(_message("m2", "leader", "all hands", broadcast=True))
    assert "broadcast" in out


@pytest.mark.level0
def test_render_messages_joins_batch(lang_en):
    out = render_messages(
        [
            _message("m1", "leader", "first", broadcast=False),
            _message("m2", "dev-2", "second", broadcast=False),
        ]
    )
    assert "first" in out
    assert "second" in out


@pytest.mark.level0
def test_render_task_board_filters_terminal_and_marks_assignment(lang_en):
    tasks = [
        _task("t1", "pending", None),
        _task("t2", "completed", "dev-1"),
        _task("t3", "claimed", "dev-1"),
        _task("t4", "cancelled", None),
    ]
    out = render_task_board(tasks, is_leader=False)
    assert "t1" in out
    assert "t3" in out
    assert "t2" not in out
    assert "t4" not in out
    assert "→ dev-1" in out


@pytest.mark.level0
def test_render_task_board_empty_when_all_terminal(lang_en):
    tasks = [_task("t1", "completed", None), _task("t2", "cancelled", None)]
    assert render_task_board(tasks, is_leader=False) == ""
