# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reader for the shared Skill library's on/off state file.

The Skill library keeps a library-wide kill switch in ``skills_state.json``,
written next to the Skills themselves by the install / marketplace flow::

    {
      "skill_configs": {
        "gamma": {"enabled": false}
      }
    }

A Skill switched off there is unavailable to *every* agent, whatever any
visibility declaration says, so the team composition folds those names into its
``disabled`` set (see
:func:`openjiuwen.agent_teams.skill.visibility.compose_skill_visibility`).

The file belongs to the library, not to the harness that happens to also read
it: this module is the team package's own reader so that the team side does not
reach into another layer's private helper for it. The format is deliberately
read defensively — a missing, unreadable or malformed file means "nothing is
switched off", which keeps a corrupted state file from silently blanking a
member's Skill view.
"""

from __future__ import annotations

import json
from pathlib import Path

from openjiuwen.core.common.logging import logger

# Basename of the library-wide Skill on/off state file.
SKILLS_STATE_FILENAME = "skills_state.json"


def collect_disabled_skills(skills_dirs: list[str | Path]) -> list[str]:
    """Collect the Skill names switched off in the given library roots.

    Args:
        skills_dirs: Skill library roots to inspect. Roots that hold no state
            file are skipped.

    Returns:
        Sorted, de-duplicated Skill names whose stored config says
        ``enabled: false``.
    """
    disabled: set[str] = set()
    for skills_dir in skills_dirs:
        state_path = Path(skills_dir) / SKILLS_STATE_FILENAME
        if not state_path.is_file():
            continue
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            logger.warning(
                "[SkillLibraryState] failed to read '%s'; treating it as no Skill disabled",
                state_path,
            )
            continue
        if not isinstance(data, dict):
            continue
        skill_configs = data.get("skill_configs", {})
        if not isinstance(skill_configs, dict):
            continue
        for name, cfg in skill_configs.items():
            if isinstance(cfg, dict) and cfg.get("enabled") is False:
                disabled.add(str(name))
    return sorted(disabled)


__all__ = [
    "SKILLS_STATE_FILENAME",
    "collect_disabled_skills",
]
