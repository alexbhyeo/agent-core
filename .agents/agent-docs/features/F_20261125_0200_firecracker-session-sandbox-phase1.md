---
id: F_20261125_0200_firecracker-session-sandbox-phase1
title: Firecracker 会话沙箱第一阶段实施与决策归档
type: F
author: alexbhyeo
author_email: yeo.boon.hong@huawei.com
modules: [core/sys_operation, core/runner, core/common, extensions/sys_operation]
files:
  - openjiuwen/extensions/sys_operation/sandbox/launchers/firecracker.py
  - openjiuwen/extensions/sys_operation/sandbox/launchers/__init__.py
  - openjiuwen/core/sys_operation/sandbox/sandbox_mixin.py
  - openjiuwen/core/runner/runner.py
  - openjiuwen/core/common/logging/utils.py
  - deploy/firecracker/manager/fc_session_manager/api.py
  - deploy/firecracker/manager/fc_session_manager/sessions.py
  - deploy/firecracker/manager/fc_session_manager/network.py
  - deploy/firecracker/manager/fc_session_manager/firecracker.py
  - deploy/firecracker/manager/fc_session_manager/proxy.py
  - deploy/firecracker/guest/fc_guest_agent.py
  - deploy/firecracker/guest/fc-init
  - examples/firecracker/deep_agent_firecracker.py
  - tests/unit_tests/extensions/sys_operation/sandbox/test_firecracker_launcher.py
  - tests/unit_tests/core/sys_operation/sandbox/test_sandbox_mixin_session_keys.py
  - tests/unit_tests/core/runner/test_runner_session_id_scope.py
  - tests/system_tests/extensions/sys_operation/sandbox/test_firecracker_e2e.py
designs: [D-20261124-0855-firecracker-session-manager]
design_note: null
---

# Firecracker 会话沙箱第一阶段实施与决策归档

## 关联依据

- 设计：D-20261124-0855-firecracker-session-manager（状态 draft；本次实现其“第一阶段：单机 MVP”）。
- spec：[S_01 Firecracker 会话沙箱规约](../specs/sys_operation/S_01_firecracker-session-sandbox.md)（新建）。
- 日期：2026-11-25。
- 实际实施范围：FcSessionManager（create / pause / resume / destroy、归属校验、jailer、每 VM netns + NAT + 私网屏蔽、vsock 代理、MMDS V2、熵设备、SQLite 状态）；guest agent（AIO 兼容 7 个接口、MMDS 身份、token 校验的 CDP 桥、无头 Chromium）；Ubuntu Docker 镜像与构建脚本；agent-core `FirecrackerLauncher` / `FirecrackerLauncherConfig` 及辅助函数；示例与测试。persist、snapshot、fork 等第二阶段及以后的能力未实现。

## 决策归属与关系

新建记录，无既有实施记录可续写。实现过程中修复了两处 agent-core 既有缺陷（见下），它们是多用户、多会话验收的前提，归档在本记录中，不另建 bugfix 记录。

## 上下文与跨边界影响

- 会话 id 的来源：sandbox 的 SESSION 作用域通过 `core/common/logging/utils.get_session_id()` 解析隔离 key。已证实：此前只有 `agent_teams` 会设置该 contextvar，`Runner.run_agent` 不设置，单智能体运行时所有会话都解析为 `default_trace_id`。
- 资源注册：`SysOperationManager` 按隔离 key 模板去重（每个用户一个 `SysOperation`），而 sandbox 操作实例此前只缓存首次调用解析出的 gateway client。两者叠加导致同一用户的多个会话无法各自路由到自己的 sandbox。
- 浏览器：browser subagent 通过 agent-core 主机上的 Playwright MCP 驱动浏览器，未设置 `browser_key` 时进程内共享一个浏览器并按任务排队（`service_registry.task_turn`）。

## 实际实现与开发决策

