# Firecracker session sandbox

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
| `manager/fc_session_manager/` | FcSessionManager: REST API, session lifecycle (persist, wake-on-request, idle policy, fork), jailer, per-VM netns + NAT, vsock proxies, SQLite state |
| `guest/fc_guest_agent.py` | Guest agent (stdlib only): the AIO API subset the aio provider uses, MMDS identity, token-checked CDP and MCP bridges, workspace freeze/swap |
| `guest/demo_mcp_server.py` | Small stdio MCP server used by the tests (`whoami`, `add`) |
| `guest/fc-msg` | `fc-notify` / `fc-ask`: tasks in the VM message the session's user |
| `guest/fc-init` | PID 1 in the guest: mounts (incl. the `/workspace` drive), DNS, MMDS route, then `tini` + guest agent |
| `docker/guest.Dockerfile` | Guest root filesystem (Ubuntu 24.04, Python, git, build tools, headless Chromium) |
| `docker/manager.Dockerfile` | Manager image with Firecracker/jailer/snapshot-editor v1.18.0, CPU templates and the 6.1 CI guest kernel |
| `docker/e2e-runner.Dockerfile` | Test runner with agent-core's locked dependencies, Node and Playwright |
| `scripts/` | `build.sh`, `run_manager.sh` (passes every `FCM_*` setting through), `run_e2e.sh`, `agent_core_egress.sh` |
| `playground/backend/` | Playground backend: agent-core (DeepAgent / AgentTeam) behind the HTTP API `/api/v1`; runs next to the manager |
| `playground/frontend/` | Playground frontend: static ChatGPT-style web UI (nginx image); can be hosted on another machine |

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
`session` in `Runner.run_agent`) gets its own microVM. An AgentTeam shares one VM per
team session (give every member the same `SysOperationSpec` id). `Runner.release(session_id)`
deletes the session's VM. Browser agents:
`create_browser_agent(..., browser_instance=await build_browser_instance(config, session_id))`.
A full example: `examples/firecracker/deep_agent_firecracker.py`.

Other helpers in `openjiuwen.extensions.sys_operation.sandbox.launchers`:

| Helper | Purpose |
| --- | --- |
| `FirecrackerLauncherConfig(pause_mode="snapshot")` | Gateway `pause` persists the VM to disk instead of keeping it in memory |
| `register_session_mcp_server(config, session_id, name, command, args, env)` | Run a user's stdio MCP server inside the session VM; registers it as `<owner>--<session>--<name>` |
| `set_session_secrets(config, session_id, {"GH_TOKEN": ...})` | Per-session tool credentials through MMDS (memory only, never on disk or in snapshots) |
| `fork_session(config, session_id, [new ids])` | Clone a session VM (memory, processes, disks) into new sessions |
| `get_session_messages(config, session_id, after, wait_seconds)` | Messages that tasks in the VM sent to the user (long-poll) |
| `reply_to_session_message(config, session_id, message_id, text)` | Answer a task's question (wakes a paused / persisted VM) |

Set `GatewayStoreConfig(type="sqlite", sqlite_path=...)` so the Gateway re-attaches sessions
after an agent-core restart; a request that hits a gone VM is retried once after re-launch.

## Playground (web UI)

A ChatGPT-style web UI to use and test everything above: switch users, open many sessions
(DeepAgent or AgentTeam), run queries in several sessions at once or as a batch, watch VMs
pause / persist / wake, answer `fc-ask` questions from tasks in the VM, and drive every session
feature (lifecycle, fork, workspace checkpoints and swap, secrets, MCP, shell, files, egress,
debug) plus an operations view (metrics, alerts, per-user sessions, manager restart).
Design: `.agents/agent-docs/designs/active/D-20261127-0315-firecracker-playground/`.

The frontend and the backend are separate deployables; the only link between them is the HTTP
API `/api/v1` (bearer token + CORS allowlist):

```bash
# backend host (with KVM, next to FcSessionManager on the docker network fc-net)
export PLAYGROUND_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
export PLAYGROUND_ALLOWED_ORIGINS=https://frontend.example     # where the UI is served from
export FCM_API_TOKEN=... API_KEY=... API_BASE=... MODEL_NAME=...
PLAYGROUND_DOCKER=1 deploy/firecracker/playground/backend/run_backend.sh    # port 8800

# frontend host (any machine with Docker; or copy playground/frontend/* to any static host)
PLAYGROUND_API_BASE_URL=https://backend.example:8800 deploy/firecracker/playground/frontend/run_frontend.sh   # port 8080
```

Open the frontend, enter the backend URL and `PLAYGROUND_TOKEN` in Settings, then pick a user in
the bottom-left menu. User switching is not authenticated: anyone with the token can act as any
user, so keep the Playground on test environments (put TLS in front of the backend when it is
reached over a network). `PLAYGROUND_DOCKER=1` enables the "restart manager" button.

