---
id: F_20261126_0926_firecracker-session-sandbox-phase5-messaging
title: Firecracker 会话沙箱第五阶段与 VM 任务用户消息实施与决策归档
type: F
author: alexbhyeo
author_email: yeo.boon.hong@huawei.com
modules: [extensions/sys_operation]
files:
  - openjiuwen/extensions/sys_operation/sandbox/launchers/firecracker.py
  - openjiuwen/extensions/sys_operation/sandbox/launchers/__init__.py
  - deploy/firecracker/manager/fc_session_manager/tiers.py
  - deploy/firecracker/manager/fc_session_manager/egress.py
  - deploy/firecracker/manager/fc_session_manager/metrics.py
  - deploy/firecracker/manager/fc_session_manager/snapcodec.py
  - deploy/firecracker/manager/fc_session_manager/sessions.py
  - deploy/firecracker/manager/fc_session_manager/firecracker.py
  - deploy/firecracker/manager/fc_session_manager/network.py
  - deploy/firecracker/manager/fc_session_manager/store.py
  - deploy/firecracker/manager/fc_session_manager/config.py
  - deploy/firecracker/manager/fc_session_manager/api.py
  - deploy/firecracker/guest/fc_guest_agent.py
  - deploy/firecracker/guest/fc-msg
  - deploy/firecracker/docker/guest.Dockerfile
  - deploy/firecracker/docker/manager.Dockerfile
  - deploy/firecracker/scripts/agent_core_egress.sh
  - deploy/firecracker/scripts/run_manager.sh
  - tests/unit_tests/extensions/sys_operation/sandbox/test_firecracker_launcher.py
  - tests/system_tests/extensions/sys_operation/sandbox/fc_helpers.py
  - tests/system_tests/extensions/sys_operation/sandbox/test_firecracker_phase5_e2e.py
  - tests/system_tests/extensions/sys_operation/sandbox/test_firecracker_messages_e2e.py
designs: [D-20261124-0855-firecracker-session-manager]
design_note: VM 任务与用户之间的消息通道不在设计中，由用户在第五阶段实施期间提出（2026-11-26），方案经用户选择“Messages & questions”确认。
---

# Firecracker 会话沙箱第五阶段与 VM 任务用户消息实施与决策归档

## 关联依据

- 设计：D-20261124-0855-firecracker-session-manager（状态 draft；本次实现其第五阶段“生产化”）。
- spec：[S_01 Firecracker 会话沙箱规约](../specs/sys_operation/S_01_firecracker-session-sandbox.md)（本次修订）。
- 前序记录：[F_20261126_0726_firecracker-session-sandbox-phase2-4](F_20261126_0726_firecracker-session-sandbox-phase2-4.md)。
- 日期：2026-11-26。
- 实际实施范围：
  - 第五阶段：按用户分级（VM 规格上限、会话配额、磁盘 / 网卡限速、TTL、出站白名单）；内存 balloon 与基于统计的回收；按会话选择 CPU 模板；guest 出站审计；可选出站白名单代理；agent-core 仅访问 LLM 的出站限制脚本；冷快照压缩与加密；warm pool；指标与告警；安全审视。
  - 用户新增需求：DeepAgent / AgentTeam 在 VM 中创建的任务与会话用户双向通信（通知、提问并等待回答）。

## 决策归属与关系

新建记录，续接第二至第四阶段记录。消息通道是用户在本阶段提出的新需求，用户在“消息与提问 / 用户访问 VM 服务 / 经智能体对话转发”三种方案中选择了第一种。

## 上下文与跨边界影响

- 用户由 agent-core 以服务凭据加 `X-Owner-User-Id` 标识；分级必须由运维按用户配置，不能由调用方 profile 选择，否则可绕过限额。
- aio provider 把命令包装为 `{ CMD; }`，以 `&` 结尾的命令会成为语法错误而结果码仍为成功（既有行为，非本次引入）；后台任务需写成 `... & echo started` 等形式。
- 智能体只有在提示中得知 `fc-notify` / `fc-ask` 时才会使用；产品侧需要在系统提示中说明（e2e 用例在查询中加入一句说明）。

## 实际实现与开发决策

### 分级与限速（`tiers.py`）

- 内置 `standard`、`small`、`restricted` 三级，`FCM_TIERS` 可覆盖或新增，`FCM_USER_TIERS` 把用户映射到级别（均支持内联 JSON 或 `*_FILE`）。
- 级别规定默认与上限 vCPU / 内存、workspace 大小、可保留会话数（超出返回 429）、每块盘带宽与 IOPS、网卡双向带宽、TTL 与出站白名单；profile 只能在级别范围内取值，profile 的白名单只能是级别白名单的子集。
- 限速使用 Firecracker 令牌桶（100 ms 补充），在启动时写入盘与网卡配置；快照保存设备状态，恢复后限速仍生效。

