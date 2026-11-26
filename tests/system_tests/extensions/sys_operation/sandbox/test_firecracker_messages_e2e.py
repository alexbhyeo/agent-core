# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""End-to-end tests: tasks in a session VM talk to the session's user.

A task inside the VM runs ``fc-notify TEXT`` (one-way) or ``fc-ask TEXT`` (blocks until
the user answers, prints the answer). The user side (a frontend, here the test) reads
``GET /v1/sessions/{id}/messages`` through ``get_session_messages`` and answers with
``reply_to_session_message``. Covered: plain tasks, a question pending across persist,
two users, a DeepAgent-created task and an AgentTeam member's task.
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from typing import Any, Callable

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.extensions.sys_operation.sandbox.launchers import (
    get_session_messages,
    is_ownership_error,
    reply_to_session_message,
)
from tests.system_tests.extensions.sys_operation.sandbox.fc_helpers import (
    LLM_READY,
    MANAGER_URL,
    manager,
    sh,
)
from tests.test_logger import logger as test_logger

pytestmark = pytest.mark.skipif(not MANAGER_URL, reason="needs FC_MANAGER_URL (a running FcSessionManager)")


async def _wait_message(config, session_id: str, match: Callable[[dict[str, Any]], bool],
                        timeout: float = 120) -> dict[str, Any]:
    """Long-poll the session's messages, as a frontend would, until one matches."""
    deadline, after = time.monotonic() + timeout, 0
    seen: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        page = await get_session_messages(config, session_id, after=after, wait_seconds=20)
        for msg in page["messages"]:
            seen.append(msg)
            if match(msg):
                return msg
        after = max([after] + [m["id"] for m in page["messages"]])
    raise AssertionError(f"no matching message from {session_id} within {timeout}s; saw {seen}")


# The aio provider runs commands as "{ CMD; }", so a command may not end with "&":
# background commands below end with "& echo started".


def _is_question(msg: dict[str, Any]) -> bool:
    return msg["type"] == "question" and msg.get("status") == "pending"


@pytest.mark.asyncio
async def test_background_task_notifies_and_asks_the_user(fc):
    sid, op = fc.open("alice")
    cfg = fc.config("alice")
    await sh(op, sid, "FC_TASK=deploy nohup sh -c 'fc-notify build started; "
                      "a=$(fc-ask --timeout 300 Deploy to production?); echo \"$a\" > /workspace/answer.txt; "
                      "fc-notify deploy finished: $a' >/dev/null 2>&1 & echo started")
    started = await _wait_message(cfg, sid, lambda m: m["type"] == "notify")
    assert started["text"] == "build started" and started["task"] == "deploy" and started["session_id"] == sid
    question = await _wait_message(cfg, sid, _is_question)
    assert question["text"] == "Deploy to production?"
    _, info = await manager("GET", f"/v1/sessions/{sid}", "alice")
    assert info["pending_questions"] == 1

    answered = await reply_to_session_message(cfg, sid, question["id"], "yes, ship it")
    assert answered["status"] == "answered"
    done = await _wait_message(cfg, sid, lambda m: m["text"].startswith("deploy finished"))
    assert done["text"] == "deploy finished: yes, ship it"
    assert (await sh(op, sid, "cat /workspace/answer.txt")).stdout.strip() == "yes, ship it"

    with pytest.raises(BaseError) as exc:  # a question is answered once
        await reply_to_session_message(cfg, sid, question["id"], "again")
    assert "409" in str(exc.value) or "answered" in str(exc.value)

    # Unanswered questions expire and the task sees a non-zero exit code.
    out = await sh(op, sid, "fc-ask --timeout 2 anyone there? ; echo rc=$?")
    assert out.stdout.strip().endswith("rc=2"), out.stdout


@pytest.mark.asyncio
async def test_pending_question_survives_persist_and_the_reply_wakes_the_vm(fc):
    sid, op = fc.open("alice")
    cfg = fc.config("alice")
    await sh(op, sid, "nohup sh -c 'a=$(fc-ask --timeout 900 Which region?); echo \"$a\" > /workspace/region.txt; "
                      "fc-notify region saved' >/dev/null 2>&1 & echo started")
    question = await _wait_message(cfg, sid, _is_question)

    status, info = await manager("POST", f"/v1/sessions/{sid}:persist", "alice")
    assert status == 200 and info["state"] == "PERSISTED"
    page = await get_session_messages(cfg, sid)
    assert page["state"] == "PERSISTED"
    assert any(m["id"] == question["id"] and m["status"] == "pending" for m in page["messages"]), \
        "the user still sees the question while the VM is persisted"

    started = time.monotonic()
    await reply_to_session_message(cfg, sid, question["id"], "eu-west")
    test_logger.info("reply woke the persisted VM and was delivered in %.1fs", time.monotonic() - started)
    await _wait_message(cfg, sid, lambda m: m["text"] == "region saved")
    assert (await sh(op, sid, "cat /workspace/region.txt")).stdout.strip() == "eu-west"


