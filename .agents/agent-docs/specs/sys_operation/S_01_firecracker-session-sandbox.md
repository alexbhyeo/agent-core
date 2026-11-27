---
id: S_01_firecracker-session-sandbox
title: Firecracker 会话沙箱规约
type: S
author: alexbhyeo
author_email: yeo.boon.hong@huawei.com
modules:
  - openjiuwen/extensions/sys_operation
  - openjiuwen/core/sys_operation
  - openjiuwen/core/runner
  - deploy/firecracker
files:
  - openjiuwen/extensions/sys_operation/sandbox/launchers/firecracker.py
  - openjiuwen/extensions/sys_operation/sandbox/launchers/__init__.py
  - openjiuwen/core/sys_operation/sandbox/gateway/gateway.py
  - openjiuwen/core/sys_operation/sandbox/gateway/sandbox_store.py
  - openjiuwen/core/sys_operation/sandbox/launchers/base.py
  - openjiuwen/core/sys_operation/config.py
  - openjiuwen/core/runner/runner.py
  - deploy/firecracker/manager/fc_session_manager/api.py
  - deploy/firecracker/manager/fc_session_manager/sessions.py
  - deploy/firecracker/manager/fc_session_manager/store.py
  - deploy/firecracker/manager/fc_session_manager/firecracker.py
  - deploy/firecracker/manager/fc_session_manager/network.py
  - deploy/firecracker/manager/fc_session_manager/proxy.py
  - deploy/firecracker/manager/fc_session_manager/config.py
  - deploy/firecracker/manager/fc_session_manager/tiers.py
  - deploy/firecracker/manager/fc_session_manager/egress.py
  - deploy/firecracker/manager/fc_session_manager/metrics.py
  - deploy/firecracker/manager/fc_session_manager/snapcodec.py
  - deploy/firecracker/guest/fc_guest_agent.py
  - deploy/firecracker/guest/fc-init
  - deploy/firecracker/guest/fc-msg
  - deploy/firecracker/scripts/agent_core_egress.sh
---

# Firecracker 会话沙箱规约

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | S_01 / firecracker-session-sandbox |
| 关联模块 | `openjiuwen/extensions/sys_operation/sandbox/launchers`、`openjiuwen/core/sys_operation/sandbox/gateway`、`openjiuwen/core/runner`（agent-core 侧）；`deploy/firecracker`（FcSessionManager、guest agent） |
| 最近一次修订日期 | 2026-11-26 |
| 关联 feature | [F_20261125_0200_firecracker-session-sandbox-phase1](../../features/F_20261125_0200_firecracker-session-sandbox-phase1.md)、[F_20261126_0726_firecracker-session-sandbox-phase2-4](../../features/F_20261126_0726_firecracker-session-sandbox-phase2-4.md)、[F_20261126_0926_firecracker-session-sandbox-phase5-messaging](../../features/F_20261126_0926_firecracker-session-sandbox-phase5-messaging.md)；使用方：[F_20261127_0456_firecracker-playground](../../features/F_20261127_0456_firecracker-playground.md)（Playground，只消费本契约） |
| 设计依据 | D-20261124-0855-firecracker-session-manager（第一至第五阶段范围）；VM 任务用户消息为用户追加需求 |

本规约定义 Firecracker 会话沙箱的当前契约：agent-core 的 `firecracker` launcher 与辅助函数、SandboxGateway 的持久化 store 与兜底重试、`Runner.release` 对会话 VM 的清理、FcSessionManager REST API（含 persist、唤醒、闲置策略、fork、workspace 热替换、会话内 MCP 与密钥、用户分级与限速、balloon、CPU 模板选择、出站审计与白名单、冷快照封存、warm pool、指标告警、VM 任务与用户的消息）、会话归属、网络隔离和 guest agent 接口，以及 agent-core 容器的出站限制脚本。

## 1. 范围、术语与适用条件

### 1.1 责任范围

