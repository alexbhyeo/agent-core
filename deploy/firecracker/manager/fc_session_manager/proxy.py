# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TCP listeners that forward each connection to a guest vsock port.

Firecracker exposes the guest's vsock as a Unix socket on the host: a client
connects, sends ``CONNECT <port>\\n`` and gets ``OK <n>\\n`` back, after which
the stream is the guest connection. Each session gets fixed TCP ports for its
lifetime, so the ``base_url`` handed to agent-core never changes.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


async def open_guest_stream(uds_path: Path, guest_port: int,
                            timeout: float = 5.0) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(uds_path)), timeout)
    writer.write(f"CONNECT {guest_port}\n".encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout)
    if not line.startswith(b"OK"):
        writer.close()
        raise ConnectionError(f"vsock CONNECT {guest_port} refused: {line!r}")
    return reader, writer


async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


class VsockProxy:
    def __init__(self, listen_port: int, uds_path: Path, guest_port: int):
        self.listen_port = listen_port
        self.uds_path = uds_path
        self.guest_port = guest_port
        self._server: Optional[asyncio.base_events.Server] = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host="0.0.0.0", port=self.listen_port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        # Python 3.12+: wait_closed() waits for live connections (e.g. HTTP
        # keep-alive from the aio provider), so close them explicitly first.
        for writer in list(self._writers):
            writer.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("proxy %s: connections still open after stop", self.listen_port)
        self._server = None

    async def _handle(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        self._writers.add(client_writer)
        try:
            try:
                guest_reader, guest_writer = await open_guest_stream(self.uds_path, self.guest_port)
            except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
                logger.warning("proxy %s -> vsock %s failed: %s", self.listen_port, self.guest_port, exc)
                client_writer.close()
                return
            self._writers.add(guest_writer)
            try:
                await asyncio.gather(_pump(client_reader, guest_writer), _pump(guest_reader, client_writer))
            finally:
                self._writers.discard(guest_writer)
        finally:
            self._writers.discard(client_writer)


async def guest_http_get(uds_path: Path, guest_port: int, path: str, timeout: float = 3.0) -> tuple[int, bytes]:
    """Tiny HTTP/1.0 GET over vsock, used for the guest health check."""
    reader, writer = await open_guest_stream(uds_path, guest_port, timeout)
    try:
        writer.write(f"GET {path} HTTP/1.0\r\nHost: guest\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout)
    finally:
        writer.close()
    status_line = raw.split(b"\r\n", 1)[0].split()
    status = int(status_line[1]) if len(status_line) > 1 else 0
    return status, raw.split(b"\r\n\r\n", 1)[-1]
