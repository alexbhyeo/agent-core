---
id: F_20261127_0456_firecracker-playground
title: Firecracker Playground 前后端实施与决策归档
type: F
author: alexbhyeo
author_email: yeo.boon.hong@huawei.com
modules: [deploy/firecracker]
files:
  - deploy/firecracker/playground/backend/fc_playground/__init__.py
  - deploy/firecracker/playground/backend/fc_playground/__main__.py
  - deploy/firecracker/playground/backend/fc_playground/config.py
  - deploy/firecracker/playground/backend/fc_playground/events.py
  - deploy/firecracker/playground/backend/fc_playground/runs.py
  - deploy/firecracker/playground/backend/fc_playground/server.py
  - deploy/firecracker/playground/backend/fc_playground/store.py
  - deploy/firecracker/playground/backend/fc_playground/vm.py
  - deploy/firecracker/playground/backend/Dockerfile
  - deploy/firecracker/playground/backend/run_backend.sh
  - deploy/firecracker/playground/frontend/index.html
  - deploy/firecracker/playground/frontend/app.js
  - deploy/firecracker/playground/frontend/style.css
  - deploy/firecracker/playground/frontend/config.js
  - deploy/firecracker/playground/frontend/config.template.js
  - deploy/firecracker/playground/frontend/40-playground-config.sh
  - deploy/firecracker/playground/frontend/nginx.conf
  - deploy/firecracker/playground/frontend/Dockerfile
  - deploy/firecracker/playground/frontend/run_frontend.sh
  - deploy/firecracker/docker/e2e-runner.Dockerfile
  - deploy/firecracker/README.md
  - tests/system_tests/extensions/sys_operation/sandbox/test_firecracker_playground_e2e.py
designs: [D-20261127-0315-firecracker-playground]
design_note: null
---

# Firecracker Playground 前后端实施与决策归档

## 关联依据

- 设计：[D-20261127-0315-firecracker-playground](../designs/active/D-20261127-0315-firecracker-playground/design.md)（2026-11-27 用户批准，Q-1 / Q-2 采用建议方案；状态 approved，阶段 active）。
- 相关 spec：S_01_firecracker-session-sandbox（Playground 只消费其契约，不改变契约）。
- 日期：2026-11-27。
- 实际实施范围：设计 §6 的 I-01 至 I-08 全部实施：后端包 `fc_playground`（API、事件流、运行、manager 访问、SQLite）、前端静态站点（单页 UI、fetch-SSE、设置对话框）、两个独立镜像与启动脚本、e2e runner 增加本地 Chromium、README 与本记录。未修改 `openjiuwen/` 下任何代码。

## 决策归属与关系

新建记录，对应已批准设计；不替代既有记录。

## 实际实现与开发决策

- **前后端分离（D-08）**：`playground/backend/` 只提供 `/api/v1/*` 与 `/healthz`（`GET /` 为 404）；`playground/frontend/` 为 nginx 静态镜像，`config.js` 在容器启动时由 `PLAYGROUND_API_BASE_URL` 生成，浏览器也可在设置对话框中覆盖后端地址并输入令牌（保存在 `localStorage`）。两者唯一耦合是 HTTP 契约。CORS 预检只匹配 `/api/*`。
- **事件流**：每用户一条 SSE；浏览器以 `fetch` 读取（原生 `EventSource` 不能带 `Authorization`），15 秒心跳，`Last-Event-ID` 补发。VM 消息由后端在用户有事件流时按会话长轮询 manager。
- **DeepAgent 会话延续**：探针证实，同一后端进程内复用同一 DeepAgent 实例并以相同 session id 调用 `Runner.run_agent_streaming` 时，续问保留上下文（假设 A-1 成立）；后端按会话缓存实例。
- **流式输出规整**：实际观察到的块为 `OutputSchema(llm_output)`（增量）、`OutputSchema(answer)`（终答）、`TraceSchema(tracer_agent)`（工具调用，含 inputs / outputs）、`context.usage` / `llm_usage`（忽略）；团队块带 `source_member`，团队事件 `team.*`。
- **团队成员必须落在会话 VM（实施中发现的缺陷）**：首版团队会话的成员工具实际在后端容器内本地执行（`/workspace/team-hello.txt` 出现在后端容器，VM 中没有）。根因：团队 `SysOperationSpec` 与该用户 DeepAgent 的 SysOperation 网关配置相同，资源管理器按隔离 key 模板去重而拒绝注册；`SysOperationSpec.resolve()` 返回空后，`harness/factory.py` 静默回退为 LOCAL sys_operation。处置：团队使用独立隔离前缀（`-team`，VM 仍按会话 id 选择）；后端在每次团队运行前自行注册并校验其为 SANDBOX，失败即拒绝运行（失败关闭）；DeepAgent 的 SysOperation 同样校验模式。e2e 增加回归断言：团队运行后后端容器内不存在成员写入的文件。
- **MCP 演示命令**：演示服务需要 `/opt/mcp` venv 中的 `mcp` 包，界面默认命令改为 `/opt/mcp/bin/python`。
- **后端镜像依赖**：AgentTeam 的 SQLite 存储需要 `sqlite` extra（`aiosqlite`），镜像使用 `--extra sandbox --extra observability --extra sqlite`。

