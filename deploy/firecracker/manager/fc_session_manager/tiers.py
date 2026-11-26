# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-user tiers: how large a user's sessions may be and how hard they are throttled.

A tier is chosen by the operator per user (``FCM_USER_TIERS``: ``{"alice": "small"}``),
never by the caller; a session ``profile`` can only ask for values inside its tier.
``FCM_TIERS`` (``{"name": {field: value}}``) adds tiers or overrides built-in ones. Both
take inline JSON or a file through ``FCM_TIERS_FILE`` / ``FCM_USER_TIERS_FILE``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, fields, replace
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REFILL_MS = 100  # token buckets refill every 100 ms, so bursts stay short


@dataclass(frozen=True)
class Tier:
    name: str
    vcpus: Optional[int] = None  # defaults; None = manager default
    mem_mib: Optional[int] = None
    max_vcpus: Optional[int] = None  # caps; None = manager maximum
    max_mem_mib: Optional[int] = None
    workspace_mib: Optional[int] = None
    max_sessions: int = 0  # kept (not destroyed) sessions per user; 0 = unlimited
    disk_mbps: float = 0  # MiB/s per drive; 0 = unlimited
    disk_iops: int = 0  # operations/s per drive; 0 = unlimited
    net_mbps: float = 0  # Mbit/s per direction; 0 = unlimited
    ttl_pause_s: Optional[float] = None
    ttl_persist_s: Optional[float] = None
    ttl_destroy_s: Optional[float] = None
    egress_allowlist: Optional[tuple[str, ...]] = None  # None = open internet; else domains (``*.x`` allowed)
    extra: dict[str, Any] = field(default_factory=dict)

    def drive_rate_limiter(self) -> Optional[dict[str, Any]]:
        limiter: dict[str, Any] = {}
        if self.disk_mbps > 0:
            limiter["bandwidth"] = {"size": int(self.disk_mbps * 2 ** 20 * _REFILL_MS / 1000),
                                    "refill_time": _REFILL_MS}
        if self.disk_iops > 0:
            limiter["ops"] = {"size": max(1, int(self.disk_iops * _REFILL_MS / 1000)), "refill_time": _REFILL_MS}
        return limiter or None

    def net_rate_limiter(self) -> Optional[dict[str, Any]]:
        if self.net_mbps <= 0:
            return None
        return {"bandwidth": {"size": int(self.net_mbps * 1e6 / 8 * _REFILL_MS / 1000), "refill_time": _REFILL_MS}}

    def describe(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "extra"}
        if self.egress_allowlist is not None:
            out["egress_allowlist"] = list(self.egress_allowlist)
        return out


BUILTIN_TIERS: dict[str, Tier] = {
    "standard": Tier("standard"),
    "small": Tier("small", vcpus=1, mem_mib=1024, max_vcpus=2, max_mem_mib=2048, workspace_mib=2048,
                  max_sessions=5, disk_mbps=50, disk_iops=2000, net_mbps=50),
    "restricted": Tier("restricted", vcpus=1, mem_mib=1024, max_vcpus=2, max_mem_mib=2048, max_sessions=5,
                       disk_mbps=50, disk_iops=2000, net_mbps=20,
                       egress_allowlist=("pypi.org", "files.pythonhosted.org", "github.com", "*.github.com",
                                         "*.githubusercontent.com")),
}

_FIELD_NAMES = {f.name for f in fields(Tier)} - {"name", "extra"}


def _tier_from(name: str, base: Optional[Tier], values: dict[str, Any]) -> Tier:
    unknown = set(values) - _FIELD_NAMES
    if unknown:
        raise ValueError(f"tier {name}: unknown fields {sorted(unknown)}")
    if "egress_allowlist" in values and values["egress_allowlist"] is not None:
        values = dict(values, egress_allowlist=tuple(str(d).lower() for d in values["egress_allowlist"]))
    return replace(base or Tier(name), name=name, **values)


class TierBook:
    def __init__(self, tiers_json: str, user_tiers_json: str, default_tier: str):
        self.tiers = dict(BUILTIN_TIERS)
        for name, values in (json.loads(tiers_json) if tiers_json else {}).items():
            self.tiers[name] = _tier_from(name, self.tiers.get(name), dict(values))
        self.users: dict[str, str] = {
            str(k): str(v) for k, v in (json.loads(user_tiers_json) if user_tiers_json else {}).items()}
        for user, tier in self.users.items():
            if tier not in self.tiers:
                raise ValueError(f"user {user} is mapped to unknown tier {tier}")
        if default_tier not in self.tiers:
            raise ValueError(f"default tier {default_tier} is not defined")
        self.default = default_tier
        logger.info("tiers: %s; %d user mapping(s); default %s", sorted(self.tiers), len(self.users), default_tier)

    def for_user(self, owner_user_id: str) -> Tier:
        return self.tiers[self.users.get(owner_user_id, self.default)]

    def get(self, name: str) -> Tier:
        return self.tiers.get(name) or self.tiers[self.default]


def domain_allowed(host: str, allowlist: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowlist:
        if entry.startswith("*."):
            if host.endswith(entry[1:]) and host != entry[2:]:
                return True
        elif host == entry:
            return True
    return False