| Backend setting | Purpose |
| --- | --- |
| `PLAYGROUND_TOKEN` | required access token |
| `PLAYGROUND_ALLOWED_ORIGINS` | comma-separated origins allowed to call the API |
| `PLAYGROUND_BIND` | listen address (default `0.0.0.0:8800`) |
| `PLAYGROUND_MAX_RUNS` / `PLAYGROUND_MAX_ITERATIONS` | concurrent queries (8) / agent steps per query (100) |
| `PLAYGROUND_DEFAULT_VCPUS` / `PLAYGROUND_DEFAULT_MEM_MIB` | default VM size (2 / 1024) |
| `PLAYGROUND_USERS` | users offered in the user menu (default `alice,bob,noisy,fenced`) |

## REST API

All calls need `Authorization: Bearer $FCM_API_TOKEN` and `X-Owner-User-Id`.

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/v1/sessions` | `{session_id, profile}`; 201 created, 200 existing (paused / persisted sessions are woken); 403 if another user owns it |
| GET | `/v1/sessions` | caller's sessions only |
| GET | `/v1/sessions/{id}` | state, `base_url`, `cdp_url`, `mcp_url` (all carry the per-session guest token), policy, checkpoints |
| POST | `/v1/sessions/{id}:pause` / `:resume` | vCPUs stopped / restarted, memory kept; `:resume {expected_checkpoint_ref}` refuses (409) a snapshot paired with another checkpoint |
| POST | `/v1/sessions/{id}:persist` | `{checkpoint_ref?}`: snapshot to disk, stop the VM process (state `PERSISTED`) |
| POST | `/v1/sessions/{id}:fork` | `{new_session_ids: [...]}` (1–8): clones of the session, each with its own disks, ports, token |
| POST | `/v1/sessions/{id}/workspace/checkpoints` | `{name}`: consistent copy of the `/workspace` drive |
| POST | `/v1/sessions/{id}/workspace:swap` | `{checkpoint}`: hot-swap `/workspace` to a copy of a checkpoint, no reboot |
| POST | `/v1/sessions/{id}/mcp-servers` | `{name, command, args, env}`: stdio MCP server in the VM, served at `mcp_url/servers/<name>/mcp` |
| PUT | `/v1/sessions/{id}/secrets` | `{KEY: value or null}`: merge tool credentials into MMDS |
| DELETE | `/v1/sessions/{id}` | kill VM, remove netns, cgroup, disks and snapshots |
| GET | `/v1/sessions/{id}/debug` | jailer facts (chroot, uid, cgroup, netns, seccomp), snapshot metadata, balloon statistics |
| GET | `/v1/sessions/{id}/messages?after=&wait=` | messages from tasks in the VM (`notify` / `question`); long-poll up to 60 s |
| POST | `/v1/sessions/{id}/messages/{mid}/reply` | `{text}`: answer a question; wakes the VM first; 409 if already answered |
| GET | `/v1/sessions/{id}/egress` | recent outbound connections and proxy decisions of the session |
| GET | `/metrics` | Prometheus text: counters, durations (P50/P95/P99), gauges |
| GET | `/v1/alerts` | current alerts (slots, host memory, disk, create P95, failures, warm pool) |

`profile` keys: `vcpus`, `mem_mib`, `workspace_mib`, `ttl_pause_s`, `ttl_persist_s`,
`ttl_destroy_s`, `wake_on_request`, `cpu_template` (`T2`, `T2S`, `T2CL`, `C3` or `none`),
`egress_allowlist`, `snapshot_seal_after_s`. Values must fit the user's tier (see below).

## Tasks talking to the user

A task the agent (or an AgentTeam member) starts inside the VM can reach the session's user:

```bash
fc-notify "build finished"                       # one-way message
answer=$(fc-ask --timeout 600 "Deploy to prod?") # blocks until the user answers (exit 2 on timeout)
```

The frontend long-polls `GET /v1/sessions/{id}/messages?after=<last id>&wait=30` and answers with
`POST /v1/sessions/{id}/messages/{mid}/reply`. A pending question stays visible while the VM is
paused or persisted, and the reply wakes it. Tell the agent about these commands in its system
prompt. The aio provider runs commands as `{ CMD; }`, so a background command must not end with
`&` (write `... & echo started`).

## Tiers, limits and production settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `FCM_TIERS` / `FCM_TIERS_FILE` | built-in `standard`, `small`, `restricted` | tiers: size caps, `max_sessions`, `disk_mbps`, `disk_iops`, `net_mbps`, TTLs, `egress_allowlist` |
| `FCM_USER_TIERS` / `FCM_USER_TIERS_FILE` | everyone `FCM_DEFAULT_TIER` (`standard`) | `{"user": "tier"}`, set by the operator only |
| `FCM_BALLOON`, `FCM_BALLOON_RECLAIM` | on | balloon with deflate-on-OOM; idle guest memory goes back to the host |
| `FCM_EGRESS_AUDIT` | on | conntrack-based log of new outbound connections (`data_dir/audit/`) |
| `FCM_EGRESS_PROXY_PORT` | 3128 | allowlist proxy for sessions with an `egress_allowlist` |
| `FCM_SNAPSHOT_COMPRESS`, `FCM_SNAPSHOT_ENCRYPT`, `FCM_SNAPSHOT_KEY_FILE`, `FCM_SNAPSHOT_SEAL_AFTER_S` | on, on, `data_dir/keys/snapshot.key`, 600 | cold snapshots: zstd + AES-256-CTR |
| `FCM_WARM_POOL_SIZE` | 0 | pre-booted default-profile VMs; creates then take well under a second |
| `FCM_ALERT_CREATE_P95_S` | 5 | alert threshold for create latency |

Limit agent-core itself to its LLM and the manager by running
`scripts/agent_core_egress.sh api.example-llm.com` inside the agent-core container (needs
`NET_ADMIN`; host names are resolved when the script runs).

## Lifecycle

```
CREATING ─▶ RUNNING ◀─▶ PAUSED ─▶ PERSISTED ─(restore on request)─▶ RUNNING     any ─▶ DESTROYED
```

- **Idle policy** (defaults, per-session overridable): no traffic for `FCM_TTL_PAUSE_S`
  (600) → pause; paused for `FCM_TTL_PERSIST_S` (1800) → persist; persisted for
  `FCM_TTL_DESTROY_S` (7 days) → destroy.
- **Wake-on-request**: each session keeps its ports while paused or persisted; the next
  request resumes or restores the VM first (`FCM_WAKE_ON_REQUEST=1`).
- **Manager restart**: running sessions are persisted on shutdown (`FCM_PERSIST_ON_SHUTDOWN=1`)
  and wake after the restart.
- **Snapshots**: the first persist writes all memory (Full); a persist after a restore or
  fork writes only changed pages (Diff, `FCM_DIFF_SNAPSHOTS=1`) and merges them into the base
  with `snapshot-editor`. Snapshots record the Firecracker version and CPU template (`T2`
  by default, `FCM_CPU_TEMPLATE`); a mismatch is refused with 409.
- **Workspace**: `/workspace` is a separate drive (`FCM_WORKSPACE_MIB`, default 4096, sparse);
  checkpoints and swaps operate on it while the VM runs.

## Isolation

- **Process**: jailer chroot, uid 10000+n, cgroup v2 (`cpu.max`, `memory.max`), Firecracker's own seccomp filter.
- **Network**: each VM in its own netns with NAT egress; the host, private ranges,
  link-local/metadata and multicast are dropped; VMs cannot reach the manager container.
- **API**: guest agent, CDP and MCP bridges require the per-session token from MMDS V2 in the
  URL path; a fork adopts its own token (and reseeds its RNG) on first use.
- **Users**: every manager call is checked against the session owner; MCP servers and
  secrets live inside the owner's session VM only.

## Tests

```bash
FC_E2E_ENV_FILE=/path/to/env deploy/firecracker/scripts/run_e2e.sh
```

The env file holds `FCM_API_TOKEN` and, for the LLM-driven tests, `API_KEY`, `API_BASE`,
`MODEL_NAME`, `MODEL_PROVIDER`. Without an LLM those tests are skipped. With
`FC_E2E_DOCKER=1` the runner mounts the Docker socket so tests can restart the manager
and inspect its disk.

| File | Covers |
| --- | --- |
| `test_firecracker_e2e.py` | phase 1: file/shell, jailer, MMDS, network rules, multi-user isolation, pause, browsers, DeepAgent |
| `test_firecracker_phase2_e2e.py` | persist/restore, wake-on-request, manager restart, checkpoint pairing, Gateway fallback and SQLite store |
| `test_firecracker_phase3_e2e.py` | `Runner.release`, idle policy, per-user MCP servers, secrets, AgentTeam sharing one VM |
| `test_firecracker_phase4_e2e.py` | fork (running and persisted), Diff snapshots, workspace checkpoint and hot-swap |
| `test_firecracker_phase5_e2e.py` | tiers and rate limits, balloon, CPU templates, egress audit / allowlist, sealed snapshots, warm pool, metrics, agent-core egress |
| `test_firecracker_messages_e2e.py` | tasks notify / ask the user, across persist, per user, from a DeepAgent and an AgentTeam |
| `test_firecracker_playground_e2e.py` | Playground: separate frontend / backend origins, token and CORS, user isolation, every panel action, VM questions, manager restart, DeepAgent / AgentTeam / parallel / batch / cancel queries, and the real UI in Chromium |

Phase 5 tests expect the manager to run with
`FCM_TIERS='{"e2e-slow": {"disk_mbps": 20, "disk_iops": 1000, "net_mbps": 20, "max_sessions": 2}}'`,
`FCM_USER_TIERS='{"noisy": "e2e-slow", "fenced": "restricted"}'` and `FCM_WARM_POOL_SIZE=2`.

## Known limits

- Each session gets a full sparse copy of the rootfs (no shared read-only base + overlay
  yet); forks copy both disks, which dominates fork time.
- Secrets live in manager memory only: after a manager restart they must be set again.
- Workspace swap stops processes that still use `/workspace`.
- Manager-side message and audit buffers are in memory (the guest keeps its messages across a
  manager restart; they are read again after the VM wakes).
- The snapshot key lives on the data volume unless `FCM_SNAPSHOT_KEY_FILE` points elsewhere.
- The warm pool only serves the default profile without tier limits.