- **launcher 设计**：`SandboxGateway` 以无参方式构造 launcher，`pause/resume/delete/check_status` 只拿到 sandbox_id，因此 launcher 在 `launch()` 时把 manager 地址、owner 与服务 token 记入进程内 handle 表。sandbox_id 等于 agent session_id。
- **归属双重校验**：manager 端按 `X-Owner-User-Id` 校验每个会话接口；agent-core 端 `build_firecracker_gateway_config` 把 owner 写入隔离前缀，使不同用户即使提交同一 session_id 也不会共用 Gateway 记录或缓存 provider，最终由 manager 返回 403。
- **guest token 放在 URL 路径**：aio provider 构造 `Sandbox(base_url=...)` 时不传额外请求头；SDK 保留 base_url 的路径前缀，因此把 token 放在 `/t/<token>` 中，provider 无需修改。CDP 桥对 `/json*` 以本地 Host 访问 Chromium（Chromium 拒绝非本地 Host），并把返回的 websocket 地址改写回 `ws://<请求 Host>/t/<token>/...`。
- **错误模型**：launcher 失败统一用 `StatusCode.SYS_OPERATION_SANDBOX_LAUNCHER_ERROR`，HTTP 状态放在 `details["http_status"]`，提供 `is_ownership_error` / `manager_http_status` 判断，不新增 StatusCode。
- **guest shell 执行**：以 shell 退出为完成条件，输出读取线程在退出后有 1 秒宽限，避免命令启动的后台进程持有管道导致请求永久挂起。
- **rootfs 缓存**：manager 启动时把基础 rootfs 复制到数据卷，会话盘从同一文件系统复制；直接读取 Docker Desktop 跨发行版的绑定挂载时，单次复制约 58 秒。
- **agent-core 修复 1**（`sandbox_mixin.py`）：gateway client 按每次调用解析出的隔离 key 分别缓存。
- **agent-core 修复 2**（`runner.py`、`logging/utils.py`）：`Runner.run_agent` / `run_agent_streaming` 在运行期间用 `bind_session_id` 绑定会话 id，结束后用 `unbind_session_id` 恢复。用户确认采用此方案（2026-11-25），副作用为单智能体日志显示真实会话 id，且所有 SESSION 作用域 sandbox 真正按会话隔离。

## 与设计的偏差

| 设计 | 实际 | 原因 / 影响 |
| --- | --- | --- |
| 共享只读 rootfs + 每会话 overlay 盘 | 每会话一份稀疏 rootfs 完整副本 | 第一阶段简化；并发创建时磁盘复制是主要耗时（见证据），第五阶段前改为共享基础盘 |
| 每会话独立 workspace.ext4 | workspace 位于会话 rootfs 内 | 同上；第四阶段热替换 workspace 盘时需要拆出 |
| 每用户一个 `SysOperation` 未在设计中写明 | 采用每用户一个 `SysOperation`，会话靠 session id 路由 | 资源管理器按模板去重的既有规则 |
| Playwright MCP 位置未指明 | 仍运行在 agent-core 主机，仅 Chromium 位于 VM | 复用既有 browser subagent；设计“Guest 镜像与浏览器”一节已注明 |

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-25 单元测试（launcher、mixin、runner、logging、core sandbox） | 工作区未提交版本；`fc-e2e-runner:dev`（Python 3.11，uv.lock，sandbox + observability extra） | `run_e2e.sh tests/unit_tests/core/runner/ tests/unit_tests/core/common/log/ tests/unit_tests/core/sys_operation/sandbox/ tests/unit_tests/extensions/sys_operation/sandbox/test_firecracker_launcher.py` | 717 passed，2 skipped | 无 |
| 2026-11-25 回归：single_agent、session、agent、sys_operation | 同上 | `run_e2e.sh tests/unit_tests/core/single_agent/ tests/unit_tests/core/session/ tests/unit_tests/core/agent/ tests/unit_tests/core/sys_operation/` | 1027 passed，3 failed，1 skipped | 3 项失败在未修改的基线（`git archive HEAD`）上同样失败：`test_shell_ping_timeout`（镜像无 ping）、`test_execute_cmd_stream_continuous_output`、`test_local_and_aio_providers_coexist`，与本次改动无关 |
| 2026-11-25 端到端（真实 Firecracker） | Windows 11 + WSL2 Ubuntu 24.04（内核 6.6，嵌套 KVM）；Docker Desktop 29.1.3；Firecracker / jailer v1.18.0；guest 内核 6.1.188；LLM：DeepSeek `deepseek-v4-flash` | `run_e2e.sh tests/system_tests/extensions/sys_operation/sandbox/test_firecracker_e2e.py` | 11 passed（9:25） | 见遗留 |

端到端用例与其覆盖的验收项：

