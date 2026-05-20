# External Agent Access: skill+cli / mcp Wrappers

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-05-20 |
| 范围 | `openjiuwen/agent_teams/external/`、`openjiuwen/agent_teams/skill/`、`openjiuwen/agent_teams/mcp/`、`openjiuwen/agent_teams/__init__.py`、`openjiuwen/agent_teams/CLAUDE.md`、`pyproject.toml`、`tests/unit_tests/agent_teams/external/` |
| 测试基线 | `pytest tests/unit_tests/agent_teams/external/`：25 passed |
| Refs | `#751` |

## 背景

`agent_teams` 的协同能力（发消息、任务板、成员管理）此前只服务**进程内**成员：
每个成员本地跑一个 DeepAgent，由 coordination kernel 驱动。需要让**外部 agent**
（第三方 CLI claudecode / codex / openclaw / hermes，或独立运行的 agent 服务进程）
也能作为一等成员加入团队、直接调用协同工具。

底座本就具备跨进程能力：SQLite WAL + `TeamTaskManager` / `TeamMessageManager`（DAO），
zmq messager（ROUTER/DEALER + XPUB/XSUB，地址走 `MessagerTransportConfig`），事件
topic 统一为 `TeamTopic.{TEAM,TASK,MESSAGE}.build(session_id, team_name)`。

核心洞察：**在 DB + zmq 这一层，外部成员与进程内成员完全对称**——外部成员调
`claim_task` publish 的是同样的 `EventMessage` 到同样的 topic，进程内成员的
`TaskBoardHandler` / `MessageHandler` 照常被 nudge，分不出对面是谁。唯一的不对称只在
"最后一公里"：框架往本地 DeepAgent 里塞 vs 外部 agent 自己读。

## 与 F_07 bridge 的关系

**正交互补，不替代**。F_07 bridge = 本地完整 DeepAgent avatar + relay 纯文本给
**无工具**的远程执行者（本地 LLM 做调度）。本特性 = 外部 agent 自己**直接调用协同
工具**（直连 DB + zmq），是自主的一等成员。两种外部能力档次各管一类；不复用
`BRIDGE_AGENT` 角色，避免语义冲突。

## 设计

三层 + 两种部署形态。

### L0 ops 核心 `external/`

- `descriptor.py`：`TeamJoinDescriptor`（`session_id` / `team_name` / `member_name` /
  `role` / `language` / `db_config` / `transport_config`）+ `TEAM_JOIN_ENV`
  （`OPENJIUWEN_TEAM_JOIN`）。JSON 可序列化，团队拉起外部 agent 时注入单环境变量，
  或运维下发给独立服务。`db_config` 用 `DatabaseConfig | MemoryDatabaseConfig` 联合
  （与 `schema/team.py` 一致），跨进程用文件 sqlite，单进程 / 测试用 memory。
- `client.py`：`ExternalTeamClient` 按 descriptor `get_shared_db` + `create_messager`，
  复用 manager 暴露 `send_message`（`to="*"` 广播）/ `list_tasks` / `claimable_tasks` /
  `get_task` / `claim_task` / `complete_task` / `update_task` / `list_members` +
  `fetch_inbox`（poll，读未读 + 标记已读 + 任务板）/ `watch`（订阅 MESSAGE/TASK，
  事件即唤醒后回查 DB——规避自发事件过滤）。`connect()` 设 `set_language` +
  `set_session_id`，保证 publish 落到正确 topic。
- `format.py`：纯函数 `render_message` / `render_messages` / `render_task_board`，复用
  `i18n.t` 文案，使外部成员看到与进程内 dispatcher 一致的文本。

### L1 前端

- `skill/cli.py`（`team-member` 入口）+ `skill/SKILL.md`：argparse 非交互脚本式 CLI
  （inbox/send/broadcast/task/claim/complete/update/members）+ 协同协议教学文档。
  与 `cli/` 交互式 TUI 区别明确：后者给人，本 CLI 给外部 agent 脚本化调用，可用 `print`。
- `mcp/server.py`（`openjiuwen-team-mcp` 入口）：FastMCP **stdio** server，ops 暴露为
  MCP 工具，协同协议放进 server-level `instructions`（工具 schema 自描述，无需单独
  skill）。client 在 server 事件循环内**懒连接**（首个工具调用时，从 env 读 descriptor），
  使 messager/DB 资源绑定到使用它的 loop。仓库首个 MCP server。

### 部署形态

- **形态 A 独立服务自驱**：无本地 presence，外部进程用 `fetch_inbox`/`watch` 自读
  （follow_up 语义，无 mid-turn steer），用工具行动，生命周期在外部。
- **形态 B team 拉起 CLI**（P2，本次未实现）：team 复用 coordination kernel，
  `deliver_input` 的"最后一公里"换成写外部 CLI 的 stdin（侧信道注入，支持 mid-turn
  steer）；规划见下"已知遗留"。

## 拒绝 / 推迟的方案

- **把协同 ops 在 CLI/MCP 里重写一遍**：拒绝。复用 `TeamTaskManager` /
  `TeamMessageManager`，保证写路径与进程内成员对称、事件语义一致。
- **inbound 用 sender_id 自过滤**：拒绝。`watch` 改为"事件即唤醒、再回查 DB"——
  回查只返回发给本成员的未读消息 + 当前任务板，天然排除自发事件，更简单更稳。
- **把 inbound 格式化从 coordination handler 抽成共享纯函数供两侧复用**：本次只实现
  `external/format.py`（外部侧），未重构 `message.py` / `task_board.py`，避免在 P1
  动到工作中的协调层。后续可做去重。

## 已知遗留（P2）

- **形态 B 自动拉起 + 侧信道注入**：team 把第三方 CLI 拉成子进程、注入连接 descriptor +
  MCP 配置，并通过 **stdin 管道**做 mid-turn steer（用户选定 stdin 传输，Unix 优先、
  接口预留 PTY/Windows）。需要：`MemberRuntime` Protocol（从 `TeamHarness` 抽出运行期
  ~7 方法）+ `ExternalCliRuntime` + per-CLI adapter + 新 spawn 路径 +
  `agent_configurator.setup_agent` 按 runtime 分支。harness 是具体类、注入点在
  `setup_agent` 第 413 行 `TeamHarness.build()`、运行期被调方法少——复用可行。
- **handler 侧格式化去重**：见上。
- **create_task 等 leader-only ops**：当前外部 client 未暴露 create，按 teammate 默认
  能力集；leader 角色的外部成员如需 create/assign，后续按 role 扩展。
