---
id: F_20261126_0726_firecracker-session-sandbox-phase2-4
title: Firecracker 会话沙箱第二至第四阶段实施与决策归档
type: F
author: alexbhyeo
author_email: yeo.boon.hong@huawei.com
modules: [core/sys_operation, core/runner, extensions/sys_operation]
files:
  - openjiuwen/core/sys_operation/config.py
  - openjiuwen/core/sys_operation/sandbox/gateway/gateway.py
  - openjiuwen/core/sys_operation/sandbox/gateway/sandbox_store.py
  - openjiuwen/core/sys_operation/sandbox/launchers/base.py
  - openjiuwen/core/runner/runner.py
  - openjiuwen/extensions/sys_operation/sandbox/launchers/firecracker.py
  - openjiuwen/extensions/sys_operation/sandbox/launchers/__init__.py
  - deploy/firecracker/manager/fc_session_manager/api.py
  - deploy/firecracker/manager/fc_session_manager/config.py
  - deploy/firecracker/manager/fc_session_manager/firecracker.py
  - deploy/firecracker/manager/fc_session_manager/proxy.py
  - deploy/firecracker/manager/fc_session_manager/sessions.py
  - deploy/firecracker/manager/fc_session_manager/store.py
  - deploy/firecracker/guest/fc_guest_agent.py
  - deploy/firecracker/guest/fc-init
  - deploy/firecracker/guest/demo_mcp_server.py
  - deploy/firecracker/docker/guest.Dockerfile
  - deploy/firecracker/docker/manager.Dockerfile
  - deploy/firecracker/scripts/run_e2e.sh
  - deploy/firecracker/scripts/run_manager.sh
  - tests/unit_tests/core/sys_operation/sandbox/test_gateway_store_and_fallback.py
  - tests/unit_tests/core/runner/test_runner_release_sandboxes.py
  - tests/unit_tests/extensions/sys_operation/sandbox/test_firecracker_launcher.py
  - tests/system_tests/extensions/sys_operation/sandbox/conftest.py
  - tests/system_tests/extensions/sys_operation/sandbox/fc_helpers.py
  - tests/system_tests/extensions/sys_operation/sandbox/test_firecracker_e2e.py
  - tests/system_tests/extensions/sys_operation/sandbox/test_firecracker_phase2_e2e.py
  - tests/system_tests/extensions/sys_operation/sandbox/test_firecracker_phase3_e2e.py
  - tests/system_tests/extensions/sys_operation/sandbox/test_firecracker_phase4_e2e.py
designs: [D-20261124-0855-firecracker-session-manager]
design_note: null
---

# Firecracker 会话沙箱第二至第四阶段实施与决策归档

## 关联依据

- 设计：D-20261124-0855-firecracker-session-manager（状态 draft；本次实现其第二阶段“持久化与唤醒”、第三阶段“多用户与工具隔离”、第四阶段“fork、Diff 快照与盘热替换”）。
- spec：[S_01 Firecracker 会话沙箱规约](../specs/sys_operation/S_01_firecracker-session-sandbox.md)（本次修订）。
- 前序记录：[F_20261125_0200_firecracker-session-sandbox-phase1](F_20261125_0200_firecracker-session-sandbox-phase1.md)。
- 日期：2026-11-26。
- 实际实施范围：
  - 第二阶段：persist / restore（Full 快照）、PERSISTED 状态、代理唤醒、manager 关停时 persist、SQLite 状态前向迁移、`checkpoint_ref` 配对、默认 CPU 模板 T2；agent-core 的 `SqliteSandboxStore`、Gateway 连接失败兜底、launcher `restore_handle` 与 `pause_mode="snapshot"`。
  - 第三阶段：`Runner.release` 删除会话 VM、三段闲置策略（可按会话覆盖）、会话 VM 内的用户 MCP 服务、经 MMDS 的会话密钥、AgentTeam 共用一台 VM。
  - 第四阶段：fork、Diff 快照与合并、独立 workspace 盘及其检查点 / 热替换、fork 身份切换与 RNG 重新播种；agent-core 的 `fork_session`。
- 第五阶段（分级 profile、balloon、跨代 CPU 模板、出站审计与白名单、快照压缩加密、warm pool、指标告警）未实现。

## 决策归属与关系

新建记录，续接第一阶段记录；第一阶段记录的“已知遗留”中 persist、唤醒、handle 表持久化、workspace 独立盘四项由本记录关闭。

## 上下文与跨边界影响