### 内存 balloon

- 每台 VM 启动时配置 balloon（初始 0、`deflate_on_oom`、2 秒统计）。reaper 每 10 秒读取统计，使 guest 可用内存保持约四分之一（至少 256 MiB），差值超过 64 MiB 才调整；guest 内存紧张时由 deflate_on_oom 先行释放，下一轮再下调目标。拒绝方案：固定比例充气（无法随负载回落）。

### CPU 模板

- 与默认模板同目录的模板（T2、T2S、T2CL、C3）均可经 profile `cpu_template` 选择并记录在会话中；快照元数据记录会话模板，恢复要求模板一致且本机提供该模板。跨主机迁移未验证。

### 出站审计与白名单（`egress.py`、`network.py`）

- 审计：跟随 manager 网络命名空间中的 conntrack `NEW` 事件；guest 流量在 VM 命名空间内被伪装为 `10.200.<槽位>.2`，据此映射到会话，写入 `audit/egress-YYYYMMDD.jsonl` 与每会话内存环形缓冲，`GET /v1/sessions/{id}/egress` 查询。拒绝方案：iptables LOG（非初始网络命名空间默认不输出内核日志）。
- 白名单：受限会话的直接出站在 FC-EGRESS 中丢弃，只放行到其 veth 地址上代理端口的连接；VM 命名空间把 `172.16.0.1:3128` DNAT 到代理，guest 通过 MMDS 的 `egress_proxy` 为 shell 命令设置代理环境变量。代理只接受 80 / 443 端口、白名单内且解析为公网地址的域名，并记录允许与拒绝。
- tap0 使用固定 MAC：恢复或 fork 到新的命名空间后，guest 的网关 ARP 表项仍有效（e2e 首轮发现恢复后经代理访问失败即此原因）。

### agent-core 出站限制（`scripts/agent_core_egress.sh`）

- 在 agent-core 容器内（需 `NET_ADMIN`）只放行回环、已建立连接、resolv.conf 中的 DNS、manager 的全部 TCP 端口与指定 LLM 主机端口，其余拒绝。主机名在执行时解析一次，地址变化需重新执行。

### 冷快照压缩与加密（`snapcodec.py`）

- PERSISTED 超过 `snapshot_seal_after_s`（默认 600 秒，可按会话设置）后，`mem` 与 `vm.state` 经 `zstd` 压缩、`openssl enc -aes-256-ctr -pbkdf2` 加密为 `*.sealed`；密钥文件默认在 `data_dir/keys/snapshot.key`（0600，可用 `FCM_SNAPSHOT_KEY_FILE` 指定外部文件），数据目录 0700。恢复或 fork 前解封并以 `fallocate --dig-holes` 恢复稀疏。快照内存包含 guest 读入内存的凭据，因此冷快照不以明文留存。

### warm pool

- `FCM_WARM_POOL_SIZE` 台默认规格 VM 预先启动（owner `__pool__`，不分配端口，不参与闲置策略）。创建请求的规格与默认一致且级别无限速、无白名单时直接领用：重命名会话目录、分配端口、经 MMDS 下发新身份与 token 并确认 guest 采用；失败时回退为正常启动。
- 状态库改为 WAL + `synchronous=NORMAL`：池补充时的大量磁盘复制曾使每次提交的 fsync 阻塞秒级，导致领用耗时 2.7 秒；修改后领用 0.01–0.04 秒。

### 指标与告警（`metrics.py`）

- `GET /metrics`（Prometheus 文本，需服务凭据）：计数（创建来源、失败、balloon 调整、快照封存、回复）、耗时分位（创建、恢复、persist、fork、封存 / 解封）与状态量（各状态会话数、槽位、warm pool、主机内存、审计事件数）。
- `GET /v1/alerts`：槽位紧张、主机内存或数据盘不足、创建 P95 超过 `FCM_ALERT_CREATE_P95_S`、10 分钟内的启动 / 恢复 / persist 失败、warm pool 为空。

### VM 任务与用户的消息

