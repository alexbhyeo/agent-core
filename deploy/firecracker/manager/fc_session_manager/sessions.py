# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Session lifecycle: one microVM per session.

States: CREATING -> RUNNING <-> PAUSED -> PERSISTED -> (restore) RUNNING; any -> DESTROYED.

* Every call is checked against the session's ``owner_user_id``.
* Each session owns a directory (its disk and snapshots) and two proxy ports
  for its whole life; a VM *run* (jailer chroot, slot, network namespace) is
  created on boot/restore and released on persist/destroy.
* Proxies stay up while a session is paused or persisted and wake it on the
  next request (unless the session disables wake-on-request).
* An idle reaper applies the three-stage policy (pause, persist, destroy).
* With dirty-page tracking, a persist after a restore writes a Diff snapshot
  and rebases it onto the memory file the VM was restored from.
* ``fork`` snapshots a session once and restores it as new sessions, each with
  its own disks, identity and guest token.
* The workspace is a separate drive: it can be checkpointed and swapped while
  the VM runs (the guest unmounts it, the drive is re-pointed, then remounted).
* Each user's tier caps VM size and session count and sets drive / NIC rate
  limits, TTLs, the CPU template choice and (optionally) an egress allowlist.
* A memory balloon returns idle guest memory to the host and deflates when the
  guest needs it back; cold snapshots are compressed and encrypted.
* A warm pool of pre-booted VMs (default profile) makes creates take < 1 s.
* Tasks in a VM message the session's user (``fc-notify`` / ``fc-ask``); the
  manager buffers those messages, so a question asked before a pause or persist
  stays visible, and a reply wakes the VM.