- `SandboxGateway` 无参构造 launcher，进程重启后 handle 表为空；只有持久化 store 加上记录里的 launcher 元数据，才能在不重新创建 VM 的情况下重新挂接。
- `SysOperationManager` 按隔离 key 模板去重；AgentTeam 成员各自 `SysOperationSpec.resolve()`，相同 id 即复用同一实例，团队会话 id 经 `agent_teams` 的 `set_session_id` 进入隔离 key。
- 团队运行时在 `team.completed` 后会自行拆除并移除其 `SysOperation` 资源；VM 不受影响，由 `Runner.release` 负责回收。

## 实际实现与开发决策

### 第二阶段：持久化与唤醒

- **会话与 VM run 分离**：会话目录（磁盘、快照）与三个代理端口在会话生命周期内固定；槽位、netns、chroot、uid 属于一次 VM run，persist 时释放、恢复时重新分配（`vm_id` 每次不同）。
- **Full 快照 + 私有映射**：恢复使用 `mem_backend: File`，内存文件经 chroot 硬链接保持映射，会话目录中的该代快照在恢复成功后删除，避免同一代快照被恢复两次。
- **唤醒在代理层**：代理每次建立上游连接前解析 vsock 路径，会话非 RUNNING 且策略允许时先恢复；活动时间至多每秒写一次 store。
- **兼容性检查**：`meta.json` 记录 Firecracker 版本与 CPU 模板，不符时 409 且不触碰快照；默认 CPU 模板 T2（屏蔽 AVX-512 等），`FCM_CPU_TEMPLATE=none` 关闭。
- **Gateway 兜底**：只对连接类失败（标记串匹配或 aiohttp 连接异常）重试一次，先清缓存与 store 记录再 launch，避免把业务错误放大为重复执行。
- **manager 重启**：关停时 persist 运行中的会话（Docker `--stop-timeout 300`）；启动时遗留的 LIVE 状态标为 FAULTED，PERSISTED 会话重新监听端口。

### 第三阶段：多用户与工具隔离

- **`Runner.release(session_id)`**：只在已存在 Gateway 实例时调用 `release_session`，不因释放而创建 Gateway。
- **闲置策略**：reaper 每 `FCM_REAPER_INTERVAL_S` 扫描；正被锁定的会话跳过，锁内重新读取记录，避免与请求竞争。
- **MCP 在会话 VM 内**：用户的 stdio MCP 服务由 VM 内的 mcp-proxy 0.15.0（`--stateless`）以 streamable HTTP 提供，经 vsock 9300 与 token 校验桥接；agent-core 侧资源 id 为 `<owner>--<session>--<name>`，两个用户同名服务互不可见。拒绝方案：所有会话共享一个 MCP 进程（无法按用户隔离凭据与状态）。
- **密钥只经 MMDS**：密钥保存在 manager 内存，写入 MMDS 的 `secrets`；MMDS 数据不属于快照，恢复后由 manager 重新写入。拒绝方案：写入会话磁盘或 SQLite（会进入快照与备份）。
- **AgentTeam**：所有成员使用同一 `SysOperationSpec` id，团队会话一台 VM；`NativeHarness.dispose` 不释放共享 VM（与设计不同，见偏差表）。

### 第四阶段：fork、Diff 快照与热替换

- **fork**：运行中的源暂停，做一次 Full 快照并复制两块磁盘后立即恢复；各 fork 以 PERSISTED 记录登记（共享快照文件的硬链接，各自磁盘副本、端口、guest token），随后逐个恢复并调用 `fc/identity` 让 guest 采用新身份。每个 fork 的网络（netns 内相同的 tap / IP）、vsock 路径与盘路径都是 chroot 相对路径，因此不需要 `network_overrides` / `vsock_override`。
- **fork 身份与熵**：guest token 不匹配时 guest 至多每秒重读一次 MMDS；身份变化时把 MMDS 中的 `entropy_seed` 写入 `/dev/urandom`（VMGenID 之外的兜底）。
- **Diff 快照**：启动与恢复均开启 `track_dirty_pages`；`_Run.base_mem` 指向下一次 Diff 的基准（恢复来源或 fork 时的 Full 快照，Full 快照会重置脏页跟踪）。persist 先写 `mem.diff`，停止进程后用 `snapshot-editor edit-memory rebase` 合并进基准；基准被多处硬链接时先复制再合并。
- **快照目标先删除**：此前在已恢复的 VM 上再次做 Full 快照会以截断方式写入仍被映射的 `snap/mem`；现在所有快照目标在创建前先 unlink（spec I-9）。
- **workspace 独立盘**：每会话一块稀疏 ext4（`FCM_WORKSPACE_MIB`，默认 4096，按大小缓存模板），`fc-init` 挂载 `/dev/vdb` 到 `/workspace`。检查点：guest `fsfreeze` 后在主机复制；热替换：复制检查点到暂存文件 → guest 卸载（终止仍在使用的其他进程）→ 替换会话目录文件并重新链接到 chroot → `PATCH /drives/workspace` → guest 挂载。路径保持 `/workspace.ext4`，快照恢复无需改写盘路径。
- **guest agent 不驻留 `/workspace`**：agent 与 mcp-proxy 的工作目录改为 `/`，否则卸载时会被一并终止（e2e 首轮失败即此原因）。

