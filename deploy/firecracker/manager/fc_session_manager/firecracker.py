# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Start a microVM through the jailer and drive the Firecracker API socket."""

from __future__ import annotations

import asyncio
import json
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
SNAP_DIR = "snap"
GUEST_CID = 3
BOOT_ARGS = (
    "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/sbin/fc-init "
    f"ip={network.GUEST_IP}::{network.TAP_IP}:255.255.255.252::eth0:off"
)


class FirecrackerError(RuntimeError):
    pass


@dataclass
class VmPaths:
    """Paths of one VM run inside its jailer chroot."""

    jailer_base: Path
    vm_id: str

    @property
    def vm_dir(self) -> Path:
        return self.jailer_base / "firecracker" / self.vm_id

    @property
    def chroot(self) -> Path:
        return self.vm_dir / "root"

    @property
    def api_sock(self) -> Path:
        return self.chroot / API_SOCK

    @property
    def vsock_uds(self) -> Path:
        return self.chroot / VSOCK_UDS

    @property
    def snap_dir(self) -> Path:
        return self.chroot / SNAP_DIR

    @property
    def console_log(self) -> Path:
        return self.vm_dir / "console.log"


def link_or_copy(src: Path, dst: Path, sparse: bool = False) -> None:
    """Hard-link when on the same filesystem (data volume), else copy."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
        return
    except OSError:
        pass
    if sparse:
        result = subprocess.run(["cp", "--sparse=always", str(src), str(dst)], capture_output=True, check=False)
        if result.returncode != 0:
            raise FirecrackerError(f"failed to copy {src}: {result.stderr.decode().strip()}")
    else:
        shutil.copy2(src, dst)


def sparse_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["cp", "--sparse=always", str(src), str(dst)], capture_output=True, check=False)
    if result.returncode != 0:
        raise FirecrackerError(f"failed to copy {src}: {result.stderr.decode().strip()}")


class FirecrackerVm:
    def __init__(self, paths: VmPaths, uid: int):
        self.paths = paths
        self.uid = uid
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._log_fh = None

    async def spawn(self, jailer_bin: Path, firecracker_bin: Path, netns_path: str, mem_mib: int,
                    vcpus: int, extra_cgroups: Optional[list[str]] = None) -> int:
        self.paths.vm_dir.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self.paths.console_log, "ab")
        cgroup_args: list[str] = ["--cgroup", f"cpu.max={vcpus * 100000} 100000",
                                  "--cgroup", f"memory.max={(mem_mib + 512) * 1024 * 1024}"]
        for item in extra_cgroups or []:
            cgroup_args += ["--cgroup", item]
        self.proc = await asyncio.create_subprocess_exec(
            str(jailer_bin),
            "--id", self.paths.vm_id,
            "--exec-file", str(firecracker_bin),
            "--uid", str(self.uid), "--gid", str(self.uid),
            "--chroot-base-dir", str(self.paths.jailer_base),
            "--netns", netns_path,
            "--cgroup-version", "2",
            "--parent-cgroup", "fc",
            *cgroup_args,
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

    def place(self, src: Path, name: str, sparse: bool = False) -> str:
        """Make a host file visible in the chroot (hard link when possible), owned by the VM uid."""
        dst = self.paths.chroot / name
        link_or_copy(src, dst, sparse=sparse)
        os.chown(dst, self.uid, self.uid)
        return f"/{name}"

    def prepare_snap_dir(self) -> None:
        self.paths.snap_dir.mkdir(parents=True, exist_ok=True)
        os.chown(self.paths.snap_dir, self.uid, self.uid)

    async def api(self, method: str, path: str, body: Optional[dict[str, Any]] = None,
                  timeout: float = 120) -> Any:
        connector = aiohttp.UnixConnector(path=str(self.paths.api_sock))
        async with aiohttp.ClientSession(connector=connector,
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.request(method, f"http://localhost{path}", json=body) as resp:
                text = await resp.text()
                if resp.status >= 300:
                    raise FirecrackerError(f"{method} {path} -> {resp.status}: {text}")
                return text

    async def configure_and_boot(self, kernel: str, rootfs: str, vcpus: int, mem_mib: int,
                                 metadata: dict[str, Any], cpu_template: Optional[dict[str, Any]] = None,
                                 track_dirty_pages: bool = False, workspace: Optional[str] = None,
                                 drive_limiter: Optional[dict[str, Any]] = None,
                                 net_limiter: Optional[dict[str, Any]] = None, balloon: bool = False) -> None:
        if cpu_template is not None:
            await self.api("PUT", "/cpu-config", cpu_template)
        machine = {"vcpu_count": vcpus, "mem_size_mib": mem_mib, "smt": False}
        if track_dirty_pages:
            machine["track_dirty_pages"] = True
        await self.api("PUT", "/machine-config", machine)
        await self.api("PUT", "/boot-source", {"kernel_image_path": kernel, "boot_args": BOOT_ARGS})
        drives = [("rootfs", rootfs, True)] + ([("workspace", workspace, False)] if workspace else [])
        for drive_id, path, is_root in drives:
            drive: dict[str, Any] = {"drive_id": drive_id, "path_on_host": path, "is_root_device": is_root,
                                     "is_read_only": False}
            if drive_limiter:
                drive["rate_limiter"] = drive_limiter
            await self.api("PUT", f"/drives/{drive_id}", drive)
        iface: dict[str, Any] = {"iface_id": "eth0", "host_dev_name": network.TAP_NAME, "guest_mac": network.GUEST_MAC}
        if net_limiter:
            iface["rx_rate_limiter"] = net_limiter
            iface["tx_rate_limiter"] = net_limiter
        await self.api("PUT", "/network-interfaces/eth0", iface)
        if balloon:
            # Starts empty; deflates by itself when the guest would otherwise OOM.
            await self.api("PUT", "/balloon", {"amount_mib": 0, "deflate_on_oom": True,
                                               "stats_polling_interval_s": 2})
        await self.api("PUT", "/vsock", {"guest_cid": GUEST_CID, "uds_path": f"/{VSOCK_UDS}"})
        await self.api("PUT", "/entropy", {})
        await self.api("PUT", "/mmds/config", {"version": "V2", "network_interfaces": ["eth0"]})
        await self.put_mmds(metadata)
        await self.api("PUT", "/actions", {"action_type": "InstanceStart"})

    async def put_mmds(self, metadata: dict[str, Any]) -> None:
        await self.api("PUT", "/mmds", {"latest": {"meta-data": metadata}})

    async def pause(self) -> None:
        await self.api("PATCH", "/vm", {"state": "Paused"})

    async def resume(self) -> None:
        await self.api("PATCH", "/vm", {"state": "Resumed"})

    async def balloon_set(self, amount_mib: int) -> None:
        await self.api("PATCH", "/balloon", {"amount_mib": int(amount_mib)})

    async def balloon_stats(self) -> dict[str, Any]:
        return json.loads(await self.api("GET", "/balloon/statistics", timeout=5))

    async def update_drive(self, drive_id: str, path_on_host: str) -> None:
        """Point a drive at a (new) backing file while the VM runs."""
        await self.api("PATCH", f"/drives/{drive_id}", {"drive_id": drive_id, "path_on_host": path_on_host})

    async def snapshot_create(self, snapshot_type: str = "Full", state_name: str = "vm.state",
                              mem_name: str = "mem") -> tuple[Path, Path]:
        """Snapshot a paused VM into the chroot; returns (vmstate, mem) host paths.

        Existing targets are unlinked first: the memory file a restored VM runs
        from is still mapped (and may be shared with forks), so it must never be
        truncated in place.
        """
        self.prepare_snap_dir()
        state, mem = self.paths.snap_dir / state_name, self.paths.snap_dir / mem_name
        for target in (state, mem):
            target.unlink(missing_ok=True)
        await self.api("PUT", "/snapshot/create", {
            "snapshot_type": snapshot_type,
            "snapshot_path": f"/{SNAP_DIR}/{state_name}",
            "mem_file_path": f"/{SNAP_DIR}/{mem_name}",
        }, timeout=600)
        return state, mem

    async def snapshot_load(self, resume: bool = True, track_dirty_pages: bool = False) -> None:
        body: dict[str, Any] = {
            "snapshot_path": f"/{SNAP_DIR}/vm.state",
            "mem_backend": {"backend_type": "File", "backend_path": f"/{SNAP_DIR}/mem"},
            "resume_vm": resume,
        }
        if track_dirty_pages:
            body["track_dirty_pages"] = True
        await self.api("PUT", "/snapshot/load", body, timeout=600)

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
