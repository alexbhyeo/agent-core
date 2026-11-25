# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Start a microVM through the jailer and drive the Firecracker API socket."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiohttp

from fc_session_manager import network

logger = logging.getLogger(__name__)

API_SOCK = "api.sock"
VSOCK_UDS = "v.sock"
GUEST_CID = 3
BOOT_ARGS = (
    "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/sbin/fc-init "
    f"ip={network.GUEST_IP}::{network.TAP_IP}:255.255.255.252::eth0:off"
)


class FirecrackerError(RuntimeError):
    pass


@dataclass
class VmPaths:
    """Paths of one VM inside its jailer chroot."""

    jailer_base: Path
    vm_id: str

    @property
    def chroot(self) -> Path:
        return self.jailer_base / "firecracker" / self.vm_id / "root"

    @property
    def vm_dir(self) -> Path:
        return self.jailer_base / "firecracker" / self.vm_id

    @property
    def api_sock(self) -> Path:
        return self.chroot / API_SOCK

    @property
    def vsock_uds(self) -> Path:
        return self.chroot / VSOCK_UDS

    @property
    def console_log(self) -> Path:
        return self.vm_dir / "console.log"


class FirecrackerVm:
    def __init__(self, paths: VmPaths, uid: int):
        self.paths = paths
        self.uid = uid
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._log_fh = None

    async def spawn(self, jailer_bin: Path, firecracker_bin: Path, netns_path: str, mem_mib: int,
                    vcpus: int) -> int:
        self.paths.vm_dir.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self.paths.console_log, "ab")
        cpu_quota = vcpus * 100000
        self.proc = await asyncio.create_subprocess_exec(
            str(jailer_bin),
            "--id", self.paths.vm_id,
            "--exec-file", str(firecracker_bin),
            "--uid", str(self.uid), "--gid", str(self.uid),
            "--chroot-base-dir", str(self.paths.jailer_base),
            "--netns", netns_path,
            "--cgroup-version", "2",
            "--parent-cgroup", "fc",
            "--cgroup", f"cpu.max={cpu_quota} 100000",
            "--cgroup", f"memory.max={(mem_mib + 512) * 1024 * 1024}",
            "--",
            "--api-sock", f"/{API_SOCK}",
            stdin=asyncio.subprocess.DEVNULL, stdout=self._log_fh, stderr=self._log_fh,
        )
        for _ in range(200):
            if self.paths.api_sock.exists():
                return self.proc.pid
            if self.proc.returncode is not None:
                break
            await asyncio.sleep(0.05)
        raise FirecrackerError(f"jailer did not create the API socket; see {self.paths.console_log}")

    def place_file(self, src: Path, name: str, sparse_copy: bool = False) -> str:
        """Copy (or hard-link) a host file into the chroot, owned by the VM uid."""
        dst = self.paths.chroot / name
        if sparse_copy:
            # The rootfs image is mostly holes; keep the per-session copy sparse.
            result = subprocess.run(["cp", "--sparse=always", str(src), str(dst)], capture_output=True, check=False)
            if result.returncode != 0:
                raise FirecrackerError(f"failed to copy {src} into the chroot: {result.stderr.decode().strip()}")
        else:
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
        os.chown(dst, self.uid, self.uid)
        return f"/{name}"

    async def api(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        connector = aiohttp.UnixConnector(path=str(self.paths.api_sock))
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.request(method, f"http://localhost{path}", json=body) as resp:
                text = await resp.text()
                if resp.status >= 300:
                    raise FirecrackerError(f"{method} {path} -> {resp.status}: {text}")
                return text

    async def configure_and_boot(self, kernel: str, rootfs: str, vcpus: int, mem_mib: int,
                                 metadata: dict[str, Any]) -> None:
        await self.api("PUT", "/machine-config", {"vcpu_count": vcpus, "mem_size_mib": mem_mib, "smt": False})
        await self.api("PUT", "/boot-source", {"kernel_image_path": kernel, "boot_args": BOOT_ARGS})
        await self.api("PUT", "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": rootfs,
                                                  "is_root_device": True, "is_read_only": False})
        await self.api("PUT", "/network-interfaces/eth0", {"iface_id": "eth0", "host_dev_name": network.TAP_NAME,
                                                            "guest_mac": network.GUEST_MAC})
        await self.api("PUT", "/vsock", {"guest_cid": GUEST_CID, "uds_path": f"/{VSOCK_UDS}"})
        await self.api("PUT", "/entropy", {})
        await self.api("PUT", "/mmds/config", {"version": "V2", "network_interfaces": ["eth0"]})
        await self.api("PUT", "/mmds", {"latest": {"meta-data": metadata}})
        await self.api("PUT", "/actions", {"action_type": "InstanceStart"})

    async def pause(self) -> None:
        await self.api("PATCH", "/vm", {"state": "Paused"})

    async def resume(self) -> None:
        await self.api("PATCH", "/vm", {"state": "Resumed"})

    async def kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.send_signal(signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.proc.wait()
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None


def kill_pid(pid: Optional[int]) -> None:
    """Best-effort kill of a VM process the manager no longer holds a handle for."""
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def remove_cgroup(vm_id: str) -> None:
    path = Path("/sys/fs/cgroup/fc") / vm_id
    try:
        path.rmdir()
    except OSError:
        pass