### 用户试用后的修订（2026-11-27）

- **思考流折叠**：`llm_reasoning` 增量此前每个 token 一行；改为每个回答一个可折叠的 “Thinking…” 块，回答开始后收起为 “Thought for Ns”。
- **工具活动分组**：每个回答的工具调用收进一个默认收起的列表（摘要 “Used N steps · bash ×k …”）；调用与结果合并为一行（✓ / ✗、工具名、命令或路径预览），点击展开完整输入输出。
- **调用与结果按 invokeId 配对**：并行工具调用的结果乱序返回，按工具名配对会把输出挂到错误的调用下；后端在 `run.chunk` 中携带 `TraceSchema.payload.invokeId`（`data.invoke_id`），前端按 id 配对，旧会话无 id 时退回按名配对。
- **复用的 DeepAgent 在后续运行中工作目录错误**：DeepAgent 只在首次初始化时把 cwd 写入当前 asyncio 任务的 ContextVar；Playground 为保留对话复用实例，第二次起的运行在新任务中读不到 cwd，退回后端进程的 `os.getcwd()`（`/var/lib/fc-playground`），VM 中的 bash 因 `cd` 失败。Playground 在每次运行前将 cwd 设为 VM 的 `/workspace`，团队成员的 cwd / workspace 也设为 `/workspace`；agent-core 侧的根因修复见 [B_20261127_0742](../bugfix/harness/B_20261127_0742_reused-agent-cwd-fallback.md)。
- **步数上限**：`PLAYGROUND_MAX_ITERATIONS` 默认由 20 提高到 100（用户要求；新闻检索类问题在 20 步内用尽）。
- **前端缓存**：nginx 对 js / css / html 返回 `Cache-Control: no-cache`，重新部署后浏览器即可取到新版本。
- 验证：DeepAgent 用例增加第三问（`pwd` 必须为 `/workspace`、所有工具事件带 `invoke_id`）；DeepAgent、团队、界面、并行 / 批量、取消用例通过；以截图核对分组效果。

## 与设计的偏差

| 设计 | 实际 | 原因 / 影响 |
| --- | --- | --- |
| 团队 `SysOperationSpec` id `fc-team-<user>`，网关配置与用户相同 | id `pg-team-<user>`，独立隔离前缀，运行前由后端注册并校验 | 相同配置导致注册失败并静默回退 LOCAL（见上）；修正后成员工具在会话 VM 中执行 |
| `GET /sessions` 只列 Playground 创建的会话 | 另列该用户在 manager 中的其他 VM（`external`），可一键导入 | 便于查看与接管测试或其他工具创建的 VM |
| 设计未列 `PATCH /sessions/{id}`（改标题） | 增加 | 界面点击标题重命名 |

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-27 探针：输出块与会话延续 | 第五阶段 manager；DeepSeek `deepseek-v4-flash` | 临时测试（已删除） | 记录上述块类型；续问回答出上一轮写入的数字 | — |
| 2026-11-27 确定性 e2e | 后端、前端为两个容器（`fc-playground-backend:8800`、`fc-playground-frontend:8080`，不同来源）；`PLAYGROUND_DOCKER=1` | `run_e2e.sh .../test_firecracker_playground_e2e.py -k "separate or own_sessions or panel or quota or vm_task"` | 5 passed | 首轮 2 项失败：`GET /` 被 OPTIONS 通配路由匹配为 405（已限定到 `/api/*`）；MCP 演示命令解释器错误（已修正） |
| 2026-11-27 LLM 与重启 e2e | 同上 | `-k "remembers or parallel or team or cancelled or restart"` | 4 passed，团队 1 项失败后修复 | 团队失败依次为：缺 `aiosqlite`（加 `sqlite` extra）；LLM 改写文件路径（测试改用扁平路径）；成员工具本地执行（见上，已修复并加回归断言） |
| 2026-11-27 界面 e2e | runner 镜像内本地 Chromium（Playwright 1.55.0） | `-k ui_` | 1 passed | — |
| 2026-11-27 全量 Playground e2e（最终构建） | 同上 | `run_e2e.sh .../test_firecracker_playground_e2e.py` | 11 passed（5:58） | — |