- **本规约管：** `launcher_type="firecracker"` 的启动、暂停（内存或快照两种模式）、恢复、删除、重新挂接与状态映射；SandboxGateway 的 store 选择、连接失败兜底与按会话释放；FcSessionManager 的 REST 接口、生命周期状态机、闲置策略与会话归属校验；每会话 VM 的进程、网络与 API 隔离；guest agent 暴露的 AIO 兼容子集、CDP 桥与 MCP 桥。
- **本规约不管：** aio provider 对结果的解释（`extensions/sys_operation/sandbox/providers/aio.py`）；浏览器 subagent 的 Playwright MCP 运行（`harness/tools/browser_move`）；AgentTeam 运行时本身（只规定其成员共用一台 VM 的配置方式）。

### 1.2 术语与适用条件

- **会话（session）：** agent-core 的 session_id，对应 FcSessionManager 中唯一一台 microVM（AgentTeam 为团队会话 id）。
- **owner：** 会话所属的最终用户（`owner_user_id`），由 `FirecrackerLauncherConfig.owner_user_id` 提供。
- **服务凭据：** `FCM_API_TOKEN`，agent-core 访问 FcSessionManager 的 Bearer token。
- **guest token：** 每会话随机生成，经 MMDS V2 下发给 guest agent，作为 `base_url` / `cdp_url` / `mcp_url` 的路径前缀 `/t/<guest_token>`。
- **VM run：** 一次 Firecracker 进程（jailer chroot、槽位、网络命名空间），在启动 / 恢复时创建，在 persist / 删除时释放；会话目录（磁盘、快照）与三个代理端口在会话生命周期内保持。
- **适用条件：** FcSessionManager 运行在带 KVM 的 Linux 主机（或开启嵌套虚拟化的 WSL2）上的特权容器中。

### 1.3 复用与适配边界

| 既有能力 | 复用的契约 | 本模块的适配责任 | 未覆盖的保证 |
| --- | --- | --- | --- |
| `SandboxLauncher` / `SandboxRegistry` | `launch / pause / resume / delete / check_status` 接口与注册 | 注册 `firecracker`；进程内 handle 表；`restore_handle` 从 store 元数据重建 handle | 服务 token 不写入元数据，重建时取环境变量 `FCM_API_TOKEN` |
| `SandboxGateway` / `SandboxStore` | 隔离 key 路由与记录 | `SqliteSandboxStore`；记录元数据含 `launcher`、`session_id`；连接失败时清缓存重试一次；`release_session` | 只重试一次；非连接类错误不重试 |
| aio provider + `agent_sandbox` SDK | fs / shell / code 调用与结果解析 | guest agent 实现 provider 实际调用的 7 个接口，wire format 与 SDK 一致 | 未实现的 AIO 接口返回 404 |
| `BrowserInstanceConfig` | `key` / `driver_mode` / `cdp_url` 远程模式 | `build_browser_instance` 返回按 owner+session 区分的 dict | Playwright MCP 进程运行在 agent-core 主机 |
| `ResourceMgr.add_mcp_server` / `McpServerConfig` | streamable-http MCP 客户端 | `register_session_mcp_server` 生成 `<owner>--<session>--<name>` 的资源 id 与会话 VM 内的 URL | MCP 服务进程随 VM 生命周期，VM persist 后经唤醒恢复 |

## 2. 数据结构与事实归属

| 对象 | 含义与作用域 | 事实权威 | 修改入口 |
| --- | --- | --- | --- |
| `FirecrackerLauncherConfig` | 每个 `SysOperation` 的启动配置（manager 地址、owner、服务 token、VM profile、`pause_mode`） | agent-core 配置 | 构造时传入；推荐经 `build_firecracker_gateway_config` |
| 会话记录（`SessionRecord`） | session_id、owner、状态、槽位、端口、guest token、profile、快照代数、`checkpoint_ref`、`workspace_mib`、`forked_from`、活跃时间 | FcSessionManager 的 SQLite（`state.db`，启动时前向迁移新列） | 仅 FcSessionManager 的生命周期方法 |
| 会话目录 `sessions/<id>/` | `rootfs.ext4`、`workspace.ext4`、`snapshots/gen-N/{vm.state,mem,meta.json}`、`workspace-checkpoints/<name>.ext4` | FcSessionManager 数据卷 | 创建、persist、fork、checkpoint、swap、删除 |
| 会话密钥 | 每会话工具凭据 | FcSessionManager 进程内存 + VM 的 MMDS | `PUT /secrets`；从不写入 store、磁盘或快照 |
| Gateway 记录 | 隔离 key → sandbox_id、base_url、元数据 | `SandboxStore`（内存或 SQLite） | `SandboxGateway` |
| 用户分级（`Tier`） | 规格上限、配额、限速、TTL、出站白名单 | `FCM_TIERS` / `FCM_USER_TIERS`（运维配置） | 仅 manager 启动时读取；会话记录保存所用级别名 |
| 用户消息 | VM 任务发给用户的通知与问题（含状态、答案） | guest 内存（环形 1000 条）；manager 内存缓冲为副本 | guest 本地端点写入；`reply` 投递答案 |
| 出站审计 | 每个新出站连接与代理决定 | `data_dir/audit/egress-*.jsonl` + 每会话内存缓冲 | 仅 manager |
| launcher handle 表 | sandbox_id → (manager_url, owner, token, pause_mode) | 进程内存（`FirecrackerLauncher._handles`） | `launch()` / `restore_handle()` 写入，`delete()` 移除 |

