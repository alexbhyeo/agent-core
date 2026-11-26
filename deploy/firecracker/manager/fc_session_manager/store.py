# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SQLite-backed session records (state is written before the VM is touched)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

CREATING = "CREATING"
RUNNING = "RUNNING"
PAUSED = "PAUSED"
PERSISTED = "PERSISTED"
DESTROYED = "DESTROYED"
FAULTED = "FAULTED"

LIVE_STATES = (CREATING, RUNNING, PAUSED)  # a Firecracker process exists
KEPT_STATES = (CREATING, RUNNING, PAUSED, PERSISTED)  # ports and disk are held


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
    last_active_ts: float = field(default_factory=time.time)
    state_since_ts: float = field(default_factory=time.time)
    snapshot_gen: int = 0
    checkpoint_ref: Optional[str] = None
    profile_json: str = "{}"
    mcp_port: int = 0
    workspace_mib: int = 0  # 0: no workspace drive (session predates it)
    forked_from: Optional[str] = None
    tier: str = ""  # "" = the owner's current tier
    cpu_template: str = ""  # "" = the host default template

    @property
    def profile(self) -> dict[str, Any]:
        return json.loads(self.profile_json or "{}")


_COLUMNS = list(SessionRecord.__dataclass_fields__)
_COLUMN_TYPES = {
    "session_id": "TEXT PRIMARY KEY", "owner_user_id": "TEXT NOT NULL", "state": "TEXT NOT NULL",
    "vm_id": "TEXT", "vm_index": "INTEGER", "agent_port": "INTEGER", "cdp_port": "INTEGER",
    "guest_token": "TEXT", "vcpus": "INTEGER", "mem_mib": "INTEGER", "pid": "INTEGER",
    "created_ts": "REAL", "updated_ts": "REAL", "last_active_ts": "REAL", "state_since_ts": "REAL",
    "snapshot_gen": "INTEGER DEFAULT 0", "checkpoint_ref": "TEXT", "profile_json": "TEXT DEFAULT '{}'",
    "mcp_port": "INTEGER DEFAULT 0", "workspace_mib": "INTEGER DEFAULT 0", "forked_from": "TEXT",
    "tier": "TEXT DEFAULT ''", "cpu_template": "TEXT DEFAULT ''",
}


class SessionStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        # WAL without per-commit fsync: state writes must not stall behind bulk disk copies.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        cols = ", ".join(f"{name} {_COLUMN_TYPES[name]}" for name in _COLUMNS)
        self._db.execute(f"CREATE TABLE IF NOT EXISTS sessions ({cols})")
        # Forward migration for databases created by an older manager.
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(sessions)")}
        for name in _COLUMNS:
            if name not in existing:
                self._db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {_COLUMN_TYPES[name]}")

    def _row(self, row: tuple) -> SessionRecord:
        rec = SessionRecord(*row)
        rec.snapshot_gen = rec.snapshot_gen or 0
        rec.profile_json = rec.profile_json or "{}"
        rec.mcp_port = rec.mcp_port or 0
        rec.workspace_mib = rec.workspace_mib or 0
        rec.tier = rec.tier or ""
        rec.cpu_template = rec.cpu_template or ""
        rec.last_active_ts = rec.last_active_ts or rec.updated_ts or time.time()
        rec.state_since_ts = rec.state_since_ts or rec.updated_ts or time.time()
        return rec

    def get(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            row = self._db.execute(
                f"SELECT {','.join(_COLUMNS)} FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return self._row(row) if row else None

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

    def touch(self, session_id: str, ts: float) -> None:
        with self._lock:
            self._db.execute("UPDATE sessions SET last_active_ts=? WHERE session_id=?", (ts, session_id))

    def list(self, owner_user_id: Optional[str] = None) -> list[SessionRecord]:
        sql = f"SELECT {','.join(_COLUMNS)} FROM sessions"
        args: tuple = ()
        if owner_user_id is not None:
            sql += " WHERE owner_user_id=?"
            args = (owner_user_id,)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created_ts", args).fetchall()
        return [self._row(row) for row in rows]
