# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Team-owned reader for the shared Skill library's ``skills_state.json``.

The library-wide kill switch is read by the team package itself instead of
through another layer's private helper. These tests pin the contract that
reader has to keep: only ``enabled: false`` entries count, the result is sorted
and de-duplicated across roots, and every unreadable shape degrades to "nothing
is switched off" rather than blanking a member's Skill view.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openjiuwen.agent_teams.skill import collect_disabled_skills
from openjiuwen.agent_teams.skill.library_state import SKILLS_STATE_FILENAME
from tests.test_logger import logger as test_logger


def _write_state(library: Path, payload: object) -> None:
    """Write a ``skills_state.json`` payload into *library*."""
    library.mkdir(parents=True, exist_ok=True)
    (library / SKILLS_STATE_FILENAME).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


@pytest.mark.level0
def test_collect_disabled_skills_reads_disabled_entries(tmp_path: Path) -> None:
    """Only entries whose stored config says ``enabled: false`` are collected."""
    library = tmp_path / "library"
    _write_state(
        library,
        {
            "skill_configs": {
                "gamma": {"enabled": False},
                "alpha": {"enabled": True},
                "beta": {},
            }
        },
    )

    disabled = collect_disabled_skills([str(library)])

    test_logger.info(f"disabled skills: {disabled}")
    assert disabled == ["gamma"]


@pytest.mark.level1
def test_collect_disabled_skills_merges_roots_sorted(tmp_path: Path) -> None:
    """Names from several roots are merged, de-duplicated and sorted."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_state(first, {"skill_configs": {"zeta": {"enabled": False}}})
    _write_state(
        second,
        {"skill_configs": {"zeta": {"enabled": False}, "alpha": {"enabled": False}}},
    )

    disabled = collect_disabled_skills([first, str(second)])

    assert disabled == ["alpha", "zeta"]


@pytest.mark.level1
def test_collect_disabled_skills_tolerates_missing_and_corrupt_state(tmp_path: Path) -> None:
    """A missing, malformed or wrongly-shaped state file disables nothing."""
    missing = tmp_path / "missing"
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / SKILLS_STATE_FILENAME).write_text("{not json", encoding="utf-8")
    not_an_object = tmp_path / "list-root"
    _write_state(not_an_object, ["gamma"])
    bad_configs = tmp_path / "bad-configs"
    _write_state(bad_configs, {"skill_configs": ["gamma"]})
    bad_entry = tmp_path / "bad-entry"
    _write_state(bad_entry, {"skill_configs": {"gamma": "disabled"}})

    disabled = collect_disabled_skills(
        [
            str(missing),
            str(corrupt),
            str(not_an_object),
            str(bad_configs),
            str(bad_entry),
        ],
    )

    test_logger.info(f"disabled skills from broken state files: {disabled}")
    assert disabled == []


@pytest.mark.level1
def test_collect_disabled_skills_has_no_harness_dependency() -> None:
    """The team reader stands alone: importing it pulls in no harness factory.

    The previous implementation delegated to a private harness helper, so a
    rename there would have broken every team member's Skill filtering with no
    test turning red. This pins the decoupling itself.
    """
    import inspect

    from openjiuwen.agent_teams.rails import team_skill_use_rail
    from openjiuwen.agent_teams.skill import library_state

    assert "openjiuwen.harness" not in inspect.getsource(library_state)
    rail_source = inspect.getsource(team_skill_use_rail)
    assert "_collect_disabled_skills_from_state" not in rail_source
