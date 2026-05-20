# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the P2 external-CLI building blocks: adapters, injector, runtime."""

import asyncio

import pytest

from openjiuwen.agent_teams.agent.member_runtime import MemberRuntime
from openjiuwen.agent_teams.external.cli_agent.adapters import (
    available_adapters,
    build_adapter,
)
from openjiuwen.agent_teams.external.cli_agent.injector import StdinPipeInjector
from openjiuwen.agent_teams.external.runtime import ExternalCliRuntime
from openjiuwen.agent_teams.harness import TeamHarness
from openjiuwen.core.common.exception.errors import BaseError


# ---- adapters -------------------------------------------------------------


@pytest.mark.level0
def test_build_adapter_claude_stream_json():
    adapter = build_adapter("claude")
    cmd = adapter.build_command()
    assert cmd[0] == "claude"
    assert "--dangerously-skip-permissions" in cmd
    framed = adapter.format_input("hello")
    assert '"type": "user"' in framed
    assert '"content": "hello"' in framed


@pytest.mark.level0
def test_claude_completion_on_result_json():
    adapter = build_adapter("claude")
    assert adapter.is_turn_complete('{"type": "result", "subtype": "success"}')
    assert not adapter.is_turn_complete('{"type": "assistant"}')
    assert not adapter.is_turn_complete("plain text")


@pytest.mark.level0
def test_generic_adapter_marker_completion():
    adapter = build_adapter("generic")
    assert adapter.format_input("hi") == "hi"
    assert adapter.is_turn_complete("done <<END_OF_TURN>> now")
    assert not adapter.is_turn_complete("still working")


@pytest.mark.level0
def test_build_adapter_command_override():
    adapter = build_adapter("claude", command_override=("/usr/local/bin/claude", "-x"))
    assert adapter.build_command() == ["/usr/local/bin/claude", "-x"]


@pytest.mark.level1
def test_build_adapter_unknown_raises():
    with pytest.raises(BaseError):
        build_adapter("nope")


@pytest.mark.level1
def test_available_adapters_includes_known_clis():
    names = set(available_adapters())
    assert {"claude", "codex", "openclaw", "hermes", "generic"} <= names


# ---- runtime --------------------------------------------------------------


class _RecordingInjector:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.closed = False

    async def write(self, text: str) -> None:
        self.writes.append(text)

    async def aclose(self) -> None:
        self.closed = True


async def _lines(*items: str):
    for item in items:
        yield item


@pytest.mark.asyncio
@pytest.mark.level0
async def test_runtime_run_streaming_writes_input_and_consumes_until_complete():
    injector = _RecordingInjector()
    runtime = ExternalCliRuntime(
        member_name="dev-1",
        adapter=build_adapter("generic"),
        injector=injector,
        output_lines=_lines("thinking...", "more <<END_OF_TURN>>", "next-turn-line"),
    )

    chunks = [chunk async for chunk in runtime.run_streaming({"query": "do it"}, session_id="s")]

    assert chunks == []  # stdout stays internal
    assert injector.writes == ["do it"]  # input delivered once


@pytest.mark.asyncio
@pytest.mark.level0
async def test_runtime_steer_and_follow_up_inject():
    injector = _RecordingInjector()
    runtime = ExternalCliRuntime(
        member_name="dev-1",
        adapter=build_adapter("generic"),
        injector=injector,
        output_lines=_lines(),
    )
    await runtime.steer("urgent")
    await runtime.follow_up("later")
    assert injector.writes == ["urgent", "later"]


@pytest.mark.asyncio
@pytest.mark.level1
async def test_runtime_abort_stops_turn():
    injector = _RecordingInjector()

    async def _slow_lines():
        yield "line-1"
        runtime._abort_requested = True  # simulate abort arriving mid-turn
        yield "line-2"
        yield "should-not-matter <<END_OF_TURN>>"

    runtime = ExternalCliRuntime(
        member_name="dev-1",
        adapter=build_adapter("generic"),
        injector=injector,
        output_lines=_slow_lines(),
    )
    chunks = [chunk async for chunk in runtime.run_streaming({"query": "go"}, session_id="s")]
    assert chunks == []


@pytest.mark.level0
def test_runtime_conforms_to_member_runtime_protocol():
    runtime = ExternalCliRuntime(
        member_name="dev-1",
        adapter=build_adapter("generic"),
        injector=_RecordingInjector(),
        output_lines=_lines(),
    )
    assert isinstance(runtime, MemberRuntime)


@pytest.mark.level1
def test_team_harness_exposes_member_runtime_surface():
    # TeamHarness is the default MemberRuntime; verify it carries every
    # member the Protocol declares (issubclass is unavailable for Protocols
    # with property members, so check attribute presence on the class).
    for member in (
        "run_streaming",
        "steer",
        "follow_up",
        "abort",
        "init_cwd_for_round",
        "has_pending_interrupt",
        "is_pending_interrupt_resume_valid",
        "find_rails",
        "register_rail",
        "unregister_rail",
        "register_member_tools",
        "inject_member_memory",
        "run_agent_customizer",
        "workspace",
        "sys_operation",
    ):
        assert hasattr(TeamHarness, member), member


# ---- injector -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.level1
async def test_stdin_pipe_injector_writes_newline_framed():
    proc = await asyncio.create_subprocess_exec(
        "cat",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    injector = StdinPipeInjector(proc.stdin)
    await injector.write("hello")
    await injector.aclose()
    stdout, _ = await proc.communicate()
    assert stdout.decode().strip() == "hello"