"""

from __future__ import annotations

import asyncio
import collections
import json
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

from fc_session_manager import network, snapcodec
from fc_session_manager.config import Settings
from fc_session_manager.egress import AllowlistProxy, EgressAuditor, slot_of
from fc_session_manager.firecracker import (
    FirecrackerError, FirecrackerVm, VmPaths, kill_pid, remove_cgroup, sparse_copy,
)
from fc_session_manager.metrics import Metrics, evaluate_alerts, host_memory
from fc_session_manager.proxy import VsockProxy, guest_http, guest_http_get
from fc_session_manager.store import (
    CREATING, DESTROYED, FAULTED, KEPT_STATES, LIVE_STATES, PAUSED, PERSISTED, RUNNING,
    SessionRecord, SessionStore,
)
from fc_session_manager.tiers import Tier, TierBook, domain_allowed

logger = logging.getLogger(__name__)

AGENT_VSOCK_PORT = 8080
CDP_VSOCK_PORT = 9222
MCP_VSOCK_PORT = 9300
PORTS_PER_SESSION = 3  # agent API, CDP, MCP
_MCP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SECRET_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
_PROFILE_KEYS = {"vcpus", "mem_mib", "ttl_pause_s", "ttl_persist_s", "ttl_destroy_s", "wake_on_request",
                 "workspace_mib", "cpu_template", "egress_allowlist", "snapshot_seal_after_s"}
_DOMAIN_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,62}\.)*[a-z0-9-]{1,63}$")
POOL_OWNER = "__pool__"  # owner of warm-pool VMs (never a valid user id)
BALLOON_CHECK_S = 10.0
_DISKS = ("rootfs.ext4", "workspace.ext4")
MAX_FORKS = 8


def _allocated_bytes(path: Path) -> int:
    """Bytes a (sparse) file really occupies."""
    return path.stat().st_blocks * 512


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


class QuotaExceeded(SessionError):
    status = 429


@dataclass
class _Run:
    """A live Firecracker process for a session."""

    vm: FirecrackerVm
    # Memory file the next Diff snapshot is relative to (the restore source or
    # the latest Full snapshot); None until one exists or without dirty tracking.
    base_mem: Optional[Path] = None
    balloon: bool = False
    balloon_checked: float = 0.0


class SessionManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = SessionStore(settings.state_db)
        self._runs: dict[str, _Run] = {}
        self._proxies: dict[str, list[VsockProxy]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._alloc_lock = asyncio.Lock()
        self._rootfs_source: Path = settings.rootfs_path
        self._kernel_source: Path = settings.kernel_path
        self._fc_version = "unknown"
        self._reaper: Optional[asyncio.Task] = None
        # Per-session tool credentials: memory only, delivered through MMDS (which
        # snapshots do not include) and never written to the store or disk.
        self._secrets: dict[str, dict[str, str]] = {}
        self.tiers = TierBook(settings.tiers_json, settings.user_tiers_json, settings.default_tier)
        self.metrics = Metrics()
        self._cpu_templates: dict[str, dict[str, Any]] = {}
        self._key_file: Optional[Path] = None
        self.auditor: Optional[EgressAuditor] = None
        self._egress_proxy: Optional[AllowlistProxy] = None
        self._pool_task: Optional[asyncio.Task] = None
        # Messages from tasks in each session's VM to its user, by guest message id.
        self._messages: dict[str, dict[int, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ setup

    async def startup(self) -> None:
        s = self.settings
        await network.setup_host(s.egress_interface)
        s.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._rootfs_source = await asyncio.to_thread(self._cache_image, s.rootfs_path, "rootfs.ext4", True)
        self._kernel_source = await asyncio.to_thread(self._cache_image, s.kernel_path, "vmlinux", False)
        os.chmod(s.data_dir, 0o700)
        if s.cpu_template_path is not None:
            # Every template shipped next to the default one can be chosen per session.
            for path in sorted(s.cpu_template_path.parent.glob("*.json")):
                self._cpu_templates[path.stem] = json.loads(path.read_text())
        if s.snapshot_encrypt:
            self._key_file = snapcodec.ensure_key(s.snapshot_key_file or s.data_dir / "keys" / "snapshot.key")
        if s.egress_audit:
            self.auditor = EgressAuditor(s.data_dir / "audit", self._session_for_slot)
            await self.auditor.start()
        if s.egress_proxy_port:
            self._egress_proxy = AllowlistProxy(s.egress_proxy_port, self._proxy_lookup, self.auditor)
            await self._egress_proxy.start()
        self._fc_version = subprocess.run([str(s.firecracker_bin), "--version"], capture_output=True,
                                          text=True, check=False).stdout.split("\n")[0].strip()
        for rec in self.store.list():
            if rec.owner_user_id == POOL_OWNER:
                if rec.state != DESTROYED:
                    await self._destroy_locked(rec)
                continue
            if rec.state in LIVE_STATES:
                # The Firecracker process did not survive the restart.
                logger.warning("session %s was %s before restart; marking FAULTED", rec.session_id, rec.state)
                kill_pid(rec.pid)
                await self._release_run(rec)
                self._set_state(rec, FAULTED)
            elif rec.state == PERSISTED:
                await self._start_proxies(rec)
        logger.info("startup done: %s, cpu template %s (available %s), %d persisted session(s) listening",
                    self._fc_version, s.cpu_template_name, sorted(self._cpu_templates), len(self._proxies))
        self._reaper = asyncio.create_task(self._reap_forever())
        if s.warm_pool_size > 0:
            self._pool_task = asyncio.create_task(self._fill_pool_forever())

    async def shutdown(self) -> None:
        for task in (self._reaper, self._pool_task):
            if task is not None:
                task.cancel()
        for session_id in list(self._runs):
            rec = self.store.get(session_id)
            if rec is None:
                continue
            async with self._lock(session_id):
                try:
                    if self.settings.persist_on_shutdown and rec.owner_user_id != POOL_OWNER:
                        await self._persist_locked(rec)
                    else:
                        await self._destroy_locked(rec)
                except Exception:
                    logger.exception("shutdown handling of session %s failed", session_id)
        for session_id in list(self._proxies):
            await self._stop_proxies(session_id)
        if self._egress_proxy is not None:
            await self._egress_proxy.stop()
        if self.auditor is not None:
            await self.auditor.stop()

    def _cache_image(self, src: Path, name: str, sparse: bool) -> Path:
        """Keep a copy of a base image on the data volume (same filesystem as sessions)."""
        cached = self.settings.data_dir / "images" / name
        st = src.stat()
        if cached.exists() and cached.stat().st_size == st.st_size and cached.stat().st_mtime >= st.st_mtime:
            return cached
        started = time.monotonic()
        tmp = cached.with_suffix(".tmp")
        try:
            sparse_copy(src, tmp) if sparse else shutil.copy2(src, tmp)
        except Exception as exc:
            logger.warning("caching %s failed (%s); using it in place", src, exc)
            return src
        os.chmod(tmp, 0o644)
        os.replace(tmp, cached)
        os.utime(cached, (st.st_atime, st.st_mtime))
        logger.info("cached %s -> %s in %.1fs", src, cached, time.monotonic() - started)
        return cached

    def _workspace_template(self, mib: int) -> Path:
        """An empty ext4 image of ``mib`` MiB (sparse), built once per size."""
        path = self.settings.data_dir / "images" / f"workspace-{mib}-empty.ext4"
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{secrets.token_hex(3)}.tmp")
        with open(tmp, "wb") as fh:
            fh.truncate(mib * 1024 * 1024)
        result = subprocess.run(["mkfs.ext4", "-q", "-F", "-m", "0", "-L", "workspace", str(tmp)],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise FirecrackerError(f"mkfs.ext4 failed: {result.stderr.strip()}")
        # Agents list /workspace; an empty one should look empty (e2fsck recreates it if needed).
        subprocess.run(["debugfs", "-w", "-R", "rmdir lost+found", str(tmp)], capture_output=True, check=False)
        os.replace(tmp, path)
        return path

    # ---------------------------------------------------------------- helpers

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

    def _session_dir(self, session_id: str) -> Path:
        return self.settings.sessions_dir / session_id

    def _snapshot_dir(self, rec: SessionRecord) -> Path:
        return self._session_dir(rec.session_id) / "snapshots" / f"gen-{rec.snapshot_gen}"

    def _set_state(self, rec: SessionRecord, state: str) -> None:
        rec.state = state
        rec.state_since_ts = time.time()
        self.store.put(rec)

    def _tier(self, rec: SessionRecord) -> Tier:
        return self.tiers.get(rec.tier) if rec.tier else self.tiers.for_user(rec.owner_user_id)

    def _policy(self, rec: SessionRecord) -> dict[str, Any]:
        p = rec.profile
        s = self.settings
        tier = self._tier(rec)

        def ttl(key: str) -> float:
            tier_value = getattr(tier, key)
            return float(p.get(key, tier_value if tier_value is not None else getattr(s, key)))

        return {
            "ttl_pause_s": ttl("ttl_pause_s"),
            "ttl_persist_s": ttl("ttl_persist_s"),
            "ttl_destroy_s": ttl("ttl_destroy_s"),
            "wake_on_request": bool(p.get("wake_on_request", s.wake_on_request)),
            "snapshot_seal_after_s": float(p.get("snapshot_seal_after_s", s.snapshot_seal_after_s)),
        }

    def _template_name(self, rec: SessionRecord) -> str:
        return rec.cpu_template or self.settings.cpu_template_name or "none"

    def _allowlist(self, rec: SessionRecord) -> Optional[tuple[str, ...]]:
        entries = rec.profile.get("egress_allowlist")
        return tuple(entries) if entries is not None else None

    def describe(self, rec: SessionRecord) -> dict[str, Any]:
        host = self.settings.advertise_host
        return {
            "session_id": rec.session_id,
            "owner_user_id": rec.owner_user_id,
            "state": rec.state,
            "base_url": f"http://{host}:{rec.agent_port}/t/{rec.guest_token}",
            "cdp_url": f"http://{host}:{rec.cdp_port}/t/{rec.guest_token}",
            "mcp_url": f"http://{host}:{rec.mcp_port}/t/{rec.guest_token}" if rec.mcp_port else None,
            "secret_keys": sorted(self._secrets.get(rec.session_id, {})),
            "vcpus": rec.vcpus,
            "mem_mib": rec.mem_mib,
            "created_ts": rec.created_ts,
            "updated_ts": rec.updated_ts,
            "last_active_ts": rec.last_active_ts,
            "state_since_ts": rec.state_since_ts,
            "snapshot_gen": rec.snapshot_gen,
            "checkpoint_ref": rec.checkpoint_ref,
            "policy": self._policy(rec),
            "workspace_mib": rec.workspace_mib,
            "workspace_checkpoints": self._workspace_checkpoints(rec.session_id),
            "forked_from": rec.forked_from,
            "tier": rec.tier or self.tiers.for_user(rec.owner_user_id).name,
            "cpu_template": self._template_name(rec),
            "egress": "allowlist" if self._allowlist(rec) is not None else "open",
            "egress_allowlist": rec.profile.get("egress_allowlist"),
            "pending_questions": sum(1 for m in self._messages.get(rec.session_id, {}).values()
                                     if m.get("status") == "pending"),
        }

    def _workspace_checkpoints(self, session_id: str) -> list[str]:
        ckpt_dir = self._session_dir(session_id) / "workspace-checkpoints"
        return sorted(p.stem for p in ckpt_dir.glob("*.ext4")) if ckpt_dir.is_dir() else []

    def _allocate(self) -> tuple[int, int]:
        """A free VM slot (live runs) and a free port pair (all kept sessions)."""
        records = self.store.list()
        used_slots = {r.vm_index for r in records if r.state in LIVE_STATES and r.vm_index}
        used_ports = {r.agent_port for r in records if r.state in KEPT_STATES}
        slot = next((i for i in range(1, self.settings.max_vms + 1) if i not in used_slots), None)
        base = self.settings.proxy_port_start
        step = PORTS_PER_SESSION
        port = next((base + step * i for i in range(self.settings.proxy_port_count // step)
                     if base + step * i not in used_ports), None)
        if slot is None or port is None:
            raise Unavailable("no free VM slots or proxy ports on this host")
        return slot, port

    def _allocate_ports(self) -> int:
        used = {r.agent_port for r in self.store.list() if r.state in KEPT_STATES}
        base, step = self.settings.proxy_port_start, PORTS_PER_SESSION
        port = next((base + step * i for i in range(self.settings.proxy_port_count // step)
                     if base + step * i not in used), None)
        if port is None:
            raise Unavailable("no free proxy ports on this host")
        return port

    def _allocate_slot(self) -> int:
        used = {r.vm_index for r in self.store.list() if r.state in LIVE_STATES and r.vm_index}
        slot = next((i for i in range(1, self.settings.max_vms + 1) if i not in used), None)
        if slot is None:
            raise Unavailable("no free VM slots on this host")
        return slot

    def _resolve_profile(self, profile: Optional[dict[str, Any]], tier: Tier) -> tuple[int, int, dict[str, Any]]:
        """Validate a requested profile against the user's tier; returns (vcpus, mem_mib, stored profile)."""
        s = self.settings
        profile = dict(profile or {})
        unknown = set(profile) - _PROFILE_KEYS
        if unknown:
            raise SessionError(f"unknown profile keys: {sorted(unknown)}")
        max_vcpus = min(tier.max_vcpus or s.max_vcpus, s.max_vcpus)
        max_mem = min(tier.max_mem_mib or s.max_mem_mib, s.max_mem_mib)
        vcpus = int(profile.get("vcpus") or min(tier.vcpus or s.default_vcpus, max_vcpus))
        mem_mib = int(profile.get("mem_mib") or min(tier.mem_mib or s.default_mem_mib, max_mem))
        if not 1 <= vcpus <= max_vcpus:
            raise SessionError(f"vcpus must be between 1 and {max_vcpus} (tier {tier.name})")
        if not 256 <= mem_mib <= max_mem:
            raise SessionError(f"mem_mib must be between 256 and {max_mem} (tier {tier.name})")
        default_ws = tier.workspace_mib if tier.workspace_mib is not None else s.workspace_mib
        workspace_mib = int(profile.get("workspace_mib", default_ws))
        if workspace_mib and not 64 <= workspace_mib <= 65536:
            raise SessionError("workspace_mib must be 0 or between 64 and 65536")
        profile["workspace_mib"] = workspace_mib
        template = profile.get("cpu_template")
        if template is not None and template != "none" and template not in self._cpu_templates:
            raise SessionError(f"cpu_template must be one of {sorted(self._cpu_templates) + ['none']}")
        requested = profile.get("egress_allowlist")
        if requested is not None:
            if not isinstance(requested, list) or not all(isinstance(d, str) and _DOMAIN_RE.match(d.lower())
                                                          for d in requested):
                raise SessionError("egress_allowlist must be a list of domain names (optionally *.domain)")
            requested = [d.lower() for d in requested]
            if tier.egress_allowlist is not None:
                outside = [d for d in requested if d not in tier.egress_allowlist
                           and (d.startswith("*.") or not domain_allowed(d, tier.egress_allowlist))]
                if outside:
                    raise SessionError(f"tier {tier.name} does not allow egress to {outside}")
            profile["egress_allowlist"] = requested
        elif tier.egress_allowlist is not None:
            profile["egress_allowlist"] = list(tier.egress_allowlist)
        return vcpus, mem_mib, profile

    def _check_quota(self, owner_user_id: str, tier: Tier, adding: int = 1) -> None:
        if not tier.max_sessions:
            return
        kept = sum(1 for r in self.store.list(owner_user_id) if r.state in KEPT_STATES)
        if kept + adding > tier.max_sessions:
            raise QuotaExceeded(f"tier {tier.name} allows {tier.max_sessions} sessions; {kept} in use")

    # ----------------------------------------------------------------- proxies

    async def _start_proxies(self, rec: SessionRecord) -> None:
        if rec.session_id in self._proxies:
            return
        sid = rec.session_id

        def touch() -> None:
            self.store.touch(sid, time.time())

        proxies = [
            VsockProxy(rec.agent_port, AGENT_VSOCK_PORT, lambda: self._resolve_uds(sid), touch),
            VsockProxy(rec.cdp_port, CDP_VSOCK_PORT, lambda: self._resolve_uds(sid), touch),
        ]
        if rec.mcp_port:
            proxies.append(VsockProxy(rec.mcp_port, MCP_VSOCK_PORT, lambda: self._resolve_uds(sid), touch))
        for proxy in proxies:
            await proxy.start()
        self._proxies[sid] = proxies

    async def _stop_proxies(self, session_id: str) -> None:
        for proxy in self._proxies.pop(session_id, []):
            await proxy.stop()

    def _drop_connections(self, session_id: str) -> None:
        for proxy in self._proxies.get(session_id, []):
            proxy.close_connections()

    async def _resolve_uds(self, session_id: str) -> Path:
        """Vsock socket of the running VM; wakes it first when the policy allows."""
        rec = self.store.get(session_id)
        if rec is None or rec.state in (DESTROYED, FAULTED):
            raise ConnectionError(f"session {session_id} is gone")
        if rec.state != RUNNING:
            if not self._policy(rec)["wake_on_request"]:
                raise ConnectionError(f"session {session_id} is {rec.state} and wake-on-request is off")
            await self._ensure_running(session_id)
        run = self._runs.get(session_id)
        if run is None:
            raise ConnectionError(f"session {session_id} has no running VM")
        return run.vm.paths.vsock_uds

    # -------------------------------------------------------------- lifecycle

    async def create(self, session_id: str, owner_user_id: str,
                     profile: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], bool]:
        """Create the session VM, or return/wake the existing one for the same owner."""
        self._check_ids(session_id, owner_user_id)
        async with self._lock(session_id):
            existing = self.store.get(session_id)
            if existing is not None and existing.state != DESTROYED:
                if existing.owner_user_id != owner_user_id:
                    raise Forbidden(f"session {session_id} belongs to another user")
                if existing.state in (RUNNING, PAUSED, PERSISTED):
                    await self._ensure_running_locked(existing)
                    return self.describe(existing), False
                await self._destroy_locked(existing)  # FAULTED or stale CREATING
            tier = self.tiers.for_user(owner_user_id)
            vcpus, mem_mib, profile = self._resolve_profile(profile, tier)
            self._check_quota(owner_user_id, tier)
            started = time.monotonic()
            if self._pool_eligible(vcpus, mem_mib, profile, tier):
                claimed = await self._claim_warm(session_id, owner_user_id, profile, tier)
                if claimed is not None:
                    self.metrics.observe("session_create", time.monotonic() - started)
                    self.metrics.inc("sessions_created", source="warm_pool")
                    return self.describe(claimed), True
            async with self._alloc_lock:
                slot, port = self._allocate()
                rec = SessionRecord(
                    session_id=session_id, owner_user_id=owner_user_id, state=CREATING,
                    vm_id=f"vm{slot}-{secrets.token_hex(3)}", vm_index=slot,
                    agent_port=port, cdp_port=port + 1, guest_token=secrets.token_urlsafe(24),
                    vcpus=vcpus, mem_mib=mem_mib, profile_json=json.dumps(profile), mcp_port=port + 2,
                    workspace_mib=int(profile["workspace_mib"]), tier=tier.name,
                    cpu_template=str(profile.get("cpu_template") or ""),
                )
                self.store.put(rec)
            try:
                disk = self._session_dir(session_id) / "rootfs.ext4"
                await asyncio.to_thread(sparse_copy, self._rootfs_source, disk)
                if rec.workspace_mib:
                    template = await asyncio.to_thread(self._workspace_template, rec.workspace_mib)
                    await asyncio.to_thread(sparse_copy, template, disk.with_name("workspace.ext4"))
                t_copied = time.monotonic()
                await self._start_proxies(rec)
                await self._start_run(rec, restore=False)
                logger.info("session %s ready in %.1fs (disk copy %.1fs, boot %.1fs; vm %s, owner %s)",
                            session_id, time.monotonic() - started, t_copied - started,
                            time.monotonic() - t_copied, rec.vm_id, owner_user_id)
            except Exception:
                logger.exception("boot of session %s failed", session_id)
                self.metrics.inc("boot_failures")
                await self._destroy_locked(rec)
                self._set_state(rec, FAULTED)
                raise
            rec.last_active_ts = time.time()
            self._set_state(rec, RUNNING)
            self.metrics.observe("session_create", time.monotonic() - started)
            self.metrics.inc("sessions_created", source="boot")
            return self.describe(rec), True

    # --------------------------------------------------------------- warm pool

    def _pool_eligible(self, vcpus: int, mem_mib: int, profile: dict[str, Any], tier: Tier) -> bool:
        """Warm VMs are booted with the default profile and no tier limits."""
        s = self.settings
        return (s.warm_pool_size > 0 and vcpus == s.default_vcpus and mem_mib == s.default_mem_mib
                and int(profile.get("workspace_mib", s.workspace_mib)) == s.workspace_mib
                and profile.get("cpu_template") in (None, s.cpu_template_name)
                and profile.get("egress_allowlist") is None
                and tier.drive_rate_limiter() is None and tier.net_rate_limiter() is None)

    def _pool_records(self) -> list[SessionRecord]:
        return [r for r in self.store.list(POOL_OWNER) if r.state == RUNNING and r.session_id in self._runs]

    async def _fill_pool_forever(self) -> None:
        while True:
            try:
                pending = [r for r in self.store.list(POOL_OWNER) if r.state == CREATING]
                if len(self._pool_records()) + len(pending) < self.settings.warm_pool_size:
                    await self._boot_pool_vm()
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("warm pool refill failed")
                await asyncio.sleep(5)
            await asyncio.sleep(0.5)

    async def _boot_pool_vm(self) -> None:
        s = self.settings
        async with self._alloc_lock:
            slot = self._allocate_slot()
            rec = SessionRecord(
                session_id=f"__warm-{secrets.token_hex(4)}", owner_user_id=POOL_OWNER, state=CREATING,
                vm_id=f"vm{slot}-{secrets.token_hex(3)}", vm_index=slot, agent_port=0, cdp_port=0,
                guest_token=secrets.token_urlsafe(24), vcpus=s.default_vcpus, mem_mib=s.default_mem_mib,
                profile_json=json.dumps({"workspace_mib": s.workspace_mib}), workspace_mib=s.workspace_mib,
                tier=self.tiers.default,
            )
            self.store.put(rec)
        try:
            disk = self._session_dir(rec.session_id) / "rootfs.ext4"
            await asyncio.to_thread(sparse_copy, self._rootfs_source, disk)
            if rec.workspace_mib:
                template = await asyncio.to_thread(self._workspace_template, rec.workspace_mib)
                await asyncio.to_thread(sparse_copy, template, disk.with_name("workspace.ext4"))
            await self._start_run(rec, restore=False)
        except Exception:
            self.metrics.inc("boot_failures")
            await self._destroy_locked(rec)
            raise
        self._set_state(rec, RUNNING)
        logger.info("warm pool: %s ready (%d/%d)", rec.vm_id, len(self._pool_records()), s.warm_pool_size)

    async def _claim_warm(self, session_id: str, owner_user_id: str, profile: dict[str, Any],
                          tier: Tier) -> Optional[SessionRecord]:
        """Hand a pre-booted VM to a new session: new identity and token via MMDS, own ports."""
        started = time.monotonic()
        async with self._alloc_lock:
            pool = self._pool_records()
            if not pool:
                return None
            warm = pool[0]
            port = self._allocate_ports()
            run = self._runs.pop(warm.session_id)
            target = self._session_dir(session_id)
            shutil.rmtree(target, ignore_errors=True)
            os.rename(self._session_dir(warm.session_id), target)
            self.store.delete(warm.session_id)
            rec = SessionRecord(
                session_id=session_id, owner_user_id=owner_user_id, state=RUNNING, vm_id=warm.vm_id,
                vm_index=warm.vm_index, agent_port=port, cdp_port=port + 1, guest_token=secrets.token_urlsafe(24),
                vcpus=warm.vcpus, mem_mib=warm.mem_mib, pid=warm.pid, profile_json=json.dumps(profile),
                mcp_port=port + 2, workspace_mib=warm.workspace_mib, tier=tier.name,
            )
            self.store.put(rec)
            self._runs[session_id] = run
        t_alloc = time.monotonic()
        try:
            await run.vm.put_mmds(self._metadata(rec))
            await self._start_proxies(rec)
            t_proxies = time.monotonic()
            await self._claim_identity(rec)
        except Exception:
            logger.exception("claiming warm VM %s for %s failed; booting instead", rec.vm_id, session_id)
            await self._destroy_locked(rec)
            rec.state = CREATING
            self.store.delete(session_id)
            return None
        rec.last_active_ts = time.time()
        self._set_state(rec, RUNNING)
        logger.info("session %s took warm VM %s in %.2fs (allocate %.2fs, mmds+proxies %.2fs, identity %.2fs; owner %s)",
                    session_id, rec.vm_id, time.monotonic() - started, t_alloc - started, t_proxies - t_alloc,
                    time.monotonic() - t_proxies, owner_user_id)
        return rec

    async def _start_run(self, rec: SessionRecord, restore: bool) -> None:
        """Start a Firecracker process for ``rec`` (fresh boot or snapshot restore)."""
        s = self.settings
        await network.create_vm_network(rec.vm_index)
        if self._allowlist(rec) is not None:
            if not s.egress_proxy_port:
                raise Unavailable("this session restricts egress but the allowlist proxy is disabled")
            await network.restrict_egress(rec.vm_index, s.egress_proxy_port)
        vm = FirecrackerVm(VmPaths(s.jailer_base, rec.vm_id), uid=s.uid_base + rec.vm_index)
        run = self._runs[rec.session_id] = _Run(vm=vm, balloon=s.balloon)
        rec.pid = await vm.spawn(s.jailer_bin, s.firecracker_bin, network.netns_path(rec.vm_index),
                                 rec.mem_mib, rec.vcpus)
        self.store.put(rec)
        metadata = self._metadata(rec)
        session_dir = self._session_dir(rec.session_id)
        rootfs = await asyncio.to_thread(vm.place, session_dir / "rootfs.ext4", "rootfs.ext4")
        workspace = None
        if rec.workspace_mib and (session_dir / "workspace.ext4").exists():
            workspace = await asyncio.to_thread(vm.place, session_dir / "workspace.ext4", "workspace.ext4")
        if restore:
            snap = self._snapshot_dir(rec)
            vm.prepare_snap_dir()
            await asyncio.to_thread(vm.place, snap / "vm.state", "snap/vm.state")
            await asyncio.to_thread(vm.place, snap / "mem", "snap/mem")
            await vm.snapshot_load(resume=True, track_dirty_pages=s.diff_snapshots)
            await vm.put_mmds(metadata)  # MMDS data is not part of a snapshot
            if s.diff_snapshots:
                run.base_mem = vm.paths.snap_dir / "mem"
        else:
            kernel = await asyncio.to_thread(vm.place, self._kernel_source, "vmlinux")
            tier = self._tier(rec)
            await vm.configure_and_boot(kernel, rootfs, rec.vcpus, rec.mem_mib, metadata,
                                        cpu_template=self._cpu_templates.get(self._template_name(rec)),
                                        track_dirty_pages=s.diff_snapshots, workspace=workspace,
                                        drive_limiter=tier.drive_rate_limiter(), net_limiter=tier.net_rate_limiter(),
                                        balloon=s.balloon)
        await self._wait_healthy(rec, vm)

    def _metadata(self, rec: SessionRecord) -> dict[str, Any]:
        """MMDS content: identity, guest token and this session's tool credentials."""
        return {"session_id": rec.session_id, "owner_user_id": rec.owner_user_id,
                "guest_token": rec.guest_token, "secrets": dict(self._secrets.get(rec.session_id, {})),
                # Mixed into the guest RNG when a fork takes on its new identity.
                "entropy_seed": secrets.token_hex(32),
                # Restricted sessions reach the internet only through the allowlist proxy.
                **({"egress_proxy": f"http://{network.TAP_IP}:{network.GUEST_PROXY_PORT}"}
                   if self._allowlist(rec) is not None else {})}

    async def set_secrets(self, session_id: str, owner_user_id: str,
                          values: dict[str, Optional[str]]) -> dict[str, Any]:
        """Merge tool credentials (``None`` removes a key) and push them to MMDS if the VM runs."""
        if not isinstance(values, dict):
            raise SessionError("secrets must be an object")
        for key, value in values.items():
            if not _SECRET_KEY_RE.match(str(key)):
                raise SessionError(f"invalid secret name {key!r}")
            if value is not None and not isinstance(value, str):
                raise SessionError(f"secret {key} must be a string or null")
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            current = self._secrets.setdefault(session_id, {})
            for key, value in values.items():
                if value is None:
                    current.pop(key, None)
                else:
                    current[key] = value
            run = self._runs.get(session_id)
            if run is not None and rec.state in (RUNNING, PAUSED):
                await run.vm.put_mmds(self._metadata(rec))
            return {"session_id": session_id, "secret_keys": sorted(current)}

    async def add_mcp_server(self, session_id: str, owner_user_id: str, name: str, command: str,
                             args: Optional[list[str]] = None, env: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """Run a user's stdio MCP server inside the session VM; returns its streamable-HTTP URL."""
        if not _MCP_NAME_RE.match(name or ""):
            raise SessionError("MCP server name must match [a-z0-9][a-z0-9_-]{0,63}")
        await self._ensure_running(session_id)
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            if not rec.mcp_port:
                raise Conflict("this session was created before MCP support; recreate it")
            run = self._runs[session_id]
            body = json.dumps({"name": name, "command": command, "args": args or [], "env": env or {}}).encode()
            status, raw = await guest_http(run.vm.paths.vsock_uds, AGENT_VSOCK_PORT, "POST",
                                           f"/t/{rec.guest_token}/fc/mcp/servers", body, timeout=60)
            if status != 200:
                raise Unavailable(f"guest refused MCP server {name}: {raw.decode(errors='replace')[:300]}")
            rec.last_active_ts = time.time()
            self.store.put(rec)
            info = self.describe(rec)
            return {"name": name, "url": f"{info['mcp_url']}/servers/{name}/mcp",
                    "servers": json.loads(raw)["data"]["servers"]}

    async def _wait_healthy(self, rec: SessionRecord, vm: FirecrackerVm) -> None:
        deadline = time.monotonic() + self.settings.boot_timeout_s
        last: Exception | str = "no response"
        while time.monotonic() < deadline:
            try:
                status, _ = await guest_http_get(vm.paths.vsock_uds, AGENT_VSOCK_PORT, "/healthz")
                if status == 200:
                    return
                last = f"status {status}"
            except Exception as exc:  # guest still booting / resuming
                last = exc
            await asyncio.sleep(0.2)
        raise Unavailable(f"guest agent of {rec.session_id} not ready after {self.settings.boot_timeout_s}s: {last}")

    async def get(self, session_id: str, owner_user_id: str) -> dict[str, Any]:
        return self.describe(self._owned(session_id, owner_user_id))

    async def list(self, owner_user_id: str) -> list[dict[str, Any]]:
        return [self.describe(r) for r in self.store.list(owner_user_id) if r.state != DESTROYED]

    async def pause(self, session_id: str, owner_user_id: str) -> dict[str, Any]:
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            if rec.state == RUNNING:
                await self._pause_locked(rec)
            elif rec.state not in (PAUSED, PERSISTED):
                raise Conflict(f"cannot pause a session in state {rec.state}")
            return self.describe(rec)

    async def _pause_locked(self, rec: SessionRecord) -> None:
        await self._pull_messages(rec, 0, 0)  # keep them readable while the VM is frozen
        await self._runs[rec.session_id].vm.pause()
        self._set_state(rec, PAUSED)

    async def resume(self, session_id: str, owner_user_id: str,
                     expected_checkpoint_ref: Optional[str] = None) -> dict[str, Any]:
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            if rec.state not in (RUNNING, PAUSED, PERSISTED):
                raise Conflict(f"cannot resume a session in state {rec.state}")
            if expected_checkpoint_ref is not None and rec.checkpoint_ref != expected_checkpoint_ref:
                raise Conflict(f"checkpoint mismatch: VM snapshot is paired with {rec.checkpoint_ref!r}, "
                               f"caller expects {expected_checkpoint_ref!r}")
            await self._ensure_running_locked(rec)
            return self.describe(rec)

    async def _ensure_running(self, session_id: str) -> None:
        async with self._lock(session_id):
            rec = self.store.get(session_id)
            if rec is not None:
                await self._ensure_running_locked(rec)

    async def _ensure_running_locked(self, rec: SessionRecord) -> None:
        if rec.state == PAUSED:
            await self._runs[rec.session_id].vm.resume()
            rec.last_active_ts = time.time()
            self._set_state(rec, RUNNING)
        elif rec.state == PERSISTED:
            await self._restore_locked(rec)

    async def persist(self, session_id: str, owner_user_id: str,
                      checkpoint_ref: Optional[str] = None) -> dict[str, Any]:
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            if rec.state in (RUNNING, PAUSED):
                await self._persist_locked(rec, checkpoint_ref)
            elif rec.state == PERSISTED:
                if checkpoint_ref is not None:
                    rec.checkpoint_ref = checkpoint_ref
                    self.store.put(rec)
            else:
                raise Conflict(f"cannot persist a session in state {rec.state}")
            return self.describe(rec)

    def _snapshot_meta(self, rec: SessionRecord, snapshot_type: str) -> dict[str, Any]:
        return {
            "session_id": rec.session_id, "generation": rec.snapshot_gen, "snapshot_type": snapshot_type,
            "firecracker_version": self._fc_version, "cpu_template": self._template_name(rec),
            "kernel": self.settings.kernel_path.name, "vcpus": rec.vcpus, "mem_mib": rec.mem_mib,
            "checkpoint_ref": rec.checkpoint_ref, "created_ts": time.time(),
        }

    def _check_snapshot(self, meta: dict[str, Any], rec: SessionRecord) -> None:
        """A snapshot restores only on the same Firecracker and with a template this host offers."""
        if meta.get("firecracker_version") != self._fc_version:
            raise Conflict(f"snapshot taken with {meta.get('firecracker_version')}, host runs {self._fc_version}")
        template = meta.get("cpu_template") or "none"
        if template != self._template_name(rec):
            raise Conflict(f"snapshot CPU template {template} != session template {self._template_name(rec)}")
        if template != "none" and template not in self._cpu_templates:
            raise Conflict(f"CPU template {template} is not available on this host")

    async def _unseal(self, snap: Path) -> None:
        """Decrypt / decompress a sealed snapshot generation in place (no-op otherwise)."""
        meta_path = snap / "meta.json"
        meta = json.loads(meta_path.read_text())
        sealed = meta.get("sealed")
        if not sealed:
            return
        started = time.monotonic()
        await asyncio.to_thread(snapcodec.unseal_dir, snap, sealed, self._key_file)
        meta.pop("sealed")
        meta_path.write_text(json.dumps(meta, indent=2))
        self.metrics.observe("snapshot_unseal", time.monotonic() - started)

    async def _seal_locked(self, rec: SessionRecord) -> None:
        snap = self._snapshot_dir(rec)
        meta_path = snap / "meta.json"
        meta = json.loads(meta_path.read_text())
        if meta.get("sealed"):
            return
        started = time.monotonic()
        sealed = await asyncio.to_thread(snapcodec.seal_dir, snap, ("mem", "vm.state"),
                                         self.settings.snapshot_compress, self._key_file)
        if sealed:
            meta["sealed"] = sealed
            meta_path.write_text(json.dumps(meta, indent=2))
            self.metrics.inc("snapshots_sealed")
            self.metrics.observe("snapshot_seal", time.monotonic() - started)
            logger.info("session %s snapshot gen %d sealed (%s) in %.1fs: %s", rec.session_id, rec.snapshot_gen,
                        "+".join(x for x in (sealed["compress"], sealed["encrypt"]) if x),
                        time.monotonic() - started, sealed["sealed_bytes"])

    def _rebase(self, base: Path, diff: Path) -> None:
        """Write the pages of a Diff snapshot into the full memory file it is relative to."""
        result = subprocess.run([str(self.settings.snapshot_editor_bin), "edit-memory", "rebase",
                                 "--memory-path", str(base), "--diff-path", str(diff)],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise FirecrackerError(f"snapshot-editor rebase failed: {result.stderr.strip() or result.stdout.strip()}")

    async def _persist_locked(self, rec: SessionRecord, checkpoint_ref: Optional[str] = None) -> None:
        """Snapshot the VM to the session directory and stop its process.

        After a restore (or a fork), only the pages written since are dumped
        (Diff) and merged into the base memory file; otherwise all memory (Full).
        """
        run = self._runs[rec.session_id]
        started = time.monotonic()
        if rec.state == RUNNING:
            await self._pull_messages(rec, 0, 0)
            await run.vm.pause()
        base = run.base_mem
        diff = base is not None and base.exists()
        vmstate, mem = await run.vm.snapshot_create("Diff" if diff else "Full",
                                                    mem_name="mem.diff" if diff else "mem")
        t_snap = time.monotonic()
        written = _allocated_bytes(mem)
        self._drop_connections(rec.session_id)  # their guest streams end with the process
        await run.vm.kill()
        self._runs.pop(rec.session_id, None)
        old_dir = self._snapshot_dir(rec)
        rec.snapshot_gen += 1
        new_dir = self._snapshot_dir(rec)
        new_dir.mkdir(parents=True, exist_ok=True)
        os.replace(vmstate, new_dir / "vm.state")
        t_merge = time.monotonic()
        if base is not None and diff:
            if base.stat().st_nlink > 1:  # shared with forks or a kept snapshot: never edit in place
                await asyncio.to_thread(sparse_copy, base, new_dir / "mem")
            else:
                os.replace(base, new_dir / "mem")
            await asyncio.to_thread(self._rebase, new_dir / "mem", mem)
            mem.unlink()
        else:
            os.replace(mem, new_dir / "mem")
        if checkpoint_ref is not None:
            rec.checkpoint_ref = checkpoint_ref
        meta = self._snapshot_meta(rec, "Diff" if diff else "Full")
        meta.update({"snapshot_s": round(t_snap - started, 3), "merge_s": round(time.monotonic() - t_merge, 3),
                     "persist_s": round(time.monotonic() - started, 3), "mem_bytes_written": written})
        (new_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        if old_dir != new_dir:
            shutil.rmtree(old_dir, ignore_errors=True)
        await self._release_run(rec)
        rec.pid, rec.vm_index = None, 0
        self._set_state(rec, PERSISTED)
        self.metrics.observe("session_persist", meta["persist_s"])
        logger.info("session %s persisted (gen %d, %s) in %.1fs (snapshot %.1fs, %.1f MiB written, %d MiB memory)",
                    rec.session_id, rec.snapshot_gen, meta["snapshot_type"], meta["persist_s"], meta["snapshot_s"],
                    written / 2 ** 20, rec.mem_mib)

    async def _restore_locked(self, rec: SessionRecord) -> None:
        snap = self._snapshot_dir(rec)
        self._check_snapshot(json.loads((snap / "meta.json").read_text()), rec)
        started = time.monotonic()
        await self._unseal(snap)
        async with self._alloc_lock:
            rec.vm_index = self._allocate_slot()
            rec.vm_id = f"vm{rec.vm_index}-{secrets.token_hex(3)}"
            self.store.put(rec)
        try:
            await self._start_run(rec, restore=True)
        except Exception:
            logger.exception("restore of session %s failed", rec.session_id)
            self.metrics.inc("restore_failures")
            run = self._runs.pop(rec.session_id, None)
            if run is not None:
                await run.vm.kill()
            await self._release_run(rec)
            rec.pid, rec.vm_index = None, 0
            self.store.put(rec)  # stays PERSISTED; the snapshot is untouched
            raise
        # The memory file stays mapped through the chroot hard link; drop the
        # session-dir copy so a snapshot generation is never restored twice.
        shutil.rmtree(snap, ignore_errors=True)
        rec.last_active_ts = time.time()
        self._set_state(rec, RUNNING)
        self.metrics.observe("session_restore", time.monotonic() - started)
        logger.info("session %s restored (gen %d) in %.1fs (vm %s)", rec.session_id, rec.snapshot_gen,
                    time.monotonic() - started, rec.vm_id)

    # ------------------------------------------------------------------- fork

    async def fork(self, session_id: str, owner_user_id: str, new_session_ids: list[str]) -> list[dict[str, Any]]:
        """Clone a session into new sessions of the same owner from one snapshot.

        A running source is paused only while its memory is dumped and its disks
        are copied. Each fork gets its own disks, ports, guest token and RNG seed
        (through MMDS), and inherits the source's secrets.
        """
        if not isinstance(new_session_ids, list) or not 1 <= len(new_session_ids) <= MAX_FORKS:
            raise SessionError(f"new_session_ids must list 1 to {MAX_FORKS} session ids")
        if len(set(new_session_ids)) != len(new_session_ids) or session_id in new_session_ids:
            raise SessionError("new_session_ids must be distinct and differ from the source")
        for new_id in new_session_ids:
            self._check_ids(new_id, owner_user_id)
        self._check_quota(owner_user_id, self.tiers.for_user(owner_user_id), len(new_session_ids))
        started = time.monotonic()
        forks: list[SessionRecord] = []
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            if rec.state not in (RUNNING, PAUSED, PERSISTED):
                raise Conflict(f"cannot fork a session in state {rec.state}")
            for new_id in new_session_ids:
                existing = self.store.get(new_id)
                if existing is not None and existing.state != DESTROYED:
                    raise Conflict(f"session {new_id} already exists")
            work = self._session_dir(session_id) / f"fork-{secrets.token_hex(4)}"
            work.mkdir(parents=True)
            try:
                if rec.state == PERSISTED:
                    snap = self._snapshot_dir(rec)
                    await self._unseal(snap)
                    meta = json.loads((snap / "meta.json").read_text())
                    self._check_snapshot(meta, rec)
                    for name in ("vm.state", "mem"):
                        os.link(snap / name, work / name)
                    await asyncio.to_thread(self._copy_disks, self._session_dir(session_id), work)
                else:
                    meta = await self._fork_snapshot(rec, work)
                t_snap = time.monotonic()
                for i, new_id in enumerate(new_session_ids):
                    forks.append(await self._fork_record(rec, new_id, meta, work,
                                                         last=i == len(new_session_ids) - 1))
            finally:
                shutil.rmtree(work, ignore_errors=True)
        results = []
        for frec in forks:
            async with self._lock(frec.session_id):
                try:
                    await self._start_proxies(frec)
                    await self._restore_locked(frec)
                    await self._claim_identity(frec)
                except Exception:
                    logger.exception("restore of fork %s failed", frec.session_id)
                    await self._destroy_locked(frec)
                    raise
            results.append(self.describe(frec))
        self.metrics.observe("session_fork", time.monotonic() - started)
        self.metrics.inc("sessions_created", len(results), source="fork")
        logger.info("session %s forked into %s in %.1fs (snapshot and disk copy %.1fs)", session_id,
                    new_session_ids, time.monotonic() - started, t_snap - started)
        return results

    def _copy_disks(self, src_dir: Path, dst_dir: Path) -> None:
        for name in _DISKS:
            if (src_dir / name).exists():
                sparse_copy(src_dir / name, dst_dir / name)

    async def _fork_snapshot(self, rec: SessionRecord, work: Path) -> dict[str, Any]:
        """Full snapshot of a live session plus copies of its disks, taken while it is paused."""
        run = self._runs[rec.session_id]
        was_running = rec.state == RUNNING
        if was_running:
            await run.vm.pause()
        try:
            state, mem = await run.vm.snapshot_create("Full", state_name="fork.state", mem_name="fork.mem")
            os.replace(state, work / "vm.state")
            os.link(mem, work / "mem")
            # Disks are copied before resuming so they match the memory image.
            await asyncio.to_thread(self._copy_disks, self._session_dir(rec.session_id), work)
            if self.settings.diff_snapshots:
                # A Full snapshot resets dirty tracking: later Diffs are relative to this file.
                base = run.vm.paths.snap_dir / "mem"
                os.replace(mem, base)
                run.base_mem = base
            else:
                mem.unlink()
        finally:
            if was_running:
                await run.vm.resume()
                rec.last_active_ts = time.time()
                self.store.put(rec)
        return self._snapshot_meta(rec, "Full")

    async def _fork_record(self, src: SessionRecord, new_id: str, meta: dict[str, Any], work: Path,
                           last: bool) -> SessionRecord:
        """A PERSISTED record for ``new_id`` holding the fork snapshot and its own disk copies."""
        async with self._alloc_lock:
            port = self._allocate_ports()
            frec = SessionRecord(
                session_id=new_id, owner_user_id=src.owner_user_id, state=PERSISTED,
                vm_id=f"fork-{secrets.token_hex(3)}", vm_index=0,
                agent_port=port, cdp_port=port + 1, guest_token=secrets.token_urlsafe(24),
                vcpus=src.vcpus, mem_mib=src.mem_mib, profile_json=src.profile_json, mcp_port=port + 2,
                snapshot_gen=1, workspace_mib=src.workspace_mib, forked_from=src.session_id,
                tier=src.tier, cpu_template=src.cpu_template,
            )
            self.store.put(frec)
        try:
            fdir = self._session_dir(new_id)
            shutil.rmtree(fdir, ignore_errors=True)
            snap = self._snapshot_dir(frec)
            snap.mkdir(parents=True)
            # Restored VMs map memory privately and never write the snapshot files: share them.
            for name in ("vm.state", "mem"):
                os.link(work / name, snap / name)
            for name in _DISKS:
                if (work / name).exists():
                    if last:
                        os.replace(work / name, fdir / name)
                    else:
                        await asyncio.to_thread(sparse_copy, work / name, fdir / name)
            (snap / "meta.json").write_text(json.dumps(dict(meta, session_id=new_id, generation=1,
                                                            forked_from=src.session_id), indent=2))
        except Exception:
            await self._destroy_locked(frec)
            raise
        if src.session_id in self._secrets:
            self._secrets[new_id] = dict(self._secrets[src.session_id])
        return frec

    async def _claim_identity(self, rec: SessionRecord) -> None:
        """Make a restored fork adopt its new identity (the guest re-reads MMDS on a token miss)."""
        uds = self._runs[rec.session_id].vm.paths.vsock_uds
        deadline = time.monotonic() + 10
        status = 0
        while time.monotonic() < deadline:
            status, _ = await guest_http_get(uds, AGENT_VSOCK_PORT, f"/t/{rec.guest_token}/fc/identity")
            if status == 200:
                return
            await asyncio.sleep(0.1)
        raise Unavailable(f"fork {rec.session_id} did not adopt its new identity (status {status})")

    # -------------------------------------------------------------- workspace

    def _workspace_ckpt(self, session_id: str, name: str) -> Path:
        if not _MCP_NAME_RE.match(name or ""):
            raise SessionError("checkpoint name must match [a-z0-9][a-z0-9_-]{0,63}")
        return self._session_dir(session_id) / "workspace-checkpoints" / f"{name}.ext4"

    async def _workspace_call(self, rec: SessionRecord, action: str) -> None:
        run = self._runs[rec.session_id]
        status, raw = await guest_http(run.vm.paths.vsock_uds, AGENT_VSOCK_PORT, "POST",
                                       f"/t/{rec.guest_token}/fc/workspace",
                                       json.dumps({"action": action}).encode(), timeout=60)
        if status != 200:
            raise Unavailable(f"guest workspace {action} failed: {raw.decode(errors='replace')[:300]}")

    async def checkpoint_workspace(self, session_id: str, owner_user_id: str, name: str) -> dict[str, Any]:
        """Copy the workspace drive (frozen for a consistent image) to a named checkpoint."""
        await self._ensure_running(session_id)
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            if not rec.workspace_mib:
                raise Conflict("this session has no workspace drive")
            target = self._workspace_ckpt(session_id, name)
            started = time.monotonic()
            await self._workspace_call(rec, "freeze")
            try:
                await asyncio.to_thread(sparse_copy, self._session_dir(session_id) / "workspace.ext4", target)
            finally:
                await self._workspace_call(rec, "thaw")
            rec.last_active_ts = time.time()
            self.store.put(rec)
            logger.info("session %s workspace checkpoint %s in %.1fs", session_id, name, time.monotonic() - started)
            return {"name": name, "workspace_checkpoints": self._workspace_checkpoints(session_id)}

    async def swap_workspace(self, session_id: str, owner_user_id: str, checkpoint: str) -> dict[str, Any]:
        """Hot-swap the workspace drive to a copy of a checkpoint, without rebooting the VM."""
        await self._ensure_running(session_id)
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            if not rec.workspace_mib:
                raise Conflict("this session has no workspace drive")
            source = self._workspace_ckpt(session_id, checkpoint)
            if not source.exists():
                raise NotFound(f"workspace checkpoint {checkpoint} not found")
            run = self._runs[session_id]
            current = self._session_dir(session_id) / "workspace.ext4"
            started = time.monotonic()
            staged = current.with_name("workspace.next.ext4")
            await asyncio.to_thread(sparse_copy, source, staged)  # the checkpoint stays reusable
            await self._workspace_call(rec, "unmount")
            try:
                os.replace(staged, current)  # the chroot still links the old image until re-placed
                drive = await asyncio.to_thread(run.vm.place, current, "workspace.ext4")
                await run.vm.update_drive("workspace", drive)
            finally:
                await self._workspace_call(rec, "mount")
            rec.last_active_ts = time.time()
            self.store.put(rec)
            logger.info("session %s workspace swapped to %s in %.1fs", session_id, checkpoint,
                        time.monotonic() - started)
            return self.describe(rec)

    async def destroy(self, session_id: str, owner_user_id: str) -> None:
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            await self._destroy_locked(rec)

    async def debug(self, session_id: str, owner_user_id: str) -> dict[str, Any]:
        """Facts about the VM process, used to verify jailer isolation."""
        rec = self._owned(session_id, owner_user_id)
        info: dict[str, Any] = {"session_id": rec.session_id, "vm_id": rec.vm_id, "pid": rec.pid,
                                "netns": network.netns_name(rec.vm_index) if rec.vm_index else None,
                                "state": rec.state, "cpu_template": self.settings.cpu_template_name,
                                "firecracker_version": self._fc_version}
        snap = self._snapshot_dir(rec)
        if (snap / "meta.json").exists():
            info["snapshot"] = json.loads((snap / "meta.json").read_text())
            info["snapshot_files"] = {p.name: p.stat().st_size for p in snap.iterdir()}
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
        run = self._runs.get(session_id)
        if run is not None and run.balloon and rec.state == RUNNING:
            try:
                info["balloon"] = await run.vm.balloon_stats()
            except FirecrackerError as exc:
                info["balloon"] = {"error": str(exc)[:200]}
        info["manager_netns_inode"] = os.readlink("/proc/self/ns/net")
        info["manager_mntns_inode"] = os.readlink("/proc/self/ns/mnt")
        return info

    # ------------------------------------------------------------- user messages

    async def _pull_messages(self, rec: SessionRecord, after: int, wait: float) -> None:
        """Copy the guest's messages (newer than ``after``) into the manager-side buffer."""
        run = self._runs.get(rec.session_id)
        if run is None or rec.state != RUNNING:
            return
        buffer = self._messages.setdefault(rec.session_id, {})
        uds = run.vm.paths.vsock_uds
        # Questions still pending here may have been answered or expired in the guest: re-read from the oldest.
        pending = [i for i, m in buffer.items() if m.get("status") == "pending" and i <= after]
        for since, timeout in ([(min(pending) - 1, 0.0)] if pending else []) + [(after, wait)]:
            try:
                status, raw = await guest_http(uds, AGENT_VSOCK_PORT, "GET",
                                               f"/t/{rec.guest_token}/fc/messages?after={since}&wait={timeout:g}",
                                               None, timeout=timeout + 10)
            except (OSError, ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                return  # paused or stopped meanwhile: serve what is buffered
            if status == 200:
                for msg in json.loads(raw)["data"]["messages"]:
                    buffer[int(msg["id"])] = msg

    async def messages(self, session_id: str, owner_user_id: str, after: int = 0,
                       wait: float = 0.0) -> dict[str, Any]:
        """Messages from the session's tasks newer than ``after``; long-polls up to ``wait`` seconds."""
        rec = self._owned(session_id, owner_user_id)
        wait = max(0.0, min(float(wait), 60.0))
        buffer = self._messages.setdefault(session_id, {})
        if rec.state == RUNNING:
            await self._pull_messages(rec, after, wait)
        elif wait and not any(i > after for i in buffer):
            await asyncio.sleep(min(wait, 5.0))  # frozen VM: nothing new can arrive
        found = [m for i, m in sorted(buffer.items()) if i > after]
        return {"session_id": session_id, "state": self.store.get(session_id).state, "messages": found,
                "last_id": max(buffer, default=0)}

    async def reply_message(self, session_id: str, owner_user_id: str, message_id: int,
                            text: Any) -> dict[str, Any]:
        """Answer a task's question; wakes a paused or persisted VM to deliver it."""
        if not isinstance(text, str):
            raise SessionError("text must be a string")
        self._owned(session_id, owner_user_id)
        await self._ensure_running(session_id)
        async with self._lock(session_id):
            rec = self._owned(session_id, owner_user_id)
            run = self._runs.get(session_id)
            if run is None or rec.state != RUNNING:
                raise Unavailable(f"session {session_id} is {rec.state}")
            body = json.dumps({"id": int(message_id), "text": text}).encode()
            status, raw = await guest_http(run.vm.paths.vsock_uds, AGENT_VSOCK_PORT, "POST",
                                           f"/t/{rec.guest_token}/fc/messages/reply", body, timeout=30)
            if status in (404, 409):
                error = (NotFound if status == 404 else Conflict)
                raise error(json.loads(raw).get("message", f"cannot answer message {message_id}"))
            if status != 200:
                raise Unavailable(f"guest refused the reply: {raw.decode(errors='replace')[:300]}")
            msg = json.loads(raw)["data"]
            self._messages.setdefault(session_id, {})[int(msg["id"])] = msg
            rec.last_active_ts = time.time()
            self.store.put(rec)
            self.metrics.inc("user_replies")
            return msg

    # ------------------------------------------------------------ egress / metrics

    def _session_for_slot(self, slot: int) -> Optional[str]:
        for rec in self.store.list():
            if rec.vm_index == slot and rec.state in LIVE_STATES and rec.owner_user_id != POOL_OWNER:
                return rec.session_id
        return None

    def _proxy_lookup(self, peer_ip: str) -> Optional[tuple[str, tuple[str, ...]]]:
        slot = slot_of(peer_ip)
        session_id = self._session_for_slot(slot) if slot else None
        rec = self.store.get(session_id) if session_id else None
        allowlist = self._allowlist(rec) if rec is not None else None
        return (rec.session_id, allowlist) if rec is not None and allowlist is not None else None

    async def egress_log(self, session_id: str, owner_user_id: str, limit: int = 100) -> dict[str, Any]:
        rec = self._owned(session_id, owner_user_id)
        entries = self.auditor.recent(session_id, limit) if self.auditor is not None else []
        return {"session_id": rec.session_id, "audit": self.auditor is not None, "entries": entries}

    def gauges(self) -> dict[str, Any]:
        records = self.store.list()
        users = [r for r in records if r.owner_user_id != POOL_OWNER and r.state != DESTROYED]
        total, available = host_memory()
        return {
            "sessions": dict(collections.Counter(r.state for r in users)),
            "vm_slots_used": sum(1 for r in records if r.state in LIVE_STATES and r.vm_index),
            "vm_slots_max": self.settings.max_vms,
            "warm_pool_ready": len(self._pool_records()),
            "warm_pool_target": self.settings.warm_pool_size,
            "guest_mem_mib_running": sum(r.mem_mib for r in users if r.state == RUNNING),
            "host_mem_total_mib": total,
            "host_mem_available_mib": available,
            "egress_audit_events": self.auditor.events if self.auditor is not None else 0,
        }

    def metrics_text(self) -> str:
        return self.metrics.render(self.gauges())

    def alerts(self) -> list[dict[str, Any]]:
        return evaluate_alerts(self.metrics, self.gauges(), self.settings.max_vms, self.settings.data_dir,
                               self.settings.alert_create_p95_s)

    # ------------------------------------------------------------------ reaper

    async def _reap_forever(self) -> None:
        while True:
            try:
                await self._reap_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("idle reaper pass failed")
            await asyncio.sleep(self.settings.reaper_interval_s)

    async def _reap_once(self) -> None:
        now = time.time()
        for rec in self.store.list():
            if rec.state not in (RUNNING, PAUSED, PERSISTED) or rec.owner_user_id == POOL_OWNER:
                continue
            policy = self._policy(rec)
            lock = self._lock(rec.session_id)
            if lock.locked():
                continue
            async with lock:
                rec = self.store.get(rec.session_id)  # fresh view under the lock
                if rec is None:
                    continue
                if rec.state == RUNNING and 0 < policy["ttl_pause_s"] < now - rec.last_active_ts:
                    logger.info("idle policy: pausing %s", rec.session_id)
                    await self._pause_locked(rec)
                elif rec.state == PAUSED and 0 < policy["ttl_persist_s"] < now - rec.state_since_ts:
                    logger.info("idle policy: persisting %s", rec.session_id)
                    await self._persist_locked(rec)
                elif rec.state == PERSISTED and 0 < policy["ttl_destroy_s"] < now - rec.state_since_ts:
                    logger.info("idle policy: destroying %s", rec.session_id)
                    await self._destroy_locked(rec)
                elif (rec.state == PERSISTED and 0 <= policy["snapshot_seal_after_s"] < now - rec.state_since_ts
                      and (self.settings.snapshot_compress or self._key_file is not None)
                      and (self._snapshot_dir(rec) / "mem").exists()):
                    try:
                        await self._seal_locked(rec)
                    except Exception:
                        logger.exception("sealing the snapshot of %s failed", rec.session_id)
                        self.metrics.inc("seal_failures")
                elif rec.state == RUNNING and self.settings.balloon_reclaim:
                    await self._reclaim_memory(rec)

    async def _reclaim_memory(self, rec: SessionRecord) -> None:
        """Size the balloon so the guest keeps about a quarter of its memory available.

        Memory the guest does not use goes back to the host; when the guest needs it,
        the balloon deflates (here on the next check, and in the guest on OOM).
        """
        run = self._runs.get(rec.session_id)
        if run is None or not run.balloon or time.monotonic() - run.balloon_checked < BALLOON_CHECK_S:
            return
        run.balloon_checked = time.monotonic()
        try:
            stats = await run.vm.balloon_stats()
        except FirecrackerError as exc:
            if "not configured" in str(exc).lower() or "no balloon" in str(exc).lower():
                run.balloon = False  # e.g. restored from a snapshot taken before the balloon existed
            return
        available = int(stats.get("available_memory", 0)) // 2 ** 20
        if not available:
            return  # the guest has not reported statistics yet
        target = int(stats.get("target_mib", 0))
        keep = max(256, rec.mem_mib // 4)
        wanted = max(0, min(target + available - keep, rec.mem_mib - 256))
        if abs(wanted - target) >= 64:
            await run.vm.balloon_set(wanted)
            self.metrics.inc("balloon_adjustments", direction="inflate" if wanted > target else "deflate")
            logger.info("balloon of %s: %d -> %d MiB (guest available %d MiB)", rec.session_id, target, wanted,
                        available)

    # ---------------------------------------------------------------- teardown

    async def _destroy_locked(self, rec: SessionRecord) -> None:
        await self._stop_proxies(rec.session_id)
        run = self._runs.pop(rec.session_id, None)
        if run is not None:
            await run.vm.kill()
        else:
            kill_pid(rec.pid)
        await self._release_run(rec)
        await asyncio.to_thread(shutil.rmtree, self._session_dir(rec.session_id), True)
        self._secrets.pop(rec.session_id, None)
        self._messages.pop(rec.session_id, None)
        if self.auditor is not None:
            self.auditor.forget(rec.session_id)
        rec.pid = None
        self._set_state(rec, DESTROYED)

    async def _release_run(self, rec: SessionRecord) -> None:
        """Free what one VM run holds: network namespace, cgroup and chroot."""
        if rec.vm_index:
            restricted = self._allowlist(rec) is not None
            await network.teardown_vm_network(rec.vm_index, self.settings.egress_proxy_port if restricted else 0)
        if not rec.vm_id:  # never resolve to the shared cgroup / chroot parent
            return
        remove_cgroup(rec.vm_id)
        await asyncio.to_thread(shutil.rmtree, VmPaths(self.settings.jailer_base, rec.vm_id).vm_dir, True)
