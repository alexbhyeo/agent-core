---
title: Firecracker Session Sandbox Playground — Design (English version)
author: null
author_note: 历史作者待确认；缺少原 owner 和可靠创建署名
id: design_en
type: D
modules: []
files: []
---

# Firecracker Session Sandbox Playground — Design (English version)

Chinese master copy: [../design.md](../design.md). Design ID `D-20261127-0315-firecracker-playground`, status **approved** (2026-11-27).

## 1. Purpose

A Playground to use and test every Firecracker session-sandbox feature the way a real user
would: a ChatGPT-style web **frontend** and a **backend** API service that runs agent-core.
They are two independent deployables: the frontend can live on one VM, the backend together
with Firecracker (FcSessionManager) on another VM with KVM. Covered features: multiple users,
multiple sessions, DeepAgent / AgentTeam, lifecycle, fork, workspace, secrets, MCP, egress,
VM-task ↔ user messages, metrics and alerts.

## 2. Background, goals, scope

### 2.1 Background (verified)

- Phases 1–5 and messaging are usable only through REST, Python helpers and e2e tests; there
  is no UI to switch users, open several sessions, watch VM state or answer VM-task questions.
- The manager's `base_url` / `cdp_url` use host names inside the Docker network `fc-net`
  (`FCM_ADVERTISE_HOST=fc-manager`), unreachable from a browser; the manager needs the service
  credential `FCM_API_TOKEN` and the LLM needs an API key. Neither may reach the browser.
- DeepAgent streams `OutputSchema(type, index, payload)` via `Runner.run_agent_streaming`;
  AgentTeam streams `TeamOutputSchema` (+ `source_member`, `role`) via
  `Runner.run_agent_team_streaming`; team events have `payload.event_type = "team.*"`.

### 2.2 Goals and key scenarios

| Scenario | Today | Target |
| --- | --- | --- |
| Switch user (bottom-left menu: pick or type alice, bob, noisy, fenced, ...) | not possible | sidebar shows that user's sessions; every action runs as that user, so ownership, tiers, quotas and allowlists apply |
| New chat (DeepAgent or AgentTeam, optional profile) | REST only | session appears with VM state, tier, CPU template, egress mode |
| Ask a question | code only | streamed answer; team output labelled per member; title from first question |
| Queries in several sessions at once; one query sent to several sessions | not possible | each session runs independently with a running badge; no cross-talk |
| Ask again after pause / persist / manager restart | REST only | the query wakes or restores the VM; UI shows state changes and timing; files and processes are still there |
| VM task runs `fc-notify` / `fc-ask` | REST only | notification / question cards appear in the session; the user answers in the card and the task continues; unread badges elsewhere |
| Session panel: pause, persist, resume, delete, fork, workspace checkpoint / swap, secrets, MCP, shell, files, egress audit, debug | REST only | each action available with results or errors (403 / 409 / 429 ...) |
| Operator view | curl only | key `/metrics` values, alerts, sessions per user; optional manager restart to test persist-on-shutdown |

### 2.3 Scope

- In scope: `deploy/firecracker/playground/backend/` (aiohttp + agent-core Runner API service)
  and `deploy/firecracker/playground/frontend/` (plain HTML/CSS/JS static site, no build step),
  a Docker image and start script for each, cross-host deployment (access token, CORS), backend
  API tests and Playwright UI e2e tests, README.
- Out of scope: production login and per-user permissions (user switching is not
  authenticated; one shared access token only keeps strangers out); changes to agent-core
  public APIs or the manager contract; editing tiers in the UI (still manager env vars);
  scheduling across several Firecracker hosts.

### 2.4 Terms

- **Backend**: Python API service on the same host as FcSessionManager (Docker network
  `fc-net`); holds the manager credential and LLM settings; calls agent-core and the manager on
  behalf of the selected user. Exposes only `/api/v1/*` and `/healthz`.
