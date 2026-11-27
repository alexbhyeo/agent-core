# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SQLite store for what the Playground owns: session metadata and transcripts.

VM state is not stored here; FcSessionManager is the authority for it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    team_size INTEGER NOT NULL DEFAULT 0,
    created_ts REAL NOT NULL,
    last_query_ts REAL
);
CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(owner);
CREATE TABLE IF NOT EXISTS transcript (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT,
    role TEXT NOT NULL,
    member TEXT,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS transcript_session ON transcript(session_id, seq);
"""


class PlaygroundStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def _query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, args).fetchall()]

    def _exec(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._db.execute(sql, args)

    # ------------------------------------------------------------- sessions

    def add_session(self, session_id: str, owner: str, kind: str, title: str, team_size: int = 0) -> dict[str, Any]:
        self._exec("INSERT INTO sessions (session_id, owner, kind, title, team_size, created_ts) VALUES (?,?,?,?,?,?)",
                   (session_id, owner, kind, title, team_size, time.time()))
        return self.get_session(session_id)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        rows = self._query("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        return rows[0] if rows else None

    def list_sessions(self, owner: str) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM sessions WHERE owner=? "
                           "ORDER BY COALESCE(last_query_ts, created_ts) DESC", (owner,))

    def owners(self) -> list[str]:
        return [r["owner"] for r in self._query("SELECT DISTINCT owner FROM sessions ORDER BY owner")]

    def update_session(self, session_id: str, **fields: Any) -> None:
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self._exec(f"UPDATE sessions SET {cols} WHERE session_id=?", (*fields.values(), session_id))

    def delete_session(self, session_id: str) -> None:
        self._exec("DELETE FROM transcript WHERE session_id=?", (session_id,))
        self._exec("DELETE FROM sessions WHERE session_id=?", (session_id,))

    # ----------------------------------------------------------- transcript

    def add_entry(self, session_id: str, run_id: Optional[str], role: str, kind: str, text: str,
                  member: Optional[str] = None) -> None:
        self._exec("INSERT INTO transcript (session_id, run_id, role, member, kind, text, ts) VALUES (?,?,?,?,?,?,?)",
                   (session_id, run_id, role, member, kind, text, time.time()))

    def transcript(self, session_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM transcript WHERE session_id=? ORDER BY seq DESC LIMIT ?",
                           (session_id, limit))
        return list(reversed(rows))