关键字段：`state ∈ {CREATING, RUNNING, PAUSED, PERSISTED, DESTROYED, FAULTED}`；`base_url = http://<advertise_host>:<agent_port>/t/<guest_token>`，`cdp_url`、`mcp_url` 分别使用 `agent_port + 1`、`agent_port + 2`。

## 3. 不变量

1. **I-1 归属校验：** FcSessionManager 对每个会话接口（含 persist、resume、fork、workspace、mcp-servers、secrets、debug）校验 `X-Owner-User-Id` 等于会话 owner，不符返回 403，且不改变会话状态。列表只返回调用方自己的会话；fork 只产生同一 owner 的会话。
2. **I-2 隔离 key 含 owner：** `build_firecracker_gateway_config` 把 owner 写入隔离前缀。每个用户注册一个 `SysOperation`；sandbox 操作按每次调用解析出的隔离 key 分别缓存 gateway client，同一用户的不同会话各自路由到自己的 VM。
3. **I-3 一会话一 VM：** 同一 session_id 至多对应一台非 DESTROYED 的 VM；同 owner 重复 `POST /v1/sessions` 幂等返回现有会话，PAUSED / PERSISTED 时先唤醒。AgentTeam 成员使用相同的 `SysOperationSpec` id，共用团队会话的一台 VM。
4. **I-4 进程隔离：** 每次 VM run 经 jailer 启动：chroot、独立 uid（`uid_base + 槽位`）、cgroup v2（`/fc/<vm_id>`）、独立网络命名空间；Firecracker 内置 seccomp 必须启用，禁止 `--no-seccomp`。
5. **I-5 网络边界：** guest 只能经主机 NAT 访问公网；主机、私网（10/8、172.16/12、192.168/16、100.64/10）、链路本地/元数据、组播与回环一律丢弃。MMDS 由 Firecracker 设备层拦截，不受此规则影响。
6. **I-6 guest API 鉴权：** guest agent、CDP 桥与 MCP 桥只接受以 `/t/<guest_token>/` 开头的请求（`/healthz` 除外），否则 403；guest token 只经 MMDS V2 下发。token 不匹配时 guest 至多每秒重读一次 MMDS，以便 fork 出的 VM 采用新身份，旧 token 随即失效。
7. **I-7 删除即清理：** 删除会话会终止 VM 进程，关闭代理及其连接，删除网络命名空间、cgroup、chroot、会话目录（磁盘、快照、workspace 检查点）与内存中的密钥，记录置为 DESTROYED。
8. **I-8 快照兼容：** 每代快照的 `meta.json` 记录 Firecracker 版本与 CPU 模板；恢复或 fork 时任一不符返回 409，快照保持不动。
9. **I-9 快照文件不被就地改写：** 快照目标文件在创建前先删除；被多处硬链接（fork 共享、保留的快照）的基础内存文件在合并 Diff 前先复制，只有单一链接时才就地合并。
10. **I-10 密钥不落盘：** 密钥只存在于 manager 内存与 VM 的 MMDS（MMDS 数据不属于快照，恢复后由 manager 重新下发）；会话描述只返回密钥名。
11. **I-11 checkpoint 配对：** `:resume` 携带 `expected_checkpoint_ref` 且与记录不符时返回 409，不触碰快照。
12. **I-12 Gateway 兜底：** Gateway 调用遇到连接类失败时，清除该隔离 key 的缓存与 store 记录，重新 `launch` 后重试一次；仍失败则返回错误。
13. **I-13 分级不可由调用方扩大：** 用户的级别只由运维配置决定；profile 的 vCPU、内存、白名单只能落在级别范围内（否则 400），保留会话数超过级别配额返回 429。
14. **I-14 受限出站：** 带白名单的会话不能直接出站；只能经 manager 的白名单代理访问白名单内、解析为公网地址的域名的 80 / 443 端口，该限制在每次 VM run（含恢复、fork）重新施加。
15. **I-15 冷快照不留明文：** 启用压缩 / 加密时，PERSISTED 超过 `snapshot_seal_after_s` 的快照只以 `*.sealed` 存在；密钥文件 0600，数据目录 0700。
16. **I-16 warm pool 不泄露身份：** 池中 VM 无用户、无端口；被领用时先经 MMDS 下发新身份、token 与熵种子并确认 guest 采用，才返回给调用方。
17. **I-17 消息归属：** 只有会话 owner 能读取其消息与回答问题；VM 内消息端点只监听 guest 回环地址；一个问题只能被回答一次（409）。