- **Frontend**: static files only, served by any static server (an nginx image is provided);
  reads the backend URL from `config.js`; coupled to the backend only through the HTTP API.
- **Session kind**: `deepagent` (one DeepAgent) or `team` (AgentTeam: leader + N predefined
  members sharing one VM). **Run**: one query = one asyncio task in the backend.

## 3. Constraints, assumptions, open questions

- Constraints: the browser never gets `FCM_API_TOKEN` or the LLM key (D-01). User choices on
  2026-11-27: design first; Python backend + web UI; free user switcher without login; frontend
  and backend cleanly separated so they can run on different VMs (D-08). Reuse existing entry
  points (`build_firecracker_gateway_config`, `get_session_info`, `fork_session`,
  `set_session_secrets`, `register_session_mcp_server`, `get_session_messages`,
  `reply_to_session_message`, manager REST) without changing them. One `SysOperation` per user;
  team members share one `SysOperationSpec` id.
- Assumptions: **A-1** calling `Runner.run_agent_streaming` again with the same session id in
  the same backend process continues the conversation (checked by a follow-up question in e2e;
  fallback: prepend recent transcript). **A-2** after a backend restart the DeepAgent context is
  not guaranteed, but the VM and its files remain and the transcript is kept in SQLite. **A-3**
  `OutputSchema.type` values vary; known types get dedicated rendering, others a generic block.
- Decided questions (2026-11-27): **Q-1** 3 generic team members by default, 1–5 selectable.
  **Q-2** "restart manager" button, off by default, enabled with `PLAYGROUND_DOCKER=1`.

## 4. Solution and decisions

### 4.1 Overview

```mermaid
flowchart LR
  subgraph VMF[Frontend VM]
    F[Static site<br/>nginx: index.html / app.js / config.js]
  end
  B[Browser] -- "load static files" --> F
  B -- "HTTPS /api/v1/*<br/>Authorization: Bearer PLAYGROUND_TOKEN<br/>X-Playground-User" --> P
  P -- "SSE /api/v1/events (per user, fetch stream)" --> B
  subgraph VMB[Backend VM (KVM)]
    P[Playground backend<br/>aiohttp + agent-core Runner]
    A[DeepAgent / AgentTeam<br/>(in the backend process)]
    M[FcSessionManager]
    V[(microVM per session)]
  end
  P -- "Runner.run_agent_streaming / run_agent_team_streaming" --> A
  A -- "sys_operation calls (firecracker launcher)" --> M
  P -- "REST + service credential + X-Owner-User-Id" --> M
  M -- vsock --> V
  A -- HTTPS --> L[LLM]
```

The browser loads static files from the frontend VM and then calls `/api/v1` on the backend VM
directly (cross-origin); the frontend VM neither forwards API calls nor holds credentials.

### 4.2 Decisions

- **D-01 Backend fronts all access.** The browser calls only `/api/v1/*`; the backend holds the
  credentials, picks the user from `X-Playground-User` and calls the manager with
  `X-Owner-User-Id`. User switching is unauthenticated; a shared `PLAYGROUND_TOKEN` keeps others
  out (D-08).
- **D-02 One SSE stream per user** (`GET /api/v1/events`) carrying `session.updated`, `run.*`,
  `vm.message`, `alert`; replay by `Last-Event-ID` from a 1000-event buffer per user. Because
  `EventSource` cannot send `Authorization`, the frontend reads `text/event-stream` with `fetch`
  and reconnects itself.
- **D-03 One run per session, parallel across sessions.** A second query to a busy session
  gets 409 (the UI offers cancel); runs across sessions and users run in parallel up to
  `PLAYGROUND_MAX_RUNS` (default 8). Batch query sends one question to selected sessions as
  separate runs.
