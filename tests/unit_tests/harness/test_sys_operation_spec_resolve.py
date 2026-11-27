# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SysOperationSpec.resolve fails closed instead of letting the harness fall back to LOCAL.

``create_deep_agent`` mints a LOCAL sys_operation when it receives none, so a spec
that resolved to ``None`` (e.g. its SANDBOX card could not be registered) used to
run the agent's tools on the host. Resolution must raise instead.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.runner import Runner
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode
from openjiuwen.core.sys_operation.config import SandboxGatewayConfig
from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec, SysOperationSpec


class _Err:
    def is_err(self) -> bool:
        return True

    def msg(self) -> str:
        return "duplicate isolation template"


def _sandbox_spec(spec_id: str) -> SysOperationSpec:
    return SysOperationSpec(id=spec_id, mode=OperationMode.SANDBOX, gateway_config=SandboxGatewayConfig())


@pytest.fixture
def failing_registration(monkeypatch):
    """The resource manager refuses the card and has nothing under its id."""
    calls = []

    def add(card):
        calls.append(card.id)
        return _Err()

    monkeypatch.setattr(Runner.resource_mgr, "get_sys_operation", lambda _id: None)
    monkeypatch.setattr(Runner.resource_mgr, "add_sys_operation", add)
    return calls


def test_local_spec_registers_once_and_returns_the_same_instance(tmp_path: Path):
    spec = SysOperationSpec(id=f"resolve_local_{uuid.uuid4().hex[:8]}", mode=OperationMode.LOCAL,
                            work_config=LocalWorkConfig(work_dir=str(tmp_path)))
    try:
        first = spec.resolve()
        assert first is not None and first.mode == OperationMode.LOCAL
        assert spec.resolve() is first, "resolve is get-or-create"
    finally:
        Runner.resource_mgr.remove_sys_operation(spec.id)


def test_failed_registration_raises_instead_of_returning_none(failing_registration):
    spec = _sandbox_spec("resolve_fail_sandbox")
    with pytest.raises(BaseError) as exc:
        spec.resolve()
    assert exc.value.code == StatusCode.SYS_OPERATION_MANAGER_PROCESS_ERROR.code
    assert "resolve_fail_sandbox" in str(exc.value) and "duplicate isolation template" in str(exc.value)
    assert failing_registration == ["resolve_fail_sandbox"]


def test_mode_mismatch_under_the_same_id_raises(monkeypatch):
    local = SimpleNamespace(mode=OperationMode.LOCAL)
    monkeypatch.setattr(Runner.resource_mgr, "get_sys_operation", lambda _id: local)
    with pytest.raises(BaseError) as exc:
        _sandbox_spec("resolve_mismatch").resolve()
    assert "registered as local" in str(exc.value) and "asks for sandbox" in str(exc.value)


def test_matching_mode_returns_the_registered_instance(monkeypatch):
    sandbox = SimpleNamespace(mode=OperationMode.SANDBOX)
    monkeypatch.setattr(Runner.resource_mgr, "get_sys_operation", lambda _id: sandbox)
    assert _sandbox_spec("resolve_existing").resolve() is sandbox


def test_deep_agent_build_does_not_fall_back_to_a_local_sys_operation(failing_registration):
    spec = DeepAgentSpec(sys_operation=_sandbox_spec("resolve_fail_build"), enable_task_loop=False)
    with pytest.raises(BaseError) as exc:
        spec.build()
    assert exc.value.code == StatusCode.SYS_OPERATION_MANAGER_PROCESS_ERROR.code