## 与设计的偏差

| 设计 | 实际 | 原因 / 影响 |
| --- | --- | --- |
| fork 使用 reflink 复制磁盘 | `cp --sparse=always` 完整复制 | 数据卷为 ext4，不支持 reflink；3 个 fork 的磁盘复制约 25 秒 |
| fork 需要 `network_overrides` / `vsock_override` | 不需要 | 每个 VM run 有独立 netns 与 chroot，相对路径天然隔离 |
| VMGenID 负责 fork 后的熵 | VMGenID + MMDS 种子兜底 | 不依赖 guest 内核配置 |
| `NativeHarness.dispose` 释放团队 VM | 不释放，由 `Runner.release` 回收 | 成员各自 dispose 会提前销毁共享 VM |
| workspace 盘热替换用于挂载用户数据盘 | 以“workspace 检查点 + 热替换”形式提供 | 先满足“无需重启即可看到新内容”的验收；任意外部盘挂载留待后续 |

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-26 单元测试 | 工作区未提交版本；`fc-e2e-runner:dev` | `run_e2e.sh tests/unit_tests/extensions/sys_operation/sandbox/test_firecracker_launcher.py tests/unit_tests/core/sys_operation/sandbox/test_gateway_store_and_fallback.py tests/unit_tests/core/runner/test_runner_release_sandboxes.py` | 31 passed | 无 |
| 2026-11-26 第二阶段 e2e | Windows 11 + WSL2 Ubuntu 24.04（内核 6.6，嵌套 KVM）；Firecracker / jailer / snapshot-editor v1.18.0；guest 内核 6.1.188 | `run_e2e.sh .../test_firecracker_phase2_e2e.py` | 7 passed | 见遗留 |
| 2026-11-26 第三阶段 e2e（含 LLM） | 同上；LLM：DeepSeek `deepseek-v4-flash` | `run_e2e.sh .../test_firecracker_phase3_e2e.py` | 5 passed（团队用例 129 秒） | 团队用例首轮两次失败为测试自身问题：团队完成后其 `SysOperation` 资源被移除、`stop_agent_team` 在运行时已结束时返回 False；已改为经 spec `resolve()` 取实例、不断言返回值 |
| 2026-11-26 第一、二阶段回归（第三阶段构建） | 同上 | `run_e2e.sh .../test_firecracker_e2e.py .../test_firecracker_phase2_e2e.py` | 18 passed（10:29） | 无 |
| 2026-11-26 第四阶段 e2e | 同上（第四阶段镜像） | `run_e2e.sh .../test_firecracker_phase4_e2e.py` | 4 passed（2:00） | 首轮热替换用例失败（guest agent 工作目录在 `/workspace`，卸载时被终止），修复后通过 |
| 2026-11-26 第一至第三阶段回归（第四阶段构建） | 同上 | `run_e2e.sh .../test_firecracker_e2e.py .../phase2 .../phase3` | 22 passed，1 failed | 失败项为 `/workspace` 改为独立盘后出现 `lost+found`；已在 workspace 模板中删除，并由最终全量回归（第五阶段记录）验证 |

端到端用例与其覆盖的验收项：

