# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import abc
import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from enum import Enum
from typing import Any, Dict, List, Optional


class SandboxStatus(Enum):
    RUNNING = "running"
    PAUSED = "paused"
    KILLED = "killed"


@dataclass
class SandboxRecord:
    sandbox_id: str
    base_url: str
    status: SandboxStatus
    launcher_type: str
    sandbox_type: str
    container_config_hash: str
    created_ts: float = field(default_factory=time.time)
    last_used_ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class AbstractSandboxStore(abc.ABC):
    @abc.abstractmethod
    async def get(self, key: str) -> Optional[SandboxRecord]:
        pass

    @abc.abstractmethod
    async def set(self, key: str, record: SandboxRecord) -> None:
        pass

    @abc.abstractmethod
    async def hdel(self, key: str) -> Optional[SandboxRecord]:
        pass

    @abc.abstractmethod
    async def flushdb(self) -> List[SandboxRecord]:
        pass

    @abc.abstractmethod
    async def evict_expired(self, idle_ttl_seconds: int, now: float) -> List[SandboxRecord]:
        pass

    async def list_records(self) -> List[tuple[str, SandboxRecord]]:
        """All (key, record) pairs; optional for custom stores (used by session release)."""
        raise NotImplementedError(f"{type(self).__name__} does not support listing records")


class SqliteSandboxStore(AbstractSandboxStore):
    """Sandbox records in a SQLite file, so they survive an agent-core restart.

    Records hold no secrets: launcher metadata saved here must not include tokens.
    """

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.execute("CREATE TABLE IF NOT EXISTS sandbox_records (key TEXT PRIMARY KEY, record TEXT NOT NULL)")

    @staticmethod
    def _dump(record: SandboxRecord) -> str:
        data = asdict(record)
        data["status"] = record.status.value
        return json.dumps(data, default=str)

    @staticmethod
    def _load(raw: str) -> SandboxRecord:
        data = json.loads(raw)
        data["status"] = SandboxStatus(data["status"])
        return SandboxRecord(**data)

    def _get(self, key: str) -> Optional[SandboxRecord]:
        with self._lock:
            row = self._db.execute("SELECT record FROM sandbox_records WHERE key=?", (key,)).fetchone()
        return self._load(row[0]) if row else None

    def _set(self, key: str, record: SandboxRecord) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO sandbox_records (key, record) VALUES (?, ?)",
                             (key, self._dump(record)))

    def _hdel(self, key: str) -> Optional[SandboxRecord]:
        with self._lock:
            row = self._db.execute("SELECT record FROM sandbox_records WHERE key=?", (key,)).fetchone()
            self._db.execute("DELETE FROM sandbox_records WHERE key=?", (key,))
        return self._load(row[0]) if row else None

    def _all(self) -> List[tuple[str, SandboxRecord]]:
        with self._lock:
            rows = self._db.execute("SELECT key, record FROM sandbox_records").fetchall()
        return [(key, self._load(raw)) for key, raw in rows]

    async def get(self, key: str) -> Optional[SandboxRecord]:
        return await asyncio.to_thread(self._get, key)

    async def set(self, key: str, record: SandboxRecord) -> None:
        await asyncio.to_thread(self._set, key, record)

    async def hdel(self, key: str) -> Optional[SandboxRecord]:
        return await asyncio.to_thread(self._hdel, key)

    async def flushdb(self) -> List[SandboxRecord]:
        records = await asyncio.to_thread(self._all)
        for key, _ in records:
            await asyncio.to_thread(self._hdel, key)
        return [record for _, record in records]

    async def list_records(self) -> List[tuple[str, SandboxRecord]]:
        return await asyncio.to_thread(self._all)

    async def evict_expired(self, idle_ttl_seconds: int, now: float) -> List[SandboxRecord]:
        expired = [(k, r) for k, r in await asyncio.to_thread(self._all)
                   if (now - r.last_used_ts) > idle_ttl_seconds]
        for key, _ in expired:
            await asyncio.to_thread(self._hdel, key)
        return [record for _, record in expired]


def build_sandbox_store(store_type: str = "memory", sqlite_path: Optional[str] = None) -> AbstractSandboxStore:
    """Store for SandboxGateway records, selected by ``GatewayStoreConfig.type``."""
    from openjiuwen.core.common.exception.codes import StatusCode
    from openjiuwen.core.common.exception.errors import build_error

    if store_type == "sqlite":
        if not sqlite_path:
            raise build_error(StatusCode.SYS_OPERATION_SANDBOX_GATEWAY_ERROR, operation="build_store",
                              error_msg="GatewayStoreConfig.sqlite_path is required for type='sqlite'")
        return SqliteSandboxStore(sqlite_path)
    if store_type != "memory":
        raise build_error(StatusCode.SYS_OPERATION_SANDBOX_GATEWAY_ERROR, operation="build_store",
                          error_msg=f"unsupported sandbox store type: {store_type}")
    return InMemorySandboxStore()


class InMemorySandboxStore(AbstractSandboxStore):
    def __init__(self) -> None:
        self._records: Dict[str, SandboxRecord] = {}

    async def get(self, key: str) -> Optional[SandboxRecord]:
        return self._records.get(key)

    async def set(self, key: str, record: SandboxRecord) -> None:
        self._records[key] = record

    async def hdel(self, key: str) -> Optional[SandboxRecord]:
        return self._records.pop(key, None)

    async def flushdb(self) -> List[SandboxRecord]:
        records = list(self._records.values())
        self._records.clear()
        return records

    async def list_records(self) -> List[tuple[str, SandboxRecord]]:
        return list(self._records.items())

    async def evict_expired(self, idle_ttl_seconds: int, now: float) -> List[SandboxRecord]:
        expired = [k for k, r in self._records.items() if (now - r.last_used_ts) > idle_ttl_seconds]
        return [self._records.pop(k) for k in expired]
