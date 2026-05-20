# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ExternalCliRuntime: a team member whose brain is a third-party CLI.

This is the P2 counterpart to ``TeamHarness``. Instead of driving a local
DeepAgent, it drives an external CLI subprocess: a round delivers the inbound
text to the CLI's input channel (the :class:`Injector`) and consumes the
CLI's stdout until the per-CLI adapter signals the turn is complete. The
CLI's *actions* (sending messages, claiming tasks) flow out-of-process
through the team CLI/MCP tools, so the CLI's stdout stays internal and is
not surfaced as team-stream chunks.

It implements the :class:`MemberRuntime` Protocol; rail / memory / customizer
hooks are no-ops because an external CLI member has none (the configurator
skips those features for it).
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from openjiuwen.agent_teams.agent.member_runtime import AgentCustomizer
from openjiuwen.agent_teams.external.cli_agent.adapters import CliAgentAdapter
from openjiuwen.agent_teams.external.cli_agent.injector import Injector
from openjiuwen.core.common.logging import team_logger


class ExternalCliRuntime:
    """Drive a spawned third-party CLI as a team member's brain."""

    def __init__(
        self,
        *,
        member_name: str,
        adapter: CliAgentAdapter,
        injector: Injector,
        output_lines: AsyncIterator[str],
    ):
        """Bind the runtime to its CLI transport.

        Args:
            member_name: The team member this runtime serves.
            adapter: Per-CLI input framing + turn-completion strategy.
            injector: Side-channel that delivers text to the CLI input.
            output_lines: Async iterator over the CLI's stdout lines,
                consumed across rounds (position is preserved between turns).
        """
        self._member_name = member_name
        self._adapter = adapter
        self._injector = injector
        self._output_lines = output_lines
        self._abort_requested = False

    # ---- round runtime surface ----

    async def run_streaming(self, inputs: dict[str, Any], *, session_id: Optional[str]) -> AsyncIterator[Any]:
        """Deliver the round input to the CLI and wait out its turn.

        Yields nothing: the CLI's stdout is internal reasoning, while its
        team-visible work happens via the injected tools. The async-generator
        form matches how ``StreamController`` consumes ``run_streaming``.
        """
        await self._drive_turn(inputs)
        for _never in ():  # pragma: no cover - empty: makes this an async generator
            yield _never

    async def _drive_turn(self, inputs: dict[str, Any]) -> None:
        query = inputs.get("query")
        text = query if isinstance(query, str) else str(query)
        self._abort_requested = False
        await self._injector.write(self._adapter.format_input(text))
        async for line in self._output_lines:
            if self._abort_requested:
                team_logger.debug("[{}] external cli turn aborted", self._member_name)
                return
            if self._adapter.is_turn_complete(line):
                return

    async def steer(self, content: str) -> None:
        """Inject content into the CLI mid-turn (if it reads stdin live)."""
        await self._injector.write(self._adapter.format_input(content))

    async def follow_up(self, content: str) -> None:
        """Inject content for the CLI to handle after the current turn."""
        await self._injector.write(self._adapter.format_input(content))

    async def abort(self) -> None:
        """Request the in-flight turn to stop at the next output line."""
        self._abort_requested = True

    def init_cwd_for_round(self) -> None:
        """No-op: the subprocess owns its working directory."""
        return None

    def has_pending_interrupt(self) -> bool:
        """External CLI members have no interrupt-resume concept."""
        return False

    def is_pending_interrupt_resume_valid(self, user_input: Any) -> bool:
        """External CLI members have no interrupt-resume concept."""
        return False

    # ---- rail / memory / customizer hooks (no-ops for external CLI) ----

    def find_rails(self, rail_type: type) -> list[Any]:
        """No rails on an external CLI runtime."""
        return []

    async def register_rail(self, rail: Any) -> None:
        """No-op: external CLI runtime has no rail stack."""
        return None

    async def unregister_rail(self, rail: Any) -> None:
        """No-op: external CLI runtime has no rail stack."""
        return None

    def register_member_tools(self, memory_manager: Any) -> None:
        """No-op: external CLI members do not use the team memory toolkit."""
        return None

    async def inject_member_memory(self, memory_manager: Any, query: str) -> None:
        """No-op: external CLI members do not use team memory injection."""
        return None

    def run_agent_customizer(self, customizer: AgentCustomizer) -> None:
        """No-op: the agent_customizer hook targets a local DeepAgent."""
        return None

    # ---- config snapshots ----

    @property
    def workspace(self) -> Optional[Any]:
        """External CLI runtime exposes no team workspace handle."""
        return None

    @property
    def sys_operation(self) -> Optional[Any]:
        """External CLI runtime exposes no sys_operation handle."""
        return None

    # ---- lifecycle ----

    async def aclose(self) -> None:
        """Release the input channel. Idempotent."""
        await self._injector.aclose()


__all__ = ["ExternalCliRuntime"]