- guest：`fc-notify TEXT` 发送通知，`fc-ask TEXT` 发出问题并阻塞到用户回答（打印答案，超时退出码 2）；二者访问 guest agent 仅监听 `127.0.0.1:8090` 的本地端点。消息保存在 guest 内存的环形队列中（最多 1000 条），因此等待中的问题随内存快照跨越 pause / persist。
- manager：`GET /v1/sessions/{id}/messages?after=&wait=` 经 vsock 长轮询 guest，并在 manager 内缓存；pause / persist 前先拉取一次，因此 VM 冻结或持久化时用户仍能看到问题；`POST .../messages/{mid}/reply` 先唤醒 VM 再投递答案；均做归属校验。问题只能回答一次（409）。
- agent-core：`get_session_messages(config, session_id, after, wait_seconds)` 与 `reply_to_session_message(config, session_id, message_id, text)`。
- 拒绝方案：经智能体对话转发（需要改动 DeepAgent / AgentTeam 的中断与流事件，且智能体结束后的后台任务无法提问）；manager 常驻跟随每个会话（无请求时的开销）。

## 安全审视

| 项 | 结论 |
| --- | --- |
| 调用方身份 | REST 全部接口需服务凭据；会话接口按 owner 校验（含 egress、messages、reply、fork、workspace） |
| 用户分级 | 只由运维配置；profile 无法扩大规格、配额或白名单（400 / 429） |
| 进程与网络隔离 | 沿用 I-4 / I-5；受限会话只能经白名单代理出站，代理拒绝解析到非公网地址的域名 |
| VM 内消息端点 | 只监听 guest 回环地址，不经 vsock 暴露；manager 读取 / 回复需 guest token |
| 快照与密钥 | 冷快照压缩加密；密钥 0600、数据目录 0700；密钥与数据同卷时只防御快照文件被单独带走，生产应使用外部密钥文件 |
| 审计 | 每个新出站连接与代理决定均写入审计日志 |
| 已知风险 | agent-core 出站脚本按解析时地址放行；warm pool VM 的启动熵在领用时经 MMDS 种子重新播种 |

## 与设计的偏差

| 设计 | 实际 | 原因 / 影响 |
| --- | --- | --- |
| 出站审计经 NFLOG / 日志 | conntrack 事件 | 容器网络命名空间中 LOG 不输出 |
| 快照压缩加密在 persist 时完成 | 冷快照延迟封存 | 避免唤醒路径增加解封时间；`snapshot_seal_after_s` 可设为 0 |
| 设计未包含 VM 任务消息 | 新增消息通道 | 用户新需求 |

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-26 单元测试 | 工作区未提交版本；`fc-e2e-runner:dev` | `run_e2e.sh tests/unit_tests/extensions/sys_operation/sandbox/test_firecracker_launcher.py` | 25 passed | 无 |
| 2026-11-26 第五阶段 e2e | WSL2 Ubuntu 24.04（嵌套 KVM）；Firecracker v1.18.0；manager 以 `FCM_TIERS`、`FCM_USER_TIERS`、`FCM_WARM_POOL_SIZE=2` 启动 | `run_e2e.sh .../test_firecracker_phase5_e2e.py` | 9 passed | 首轮 2 项失败：恢复后 tap0 MAC 变化导致经代理访问失败；warm pool 领用 2.7 秒（SQLite fsync）。修复后通过 |
| 2026-11-26 消息 e2e（含 LLM） | 同上；DeepSeek `deepseek-v4-flash` | `run_e2e.sh .../test_firecracker_messages_e2e.py` | 5 passed | 首轮 3 个确定性用例失败为测试命令问题（`&` 结尾被 provider 包装成语法错误、未闭合引号），修正后通过 |
| 2026-11-26 全量回归（第一至第五阶段 + 消息 + 单元） | 同上（最终构建） | `run_e2e.sh <单元测试> tests/system_tests/extensions/sys_operation/sandbox/` | 74 passed（10:49） | 前两轮失败已修复：封存中途被观测（改为原子写入并等待元数据）；封存压缩占满 CPU 使 warm pool 领用 2.09 秒（改为 `nice` + `zstd -T2`）；浏览器 context processor 被根 conftest 在测试间移除（在沙箱 conftest 收集期导入） |

用例与验收项：

