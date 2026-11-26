# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Settings for FcSessionManager, read once from environment variables."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _json_setting(name: str) -> str:
    """Inline JSON in ``NAME`` or the content of the file named by ``NAME_FILE`` ("" if neither)."""
    inline = os.environ.get(name, "").strip()
    if inline:
        return inline
    path = os.environ.get(f"{name}_FILE", "").strip()
    return Path(path).read_text() if path else ""


def _opt_path(name: str) -> Optional[Path]:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


@dataclass(frozen=True)
class Settings:
    api_token: str
    listen_host: str
    listen_port: int
    advertise_host: str
    proxy_port_start: int
    max_vms: int
    data_dir: Path
    firecracker_bin: Path
    jailer_bin: Path
    kernel_path: Path
    rootfs_path: Path
    default_vcpus: int
    default_mem_mib: int
    max_vcpus: int
    max_mem_mib: int
    uid_base: int
    egress_interface: str
    boot_timeout_s: float
    proxy_port_count: int
    ttl_pause_s: float
    ttl_persist_s: float
    ttl_destroy_s: float
    reaper_interval_s: float
    wake_on_request: bool
    persist_on_shutdown: bool
    cpu_template_path: Optional[Path]
    workspace_mib: int
    diff_snapshots: bool
    snapshot_editor_bin: Path
    tiers_json: str
    user_tiers_json: str
    default_tier: str
    balloon: bool
    balloon_reclaim: bool
    egress_audit: bool
    egress_proxy_port: int
    snapshot_compress: bool
    snapshot_encrypt: bool
    snapshot_key_file: Optional[Path]
    snapshot_seal_after_s: float
    warm_pool_size: int
    alert_create_p95_s: float

    @property
    def cpu_template_name(self) -> Optional[str]:
        return self.cpu_template_path.stem if self.cpu_template_path else None

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def jailer_base(self) -> Path:
        return self.data_dir / "jl"

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state.db"

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("FCM_API_TOKEN", "").strip()
        if not token:
            raise RuntimeError("FCM_API_TOKEN must be set")
        return cls(
            api_token=token,
            listen_host=_env("FCM_LISTEN_HOST", "0.0.0.0"),
            listen_port=int(_env("FCM_LISTEN_PORT", "8700")),
            advertise_host=_env("FCM_ADVERTISE_HOST", socket.gethostname()),
            proxy_port_start=int(_env("FCM_PROXY_PORT_START", "20000")),
            # Index n uses 10.200.n.0/30 on the host side, so at most 250 VMs.
            max_vms=min(int(_env("FCM_MAX_VMS", "64")), 250),
            data_dir=Path(_env("FCM_DATA_DIR", "/var/lib/fc-sessions")),
            firecracker_bin=Path(_env("FCM_FIRECRACKER_BIN", "/usr/local/bin/firecracker")),
            jailer_bin=Path(_env("FCM_JAILER_BIN", "/usr/local/bin/jailer")),
            kernel_path=Path(_env("FCM_KERNEL", "/opt/fc/vmlinux")),
            rootfs_path=Path(_env("FCM_ROOTFS", "/var/lib/fc-images/rootfs.ext4")),
            default_vcpus=int(_env("FCM_DEFAULT_VCPUS", "2")),
            default_mem_mib=int(_env("FCM_DEFAULT_MEM_MIB", "2048")),
            max_vcpus=int(_env("FCM_MAX_VCPUS", "4")),
            max_mem_mib=int(_env("FCM_MAX_MEM_MIB", "8192")),
            uid_base=int(_env("FCM_UID_BASE", "10000")),
            egress_interface=_env("FCM_EGRESS_IFACE", "eth0"),
            boot_timeout_s=float(_env("FCM_BOOT_TIMEOUT_S", "90")),
            proxy_port_count=int(_env("FCM_PROXY_PORT_COUNT", "400")),
            # Idle policy: T1 running -> paused (no traffic), T2 paused -> persisted, T3 persisted -> destroyed.
            ttl_pause_s=float(_env("FCM_TTL_PAUSE_S", "600")),
            ttl_persist_s=float(_env("FCM_TTL_PERSIST_S", "1800")),
            ttl_destroy_s=float(_env("FCM_TTL_DESTROY_S", str(7 * 24 * 3600))),
            reaper_interval_s=float(_env("FCM_REAPER_INTERVAL_S", "2")),
            wake_on_request=_env("FCM_WAKE_ON_REQUEST", "1") == "1",
            persist_on_shutdown=_env("FCM_PERSIST_ON_SHUTDOWN", "1") == "1",
            cpu_template_path=Path(p) if (p := _env("FCM_CPU_TEMPLATE", "/opt/fc/cpu-templates/T2.json"))
            and p != "none" else None,
            # Size of each session's /workspace drive (sparse; 0 keeps /workspace on the rootfs).
            workspace_mib=int(_env("FCM_WORKSPACE_MIB", "4096")),
            # Track dirty pages so a persist after a restore writes only changed memory.
            diff_snapshots=_env("FCM_DIFF_SNAPSHOTS", "1") == "1",
            snapshot_editor_bin=Path(_env("FCM_SNAPSHOT_EDITOR_BIN", "/usr/local/bin/snapshot-editor")),
            # Per-user tiers (VM size caps, rate limits, TTLs, session quota, egress); see tiers.py.
            tiers_json=_json_setting("FCM_TIERS"),
            user_tiers_json=_json_setting("FCM_USER_TIERS"),
            default_tier=_env("FCM_DEFAULT_TIER", "standard"),
            # Memory balloon (deflates on guest OOM); reclaim returns idle guest memory to the host.
            balloon=_env("FCM_BALLOON", "1") == "1",
            balloon_reclaim=_env("FCM_BALLOON_RECLAIM", "1") == "1",
            # Log every new outbound connection of a guest (conntrack events) to data_dir/audit.
            egress_audit=_env("FCM_EGRESS_AUDIT", "1") == "1",
            # Allowlist proxy for sessions whose tier/profile restricts egress (0 disables it).
            egress_proxy_port=int(_env("FCM_EGRESS_PROXY_PORT", "3128")),
            # Persisted snapshots older than snapshot_seal_after_s are compressed (zstd) and
            # encrypted (AES-256-CTR, key generated under data_dir/keys unless a key file is given);
            # they are unsealed on restore.
            snapshot_compress=_env("FCM_SNAPSHOT_COMPRESS", "1") == "1",
            snapshot_encrypt=_env("FCM_SNAPSHOT_ENCRYPT", "1") == "1",
            snapshot_key_file=_opt_path("FCM_SNAPSHOT_KEY_FILE"),
            snapshot_seal_after_s=float(_env("FCM_SNAPSHOT_SEAL_AFTER_S", "600")),
            # Pre-booted VMs with the default profile, handed out on create.
            warm_pool_size=int(_env("FCM_WARM_POOL_SIZE", "0")),
            alert_create_p95_s=float(_env("FCM_ALERT_CREATE_P95_S", "5")),
        )