## 4. 接口契约

### 4.1 共通规则

- 所有 REST 调用需 `Authorization: Bearer <FCM_API_TOKEN>`（`/healthz` 除外），缺失或错误返回 401。
- 会话接口需 `X-Owner-User-Id`；缺失返回 400。
- 错误体为 `{"error": "<原因>"}`；状态码：400 参数错误，403 归属不符，404 不存在，409 状态冲突或快照不兼容，503 无可用槽位 / 端口或 guest 未就绪，500 内部错误（不回传堆栈）。
- agent-core 侧所有失败抛出 `StatusCode.SYS_OPERATION_SANDBOX_LAUNCHER_ERROR`，`details={"launcher": "firecracker", "http_status": <状态码或 None>}`；网络异常以 `cause` 附带原始异常。`is_ownership_error(exc)` 判断 403。store 工厂的配置错误抛出 `SYS_OPERATION_SANDBOX_GATEWAY_ERROR`。

### 4.2 FcSessionManager REST API

| 方法 / 路径 | 成功响应 | 说明 |
| --- | --- | --- |
| `POST /v1/sessions` `{session_id, owner_user_id?, profile?}` | 201 新建 / 200 已存在 | profile 键：`vcpus`、`mem_mib`（均受用户级别上限约束）、`workspace_mib`（0 或 64–65536）、`ttl_pause_s`、`ttl_persist_s`、`ttl_destroy_s`、`wake_on_request`、`cpu_template`（本机提供的模板或 `none`）、`egress_allowlist`（域名列表，须为级别白名单子集）、`snapshot_seal_after_s`；未知键 400；超出配额 429 |
| `GET /v1/sessions[?owner=]` | 200 `{sessions:[...]}` | `owner` 与请求头不同返回 403 |
| `GET /v1/sessions/{id}` | 200 会话描述 | 含 URL、`secret_keys`、`policy`、`snapshot_gen`、`checkpoint_ref`、`workspace_checkpoints`、`forked_from` |
| `POST /v1/sessions/{id}:pause` | 200 | RUNNING → PAUSED；PAUSED、PERSISTED 幂等 |
| `POST /v1/sessions/{id}:resume` `{expected_checkpoint_ref?}` | 200 | PAUSED → RUNNING，PERSISTED → 恢复；见 I-11 |
| `POST /v1/sessions/{id}:persist` `{checkpoint_ref?}` | 200 | RUNNING / PAUSED → PERSISTED；PERSISTED 时只更新 `checkpoint_ref` |
| `POST /v1/sessions/{id}:fork` `{new_session_ids}` | 201 `{sessions:[...]}` | 1–8 个互不相同、未被占用的 id；源须为 RUNNING / PAUSED / PERSISTED；新会话 RUNNING，继承源的 profile 与密钥 |
| `POST /v1/sessions/{id}/workspace/checkpoints` `{name}` | 201 `{name, workspace_checkpoints}` | 冻结 `/workspace` 后复制其磁盘；无 workspace 盘 409 |
| `POST /v1/sessions/{id}/workspace:swap` `{checkpoint}` | 200 会话描述 | 把 `/workspace` 换成检查点的副本，不重启 VM；检查点不存在 404 |
| `POST /v1/sessions/{id}/mcp-servers` `{name, command, args?, env?}` | 200 `{name, url, servers}` | name 匹配 `[a-z0-9][a-z0-9_-]{0,63}`；在 VM 内运行 stdio MCP 服务，`url = <mcp_url>/servers/<name>/mcp` |
| `PUT /v1/sessions/{id}/secrets` `{KEY: value \| null}` | 200 `{session_id, secret_keys}` | 合并（null 删除）并在 VM 运行时立即写入 MMDS |
| `DELETE /v1/sessions/{id}` | 200 `{deleted: true}` | 见 I-7 |
| `GET /v1/sessions/{id}/debug` | 200 | jailer 事实（`root_entries`、`exe`、uid、cgroup、命名空间 inode、seccomp）、当前快照的 `meta.json` 与文件大小、balloon 统计 |
| `GET /v1/sessions/{id}/egress?limit=` | 200 `{session_id, audit, entries}` | 最近的出站审计记录（`kind` 为 `connect` 或 `proxy`） |
| `GET /v1/sessions/{id}/messages?after=&wait=` | 200 `{session_id, state, messages, last_id}` | VM 任务消息（`notify` / `question`，问题带 `status`）；`wait` ≤ 60 秒长轮询；VM 非运行时返回缓冲 |
| `POST /v1/sessions/{id}/messages/{mid}/reply` `{text}` | 200 消息 | 先唤醒 VM 再投递；不存在 404，已回答或已过期 409 |
| `GET /metrics` | 200 Prometheus 文本 | 需服务凭据 |
| `GET /v1/alerts` | 200 `{alerts:[{name, severity, message}]}` | 槽位、主机内存、数据盘、创建 P95、近期失败、warm pool |