| 用例 | 验收项 |
| --- | --- |
| `test_tier_rate_limits_keep_a_noisy_neighbour_in_check` | 20 MiB/s 限速的 VM 并行直写不超过 25 MiB/s，同时邻居 VM 的速率超过其两倍；配额 2 个会话，第三个 429；超出级别的 vCPU 400 |
| `test_balloon_returns_idle_memory_and_gives_it_back_before_oom` | 闲置 guest 的 balloon 充气 ≥ 256 MiB；随后分配并写满 700 MiB 成功，guest 无 OOM kill |
| `test_session_chooses_a_cpu_template_that_survives_persist` | 未知模板 400；T2S 生效（无 AVX-512），persist 后快照记录 T2S 并可恢复 |
| `test_guest_connections_are_audited` | guest 访问 443 被记录在会话审计与审计文件中；他人查询 403 |
| `test_restricted_tier_reaches_only_allowlisted_domains` | profile 不能扩大白名单；pypi.org 200、pip install 成功；example.com 被拒、直连被丢弃；审计含允许与拒绝；persist / 恢复后限制不变 |
| `test_cold_snapshots_are_compressed_and_encrypted` | 明文快照含 guest 内存标记（验证有效性）；封存后只剩 `*.sealed`、AES-256-CTR、体积小于一半、检索不到标记；密钥 0600、数据目录 0700；唤醒后标记仍可读 |
| `test_warm_pool_makes_creates_take_under_a_second` | 5 次创建 P95 < 1 秒；每个会话 MMDS 身份正确、boot_id 互不相同 |
| `test_metrics_and_alerts_are_exposed_to_the_operator` | 指标包含分位、槽位、主机内存、会话状态；无凭据 401；告警结构正确 |
| `test_agent_core_container_reaches_only_the_llm_and_the_manager` | 执行脚本后 LLM 可达、manager 200、其他公网不可达 |
| `test_background_task_notifies_and_asks_the_user` | 任务通知与提问到达用户，回答后任务继续并写入文件；重复回答被拒；无人回答时超时退出码 2 |
| `test_pending_question_survives_persist_and_the_reply_wakes_the_vm` | 问题在 PERSISTED 期间仍可见，回答唤醒 VM 并送达等待中的任务 |
| `test_each_user_only_sees_and_answers_their_own_tasks` | 两个用户只看到各自任务的消息；bob 读取或回答 alice 的问题均 403，问题保持 pending |
| `test_task_started_by_a_deep_agent_asks_the_user`（LLM） | DeepAgent 启动的后台任务向用户提问，用户回答后任务完成 |
| `test_task_started_by_an_agent_team_member_asks_the_user`（LLM） | AgentTeam 成员启动的任务向用户提问并完成；团队仍只有一台 VM |

### 可见行为、副作用与证据有效范围

- 观测数据（manager 日志）：warm pool 领用 0.01–0.04 秒，补充一台约 5–10 秒（以磁盘复制为主）；1536 MiB VM 闲置时 balloon 0 → 986 MiB。
- 证据对应当前工作区（尚未提交），单机 WSL2 + Docker Desktop。限速、balloon 与 warm pool 的数值依赖主机磁盘与内存，换主机需重验。

## 验证结论

- 已证明范围：第五阶段约定的验收项（噪声邻居 I/O 隔离、balloon 不致 OOM、安全审视）及消息通道（含 DeepAgent、AgentTeam 真实联调）在真实 Firecracker 环境中通过。
- 未覆盖或未通过的承诺：跨代 CPU 模板的跨主机迁移、长时间压力测试、外部密钥服务。
- 可支持的交付判断：满足第五阶段与消息需求的验收范围；设计状态仍为 draft。

### 覆盖与资料同步

- 已更新：spec S_01、`deploy/firecracker/README.md`。
- 未同步：设计文档未回填；仓库 `docs/` 未新增章节；DeepAgent 系统提示未内置 `fc-notify` / `fc-ask` 说明（由产品侧提示决定）。

## 已知遗留

| 遗留项 | 性质 | 影响与现有处置 | 后续入口 |
| --- | --- | --- | --- |
| aio provider 把以 `&` 结尾的命令包装成语法错误且结果码为成功 | 既有问题（非本次引入） | 后台命令需另行结尾；智能体可能误以为已启动 | `extensions/sys_operation/sandbox/providers/aio.py` 维护方 |
| 智能体需经提示得知消息命令 | 有意取舍 | 未在提示中说明时任务不会主动联系用户 | 产品系统提示或后续 prompt section |
| manager 侧消息缓冲只在内存 | 有意取舍 | manager 重启后缓冲清空，但 guest 内存中的消息在唤醒后仍可读取 | 按需持久化 |
| agent-core 出站脚本按解析时地址放行 | 有意取舍 | LLM 地址变化需重新执行 | 前置 HTTPS 代理 |
| 密钥默认与数据同卷 | 有意取舍 | 只防止快照文件被单独带走 | 生产使用外部密钥文件 / KMS |
| warm pool 只服务默认规格且无限速的级别 | 有意取舍 | 其他规格仍需完整启动 | 按级别维护多个池 |
