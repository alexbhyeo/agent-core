# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-VM network namespaces with NAT egress and private-range blocking.

Layout for VM index ``n`` (1..250)::

    guest eth0 172.16.0.2/30 --tap0-- [netns fc<n>: 172.16.0.1, vn0 10.200.n.2]
        --veth-- [container: vh<n> 10.200.n.1] --NAT--> egress interface

Every VM uses the same guest/TAP addresses (each lives in its own namespace),
which keeps boot arguments identical and is what snapshot clones need later.
The container forwards VM traffic only to public addresses: the host, private
ranges, link-local/metadata and multicast are dropped, and VMs cannot reach
the container itself (manager API, proxies).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

GUEST_IP = "172.16.0.2"
TAP_IP = "172.16.0.1"
GUEST_MAC = "06:00:AC:10:00:02"
TAP_NAME = "tap0"
NS_VETH = "vn0"
EGRESS_CHAIN = "FC-EGRESS"
BLOCKED_RANGES = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


class CommandError(RuntimeError):
    pass


async def run(*args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise CommandError(f"{' '.join(args)} failed ({proc.returncode}): {err.decode().strip()}")
    return out.decode()


def netns_name(index: int) -> str:
    return f"fc{index}"


def netns_path(index: int) -> str:
    return f"/var/run/netns/{netns_name(index)}"


async def setup_host(egress_iface: str) -> None:
    """Idempotent container-wide forwarding, NAT and egress filtering."""
    await run("sysctl", "-qw", "net.ipv4.ip_forward=1")
    if not await _rule_exists("nat", "POSTROUTING", "-s", "10.200.0.0/16", "-o", egress_iface, "-j", "MASQUERADE"):
        await run("iptables", "-w", "-t", "nat", "-A", "POSTROUTING", "-s", "10.200.0.0/16", "-o", egress_iface,
                  "-j", "MASQUERADE")
    await run("iptables", "-w", "-N", EGRESS_CHAIN, check=False)
    await run("iptables", "-w", "-F", EGRESS_CHAIN)
    await run("iptables", "-w", "-A", EGRESS_CHAIN, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
              "-j", "ACCEPT")
    for cidr in BLOCKED_RANGES:
        await run("iptables", "-w", "-A", EGRESS_CHAIN, "-d", cidr, "-j", "DROP")
    await run("iptables", "-w", "-A", EGRESS_CHAIN, "-o", egress_iface, "-j", "ACCEPT")
    await run("iptables", "-w", "-A", EGRESS_CHAIN, "-j", "DROP")
    if not await _rule_exists("filter", "FORWARD", "-i", "vh+", "-j", EGRESS_CHAIN):
        await run("iptables", "-w", "-I", "FORWARD", "1", "-i", "vh+", "-j", EGRESS_CHAIN)
    if not await _rule_exists("filter", "FORWARD", "-o", "vh+", "-m", "conntrack", "--ctstate",
                              "ESTABLISHED,RELATED", "-j", "ACCEPT"):
        await run("iptables", "-w", "-I", "FORWARD", "1", "-o", "vh+", "-m", "conntrack", "--ctstate",
                  "ESTABLISHED,RELATED", "-j", "ACCEPT")
    # VMs must not reach the container itself (manager API, proxies, anything listening).
    if not await _rule_exists("filter", "INPUT", "-i", "vh+", "-j", "DROP"):
        await run("iptables", "-w", "-I", "INPUT", "1", "-i", "vh+", "-j", "DROP")


async def _rule_exists(table: str, chain: str, *spec: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "iptables", "-w", "-t", table, "-C", chain, *spec,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait() == 0


async def create_vm_network(index: int) -> None:
    ns = netns_name(index)
    host_veth = f"vh{index}"
    host_ip = f"10.200.{index}.1"
    ns_ip = f"10.200.{index}.2"
    await teardown_vm_network(index)
    await run("ip", "netns", "add", ns)
    await run("ip", "link", "add", host_veth, "type", "veth", "peer", "name", NS_VETH, "netns", ns)
    await run("ip", "addr", "add", f"{host_ip}/30", "dev", host_veth)
    await run("ip", "link", "set", host_veth, "up")
    nsx = ("ip", "netns", "exec", ns)
    await run(*nsx, "ip", "link", "set", "lo", "up")
    await run(*nsx, "ip", "addr", "add", f"{ns_ip}/30", "dev", NS_VETH)
    await run(*nsx, "ip", "link", "set", NS_VETH, "up")
    await run(*nsx, "ip", "route", "add", "default", "via", host_ip)
    await run(*nsx, "ip", "tuntap", "add", "dev", TAP_NAME, "mode", "tap")
    await run(*nsx, "ip", "addr", "add", f"{TAP_IP}/30", "dev", TAP_NAME)
    await run(*nsx, "ip", "link", "set", TAP_NAME, "up")
    await run(*nsx, "sysctl", "-qw", "net.ipv4.ip_forward=1")
    await run(*nsx, "iptables", "-w", "-t", "nat", "-A", "POSTROUTING", "-s", f"{GUEST_IP}/32", "-o", NS_VETH,
              "-j", "MASQUERADE")
    # Inside the namespace the guest may only talk to the TAP gateway for routing,
    # never to services in the namespace itself.
    await run(*nsx, "iptables", "-w", "-A", "INPUT", "-i", TAP_NAME, "-j", "DROP")


async def teardown_vm_network(index: int) -> None:
    await run("ip", "netns", "del", netns_name(index), check=False)
    await run("ip", "link", "del", f"vh{index}", check=False)