### 4.3 agent-core 接口

- `FirecrackerLauncher.launch`：以当前上下文的 session_id（缺省时退回清洗后的 isolation_key）调用 `POST /v1/sessions`，返回带 `metadata`（manager 地址、owner、`pause_mode`，不含 token）的 `LaunchedSandbox`。
- `pause`：`pause_mode="memory"` 调用 `:pause`；`"snapshot"` 调用 `:persist`。`resume` / `delete` / `check_status`（RUNNING→`RUNNING`；PAUSED、PERSISTED→`PAUSED`；其余及 404→`KILLED`）同第一阶段。
- `restore_handle(sandbox_id, metadata)`：从 Gateway 记录重建 handle（token 取 `FCM_API_TOKEN`）。
- `persist(sandbox_id, checkpoint_ref)`、`scoped_resource_id(owner, session, name)`、`register_session_mcp_server(...)`、`set_session_secrets(...)`、`fork_session(config, session_id, new_session_ids)`、`build_browser_instance(...)`、`get_session_info(...)`：见 `launchers/__init__.py` 导出。
- `SandboxGateway.release_session(session_id, on_stop="delete") -> int`：删除（或按 on_stop 处理）该 session 的全部 Gateway 记录及其 sandbox，返回数量。`Runner.release(session_id)` 在已有 Gateway 实例时调用它；不创建 Gateway。
- `GatewayStoreConfig(type="sqlite", sqlite_path=...)` 选择 `SqliteSandboxStore`。
- `get_session_messages(config, session_id, after=0, wait_seconds=0)`、`reply_to_session_message(config, session_id, message_id, text)`：VM 任务消息的读取与回答。
- `deploy/firecracker/scripts/agent_core_egress.sh <llm-host>[:port] ...`：在 agent-core 容器内（`NET_ADMIN`）只放行回环、DNS、manager 与指定 LLM 端点。

### 4.4 guest agent（vsock 8080，经代理暴露为 `base_url`）

实现 `POST v1/file/read|write|list|glob|upload`、`GET v1/file/download`、`POST v1/shell/exec`（`/bin/bash -c`，默认工作目录 `/workspace`，shell 退出即完成），请求与响应遵循 `agent_sandbox` SDK（`{success, message, data}`）。另有 `GET fc/identity`、`GET|POST fc/mcp/servers`、`POST fc/workspace {action: freeze|thaw|unmount|mount}`、`GET fc/messages?after=&wait=`、`POST fc/messages/reply {id, text}`。受限会话的 shell 命令带 MMDS 中 `egress_proxy` 的代理环境变量。

