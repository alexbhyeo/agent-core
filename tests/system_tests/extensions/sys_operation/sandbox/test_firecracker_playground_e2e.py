# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""End-to-end tests of the Firecracker Playground (design D-20261127-0315-firecracker-playground).

The backend and the frontend run as separate containers (different origins), started by
``deploy/firecracker/playground/{backend/run_backend.sh,frontend/run_frontend.sh}``; the
backend runs with ``PLAYGROUND_DOCKER=1`` and allows the origin ``http://fc-playground-frontend:8080``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from typing import Any, AsyncIterator, Callable, Optional

import aiohttp
import pytest
import pytest_asyncio

from tests.system_tests.extensions.sys_operation.sandbox.fc_helpers import DOCKER_SOCKET, docker_exec
from tests.test_logger import logger as test_logger

BACKEND = os.getenv("PLAYGROUND_BACKEND_URL", "http://fc-playground-backend:8800")
FRONTEND = os.getenv("PLAYGROUND_FRONTEND_URL", "http://fc-playground-frontend:8080")
TOKEN = os.getenv("PLAYGROUND_TOKEN", "")
ALLOWED_ORIGIN = os.getenv("PLAYGROUND_TEST_ORIGIN", "http://fc-playground-frontend:8080")
BACKEND_CONTAINER = os.getenv("PLAYGROUND_BACKEND_CONTAINER", "fc-playground-backend")
LLM_READY = bool(os.getenv("API_KEY") and os.getenv("API_BASE") and os.getenv("MODEL_NAME"))
SECRETS = [v for v in (os.getenv("FCM_API_TOKEN"), os.getenv("API_KEY")) if v]

pytestmark = pytest.mark.skipif(not TOKEN, reason="needs PLAYGROUND_TOKEN and a running playground")
needs_llm = pytest.mark.skipif(not LLM_READY, reason="needs API_KEY / API_BASE / MODEL_NAME")


async def call(method: str, path: str, user: str = "alice", body: Any = None, token: Optional[str] = TOKEN,
               origin: Optional[str] = None, timeout: float = 600) -> tuple[int, Any, dict]:
    headers = {"X-Playground-User": user}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin:
        headers["Origin"] = origin
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
        async with http.request(method, BACKEND + path, json=body, headers=headers) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else None
            except json.JSONDecodeError:
                data = text
            return resp.status, data, dict(resp.headers)


def assert_no_secrets(payload: Any) -> None:
    text = json.dumps(payload)
    for secret in SECRETS:
        assert secret not in text, "a credential leaked to the browser"


