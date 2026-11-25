# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SQLite-backed session records (state is written before the VM is touched)."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

CREATING = "CREATING"
RUNNING = "RUNNING"
PAUSED = "PAUSED"
DESTROYED = "DESTROYED"
FAULTED = "FAULTED"


@dataclass
class SessionRecord:
    session_id: str
    owner_user_id: str
    state: str
    vm_id: str
    vm_index: int
    agent_port: int
    cdp_port: int
    guest_token: str
    vcpus: int
    mem_mib: int
    pid: Optional[int] = None
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)


_COLUMNS = list(SessionRecord.__dataclass_fields__)


class SessionStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "session_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, state TEXT NOT NULL, "
            "vm_id TEXT, vm_index INTEGER, agent_port INTEGER, cdp_port INTEGER, guest_token TEXT, "
            "vcpus INTEGER, mem_mib INTEGER, pid INTEGER, created_ts REAL, updated_ts REAL)"
        )

    def get(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            row = self._db.execute(
                f"SELECT {','.join(_COLUMNS)} FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return SessionRecord(*row) if row else None

    def put(self, record: SessionRecord) -> None:
        record.updated_ts = time.time()
        values = asdict(record)
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO sessions ({','.join(_COLUMNS)}) VALUES ({','.join('?' * len(_COLUMNS))})",
                [values[c] for c in _COLUMNS],
            )

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

    def list(self, owner_user_id: Optional[str] = None) -> list[SessionRecord]:
        sql = f"SELECT {','.join(_COLUMNS)} FROM sessions"
        args: tuple = ()
        if owner_user_id is not None:
            sql += " WHERE owner_user_id=?"
            args = (owner_user_id,)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created_ts", args).fetchall()
        return [SessionRecord(*row) for row in rows]
