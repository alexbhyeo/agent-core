# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Session lifecycle: create / pause / resume / destroy, one microVM each.

Phase 1 keeps VMs in memory only (no persist). Every call is checked against
the session's ``owner_user_id``; a different owner gets ``Forbidden`` even if
it knows the session id.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fc_session_manager import network
from fc_session_manager.config import Settings
from fc_session_manager.firecracker import FirecrackerVm, VmPaths, kill_pid, remove_cgroup
from fc_session_manager.proxy import VsockProxy, guest_http_get
from fc_session_manager.store import (
    CREATING, DESTROYED, FAULTED, PAUSED, RUNNING, SessionRecord, SessionStore,
)

logger = logging.getLogger(__name__)

AGENT_VSOCK_PORT = 8080
CDP_VSOCK_PORT = 9222
_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")


class SessionError(Exception):
    status = 400


class NotFound(SessionError):
    status = 404


class Forbidden(SessionError):
    status = 403


class Conflict(SessionError):
    status = 409


class Unavailable(SessionError):
    status = 503


@dataclass
class _Runtime:
    vm: FirecrackerVm
    proxies: list[VsockProxy]


class SessionManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = SessionStore(settings.state_db)
        self._runtimes: dict[str, _Runtime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._alloc_lock = asyncio.Lock()
        self._rootfs_source: Path = settings.rootfs_path

    # ------------------------------------------------------------------ setup

    async def startup(self) -> None:
        """Reconcile: VMs never survive a manager/container restart in phase 1."""
        await network.setup_host(self.settings.egress_interface)
        self._rootfs_source = await asyncio.to_thread(self._cache_rootfs)
        for rec in self.store.list():
            if rec.state in (CREATING, RUNNING, PAUSED):
                logger.warning("session %s was %s before restart; marking FAULTED", rec.session_id, rec.state)
                kill_pid(rec.pid)
                await self._cleanup_resources(rec)
                rec.state, rec.pid = FAULTED, None
                self.store.put(rec)

    async def shutdown(self) -> None:
        for session_id in list(self._runtimes):
            rec = self.store.get(session_id)
            if rec is not None:
                await self._destroy_locked(rec)

    # ---------------------------------------------------------------- helpers

    def _cache_rootfs(self) -> Path:
        """Keep one copy of the base rootfs on the data volume.

        Per-session disks are copied from it, on the same filesystem as the
        jailer chroots; reading the image from a bind mount on every boot can
        be an order of magnitude slower (e.g. Docker Desktop file sharing).
        """
        src = self.settings.rootfs_path
        cached = self.settings.data_dir / "images" / "rootfs.ext4"
        st = src.stat()
        if cached.exists() and cached.stat().st_size == st.st_size and cached.stat().st_mtime >= st.st_mtime:
            return cached
        cached.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        tmp = cached.with_suffix(".tmp")
        result = subprocess.run(["cp", "--sparse=always", str(src), str(tmp)], capture_output=True, check=False)
        if result.returncode != 0:
            logger.warning("rootfs cache copy failed (%s); using %s directly", result.stderr.decode().strip(), src)
            return src
        os.chmod(tmp, 0o644)
        os.replace(tmp, cached)
        os.utime(cached, (st.st_atime, st.st_mtime))
        logger.info("cached rootfs %s -> %s in %.1fs", src, cached, time.monotonic() - started)
        return cached

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    @staticmethod
    def _check_ids(session_id: str, owner_user_id: str) -> None:
        if not _ID_RE.match(session_id or ""):
            raise SessionError("invalid session_id")
        if not _ID_RE.match(owner_user_id or ""):
            raise SessionError("invalid owner_user_id")

    def _owned(self, session_id: str, owner_user_id: str) -> SessionRecord:
        self._check_ids(session_id, owner_user_id)
        rec = self.store.get(session_id)
        if rec is None or rec.state == DESTROYED:
            raise NotFound(f"session {session_id} not found")
        if rec.owner_user_id != owner_user_id:
            raise Forbidden(f"session {session_id} belongs to another user")
        return rec

    def describe(self, rec: SessionRecord) -> dict[str, Any]:
        host = self.settings.advertise_host
        return {
            "session_id": rec.session_id,
            "owner_user_id": rec.owner_user_id,
            "state": rec.state,
            "base_url": f"http://{host}:{rec.agent_port}/t/{rec.guest_token}",
            "cdp_url": f"http://{host}:{rec.cdp_port}/t/{rec.guest_token}",
            "vcpus": rec.vcpus,
            "mem_mib": rec.mem_mib,
            "created_ts": rec.created_ts,
            "updated_ts": rec.updated_ts,
        }

    def _allocate_index(self) -> int:
        used = {r.vm_index for r in self.store.list() if r.state in (CREATING, RUNNING, PAUSED)}
        for index in range(1, self.settings.max_vms + 1):
            if index not in used:
                return index
        raise Unavailable("no free VM slots on this host")

    def _resolve_profile(self, profile: Optional[dict[str, Any]]) -> tuple[int, int]:
        profile = profile or {}
        vcpus = int(profile.get("vcpus") or self.settings.default_vcpus)
        mem_mib = int(profile.get("mem_mib") or self.settings.default_mem_mib)
        if not 1 <= vcpus <= self.settings.max_vcpus:
            raise SessionError(f"vcpus must be between 1 and {self.settings.max_vcpus}")
        if not 256 <= mem_mib <= self.settings.max_mem_mib:
            raise SessionError(f"mem_mib must be between 256 and {self.settings.max_mem_mib}")
        return vcpus, mem_mib

    # -------------------------------------------------------------- lifecycle

    async def create(self, session_id: str, owner_user_id: str,
                     profile: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], bool]:
        """Create the session VM, or return the existing one for the same owner."""
        self._check_ids(session_id, owner_user_id)
        async with self._lock(session_id):
            existing = self.store.get(session_id)
            if existing is not None and existing.state != DESTROYED:
                if existing.owner_user_id != owner_user_id:
                    raise Forbidden(f"session {session_id} belongs to another user")
                if existing.state == RUNNING:
                    return self.describe(existing), False
                if existing.state == PAUSED:
                    await self._runtimes[session_id].vm.resume()
                    existing.state = RUNNING
                    self.store.put(existing)
                    return self.describe(existing), False
                # FAULTED or a stale CREATING: clean up and start over.
                await self._destroy_locked(existing)
            vcpus, mem_mib = self._resolve_profile(profile)
            async with self._alloc_lock:
                index = self._allocate_index()
                rec = SessionRecord(
                    session_id=session_id,
                    owner_user_id=owner_user_id,
                    state=CREATING,
                    vm_id=f"vm{index}-{secrets.token_hex(3)}",
                    vm_index=index,
                    agent_port=self.settings.proxy_port_start + 2 * (index - 1),
                    cdp_port=self.settings.proxy_port_start + 2 * (index - 1) + 1,
                    guest_token=secrets.token_urlsafe(24),
                    vcpus=vcpus,
                    mem_mib=mem_mib,
                )
                self.store.put(rec)
            try:
                await self._boot(rec)
            except Exception:
                logger.exception("boot of session %s failed", session_id)
                await self._destroy_locked(rec)
                rec.state = FAULTED
                self.store.put(rec)
                raise
            rec.state = RUNNING
            self.store.put(rec)
            return self.describe(rec), True

    async def _boot(self, rec: SessionRecord) -> None:
        s = self.settings
        started = time.monotonic()
        await network.create_vm_network(rec.vm_index)
        vm = FirecrackerVm(VmPaths(s.jailer_base, rec.vm_id), uid=s.uid_base + rec.vm_index)
        rec.pid = await vm.spawn(s.jailer_bin, s.firecracker_bin, network.netns_path(rec.vm_index),
                                 rec.mem_mib, rec.vcpus)
        self.store.put(rec)
        proxies = [
            VsockProxy(rec.agent_port, vm.paths.vsock_uds, AGENT_VSOCK_PORT),
            VsockProxy(rec.cdp_port, vm.paths.vsock_uds, CDP_VSOCK_PORT),
        ]
        self._runtimes[rec.session_id] = _Runtime(vm=vm, proxies=proxies)
        t_spawned = time.monotonic()
        kernel = await asyncio.to_thread(vm.place_file, s.kernel_path, "vmlinux")
        rootfs = await asyncio.to_thread(vm.place_file, self._rootfs_source, "rootfs.ext4", True)
        t_copied = time.monotonic()
        await vm.configure_and_boot(
            kernel, rootfs, rec.vcpus, rec.mem_mib,
            metadata={"session_id": rec.session_id, "owner_user_id": rec.owner_user_id,
                      "guest_token": rec.guest_token},
        )
        for proxy in proxies:
            await proxy.start()
        deadline = time.monotonic() + s.boot_timeout_s
        last: Exception | str = "no response"
        while time.monotonic() < deadline:
            try:
                status, _ = await guest_http_get(vm.paths.vsock_uds, AGENT_VSOCK_PORT, "/healthz")
                if status == 200:
                    now = time.monotonic()
                    logger.info("session %s ready in %.1fs (net+jailer %.1fs, disk copy %.1fs, boot %.1fs; "
                                "vm %s, owner %s)", rec.session_id, now - started, t_spawned - started,
                                t_copied - t_spawned, now - t_copied, rec.vm_id, rec.owner_user_id)
                    return
                last = f"status {status}"
            except Exception as exc:  # guest still booting
                last = exc
            await asyncio.sleep(0.5)
        raise Unavailable(f"guest agent not ready after {s.boot_timeout_s}s: {last}")

    async def get(self, session_id: str, owner_user_id: str) -> dict[str, Any]:
        return self.describe(self._owned(session_id, owner_user_id))

    async def list(self, owner_user_id: str) -> list[dict[str, Any]]:
        return [self.describe(r) for r in self.store.list(owner_user_id) if r.state != DESTROYED]

    async def pause(self, session_id: str, owner_user_id: str) -> dict[str, Any]:
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            if rec.state == RUNNING:
                await self._runtimes[session_id].vm.pause()
                rec.state = PAUSED
                self.store.put(rec)
            elif rec.state != PAUSED:
                raise Conflict(f"cannot pause a session in state {rec.state}")
            return self.describe(rec)

    async def resume(self, session_id: str, owner_user_id: str) -> dict[str, Any]:
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            if rec.state == PAUSED:
                await self._runtimes[session_id].vm.resume()
                rec.state = RUNNING
                self.store.put(rec)
            elif rec.state != RUNNING:
                raise Conflict(f"cannot resume a session in state {rec.state}")
            return self.describe(rec)

    async def destroy(self, session_id: str, owner_user_id: str) -> None:
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            await self._destroy_locked(rec)

    async def debug(self, session_id: str, owner_user_id: str) -> dict[str, Any]:
        """Facts about the VM process, used to verify jailer isolation."""
        rec = self._owned(session_id, owner_user_id)
        info: dict[str, Any] = {"session_id": rec.session_id, "vm_id": rec.vm_id, "pid": rec.pid,
                                "netns": network.netns_name(rec.vm_index)}
        if rec.pid:
            proc = Path(f"/proc/{rec.pid}")
            try:
                # The jailer pivots into its own mount namespace, so /proc/<pid>/root
                # reads as "/"; what the process sees as "/" is the chroot content.
                info["root_entries"] = sorted(os.listdir(proc / "root"))
                info["mntns_inode"] = os.readlink(proc / "ns" / "mnt")
                info["exe"] = os.readlink(proc / "exe")
                status = (proc / "status").read_text()
                info["uid"] = int(next(line.split()[1] for line in status.splitlines() if line.startswith("Uid:")))
                info["cgroup"] = (proc / "cgroup").read_text().strip()
                info["netns_inode"] = os.readlink(proc / "ns" / "net")
                info["seccomp"] = next(line.split()[1] for line in status.splitlines()
                                       if line.startswith("Seccomp:"))
            except OSError as exc:
                info["error"] = str(exc)
        info["manager_netns_inode"] = os.readlink("/proc/self/ns/net")
        info["manager_mntns_inode"] = os.readlink("/proc/self/ns/mnt")
        return info

    # ---------------------------------------------------------------- teardown

    async def _destroy_locked(self, rec: SessionRecord) -> None:
        runtime = self._runtimes.pop(rec.session_id, None)
        if runtime is not None:
            for proxy in runtime.proxies:
                await proxy.stop()
            await runtime.vm.kill()
        else:
            kill_pid(rec.pid)
        await self._cleanup_resources(rec)
        rec.state, rec.pid = DESTROYED, None
        self.store.put(rec)

    async def _cleanup_resources(self, rec: SessionRecord) -> None:
        await network.teardown_vm_network(rec.vm_index)
        remove_cgroup(rec.vm_id)
        vm_dir = VmPaths(self.settings.jailer_base, rec.vm_id).vm_dir
        await asyncio.to_thread(shutil.rmtree, vm_dir, True)