VM 内任务使用 `fc-notify [--task N] TEXT` 与 `fc-ask [--task N] [--timeout S] TEXT`（打印答案；超时退出码 2），二者访问 guest agent 仅监听 `127.0.0.1:8090` 的本地端点；任务名默认取 `$FC_TASK`。guest agent 与 mcp-proxy 以 `/` 为工作目录；`unmount` 终止仍在使用 `/workspace` 的其他进程。

### 4.5 CDP 桥（vsock 9222，`cdp_url`）与 MCP 桥（vsock 9300，`mcp_url`）

- CDP：首次请求时启动无头 Chromium；`/t/<token>/json*` 以本地 Host 访问并改写 websocket 地址；websocket 升级请求双向转发。
- MCP：去掉 token 前缀、改写 Host 后转发到 VM 内 `127.0.0.1:9301` 的 mcp-proxy（streamable HTTP，`--stateless`）；未注册任何服务时 503。

## 5. 生命周期

```
CREATING ──boot ok──▶ RUNNING ◀──resume / 唤醒── PAUSED
   │                  │   ▲ └──pause / T1──────▶ │
   └─boot 失败─▶ FAULTED   │ └──恢复 / 唤醒── PERSISTED ◀──persist / T2──┘
                          └──────persist────────▶ │
          任意状态 ──delete──▶ DESTROYED ◀── T3 ──┘
```

- **闲置策略：** RUNNING 无流量超过 `ttl_pause_s`（默认 600）→ PAUSED；PAUSED 超过 `ttl_persist_s`（1800）→ PERSISTED；PERSISTED 超过 `ttl_destroy_s`（7 天）→ DESTROYED。值 ≤ 0 关闭对应阶段。
- **唤醒：** `wake_on_request` 为真时，代理收到请求先恢复 PAUSED / PERSISTED 的会话。
- **快照：** 首次 persist 写 Full；VM 由快照恢复或刚被 fork 后（启用 `FCM_DIFF_SNAPSHOTS`）persist 写 Diff，并用 `snapshot-editor edit-memory rebase` 合并进基础内存文件（见 I-9）。恢复成功后删除会话目录中的该代快照（内存文件经 chroot 硬链接保持映射）。
- **manager 重启：** 关停时（`FCM_PERSIST_ON_SHUTDOWN`）persist 运行中的会话；启动时遗留的 CREATING / RUNNING / PAUSED 标为 FAULTED，PERSISTED 会话重新监听端口。FAULTED 会话再次 `POST` 时重建。
- **fork：** 运行中的源仅在内存转储与磁盘复制期间暂停；各 fork 共享快照文件的硬链接，各自拥有磁盘副本、端口与 guest token。
- **balloon：** 每台 VM 带 `deflate_on_oom` 的 balloon；reaper 每 10 秒使 guest 可用内存保持约四分之一（至少 256 MiB）。
- **冷快照：** PERSISTED 超过 `snapshot_seal_after_s` 后压缩（zstd）并加密（AES-256-CTR），恢复或 fork 前解封。
- **warm pool：** 维持 `FCM_WARM_POOL_SIZE` 台默认规格 VM；默认规格、无限速且无白名单的创建请求直接领用。
- **消息：** pause / persist 前 manager 先拉取一次 guest 消息，冻结期间仍可读；回答会唤醒 VM。

## 6. 限制

- 每会话复制一份完整的稀疏 rootfs（无共享只读基础盘 + overlay）；fork 复制两块磁盘，是 fork 耗时的主要部分。
- 密钥只在 manager 内存中，manager 重启后需重新设置。
- workspace 热替换会终止仍在使用 `/workspace` 的进程。
- CDP、MCP 桥依赖 guest token 防护；Playwright MCP 进程运行在 agent-core 主机。
- aio provider 把命令包装为 `{ CMD; }`，以 `&` 结尾的后台命令会成为语法错误（既有行为）。
- manager 侧消息缓冲与审计环形缓冲只在内存；agent-core 出站脚本按执行时解析的地址放行。