用例与验收项：

| 用例 | 设计验收 |
| --- | --- |
| `test_frontend_and_backend_are_separate_and_guarded` | PG-V-01、PG-V-11：前端页面与 `config.js`、前端文件不含凭据；后端无页面；无令牌 / 错误令牌 401、非法用户名 400；白名单来源得到 CORS 许可、非白名单得不到；响应中 guest token 被替换、无 manager 凭据与 LLM key |
| `test_users_only_see_their_own_sessions_and_events` | PG-V-06：bob 对 alice 会话的读取、shell、文件、删除、暂停均 403；bob 的列表与事件流中没有 alice 的会话 |
| `test_session_panel_actions_work_end_to_end` | PG-V-08、PG-V-05：文件浏览与读取、密钥（只回显键名）、workspace 检查点与热替换、MCP 注册、出站审计、debug（seccomp=2）、暂停 / 恢复 / 持久化后 shell 唤醒、fork 后标题与文件 |
| `test_tier_quota_and_errors_are_reported` | PG-V-08、PG-V-10：noisy 用户第 3 个会话 429；非法 kind 400；不存在会话 404；空查询 400；批量查询逐项报错 |
| `test_vm_task_questions_reach_the_user_and_answers_reach_the_task` | PG-V-07：`vm.message` 问题经事件流到达，回答后任务写文件并通知 |
| `test_sessions_survive_a_manager_restart` | PG-V-05：经运维接口重启 manager，会话为 PERSISTED，下一次调用唤醒且文件仍在 |
| `test_deep_agent_answers_and_remembers_the_conversation`（LLM） | PG-V-02：事件顺序、工具调用与终答块、文件在 VM 中、续问保留上下文、标题取首问 |
| `test_parallel_and_batch_queries_stay_in_their_sessions`（LLM） | PG-V-03：alice 两个、bob 一个会话并行完成且结果各自正确；忙碌会话 409；批量查询两个会话均完成；事件流不串用户 |
| `test_agent_team_session_streams_member_output_in_one_vm`（LLM） | PG-V-04：输出带成员名；成员写的文件在会话 VM 中，后端容器内不存在 |
| `test_a_running_query_can_be_cancelled`（LLM） | PG-V-10：取消后 `run.finished cancelled` |
| `test_ui_switches_user_chats_and_answers_a_vm_question` | PG-V-09：Chromium 打开前端，设置后端地址与令牌，切换用户、新建会话、（有 LLM 时）收到流式回答、在会话面板启动 `fc-ask` 任务并在问题卡片中回答、持久化、打开运维视图，无控制台错误 |

### 可见行为、副作用与证据有效范围

- 证据对应当前工作区（尚未提交），单机 WSL2 + Docker Desktop，前后端在同一主机的不同容器（不同主机名 / 端口）中以跨源方式运行；跨物理主机部署未实测，但通信路径与跨源配置相同。

## 验证结论

- 已证明范围：设计 §8 的 PG-V-01 至 PG-V-11 均有对应用例通过（最终构建全量 11 passed）。
- 未覆盖：跨物理主机 + TLS 反向代理部署；长时间、多用户压力。
- 可支持的交付判断：满足设计验收范围。

### 覆盖与资料同步

- 已更新：`deploy/firecracker/README.md`（Playground 一节、测试表）。
- 未同步：S_01 无契约变化。

## 已知遗留

| 遗留项 | 性质 | 影响与现有处置 | 后续入口 |
| --- | --- | --- | --- |
| `SysOperationSpec.resolve()` 注册失败时 harness 静默回退 LOCAL sys_operation | 既有问题（agent-core，非本次引入），安全相关 | 要求沙箱的智能体可能在宿主进程内执行工具；Playground 已以失败关闭方式规避 | `openjiuwen/harness/factory.py` 与 `harness/schema/deep_agent_spec.py` 维护方：注册失败应报错而不是回退 |
| 用户切换不鉴权，共享令牌 | 有意取舍（设计） | 仅适合测试环境 | — |
| 后端重启后 DeepAgent 上下文不恢复 | 有意取舍（设计 A-2） | 对话记录仍可见 | — |
| LLM 可能改写任务中的路径等细节 | 外部行为 | 团队用例使用不易改写的扁平路径 | — |