- **D-04 Agents.** `deepagent`: built like `examples/firecracker/deep_agent_firecracker.py`
  (per-user `fc-<user>` SysOperation, `build_model()`), with a system prompt explaining the
  workspace and `fc-notify` / `fc-ask`. `team`: the phase-3 `TeamAgentSpec` shape (leader + N
  predefined members, in-process transport, SQLite storage in the backend data dir, shared
  `SysOperationSpec` id `fc-team-<user>`); output per member; the run ends on `team.completed` /
  `team.idle`.
- **D-05 Facts.** VM state, tier and snapshots come from the manager (2 s cache); titles, kinds,
  transcripts and run state from the backend's SQLite / memory.
- **D-06 UI**: plain HTML/CSS/JS, ChatGPT-like: sidebar (New chat, batch query, operator view,
  sessions with state dot / running spinner / unread count, user menu), chat area (user
  messages, streamed output, member labels, collapsible tool calls, VM message cards, input with
  stop button), collapsible session panel (all actions in §2.2). Served by the frontend's own
  static server; first launch shows settings (backend URL override, access token); light / dark.
- **D-07 Optional manager restart** with `PLAYGROUND_DOCKER=1` (Docker socket), off by default.
- **D-08 Two independent deployables.** Separate directories, images and configuration; the only
  coupling is the versioned HTTP contract `/api/v1`:
  - frontend: static files + `config.js` (`window.PLAYGROUND_CONFIG = {apiBaseUrl: ...}`,
    generated at container start from `PLAYGROUND_API_BASE_URL`); no Python, credentials or
    Firecracker knowledge; nginx image provided, any static host works;
  - backend: Python package serving only `/api/v1/*` and `/healthz` (an opt-in
    `PLAYGROUND_SERVE_FRONTEND=<dir>` same-origin mode for local debugging, off by default);
  - access control: `PLAYGROUND_TOKEN` (required, entered once in the frontend, kept in
    `localStorage`, sent as `Authorization: Bearer`, compared in constant time);
    `PLAYGROUND_ALLOWED_ORIGINS` (CORS allowlist; preflight allows `Authorization`,
    `X-Playground-User`, `Content-Type`, `Last-Event-ID`); `PLAYGROUND_BIND` (default
    `0.0.0.0:8800`); put TLS in front for cross-host use;
  - the backend reaches the manager by URL (`FC_MANAGER_URL`), so backend and manager can later
    be split as well.

### 4.3 Rejected alternatives

| Candidate | Why not | Revisit when |
| --- | --- | --- |
| R-01 browser calls the manager directly | leaks the service credential; `fc-manager` not resolvable; agents still need Python | manager offers end-user auth |
| R-02 WebSocket | REST covers uplink; SSE is simpler with reconnect | low-latency two-way needs (terminal) |
| R-03 React + Vite | user chose no build step | UI grows a lot |
| R-04 generic HTTP API inside agent-core | out of scope; new public contract | product needs an official server |
| R-05 same-origin reverse proxy on the frontend VM forwarding `/api` | token in the proxy = anyone who opens the frontend can call the backend; couples the VMs. Kept as a documented deployment option | the backend address must be hidden |

## 5. Detailed design

### 5.1 Components and guarantees

`backend/fc_playground/`: `server.py` (app, `/api/v1` routes, token + CORS middleware, SSE),
`runs.py` (run tasks, chunk normalisation, cancel), `vm.py` (manager access, token stripping,
message follower), `store.py` (SQLite metadata + transcript), `events.py` (per-user event hub).
`frontend/`: `index.html`, `app.js` (API client + fetch-SSE), `style.css`, `config.template.js`,
`nginx.conf`, `Dockerfile`.

- **P-1** no manager credential, LLM key or guest token in any response or event.
- **P-2** each request only touches sessions of `X-Playground-User`; manager 403s are passed
  through, never bypassed.
- **P-3** at most one run per session; every run ends with `run.finished`.
- **P-4** VM messages go only to the owning user's stream.
- **P-5** wrong or missing `PLAYGROUND_TOKEN` → 401 (except `/healthz`); origins outside the
  allowlist get no CORS grant.
