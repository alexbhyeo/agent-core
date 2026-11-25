---
id: S_01_firecracker-session-sandbox
title: Firecracker 会话沙箱规约
type: S
author: alexbhyeo
author_email: yeo.boon.hong@huawei.com
modules:
  - openjiuwen/extensions/sys_operation
  - deploy/firecracker
files:
  - openjiuwen/extensions/sys_operation/sandbox/launchers/firecracker.py
  - openjiuwen/extensions/sys_operation/sandbox/launchers/__init__.py
  - deploy/firecracker/manager/fc_session_manager/api.py
  - deploy/firecracker/manager/fc_session_manager/sessions.py
  - deploy/firecracker/manager/fc_session_manager/network.py
  - deploy/firecracker/manager/fc_session_manager/proxy.py
  - deploy/firecracker/guest/fc_guest_agent.py
---

# Firecracker 会话沙箱规约

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | S_01 / firecracker-session-sandbox |
| 关联模块 | `openjiuwen/extensions/sys_operation/sandbox/launchers`（agent-core 侧）；`deploy/firecracker`（FcSessionManager、guest agent） |
| 最近一次修订日期 | 2026-11-25 |
| 关联 feature | [F_20261125_0200_firecracker-session-sandbox-phase1](../../features/F_20261125_0200_firecracker-session-sandbox-phase1.md) |
| 设计依据 | D-20261124-0855-firecracker-session-manager（第一阶段范围） |

本规约定义第一阶段 Firecracker 会话沙箱的当前契约：agent-core 的 `firecracker` launcher、FcSessionManager REST API、会话归属、网络隔离和 guest agent 接口。持久化（persist / snapshot）尚未实现，不属于当前契约。

## 1. 范围、术语与适用条件

### 1.1 责任范围

- **本规约管：** `launcher_type="firecracker"` 的启动、暂停、恢复、删除与状态映射；FcSessionManager 的 REST 接口与会话归属校验；每会话 VM 的进程、网络与 API 隔离；guest agent 暴露的 AIO 兼容子集与 CDP 桥。
- **本规约不管：** `SandboxGateway` 的通用路由与缓存（`core/sys_operation/sandbox/gateway`）；aio provider 对结果的解释（`extensions/sys_operation/sandbox/providers/aio.py`）；浏览器 subagent 的 Playwright MCP 运行（`harness/tools/browser_move`）。

### 1.2 术语与适用条件

- **会话（session）：** agent-core 的 session_id，对应 FcSessionManager 中唯一一台 microVM。
- **owner：** 会话所属的最终用户（`owner_user_id`），由 `FirecrackerLauncherConfig.owner_user_id` 提供。
- **服务凭据：** `FCM_API_TOKEN`，agent-core 访问 FcSessionManager 的 Bearer token。
- **guest token：** 每会话随机生成，经 MMDS V2 下发给 guest agent，作为 `base_url` / `cdp_url` 的路径前缀 `/t/<guest_token>`。
- **适用条件：** FcSessionManager 运行在带 KVM 的 Linux 主机（或开启嵌套虚拟化的 WSL2）上的特权容器中。

### 1.3 复用与适配边界

| 既有能力 | 复用的契约 | 本模块的适配责任 | 未覆盖的保证 |
| --- | --- | --- | --- |
| `SandboxLauncher` / `SandboxRegistry` | `launch / pause / resume / delete / check_status` 接口与注册 | 注册 `firecracker`；用进程内 handle 表补足无参构造的 launcher 缺少的管理端地址与 owner | Gateway 的 store 仍是内存实现，进程重启后靠下一次 `launch()` 重新绑定 |
| aio provider + `agent_sandbox` SDK | fs / shell / code 调用与结果解析 | guest agent 实现 provider 实际调用的 7 个接口，wire format 与 SDK 一致 | 未实现的 AIO 接口（jupyter、browser 等）返回 404 |
| `BrowserInstanceConfig` | `key` / `driver_mode` / `cdp_url` 远程模式 | `build_browser_instance` 返回按 owner+session 区分的 dict | Playwright MCP 进程仍运行在 agent-core 主机 |

