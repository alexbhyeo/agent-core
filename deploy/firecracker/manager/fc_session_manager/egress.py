# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Guest egress: an audit log of outbound connections and an allowlist proxy.

* ``EgressAuditor`` follows conntrack ``NEW`` events in the manager's network
  namespace. Guest traffic leaves each VM namespace masqueraded as
  ``10.200.<slot>.2``, so every event maps to the session running in that slot.
  Records go to ``<data_dir>/audit/egress-YYYYMMDD.jsonl`` and a per-session
  in-memory ring buffer.
* ``AllowlistProxy`` is an HTTP(S) proxy for sessions whose tier or profile
  restricts egress. Their direct egress is dropped (see ``network.restrict_egress``);
  the proxy accepts CONNECT / absolute-URI requests only for allowlisted domains on
  ports 80 and 443, and only when the name resolves to a public address.
"""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import json
import logging
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional

from fc_session_manager.tiers import domain_allowed

logger = logging.getLogger(__name__)

_SLOT_SRC = re.compile(r"^10\.200\.(\d+)\.2$")
_RING = 500
_ALLOWED_PORTS = {80, 443}
_HEAD_LIMIT = 16 * 1024


def slot_of(ip: str) -> Optional[int]:
    match = _SLOT_SRC.match(ip or "")
    return int(match.group(1)) if match else None


class EgressAuditor:
    """Writes one record per new outbound guest connection (or proxy decision)."""

    def __init__(self, audit_dir: Path, session_for_slot: Callable[[int], Optional[str]]):
        self.audit_dir = audit_dir
        self._session_for_slot = session_for_slot
        self._recent: dict[str, collections.deque] = {}
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self.events = 0

    async def start(self) -> None:
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "conntrack", "-E", "-e", "NEW", "-o", "timestamp",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError:
            logger.warning("conntrack is not installed; egress audit is off")
            return
        self._task = asyncio.create_task(self._follow())
        logger.info("egress audit: following conntrack events into %s", self.audit_dir)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()

    async def _follow(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                logger.warning("conntrack event stream ended; egress audit stopped")
                return
            try:
                self._on_event(line.decode(errors="replace"))
            except Exception:  # one odd line must not stop the audit
                logger.exception("could not parse conntrack event %r", line[:200])

    def _on_event(self, line: str) -> None:
        tokens = line.replace("[NEW]", " ").split()
        fields: dict[str, str] = {}
        for token in tokens:
            key, sep, value = token.partition("=")
            if sep and key not in fields:  # the first tuple is the original direction
                fields[key] = value
        slot = slot_of(fields.get("src", ""))
        if slot is None:
            return
        dst = fields.get("dst", "")
        if dst.startswith("10.200."):  # proxy and other in-host traffic is logged by the proxy
            return
        proto = next((t for t in tokens if t in ("tcp", "udp", "icmp", "sctp")), "?")
        session_id = self._session_for_slot(slot)
        self.record(session_id, kind="connect", proto=proto, dst=dst,
                    dport=int(fields["dport"]) if fields.get("dport", "").isdigit() else None, allowed=True)

    def record(self, session_id: Optional[str], **entry: Any) -> None:
        entry = {"ts": round(time.time(), 3), "session_id": session_id, **entry}
        self.events += 1
        if session_id:
            self._recent.setdefault(session_id, collections.deque(maxlen=_RING)).append(entry)
        path = self.audit_dir / f"egress-{time.strftime('%Y%m%d', time.gmtime())}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def recent(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._recent.get(session_id, ()))[-limit:]

    def forget(self, session_id: str) -> None:
        self._recent.pop(session_id, None)


class AllowlistProxy:
    """HTTP(S) forward proxy that only lets a session reach its allowlisted domains."""

    def __init__(self, port: int, lookup: Callable[[str], Optional[tuple[str, tuple[str, ...]]]],
                 auditor: Optional[EgressAuditor]):
        self.port = port
        self._lookup = lookup  # peer ip -> (session_id, allowlist)
        self._auditor = auditor
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", self.port)
        logger.info("egress allowlist proxy listening on :%d", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()

    def _audit(self, session_id: str, **entry: Any) -> None:
        if self._auditor is not None:
            self._auditor.record(session_id, kind="proxy", **entry)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("", 0))[0]
        info = self._lookup(peer)
        try:
            if info is None:
                await self._reply(writer, 403, "not a restricted session")
                return
            session_id, allowlist = info
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                return
            if len(head) > _HEAD_LIMIT:
                await self._reply(writer, 431, "request head too large")
                return
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = (lines[0].split(" ", 2) + ["", ""])[:3]
            if method == "CONNECT":
                host, _, port_s = target.rpartition(":")
                forward = None
            else:
                url = urllib.parse.urlsplit(target)
                if url.scheme != "http" or not url.hostname:
                    await self._reply(writer, 400, "absolute http:// URL or CONNECT required")
                    return
                host, port_s = url.hostname, str(url.port or 80)
                path = urllib.parse.urlunsplit(("", "", url.path or "/", url.query, ""))
                kept = [ln for ln in lines[1:] if ln and not ln.lower().startswith("proxy-")]
                forward = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""]).encode()
            host = host.strip("[]").lower()
            port = int(port_s) if port_s.isdigit() else 0
            if port not in _ALLOWED_PORTS or not domain_allowed(host, allowlist):
                self._audit(session_id, host=host, dport=port, allowed=False, reason="not allowlisted")
                await self._reply(writer, 403, f"{host}:{port} is not on this session's allowlist")
                return
            address = await self._public_address(host, port)
            if address is None:
                self._audit(session_id, host=host, dport=port, allowed=False, reason="no public address")
                await self._reply(writer, 403, f"{host} does not resolve to a public address")
                return
            try:
                up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(address, port), 15)
            except (OSError, asyncio.TimeoutError) as exc:
                self._audit(session_id, host=host, dst=address, dport=port, allowed=True, reason=f"upstream: {exc}")
                await self._reply(writer, 502, "upstream unreachable")
                return
            self._audit(session_id, host=host, dst=address, dport=port, allowed=True)
            if forward is None:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                up_writer.write(forward)
                await up_writer.drain()
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _public_address(host: str, port: int) -> Optional[str]:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=1)  # SOCK_STREAM
        except OSError:
            return None
        for family, _, _, _, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.version == 4 and ip.is_global:
                return str(ip)
        return None

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, message: str) -> None:
        body = message.encode()
        writer.write(f"HTTP/1.1 {status} Proxy\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + body)
        try:
            await writer.drain()
        except ConnectionError:
            pass


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # already closed
            pass