- **P-6** changing the backend address only needs `config.js` / `PLAYGROUND_API_BASE_URL`.

### 5.3 API (`/api/v1`, all need `Authorization: Bearer <PLAYGROUND_TOKEN>` and `X-Playground-User` matching `[A-Za-z0-9_.@-]{1,64}`)

| Endpoint | Result | Errors |
| --- | --- | --- |
| `GET /me` | user, tier, known users | — |
| `GET /sessions` | user's sessions: metadata + manager description (token-free) | `vm_unknown` if the manager is down |
| `POST /sessions` `{kind, title?, profile?, team_size?}` | 201 new session (VM created now) | 400 / 429 / 503 passed through |
| `GET /sessions/{id}` | metadata, VM, run state, transcript | 403 / 404 |
| `DELETE /sessions/{id}` | delete VM and record (cancels a run) | 403 |
| `POST /sessions/{id}/query` `{text}` | 202 `{run_id}`; output over SSE | 409 busy; 429 global limit |
| `POST /batch-query` `{session_ids, text}` | 202 per-session `run_id` or error | per item |
| `POST /sessions/{id}/cancel` | cancel the run | 404 none |
| `POST /sessions/{id}/vm/{pause|persist|resume}` | VM action | passed through |
| `POST /sessions/{id}/fork` `{count}` | new sessions | passed through |
| `GET|POST /sessions/{id}/workspace` | list / `{"checkpoint"}` / `{"swap"}` | passed through |
| `PUT /sessions/{id}/secrets` | key names | passed through |
| `POST /sessions/{id}/mcp` | register an MCP server | passed through |
| `POST /sessions/{id}/shell` `{command}` | `{exit_code, stdout, stderr}` | passed through |
| `GET /sessions/{id}/files?path=` | listing or text (≤ 256 KiB) | passed through |
| `GET /sessions/{id}/messages`, `POST .../messages/{mid}/reply` | VM messages, answer | 404 / 409 |
| `GET /sessions/{id}/egress`, `GET /sessions/{id}/debug` | audit, debug (token-free) | passed through |
| `GET /ops`, `POST /ops/restart-manager` | metrics summary, alerts, sessions per user; restart (only with `PLAYGROUND_DOCKER=1`) | 404 if disabled |
| `GET /events?last_event_id=` | SSE | 401 |
| `GET /healthz` (no token) | `{"status": "ok", "api_version": 1}` | — |

Events (JSON, each with `id`, `type`, `session_id`): `session.updated {session}`, `run.started
{run_id, text}`, `run.chunk {run_id, member, kind, text, data}` (`kind`: answer / delta /
reasoning / tool_call / tool_result / team_event / other), `run.finished {run_id, status,
error?, duration_s}`, `vm.message {message}`, `alert {alerts}`.

### 5.7 Query flow

1. `POST /sessions/{id}/query` → ownership check (manager GET), busy and global-limit checks,
   store the user message, emit `run.started`.
2. The run task calls the agent stream under that session id; the first sys_operation call
   goes through the launcher (`POST /v1/sessions`), which wakes / restores the VM if needed;
   `session.updated` reflects the state.
3. Each chunk → `normalize_chunk` → `run.chunk` + transcript.
4. End of stream (teams: `team.completed` / `team.idle`) → `run.finished`.

VM messages: while a user has an event stream open, the follower long-polls that user's
RUNNING sessions and emits `vm.message`; replies go through `reply_to_session_message`.

### 5.8 Failures

Manager errors are passed as `{error, status}` and shown as toasts; LLM / tool errors end the
run with `status=error`; cancel ends with `cancelled` (background tasks in the VM keep
running); a backend restart loses running tasks but keeps metadata and transcripts; SSE
reconnects with `Last-Event-ID`.

### 5.11 Security and layout

Token + CORS allowlist on the backend; user switching unauthenticated (test environments
only); TLS via reverse proxy for cross-host use; URLs with guest tokens stripped; all text
escaped in the UI (safe renderer for code blocks and line breaks only).

