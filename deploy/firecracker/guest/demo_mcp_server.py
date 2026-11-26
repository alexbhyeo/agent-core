# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tiny stdio MCP server shipped in the guest image, used to verify that a
user's MCP servers run inside that user's own microVM."""

import json
import socket
import urllib.request

from mcp.server.fastmcp import FastMCP

server = FastMCP("fc-demo")


def _mmds(path: str) -> str:
    req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                 headers={"X-metadata-token-ttl-seconds": "60"})
    with urllib.request.urlopen(req, timeout=2) as resp:
        token = resp.read().decode()
    req = urllib.request.Request(f"http://169.254.169.254/latest/meta-data/{path}",
                                 headers={"X-metadata-token": token})
    with urllib.request.urlopen(req, timeout=2) as resp:
        return resp.read().decode()


@server.tool()
def whoami() -> str:
    """Return the session id and owner of the microVM this server runs in."""
    return json.dumps({"session_id": _mmds("session_id"), "owner_user_id": _mmds("owner_user_id"),
                       "hostname": socket.gethostname()})


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    server.run()