## 2. 数据结构与事实归属

| 对象 | 含义与作用域 | 事实权威 | 修改入口 |
| --- | --- | --- | --- |
| `FirecrackerLauncherConfig` | 每个 `SysOperation` 的启动配置（manager 地址、owner、服务 token、VM profile） | agent-core 配置 | 构造时传入；推荐经 `build_firecracker_gateway_config` |
| 会话记录（`SessionRecord`） | session_id、owner、状态、VM 槽位、端口、guest token | FcSessionManager 的 SQLite（`state.db`） | 仅 FcSessionManager 的生命周期方法 |
| launcher handle 表 | sandbox_id → (manager_url, owner, token) | 进程内存（`FirecrackerLauncher._handles`） | `launch()` 写入，`delete()` 移除 |

关键字段：`state ∈ {CREATING, RUNNING, PAUSED, DESTROYED, FAULTED}`；`base_url = http://<advertise_host>:<agent_port>/t/<guest_token>`；`cdp_url = http://<advertise_host>:<cdp_port>/t/<guest_token>`。端口在会话生命周期内固定。

## 3. 不变量

1. **I-1 归属校验：** FcSessionManager 对每个会话接口（创建已存在会话、查询、暂停、恢复、删除、debug）校验 `X-Owner-User-Id` 等于会话 owner，不符返回 403，且不改变会话状态。列表只返回调用方自己的会话。
2. **I-2 隔离 key 含 owner：** `build_firecracker_gateway_config` 把 owner 写入隔离前缀，不同用户即使提交相同 session_id 也不会共用 Gateway 记录或缓存的 provider。每个用户注册一个 `SysOperation`（资源管理器按隔离 key 模板去重）；sandbox 操作按每次调用解析出的隔离 key 分别缓存 gateway client，因此同一用户的不同会话各自路由到自己的 VM（`core/sys_operation/sandbox/sandbox_mixin.py`）。
3. **I-3 一会话一 VM：** 同一 session_id 至多对应一台非 DESTROYED 的 VM；同 owner 重复 `POST /v1/sessions` 幂等返回现有会话（PAUSED 时先恢复）。
4. **I-4 进程隔离：** 每台 VM 经 jailer 启动：chroot、独立 uid（`uid_base + 槽位`）、cgroup v2（`/fc/<vm_id>`，`cpu.max`、`memory.max`）、独立网络命名空间；Firecracker 内置 seccomp 必须处于启用状态，禁止传 `--no-seccomp`。
5. **I-5 网络边界：** guest 只能经主机 NAT 访问公网地址；主机、私网（10/8、172.16/12、192.168/16、100.64/10）、链路本地/元数据、组播与回环一律丢弃，VM 无法访问 manager 容器本身。MMDS 由 Firecracker 设备层拦截，不受此规则影响。
6. **I-6 guest API 鉴权：** guest agent 与 CDP 桥只接受以 `/t/<guest_token>/` 开头的请求（`/healthz` 除外），否则 403；guest token 只经 MMDS V2 下发。
7. **I-7 删除即清理：** 删除会话会终止 VM 进程，关闭代理监听及其连接，删除网络命名空间、cgroup 与 chroot 目录，记录置为 DESTROYED。

## 4. 接口契约

### 4.1 共通规则

- 所有 REST 调用需 `Authorization: Bearer <FCM_API_TOKEN>`（`/healthz` 除外），缺失或错误返回 401。
- 会话接口需 `X-Owner-User-Id`；缺失返回 400。
- 错误体为 `{"error": "<原因>"}`；状态码：400 参数错误，403 归属不符，404 不存在，409 状态冲突，503 无可用槽位或 guest 未就绪，500 内部错误（不回传堆栈）。
- agent-core 侧所有失败抛出 `StatusCode.SYS_OPERATION_SANDBOX_LAUNCHER_ERROR`，`details={"launcher": "firecracker", "http_status": <状态码或 None>}`；网络异常以 `cause` 附带原始异常。`is_ownership_error(exc)` 判断 403。