```text
deploy/firecracker/playground/
  backend/                     # backend VM (next to FcSessionManager)
    fc_playground/  __init__.py  __main__.py  server.py  runs.py  vm.py  store.py  events.py
    Dockerfile  run_backend.sh # agent-core deps + fc_playground; publishes 8800
  frontend/                    # frontend VM (any static host)
    index.html  app.js  style.css  config.template.js
    nginx.conf  Dockerfile  run_frontend.sh   # nginx:alpine; config.js from PLAYGROUND_API_BASE_URL; publishes 8080
```

Example: frontend VM `PLAYGROUND_API_BASE_URL=https://backend.example:8800 run_frontend.sh`;
backend VM `PLAYGROUND_TOKEN=... PLAYGROUND_ALLOWED_ORIGINS=https://frontend.example
run_backend.sh`. Locally both can run on one machine on different ports, still cross-origin.

## 6. Implementation items

I-01 store + events; I-02 `vm.py`; I-03 `runs.py`; I-04 `server.py`; I-05 frontend; I-06 two
images and start scripts; I-07 `test_firecracker_playground_e2e.py`; I-08 README + feature
record. No change under `openjiuwen/`, no manager contract change; S_01 only links the new
feature record.

## 8. Verification

| ID | Scenario | Pass condition |
| --- | --- | --- |
| PG-V-01 | start both containers | page loads and shows backend OK; no credential or guest token in responses |
| PG-V-02 | DeepAgent question + follow-up (LLM) | `run.started` → `run.chunk`s → `run.finished ok`; file written in the VM; follow-up uses context (A-1) |
| PG-V-03 | parallel sessions (alice ×2, bob ×1) + batch query (LLM) | events carry only their own session id; all complete; busy session → 409 |
| PG-V-04 | AgentTeam (LLM) | member-labelled output; one VM; run ends |
| PG-V-05 | wake / restart | query after pause / persist (and after manager restart with `PLAYGROUND_DOCKER=1`) succeeds; files remain |
| PG-V-06 | user isolation | bob gets 403 / 404 on alice's sessions; bob's stream has no alice events |
| PG-V-07 | VM-task messages | `vm.message` arrives; reply completes the task |
| PG-V-08 | session panel actions | fork, workspace, secrets, MCP, files, egress, debug work; noisy over quota → 429 |
| PG-V-09 | UI (Playwright) | switch user, new chat, send query, answer a VM question; no console errors |
| PG-V-10 | cancel and errors | `run.finished cancelled`; errors shown; backend keeps running |
| PG-V-11 | separation and access control | frontend and backend on different origins work end to end; missing / wrong token → 401; non-allowlisted origin gets no `Access-Control-Allow-Origin`; frontend image has no Python or credentials |

## 9. Risks and limits

Risks: unexpected `OutputSchema.type` values (generic rendering); A-1 not holding (prepend
transcript); host memory (~7 GB) limits parallel VMs (`PLAYGROUND_MAX_RUNS`, warm pool size).
Limits: unauthenticated user switching with one shared token (test use only); DeepAgent context
not restored after a backend restart; tiers not editable in the UI.

## 10. Review log

| Date | Reviewer | Topic | Outcome |
| --- | --- | --- | --- |
| 2026-11-27 | alexbhyeo (user) | process and stack | design first; Python backend + web UI; free user switcher |
| 2026-11-27 | alexbhyeo (user) | design review | approved; Q-1 and Q-2 as recommended |
| 2026-11-27 | alexbhyeo (user) | deployment | frontend and backend cleanly separated: frontend on one VM, backend + Firecracker on another (D-08, R-05, P-5, P-6, PG-V-11) |

## 11. Handover

Related: D-20261124-0855-firecracker-session-manager; S_01_firecracker-session-sandbox. Approved on
2026-11-27; implementation follows §6 and §8.
