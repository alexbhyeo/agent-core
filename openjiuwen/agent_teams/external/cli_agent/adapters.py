# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-CLI launch knowledge for spawned third-party agent members.

Each third-party CLI differs in three ways the spawn path must know:

1. **launch command** — binary + permission-bypass flags + stdin/stream mode,
2. **input framing** — how a turn's text is written to stdin,
3. **turn completion** — how to tell from stdout that the turn is done.

:class:`CliAgentAdapter` captures these as data. Launch flags follow the
conventions proven by the ClawTeam project. Input framing and completion
detection are best-effort defaults that may need per-version tuning against
the real CLI — they are deliberately data-driven so tuning needs no code
change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import raise_error

# input_format values
INPUT_TEXT = "text"
INPUT_STREAM_JSON = "stream_json"

# completion strategy values
COMPLETION_NONE = "none"
COMPLETION_RESULT_JSON = "result_json"
COMPLETION_MARKER_PREFIX = "marker:"


@dataclass(frozen=True, slots=True)
class CliAgentAdapter:
    """How to launch and talk to one third-party CLI agent.

    Attributes:
        name: Adapter key (``claude`` / ``codex`` / ...).
        command: Full launch argv (binary + flags).
        input_format: ``"text"`` (write the raw line) or ``"stream_json"``
            (wrap as a Claude/Codex stream-json user message).
        completion: ``"result_json"`` (stdout line is a JSON object with
            ``type == "result"``), ``"marker:<s>"`` (line contains ``<s>``),
            or ``"none"`` (never auto-detected — relies on the agent calling
            an idle tool, or process exit).
        supports_stdin_injection: Whether mid-turn stdin writes are observed.
            False CLIs degrade to turn-boundary delivery (no mid-turn steer).
    """

    name: str
    command: tuple[str, ...]
    input_format: str = INPUT_TEXT
    completion: str = COMPLETION_NONE
    supports_stdin_injection: bool = True

    def build_command(self, extra_args: tuple[str, ...] = ()) -> list[str]:
        """Return the launch argv, optionally with extra args appended."""
        return [*self.command, *extra_args]

    def format_input(self, text: str) -> str:
        """Frame one turn's input text for writing to the CLI stdin."""
        if self.input_format == INPUT_STREAM_JSON:
            return json.dumps({"type": "user", "message": {"role": "user", "content": text}})
        return text

    def is_turn_complete(self, line: str) -> bool:
        """Return whether a stdout ``line`` signals the current turn is done."""
        if self.completion == COMPLETION_RESULT_JSON:
            return _is_result_json(line)
        if self.completion.startswith(COMPLETION_MARKER_PREFIX):
            marker = self.completion[len(COMPLETION_MARKER_PREFIX) :]
            return bool(marker) and marker in line
        return False


def _is_result_json(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return False
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("type") == "result"


# Built-in adapters. Launch flags mirror ClawTeam's NativeCliAdapter
# conventions; input/completion are best-effort and tunable per CLI version.
_BUILTIN: dict[str, CliAgentAdapter] = {
    "claude": CliAgentAdapter(
        name="claude",
        command=(
            "claude",
            "--dangerously-skip-permissions",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
        ),
        input_format=INPUT_STREAM_JSON,
        completion=COMPLETION_RESULT_JSON,
    ),
    "codex": CliAgentAdapter(
        name="codex",
        command=("codex", "--dangerously-bypass-approvals-and-sandbox"),
        input_format=INPUT_TEXT,
        completion=COMPLETION_NONE,
    ),
    "openclaw": CliAgentAdapter(
        name="openclaw",
        command=("openclaw", "--local"),
        input_format=INPUT_TEXT,
        completion=COMPLETION_NONE,
    ),
    "hermes": CliAgentAdapter(
        name="hermes",
        command=("hermes",),
        input_format=INPUT_TEXT,
        completion=COMPLETION_NONE,
    ),
    # Line-based echo agent used by tests and simple integrations: one input
    # line, output terminated by an explicit end-of-turn marker.
    "generic": CliAgentAdapter(
        name="generic",
        command=(),
        input_format=INPUT_TEXT,
        completion=f"{COMPLETION_MARKER_PREFIX}<<END_OF_TURN>>",
    ),
}


def available_adapters() -> tuple[str, ...]:
    """Return the registered adapter names."""
    return tuple(_BUILTIN)


def build_adapter(name: str, *, command_override: tuple[str, ...] | None = None) -> CliAgentAdapter:
    """Resolve a built-in adapter by name.

    Args:
        name: Adapter key (see :func:`available_adapters`).
        command_override: Optional full launch argv replacing the default
            (e.g. an absolute binary path or extra flags).

    Raises:
        BaseError: ``AGENT_TEAM_CONFIG_INVALID`` for an unknown adapter.
    """
    adapter = _BUILTIN.get(name)
    if adapter is None:
        raise_error(
            StatusCode.AGENT_TEAM_CONFIG_INVALID,
            reason=f"unknown cli agent adapter '{name}'; known: {', '.join(available_adapters())}",
        )
        raise AssertionError  # pragma: no cover - raise_error always raises
    if command_override is not None:
        return replace(adapter, command=command_override)
    return adapter


__all__ = [
    "CliAgentAdapter",
    "available_adapters",
    "build_adapter",
    "INPUT_TEXT",
    "INPUT_STREAM_JSON",
    "COMPLETION_NONE",
    "COMPLETION_RESULT_JSON",
]
