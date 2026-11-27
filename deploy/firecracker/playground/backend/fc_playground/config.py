# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Backend settings, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Settings:
    token: str  # shared access token the frontend sends as a bearer token
    allowed_origins: tuple[str, ...]  # CORS allowlist ("*" allows any origin)
    host: str
    port: int
    data_dir: Path
    manager_url: str
    manager_token: str
    max_runs: int
    max_iterations: int
    default_profile: dict = field(default_factory=dict)
    docker_socket: Optional[str] = None  # set when PLAYGROUND_DOCKER=1: enables "restart manager"
    manager_container: str = "fc-manager"
    serve_frontend: Optional[Path] = None  # same-origin debugging only
    known_users: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        token = _env("PLAYGROUND_TOKEN")
        if not token:
            raise SystemExit("PLAYGROUND_TOKEN must be set (the frontend sends it as a bearer token)")
        manager_token = _env("FCM_API_TOKEN")
        if not manager_token:
            raise SystemExit("FCM_API_TOKEN must be set")
        host, _, port = _env("PLAYGROUND_BIND", "0.0.0.0:8800").rpartition(":")
        frontend = _env("PLAYGROUND_SERVE_FRONTEND")
        return cls(
            token=token,
            allowed_origins=tuple(o.strip().rstrip("/") for o in _env("PLAYGROUND_ALLOWED_ORIGINS").split(",")
                                  if o.strip()),
            host=host or "0.0.0.0",
            port=int(port),
            data_dir=Path(_env("PLAYGROUND_DATA_DIR", "/var/lib/fc-playground")),
            manager_url=_env("FC_MANAGER_URL", "http://fc-manager:8700").rstrip("/"),
            manager_token=manager_token,
            max_runs=int(_env("PLAYGROUND_MAX_RUNS", "8")),
            max_iterations=int(_env("PLAYGROUND_MAX_ITERATIONS", "20")),
            default_profile={"vcpus": int(_env("PLAYGROUND_DEFAULT_VCPUS", "2")),
                             "mem_mib": int(_env("PLAYGROUND_DEFAULT_MEM_MIB", "1024"))},
            docker_socket=_env("PLAYGROUND_DOCKER_SOCKET", "/var/run/docker.sock")
            if _env("PLAYGROUND_DOCKER") == "1" else None,
            manager_container=_env("FC_MANAGER_CONTAINER", "fc-manager"),
            serve_frontend=Path(frontend) if frontend else None,
            known_users=tuple(u.strip() for u in _env("PLAYGROUND_USERS", "alice,bob,noisy,fenced").split(",")
                              if u.strip()),
        )