### 4.2 FcSessionManager REST API

| 方法 / 路径 | 成功响应 | 说明 |
| --- | --- | --- |
| `POST /v1/sessions` `{session_id, owner_user_id?, profile?}` | 201 新建 / 200 已存在 | `owner_user_id` 若提供必须等于请求头；`profile.vcpus ∈ [1, FCM_MAX_VCPUS]`，`profile.mem_mib ∈ [256, FCM_MAX_MEM_MIB]` |
| `GET /v1/sessions[?owner=]` | 200 `{sessions:[...]}` | `owner` 与请求头不同返回 403 |
| `GET /v1/sessions/{id}` | 200 会话描述 | |
| `POST /v1/sessions/{id}:pause` | 200 | RUNNING → PAUSED；已 PAUSED 幂等；其他状态 409 |
| `POST /v1/sessions/{id}:resume` | 200 | PAUSED → RUNNING；已 RUNNING 幂等；其他状态 409 |
| `DELETE /v1/sessions/{id}` | 200 `{deleted: true}` | 见 I-7 |
| `GET /v1/sessions/{id}/debug` | 200 | 返回 pid、`root_entries`（VM 进程看到的根目录内容，即 chroot）、`exe`、uid、cgroup、mount / net 命名空间 inode（及 manager 自身的对照值）、seccomp，用于核验 I-4 |

### 4.3 FirecrackerLauncher

- `launch(config, timeout_seconds, isolation_key)`：以当前上下文的 session_id（`get_session_id()`；缺省值时退回清洗后的 isolation_key）调用 `POST /v1/sessions`，返回 `LaunchedSandbox(base_url, sandbox_id=session_id)` 并登记 handle。非 `FirecrackerLauncherConfig` 立即报错。
- `pause` / `resume`：调用对应接口；无 handle 时报错（`http_status=404`）。
- `delete`：无 handle 为空操作；manager 返回 404 视为已删除。
- `check_status`：RUNNING→`RUNNING`；PAUSED、PERSISTED→`PAUSED`；其余及 404→`KILLED`。
- `build_browser_instance(config, session_id)`：确保会话存在，返回 `{"key": "<owner>--<session>", "driver_mode": "remote", "cdp_url": ...}`。

### 4.4 guest agent（vsock 8080，经代理暴露为 `base_url`）

实现 `POST v1/file/read|write|list|glob|upload`、`GET v1/file/download`、`POST v1/shell/exec`，请求与响应字段遵循 `agent_sandbox` SDK（`{success, message, data}`）。`shell/exec` 以 `/bin/bash -c` 执行，stdout 与 stderr 合并到 `output`，默认工作目录 `/workspace`。另提供 `GET fc/identity`（返回 MMDS 中的 session_id、owner）。

### 4.5 CDP 桥（vsock 9222，经代理暴露为 `cdp_url`）

首次请求时启动无头 Chromium（下载目录 `/workspace/downloads`）。`/t/<token>/json*` 以本地 Host 访问 Chromium，并把响应中的 websocket 地址改写为 `ws://<请求 Host>/t/<token>/...`；带 `Upgrade: websocket` 的 `/t/<token>/devtools/...` 请求原样双向转发。

## 5. 生命周期

```
CREATING ──boot ok──▶ RUNNING ◀──resume── PAUSED
    │                    │ └────pause────▶ │
    └──boot 失败──▶ FAULTED   └──delete──▶ DESTROYED ◀──delete──┘
```

FcSessionManager 启动时把遗留的 CREATING / RUNNING / PAUSED 记录清理并标为 FAULTED（第一阶段 VM 不跨容器重启存活）；FAULTED 会话再次 `POST` 时重建。

## 6. 限制

- 无 persist / snapshot；PAUSED 只保留内存。
- 暂停中的 VM 不会被入站请求唤醒，需先 `:resume` 或重新 `POST /v1/sessions`。
- 每会话复制一份完整的稀疏 rootfs（无共享只读基础盘 + overlay）。
- CDP 桥依赖 guest token 防护；Playwright MCP 进程运行在 agent-core 主机。