| 用例 | 验收项 |
| --- | --- |
| `test_persisted_session_survives_manager_restart` | 2 GiB VM persist 后重启 manager 容器，会话仍 PERSISTED；唤醒后进程 PID 与内存中的值保持；快照为 Full、模板 T2 |
| `test_idle_session_is_persisted_then_woken_by_the_next_tool_call` | 闲置策略 persist 后，下一次工具调用经代理唤醒 |
| `test_gateway_fallback_restores_when_wake_on_request_is_off` | 关闭唤醒时由 Gateway 兜底重新 launch 并重试成功 |
| `test_browser_page_survives_persist_and_reconnect` | 浏览器页面在 persist / 恢复后仍在，CDP 重新连接 |
| `test_checkpoint_ref_is_paired_with_the_snapshot` | `expected_checkpoint_ref` 不符 409 且快照不动；相符时恢复 |
| `test_default_cpu_template_is_applied` | guest 中 AVX-512 被屏蔽 |
| `test_persistent_gateway_store_reattaches_after_agent_core_restart` | 新进程、新 Gateway 经 SQLite store 重新挂接原 VM，数据仍在 |
| `test_runner_release_destroys_the_session_vm` | `Runner.release` 后 VM 被删除 |
| `test_idle_policy_runs_pause_persist_destroy` | 按会话覆盖的 TTL 依次 PAUSED → PERSISTED → DESTROYED（14.1 秒） |
| `test_same_named_mcp_servers_of_two_users_run_in_their_own_vms` | alice、bob 同名 MCP 服务的 `whoami` 各自返回本人会话；资源 id 带 owner 与 session |
| `test_session_secrets_reach_the_guest_but_never_disk` | 密钥经 MMDS 可读，persist 恢复后仍在；state.db 与会话目录中检索不到；删除后 MMDS 中消失 |
| `test_three_member_team_shares_one_vm_and_leaves_nothing_behind`（LLM） | 3 个成员共用一台 VM 各写一个文件；切换会话得到新 VM 且看不到旧文件；`Runner.release` 后无 VM 残留 |
| `test_three_forks_share_the_past_but_not_the_future` | 3 个 fork 均保留 tmpfs 内容、后台进程与 workspace 文件；各自 MMDS 身份与 guest token；4 份 `/dev/urandom` 输出互不相同；之后写同一路径互不影响；源 token 访问 fork 403；bob fork alice 403 |
| `test_fork_of_a_persisted_session_keeps_the_source_snapshot` | fork PERSISTED 会话后源仍 PERSISTED，二者均可恢复 |
| `test_persist_after_restore_writes_a_diff_snapshot` | 首次 Full，恢复后两次 Diff；合并后的内存经两轮恢复仍完整 |
| `test_workspace_hot_swap_without_reboot` | `/workspace` 为 `/dev/vdb`；检查点 → 修改 → 热替换后内容回到检查点，`boot_id` 不变；替换后的盘经 persist / 恢复保持；越权 403、未知检查点 404 |

### 可见行为、副作用与证据有效范围

- 观测数据（manager 日志，1 GiB VM）：Full persist 2.4–4.9 秒、写入 1024 MiB；Diff persist 0.1–0.2 秒、写入 6.9–9.8 MiB；恢复 0.3–0.9 秒；3 个 fork 共 36.1 秒，其中源暂停期间的快照与磁盘复制 11.4 秒，每个 fork 恢复约 0.4 秒；workspace 检查点 0.1 秒、热替换 0.3 秒。第二阶段冒烟（1 GiB VM，第四阶段之前的 Full 路径）persist 约 2.2 秒、经唤醒恢复约 0.3 秒。
- 证据对应当前工作区（尚未提交），环境为单机 WSL2 + Docker Desktop。Firecracker 版本、guest 内核、CPU 模板或 rootfs 内容变化时需重验快照相关用例。LLM 用例依赖外部模型，属于真实联调而非确定性测试。

## 验证结论

- 已证明范围：第二、三、四阶段的验收项在真实 Firecracker 环境中通过，且第一、二阶段在第三阶段构建上回归通过。
- 未覆盖或未通过的承诺：未在裸金属 Linux 或云主机上验证；未做压力或长时间运行测试；跨主机迁移快照未验证。
- 可支持的交付判断：满足第二至第四阶段约定的验收范围；设计状态仍为 draft，尚未评审批准。

### 覆盖与资料同步

- 已更新：spec S_01、`deploy/firecracker/README.md`。
- 未同步：设计文档未回填实施结果（按规则不回填）；仓库 `docs/` 用户文档未新增 Firecracker 章节；`examples/firecracker/` 未增加 fork / MCP / 密钥示例。

## 已知遗留

| 遗留项 | 性质 | 影响与现有处置 | 后续入口 |
| --- | --- | --- | --- |
| 每会话完整复制 rootfs，fork 复制两块磁盘 | 有意取舍 | 创建与 fork 的主要耗时 | 共享只读基础盘 + overlay；支持 reflink 的数据卷 |
| 密钥只在 manager 内存 | 有意取舍 | manager 重启后需重新设置 | 外部密钥服务（范围外） |
| workspace 热替换终止使用 `/workspace` 的进程 | 有意取舍 | 正在运行的命令会被中断 | 调用方在空闲时替换 |
| fork 恢复失败时已成功的 fork 保留 | 有意取舍 | 返回错误，调用方可删除 | 后续按需改为整体回滚 |
| manager 日志每行输出两次 | 已发现缺陷（外观） | 不影响功能 | 后续清理 logging 配置 |