| 用例 | 验收项 / 多用户场景 |
| --- | --- |
| `test_file_and_shell_round_trip` | 经 `launcher_type="firecracker"` 读写文件、列目录、执行 shell，stdout / stderr / 退出码正确 |
| `test_vm_process_runs_inside_the_jailer` | VM 进程的根目录只有 chroot 内容，独立 mount / net 命名空间，uid ≥ 10000，位于 `/fc/<vm_id>` cgroup，seccomp=2 |
| `test_guest_reads_its_session_id_from_mmds` | guest 经 MMDS V2 读到自身 session_id；无 token 请求返回 401 |
| `test_guest_has_internet_but_not_host_or_private_ranges` | guest 内 pip install、git clone 成功；manager 容器、VM 链路主机端、TAP 网关、10/8、192.168/16、172.16/12 均不可达；公网 200 |
| `test_another_users_session_id_is_rejected` | bob 使用 alice 的 session_id：agent 侧调用失败（403）；查询 / 暂停 / 删除 / debug / 创建 / 按 owner 列表均 403；错误服务 token 401；alice 的数据不受影响 |
| `test_three_users_run_in_parallel_in_separate_vms` | 3 个用户 4 个会话（alice 2 个）并行：各自 VM（boot_id 互不相同）、各自 MMDS 身份、只见自身文件；列表只含本人会话 |
| `test_pause_freezes_and_resume_continues` | 暂停 3 秒期间后台计数器不前进，恢复后继续 |
| `test_destroy_removes_the_vm` | 删除后 404，列表不再包含 |
| `test_each_session_has_its_own_browser` | 两个会话的 Chromium 并行访问网页，cookie 互不可见；无 token 的 CDP 请求 403 |
| `test_example_deep_agent_works_in_its_vm`（LLM） | `examples/firecracker` 中 DeepAgent 在自身会话的 VM 中写文件并执行 shell，输出含 `firecracker ok`、`Linux`、`fc-session`；VM 以该会话 id 命名，文件经独立读取核实 |
| `test_two_users_browser_agents_run_in_parallel_with_separate_cookies`（LLM） | alice、bob 的 browser agent 并行运行（116 秒），各自 VM 浏览器中仅有本人 cookie |

### 可见行为、副作用与证据有效范围

- 观测数据（manager 日志）：单个会话就绪 4–8 秒（磁盘复制 2–5 秒、启动 2–6 秒）；4 个会话同时创建时约 31 秒，其中磁盘复制约 20 秒；负载较重时复制可达 62 秒。
- 证据对应当前工作区（尚未提交），环境为单机 WSL2 + Docker Desktop。Firecracker 版本、guest 内核、rootfs 内容、`Runner` 运行路径或 aio provider 变化时需重验对应用例。LLM 用例依赖外部模型的执行结果，属于真实联调而非确定性测试。

## 验证结论

- 已证明范围：第一阶段全部验收项（见上表）及多用户隔离场景在真实 Firecracker 环境中通过。
- 未覆盖或未通过的承诺：第二阶段起的 persist / 唤醒 / 快照未实现；未在裸金属 Linux 或云主机上验证；未做压力或长时间运行测试。
- 可支持的交付判断：满足第一阶段约定的验收范围；设计状态仍为 draft，尚未评审批准。

### 覆盖与资料同步

- 已更新：spec S_01（新建）、`deploy/firecracker/README.md`、`examples/firecracker/`。
- 未同步：设计文档未回填实施结果（按规则不回填）；仓库 `docs/` 用户文档未新增 Firecracker 章节。

## 已知遗留

| 遗留项 | 性质 | 影响与现有处置 | 后续入口 |
| --- | --- | --- | --- |
| 无 persist / snapshot | 范围外 | VM 只在内存中，manager 容器重启后会话标为 FAULTED | 设计第二阶段 |
| 暂停的 VM 不会被请求唤醒 | 范围外 | 需先 `:resume` 或重新 `POST /v1/sessions` | 设计第二阶段（代理唤醒 + Gateway 兜底） |
| 每会话完整复制 rootfs | 有意取舍 | 并发创建慢、磁盘占用高 | 共享只读基础盘 + overlay |
| launcher handle 表在进程内存 | 有意取舍 | agent-core 重启后需下一次 `launch()` 重新绑定 | 设计第二阶段的持久化 SandboxStore |
| 导入 harness 需要 `observability` extra | 既有问题（非本次引入） | 未安装时导入 harness 报 `opentelemetry.exporter` 缺失；测试镜像已加该 extra | 由 agent_evolving / observability 维护方评估 |
| 3 个既有单元测试在 Linux 容器中失败 | 既有问题 | 基线同样失败，见证据表 | 相应模块维护方 |
| manager 日志每行输出两次 | 已发现缺陷（外观） | 不影响功能 | 后续清理 logging 配置 |