class EventStream:
    """The per-user SSE stream, read the way the frontend does (fetch + Authorization)."""

    def __init__(self, user: str):
        self.user = user
        self.events: list[dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def __aenter__(self) -> "EventStream":
        self._task = asyncio.create_task(self._read())
        await asyncio.wait_for(self._ready.wait(), 30)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _read(self) -> None:
        headers = {"Authorization": f"Bearer {TOKEN}", "X-Playground-User": self.user}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as http:
            async with http.get(f"{BACKEND}/api/v1/events", headers=headers) as resp:
                assert resp.status == 200
                buf = ""
                async for raw in resp.content.iter_any():
                    self._ready.set()
                    buf += raw.decode()
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        data = "".join(line[5:].strip() for line in block.splitlines() if line.startswith("data:"))
                        if data:
                            self.events.append(json.loads(data))

    async def wait_for(self, pred: Callable[[dict[str, Any]], bool], timeout: float = 300) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events:
                if pred(event):
                    return event
            await asyncio.sleep(0.3)
        raise AssertionError(f"no matching event within {timeout}s; last events: {self.events[-8:]}")

    def of(self, session_id: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("session_id") == session_id]


@pytest_asyncio.fixture
async def created() -> AsyncIterator[list[tuple[str, str]]]:
    sessions: list[tuple[str, str]] = []
    yield sessions
    for user, sid in sessions:
        await call("DELETE", f"/api/v1/sessions/{sid}", user)


async def new_session(created: list, user: str = "alice", **body: Any) -> dict[str, Any]:
    status, session, _ = await call("POST", "/api/v1/sessions", user, {"kind": "deepagent", **body})
    assert status == 201, session
    created.append((user, session["session_id"]))
    return session


async def shell(user: str, sid: str, command: str) -> dict[str, Any]:
    status, out, _ = await call("POST", f"/api/v1/sessions/{sid}/shell", user, {"command": command})
    assert status == 200, out
    return out


# -------------------------------------------------------------- separation / access


@pytest.mark.asyncio
async def test_frontend_and_backend_are_separate_and_guarded(created):
    async with aiohttp.ClientSession() as http:
        async with http.get(FRONTEND + "/") as resp:
            page = await resp.text()
            assert resp.status == 200 and "Firecracker Playground" in page
        async with http.get(FRONTEND + "/config.js") as resp:
            config = await resp.text()
            assert "apiBaseUrl" in config
        for name in ("/app.js", "/style.css"):
            async with http.get(FRONTEND + name) as resp:
                assert resp.status == 200
                body = await resp.text()
                assert all(secret not in body for secret in SECRETS + [TOKEN])
        async with http.get(BACKEND + "/healthz") as resp:
            assert (await resp.json()) == {"status": "ok", "api_version": 1}
        async with http.get(BACKEND + "/") as resp:
            assert resp.status == 404, "the backend serves no pages"

    assert (await call("GET", "/api/v1/me", token=None))[0] == 401
    assert (await call("GET", "/api/v1/me", token="wrong"))[0] == 401
    assert (await call("GET", "/api/v1/me", user="bad user!"))[0] == 400
    status, me, headers = await call("GET", "/api/v1/me", origin=ALLOWED_ORIGIN)
    assert status == 200 and headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN
    _, _, headers = await call("GET", "/api/v1/me", origin="http://evil.example")
    assert "Access-Control-Allow-Origin" not in headers

    async with aiohttp.ClientSession() as http:
        preflight = {"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "authorization,x-playground-user,content-type"}
        async with http.options(BACKEND + "/api/v1/sessions", headers=preflight) as resp:
            assert resp.status == 204
            assert "X-Playground-User" in resp.headers["Access-Control-Allow-Headers"]
        async with http.options(BACKEND + "/api/v1/sessions", headers={**preflight, "Origin": "http://evil.example"}) as resp:
            assert "Access-Control-Allow-Origin" not in resp.headers

    session = await new_session(created)
    assert_no_secrets(session)
    assert "/t/***" in session["vm"]["base_url"], "guest tokens are stripped"
    _, listing, _ = await call("GET", "/api/v1/sessions")
    assert_no_secrets(listing)


# ---------------------------------------------------------------- isolation


@pytest.mark.asyncio
async def test_users_only_see_their_own_sessions_and_events(created):
    async with EventStream("alice") as alice_events, EventStream("bob") as bob_events:
        session = await new_session(created, "alice")
        sid = session["session_id"]
        await shell("alice", sid, "echo alice-data > /workspace/mine.txt")
        await alice_events.wait_for(lambda e: e["type"] == "session.updated" and e["session_id"] == sid)
        for method, path in (("GET", f"/api/v1/sessions/{sid}"), ("POST", f"/api/v1/sessions/{sid}/shell"),
                             ("GET", f"/api/v1/sessions/{sid}/files"), ("DELETE", f"/api/v1/sessions/{sid}"),
                             ("POST", f"/api/v1/sessions/{sid}/vm/pause")):
            status, _, _ = await call(method, path, "bob", {"command": "true"} if method == "POST" else None)
            assert status == 403, (method, path, status)
        _, bob_list, _ = await call("GET", "/api/v1/sessions", "bob")
        assert sid not in {s["session_id"] for s in bob_list["sessions"] + bob_list["external"]}
        await asyncio.sleep(1)
        assert not bob_events.of(sid), "bob's stream must not carry alice's events"
        out = await shell("alice", sid, "cat /workspace/mine.txt")
        assert out["stdout"].strip() == "alice-data"


# ------------------------------------------------------------- session panel


@pytest.mark.asyncio
async def test_session_panel_actions_work_end_to_end(created):
    session = await new_session(created, title="panel")
    sid = session["session_id"]
    await shell("alice", sid, "mkdir -p /workspace/proj && echo v1 > /workspace/proj/f.txt")

    status, listing, _ = await call("GET", f"/api/v1/sessions/{sid}/files?path=/workspace/proj")
    assert status == 200 and [e["name"] for e in listing["entries"]] == ["f.txt"]
    status, content, _ = await call("GET", f"/api/v1/sessions/{sid}/files?path=/workspace/proj/f.txt")
    assert content["type"] == "file" and content["text"].strip() == "v1"

    status, body, _ = await call("PUT", f"/api/v1/sessions/{sid}/secrets", body={"GH_TOKEN": "s3cret-value"})
    assert status == 200 and body["secret_keys"] == ["GH_TOKEN"]
    _, info, _ = await call("GET", f"/api/v1/sessions/{sid}")
    assert info["vm"]["secret_keys"] == ["GH_TOKEN"] and "s3cret-value" not in json.dumps(info)

    status, body, _ = await call("POST", f"/api/v1/sessions/{sid}/workspace", body={"checkpoint": "v1"})
    assert status == 200, body
    await shell("alice", sid, "echo v2 > /workspace/proj/f.txt")
    status, body, _ = await call("POST", f"/api/v1/sessions/{sid}/workspace", body={"swap": "v1"})
    assert status == 200, body
    assert (await shell("alice", sid, "cat /workspace/proj/f.txt"))["stdout"].strip() == "v1"

    status, body, _ = await call("POST", f"/api/v1/sessions/{sid}/mcp",
                                 body={"name": "demo", "command": "/opt/mcp/bin/python", "args": ["/opt/fc/demo_mcp_server.py"]})
    assert status == 200 and body["resource_id"] == f"alice--{sid}--demo", body

    await shell("alice", sid, "curl -s -o /dev/null -m 20 https://example.com || true")
    await asyncio.sleep(1)
    _, egress, _ = await call("GET", f"/api/v1/sessions/{sid}/egress")
    assert any(e.get("dport") == 443 for e in egress["entries"]), egress
    _, debug, _ = await call("GET", f"/api/v1/sessions/{sid}/debug")
    assert debug["pid"] and debug["seccomp"] == "2"
    assert_no_secrets(debug)

    for action, state in (("pause", "PAUSED"), ("resume", "RUNNING"), ("persist", "PERSISTED")):
        status, body, _ = await call("POST", f"/api/v1/sessions/{sid}/vm/{action}", body={})
        assert status == 200 and body["vm"]["state"] == state, body
    # A shell call (like an agent's tool call) wakes the persisted VM; the files are still there.
    assert (await shell("alice", sid, "cat /workspace/proj/f.txt"))["stdout"].strip() == "v1"

    status, body, _ = await call("POST", f"/api/v1/sessions/{sid}/fork", body={"count": 1})
    assert status == 201 and len(body["sessions"]) == 1, body
    fork = body["sessions"][0]
    created.append(("alice", fork["session_id"]))
    assert fork["title"] == "panel (fork 1)" and fork["vm"]["forked_from"] == sid
    assert (await shell("alice", fork["session_id"], "cat /workspace/proj/f.txt"))["stdout"].strip() == "v1"


@pytest.mark.asyncio
async def test_tier_quota_and_errors_are_reported(created):
    for _ in range(2):
        await new_session(created, "noisy")
    status, body, _ = await call("POST", "/api/v1/sessions", "noisy", {"kind": "deepagent"})
    assert status == 429 and "allows 2 sessions" in body["error"], body
    assert (await call("POST", "/api/v1/sessions", body={"kind": "robot"}))[0] == 400
    assert (await call("GET", "/api/v1/sessions/pg-doesnotexist"))[0] == 404
    sid = created[0][1]
    assert (await call("POST", f"/api/v1/sessions/{sid}/query", "noisy", {"text": "  "}))[0] == 400
    status, body, _ = await call("POST", "/api/v1/batch-query", "noisy",
                                 {"session_ids": ["pg-doesnotexist"], "text": "hi"})
    assert status == 202 and body["runs"][0]["status"] == 404


# ------------------------------------------------------------------- VM messages


@pytest.mark.asyncio
async def test_vm_task_questions_reach_the_user_and_answers_reach_the_task(created):
    async with EventStream("alice") as events:
        session = await new_session(created)
        sid = session["session_id"]
        await shell("alice", sid, "nohup sh -c 'a=$(fc-ask --timeout 600 Which color?); echo \"$a\" > "
                                  "/workspace/color.txt; fc-notify color saved' >/dev/null 2>&1 & echo started")
        question = await events.wait_for(lambda e: e["type"] == "vm.message" and e["session_id"] == sid
                                         and e["message"]["type"] == "question", 120)
        mid = question["message"]["id"]
        status, answered, _ = await call("POST", f"/api/v1/sessions/{sid}/messages/{mid}/reply", body={"text": "teal"})
        assert status == 200 and answered["status"] == "answered"
        await events.wait_for(lambda e: e["type"] == "vm.message" and e["message"].get("text") == "color saved", 120)
        assert (await shell("alice", sid, "cat /workspace/color.txt"))["stdout"].strip() == "teal"
        _, info, _ = await call("GET", f"/api/v1/sessions/{sid}")
        assert {m["text"] for m in info["vm_messages"]} >= {"Which color?", "color saved"}


# --------------------------------------------------------------- manager restart


@pytest.mark.asyncio
async def test_sessions_survive_a_manager_restart(created):
    session = await new_session(created)
    sid = session["session_id"]
    await shell("alice", sid, "echo before-restart > /workspace/r.txt")
    _, me, _ = await call("GET", "/api/v1/me")
    if not me["features"]["restart_manager"]:
        pytest.skip("backend started without PLAYGROUND_DOCKER=1")
    status, body, _ = await call("POST", "/api/v1/ops/restart-manager", timeout=900)
    assert status == 200, body
    test_logger.info("manager restarted through the playground in %ss", body["took_s"])
    _, info, _ = await call("GET", f"/api/v1/sessions/{sid}")
    assert info["vm"]["state"] == "PERSISTED", info["vm"]
    assert (await shell("alice", sid, "cat /workspace/r.txt"))["stdout"].strip() == "before-restart"
    _, ops, _ = await call("GET", "/api/v1/ops")
    assert ops["manager_ok"] and "fc_vm_slots_used" in ops["metrics"]


# ------------------------------------------------------------------- LLM queries


async def _ask(events: EventStream, user: str, sid: str, text: str, timeout: float = 600) -> dict[str, Any]:
    status, body, _ = await call("POST", f"/api/v1/sessions/{sid}/query", user, {"text": text})
    assert status == 202, body
    run_id = body["run_id"]
    finished = await events.wait_for(lambda e: e["type"] == "run.finished" and e.get("run_id") == run_id, timeout)
    assert finished["status"] == "ok", finished
    return finished


def _answer_text(events: EventStream, sid: str, run_id: str) -> str:
    chunks = [e for e in events.of(sid) if e["type"] == "run.chunk" and e["run_id"] == run_id]
    answers = [e["text"] for e in chunks if e["kind"] == "answer"]
    return answers[-1] if answers else "".join(e["text"] for e in chunks if e["kind"] == "delta")


@needs_llm
@pytest.mark.asyncio
async def test_deep_agent_answers_and_remembers_the_conversation(created):
    async with EventStream("alice") as events:
        session = await new_session(created)
        sid = session["session_id"]
        first = await _ask(events, "alice", sid, "Use write_file to create /workspace/n.txt containing exactly 4711. "
                                                 "Then reply with the number only.")
        kinds = {e["kind"] for e in events.of(sid) if e["type"] == "run.chunk"}
        assert {"tool_call", "answer"} <= kinds, kinds
        assert "4711" in _answer_text(events, sid, first["run_id"])
        second = await _ask(events, "alice", sid, "What number did you write in the previous step? Number only.")
        assert "4711" in _answer_text(events, sid, second["run_id"])
        _, content, _ = await call("GET", f"/api/v1/sessions/{sid}/files?path=/workspace/n.txt")
        assert content["text"].strip() == "4711"
        _, info, _ = await call("GET", f"/api/v1/sessions/{sid}")
        assert [t["role"] for t in info["transcript"] if t["role"] != "event"][:2] == ["user", "assistant"]
        assert info["title"].startswith("Use write_file")


@needs_llm
@pytest.mark.asyncio
async def test_parallel_and_batch_queries_stay_in_their_sessions(created):
    async with EventStream("alice") as alice, EventStream("bob") as bob:
        a1, a2 = (await new_session(created, "alice"))["session_id"], (await new_session(created, "alice"))["session_id"]
        b1 = (await new_session(created, "bob"))["session_id"]
        started = time.monotonic()
        runs = {}
        for user, sid, word in (("alice", a1, "APPLE"), ("alice", a2, "BANANA"), ("bob", b1, "CHERRY")):
            status, body, _ = await call("POST", f"/api/v1/sessions/{sid}/query", user,
                                         {"text": f"Run the shell command `echo {word} > /workspace/w.txt` and reply "
                                                  f"with the word {word} only."})
            assert status == 202, body
            runs[sid] = (user, word, body["run_id"])
        status, body, _ = await call("POST", f"/api/v1/sessions/{a1}/query", "alice", {"text": "again"})
        assert status == 409, "a busy session refuses a second query"
        for sid, (user, word, run_id) in runs.items():
            stream = alice if user == "alice" else bob
            done = await stream.wait_for(lambda e, r=run_id: e["type"] == "run.finished" and e.get("run_id") == r, 600)
            assert done["status"] == "ok", done
            assert word in _answer_text(stream, sid, run_id)
            assert (await shell(user, sid, "cat /workspace/w.txt"))["stdout"].strip() == word
        test_logger.info("3 sessions answered in parallel in %.1fs", time.monotonic() - started)
        assert all(e["session_id"] in (a1, a2, None) for e in alice.events)
        assert all(e["session_id"] in (b1, None) for e in bob.events)

        status, body, _ = await call("POST", "/api/v1/batch-query", "alice",
                                     {"session_ids": [a1, a2], "text": "Reply with the word READY only."})
        assert status == 202 and all("run_id" in r for r in body["runs"]), body
        for r in body["runs"]:
            await alice.wait_for(lambda e, rid=r["run_id"]: e["type"] == "run.finished" and e.get("run_id") == rid, 600)


@needs_llm
@pytest.mark.asyncio
async def test_agent_team_session_streams_member_output_in_one_vm(created):
    async with EventStream("alice") as events:
        status, session, _ = await call("POST", "/api/v1/sessions", "alice",
                                        {"kind": "team", "team_size": 2, "profile": {"vcpus": 2, "mem_mib": 1536}})
        assert status == 201, session
        sid = session["session_id"]
        created.append(("alice", sid))
        await _ask(events, "alice", sid,
                   "Create one task for member-1 and pass this path on unchanged: use write_file to create the file "
                   "/workspace/team-hello.txt containing exactly 'hi from member-1', then complete the task. Wait "
                   "until it is completed, then reply DONE.",
                   timeout=900)
        members = {e.get("member") for e in events.of(sid) if e["type"] == "run.chunk"}
        assert members - {None}, f"team output must be labelled per member: {members}"
        assert (await shell("alice", sid, "cat /workspace/team-hello.txt"))["stdout"].strip() == "hi from member-1"
        if DOCKER_SOCKET:  # the members' tools ran in the VM, never inside the backend container
            code, out = await docker_exec(["sh", "-c", "ls /workspace/team-hello.txt 2>&1"], BACKEND_CONTAINER)
            assert code != 0, f"a team tool ran inside the backend: {out}"
        _, listing, _ = await call("GET", "/api/v1/sessions")
        assert [s for s in listing["sessions"] if s["session_id"] == sid][0]["vm"]["state"] in ("RUNNING", "PAUSED")


@needs_llm
@pytest.mark.asyncio
async def test_a_running_query_can_be_cancelled(created):
    async with EventStream("alice") as events:
        sid = (await new_session(created))["session_id"]
        status, body, _ = await call("POST", f"/api/v1/sessions/{sid}/query", "alice",
                                     {"text": "Run the shell command `sleep 60` and then reply done."})
        assert status == 202
        await events.wait_for(lambda e: e["type"] == "run.chunk" and e.get("run_id") == body["run_id"], 300)
        assert (await call("POST", f"/api/v1/sessions/{sid}/cancel"))[0] == 200
        done = await events.wait_for(lambda e: e["type"] == "run.finished" and e.get("run_id") == body["run_id"], 120)
        assert done["status"] == "cancelled"


# ------------------------------------------------------------------------ the UI


@pytest.mark.asyncio
async def test_ui_switches_user_chats_and_answers_a_vm_question(created):
    from playwright.async_api import async_playwright

    user = f"ui{uuid.uuid4().hex[:6]}"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(FRONTEND + "/")

        # First visit: the settings dialog asks for the backend (a different origin) and the token.
        await page.wait_for_selector("#dlg-settings[open]")
        await page.fill("#set-api", BACKEND)
        await page.fill("#set-token", TOKEN)
        await page.click("#set-save")
        await page.wait_for_selector("#conn.on", timeout=30000)

        # Switch user through the user menu.
        await page.click("#btn-user")
        await page.fill("#user-input", user)
        await page.press("#user-input", "Enter")
        await page.wait_for_function(f"document.getElementById('user-name').textContent === '{user}'")
        await page.wait_for_selector("#conn.on", timeout=30000)

        # New chat (DeepAgent).
        await page.click("#btn-new")
        await page.fill("#new-title", "ui test chat")
        await page.click("#dlg-new button[value=create]")
        item = page.locator(".session-item", has_text="ui test chat")
        await item.wait_for(timeout=120000)
        sid = await item.get_attribute("data-sid")
        created.append((user, sid))

        if LLM_READY:
            await page.fill("#input", "Reply with the word PONG only.")
            await page.press("#input", "Enter")
            await page.locator(".msg-assistant .body", has_text="PONG").wait_for(timeout=300000)

        # A task in the VM asks a question; the card appears and the user answers in the page.
        await page.click("#btn-details")
        await page.click("#tabs button[data-tab=shell]")
        await page.click("[data-act=shell-ask]")
        card = page.locator(".vm-card.question")
        await card.wait_for(timeout=120000)
        await card.locator("input").fill("crimson")
        await card.locator("button").click()
        await page.locator(".vm-card .answer", has_text="crimson").wait_for(timeout=60000)
        await page.locator(".vm-card", has_text="color saved: crimson").wait_for(timeout=120000)

        # Lifecycle from the panel.
        await page.click("#tabs button[data-tab=actions]")
        await page.click("[data-act=vm][data-a=persist]")
        await page.locator("#chat-state", has_text="PERSISTED").wait_for(timeout=120000)

        # Operations view.
        await page.click("#btn-ops")
        await page.locator("#ops-view", has_text="VM slots used").wait_for(timeout=30000)
        await browser.close()
    assert not [e for e in errors if "favicon" not in e], errors
