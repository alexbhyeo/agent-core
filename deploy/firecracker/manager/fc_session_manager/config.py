# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Settings for FcSessionManager, read once from environment variables."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


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
        )