@pytest.mark.asyncio
async def test_each_user_only_sees_and_answers_their_own_tasks(fc):
    a_sid, a_op = fc.open("alice")
    b_sid, b_op = fc.open("bob")
    for op, sid, who in ((a_op, a_sid, "alice"), (b_op, b_sid, "bob")):
        await sh(op, sid, f"fc-notify hello from the task of {who}")
    a_msgs = (await get_session_messages(fc.config("alice"), a_sid))["messages"]
    b_msgs = (await get_session_messages(fc.config("bob"), b_sid))["messages"]
    assert [m["text"] for m in a_msgs] == ["hello from the task of alice"]
    assert [m["text"] for m in b_msgs] == ["hello from the task of bob"]

    await sh(a_op, a_sid, "nohup fc-ask --timeout 120 secret question >/dev/null 2>&1 & echo started")
    question = await _wait_message(fc.config("alice"), a_sid, _is_question)
    for call in (lambda: get_session_messages(fc.config("bob"), a_sid),
                 lambda: reply_to_session_message(fc.config("bob"), a_sid, question["id"], "hijack")):
        with pytest.raises(BaseError) as exc:
            await call()
        assert is_ownership_error(exc.value)
    page = await get_session_messages(fc.config("alice"), a_sid)
    assert next(m for m in page["messages"] if m["id"] == question["id"])["status"] == "pending"


# ----------------------------------------------------------------- agent-created tasks

_TASK_RULES = (
    "Tasks you start in this machine can talk to the user: `fc-notify TEXT` sends the user a message and "
    "`fc-ask TEXT` asks the user a question and prints the answer. "
)


@pytest.mark.skipif(not LLM_READY, reason="needs API_KEY / API_BASE / MODEL_NAME")
@pytest.mark.asyncio
async def test_task_started_by_a_deep_agent_asks_the_user(fc):
    from examples.firecracker import deep_agent_firecracker as example
    from openjiuwen.core.runner import Runner

    sid = f"agent-{uuid.uuid4().hex[:8]}"
    fc.sessions.append(("alice", sid))
    fc.cards.append("fc-alice")
    query = (_TASK_RULES + "Start this exact background task with the shell tool and do not wait for it: "
             "nohup sh -c 'n=$(fc-ask --timeout 600 What name should the report use?); "
             "echo \"report for $n\" > /workspace/report.txt; fc-notify report ready' >/dev/null 2>&1 & "
             "Then reply STARTED.")
    result = await example.run_demo("alice", sid, query)
    assert result.get("result_type") == "answer", result

    cfg = fc.config("alice")
    question = await _wait_message(cfg, sid, _is_question, timeout=180)
    assert "name" in question["text"].lower()
    await reply_to_session_message(cfg, sid, question["id"], "Quarterly")
    await _wait_message(cfg, sid, lambda m: m["text"] == "report ready")
    op = Runner.resource_mgr.get_sys_operation("fc-alice")
    assert (await sh(op, sid, "cat /workspace/report.txt")).stdout.strip() == "report for Quarterly"


@pytest.mark.skipif(not LLM_READY, reason="needs API_KEY / API_BASE / MODEL_NAME")
@pytest.mark.asyncio
async def test_task_started_by_an_agent_team_member_asks_the_user(fc):
    from tests.system_tests.extensions.sys_operation.sandbox.test_firecracker_phase3_e2e import _run_team, _team_spec

    team = f"fcmsg{uuid.uuid4().hex[:6]}"
    sid = f"{team}-s1"
    tmp = tempfile.mkdtemp(prefix="fcteam-")
    sysop_id = f"fc-team-{team}"
    fc.cards.append(sysop_id)
    fc.sessions.append(("alice", sid))
    spec = _team_spec(team, sysop_id, os.path.join(tmp, "team.sqlite"), os.path.join(tmp, "ws"))
    query = (_TASK_RULES + "Create exactly one task for writer-a and assign it: writer-a must use the shell tool "
             "to start this exact background command and must not wait for it: "
             "nohup sh -c 'f=$(fc-ask --timeout 900 Which fruit should the team pick?); mkdir -p /workspace/team; "
             "echo \"$f\" > /workspace/team/fruit.txt; fc-notify fruit saved' >/dev/null 2>&1 & "
             "then complete the task. Wait until the task is completed, then reply DONE.")
    await _run_team(spec, query, sid)

    cfg = fc.config("alice")
    question = await _wait_message(cfg, sid, _is_question, timeout=180)
    assert "fruit" in question["text"].lower()
    await reply_to_session_message(cfg, sid, question["id"], "mango")
    await _wait_message(cfg, sid, lambda m: m["text"] == "fruit saved")
    op = spec.agents["teammate"].sys_operation.resolve()
    assert (await sh(op, sid, "cat /workspace/team/fruit.txt")).stdout.strip() == "mango"
    _, info = await manager("GET", "/v1/sessions", "alice")
    assert [s["session_id"] for s in info["sessions"] if s["session_id"].startswith(team)] == [sid]
