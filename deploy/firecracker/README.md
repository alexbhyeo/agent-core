# Firecracker session sandbox (phase 1)

One Firecracker microVM per agent session, managed by **FcSessionManager** and
used from agent-core through `launcher_type="firecracker"`. Design:
`.agents/agent-docs/designs/proposals/D-20261124-0855-firecracker-session-manager/`.

```
agent-core (any OS) ──REST──▶ FcSessionManager ──jailer──▶ Firecracker microVM
   aio provider ─────HTTP (per-session port)──▶ vsock proxy ──vsock──▶ guest agent + Chromium
```

## What is here

| Path | Purpose |
| --- | --- |
| `manager/fc_session_manager/` | FcSessionManager: REST API, session lifecycle, jailer, per-VM netns + NAT, vsock proxy, SQLite state |
| `guest/fc_guest_agent.py` | Guest agent (stdlib only): the AIO API subset the aio provider uses, MMDS identity, token-checked CDP bridge |
| `guest/fc-init` | PID 1 in the guest: mounts, DNS, MMDS route, then `tini` + guest agent |
| `docker/guest.Dockerfile` | Guest root filesystem (Ubuntu 24.04, Python, git, build tools, headless Chromium) |
| `docker/manager.Dockerfile` | Manager image with Firecracker/jailer v1.18.0 and the 6.1 CI guest kernel |
| `docker/e2e-runner.Dockerfile` | Test runner with agent-core's locked dependencies, Node and Playwright |
| `scripts/` | `build.sh`, `run_manager.sh`, `run_e2e.sh` |

## Requirements

- A Linux host with KVM (`/dev/kvm`), or WSL2 with nested virtualization; the
  manager container runs `--privileged` with `/dev/kvm`.
- Docker. Windows/macOS Docker Desktop usually cannot expose `/dev/kvm`; on
  Windows run the scripts from a WSL2 distro whose Docker sees `/dev/kvm`.

## Run it

```bash
deploy/firecracker/scripts/build.sh                      # images + ~/.cache/fc-artifacts/rootfs.ext4
export FCM_API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
deploy/firecracker/scripts/run_manager.sh                # starts fc-manager on docker network fc-net
```

From agent-core:

```python
from openjiuwen.core.sys_operation import OperationMode, SysOperationCard
from openjiuwen.extensions.sys_operation.sandbox.launchers import build_firecracker_gateway_config

card = SysOperationCard(
    id=f"fc-{user_id}",
    mode=OperationMode.SANDBOX,
    gateway_config=build_firecracker_gateway_config(
        manager_url="http://fc-manager:8700", owner_user_id=user_id, api_token=FCM_API_TOKEN),
)
```

Register one `SysOperation` per user; every session of that user (the agent's
`session` in `Runner.run_agent`) gets its own microVM. Browser agents:
`create_browser_agent(..., browser_instance=await build_browser_instance(config, session_id))`.
A full example: `examples/firecracker/deep_agent_firecracker.py`.

## REST API

All calls need `Authorization: Bearer $FCM_API_TOKEN` and `X-Owner-User-Id`.

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/v1/sessions` | `{session_id, profile:{vcpus, mem_mib}}`; 201 created, 200 existing (paused sessions are resumed); 403 if another user owns it |
| GET | `/v1/sessions` | caller's sessions only |
| GET | `/v1/sessions/{id}` | state, `base_url`, `cdp_url` (both carry the per-session guest token) |
| POST | `/v1/sessions/{id}:pause` / `:resume` | vCPUs stopped / restarted, memory kept |
| DELETE | `/v1/sessions/{id}` | kill VM, remove netns, cgroup and disks |
| GET | `/v1/sessions/{id}/debug` | jailer facts: chroot, uid, cgroup, netns, seccomp |

## Isolation in phase 1

- **Process**: jailer chroot, uid 10000+n, cgroup v2 (`cpu.max`, `memory.max`), Firecracker's own seccomp filter.
- **Network**: each VM in its own netns with NAT egress; the host, private ranges,
  link-local/metadata and multicast are dropped; VMs cannot reach the manager container.
- **API**: guest agent and CDP bridge require the per-session token from MMDS V2 in the URL path.
- **Users**: every manager call is checked against the session owner.

## Tests

```bash
FC_E2E_ENV_FILE=/path/to/env deploy/firecracker/scripts/run_e2e.sh
```

The env file holds `FCM_API_TOKEN` and, for the LLM-driven tests, `API_KEY`, `API_BASE`,
`MODEL_NAME`, `MODEL_PROVIDER`. Without an LLM those two tests are skipped.

## Known limits (phase 1)

- No persist/snapshot: VMs live in memory and are lost when the manager container restarts
  (they are marked `FAULTED` on startup).
- Each session gets a full sparse copy of the rootfs (no shared read-only base + overlay yet).
- A paused VM is not woken by incoming requests; call `:resume` (or `POST /v1/sessions`) first.
- The launcher keeps manager handles in process memory; after an agent-core restart a
  session is re-attached on the next `launch()`.
