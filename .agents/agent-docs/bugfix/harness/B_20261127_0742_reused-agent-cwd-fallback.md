---
id: B_20261127_0742_reused-agent-cwd-fallback
title: 复用的 DeepAgent 在新 Task 中工作目录退回宿主 os.getcwd() 的修复
type: B
author: alexbhyeo
author_email: yeo.boon.hong@huawei.com
modules:
- openjiuwen/harness
- openjiuwen/core/sys_operation
files:
- openjiuwen/harness/deep_agent.py
- openjiuwen/core/sys_operation/cwd.py
- tests/unit_tests/harness/test_deep_agent_reused_cwd.py
designs: []
design_note: 缺陷修复，按用户在会话中的确认（2026-11-27，“fix the agent-core the same way as the sandbox fallback”）直接修复，未建立独立 D 文档。
---

# 复用的 DeepAgent 在新 Task 中工作目录退回宿主 os.getcwd() 的修复

## 现象与影响

Firecracker Playground 为保留对话为每个会话复用同一个 DeepAgent 实例。第二轮起，智能体在 VM 中执行的
bash 先 `cd /var/lib/fc-playground`（Playground 后端进程的工作目录）而失败，工具调用大量报错并耗尽步数上限
（见 [F_20261127_0456_firecracker-playground](../../features/F_20261127_0456_firecracker-playground.md)）。
任何复用 DeepAgent 实例、且每轮在新 asyncio Task 中运行的宿主都会遇到：本地模式下工具在错误的目录执行，
沙箱模式下使用了在沙箱中不存在或无意义的宿主路径。

## 关联设计与当前契约

- cwd 状态在 `core/sys_operation/cwd.py` 的 ContextVar 中，每个 Task 一份副本（`init_cwd` 新建状态）。
- 契约补充见 [S_02 DeepAgent 架构](../../specs/harness/S_02_deep-agent-architecture.md) 不变量 17；
  [S_09](../../specs/harness/S_09_workspace.md) 原本指向 S_02 的 cwd 语义，现由该不变量承接。

## 根因与修复决策

- 根因：`DeepAgent.ensure_initialized()` 只在首次初始化时调用 `init_cwd`。已初始化的实例在新 Task 中再次运行时
  走复用分支，只处理继承的 artifact 根，不绑定 cwd；`get_cwd()` 在没有绑定时退回 `os.getcwd()`。
- 修复：
  - 将首次初始化中的 cwd 绑定提取为 `DeepAgent._bind_task_cwd()`，规则不变（继承的 artifact 根优先；否则
    cwd 取 `DeepAgentConfig.cwd` 或 workspace 根，project_root 取配置或 cwd）。
  - 复用分支在当前 Task 尚未绑定 cwd 时调用同一方法；已由父智能体或宿主绑定的 cwd 保持不变（子智能体继续使用
    父 Task 传入的状态，宿主可预先指定目录）。
  - 新增 `core/sys_operation/cwd.is_cwd_initialized()`：判断当前上下文是否已绑定工作目录；`get_cwd()` 的
    惰性空状态不算绑定。为新增公共函数，未改变既有函数行为。
- 拒绝的方案：
  - 复用时无条件重新绑定：会覆盖父智能体或宿主为该 Task 指定的目录（例如团队成员的工作树）。
  - 只在 Playground 中规避：其他复用实例的宿主仍会出错（Playground 的每轮 `init_cwd` 保留为纵深防御）。

## 回归证据与适用范围

| 执行日期 / 范围 | 环境 | 命令 | 结果 |
| --- | --- | --- | --- |
| 2026-11-27 新增与相关单测 | `fc-e2e-runner:dev`（Python 3.11，uv.lock） | `run_e2e.sh tests/unit_tests/harness/test_deep_agent_reused_cwd.py tests/unit_tests/harness/test_deep_agent_workspace.py tests/unit_tests/harness/test_deep_agent.py -q` | 130 passed |
| 2026-11-27 修复有效性 | 同上；`deep_agent.py` 临时恢复为修复前版本 | `run_e2e.sh tests/unit_tests/harness/test_deep_agent_reused_cwd.py` | `test_reused_agent_binds_its_cwd_in_a_new_task` 失败、其余通过；恢复修复后通过 |
| 2026-11-27 harness / agent_teams / core sys_operation 单测回归 | 同上 | `run_e2e.sh tests/unit_tests/harness tests/unit_tests/agent_teams tests/unit_tests/core/sys_operation --ignore=.../agent_teams/cli -q` | 14230 passed，35 failed：29 项与 [B_20261127_0532](B_20261127_0532_sysop-spec-silent-local-fallback.md) 基线对照中修复前即失败的相同；3 项 core sys_operation 为第一阶段记录的基线失败；observability 导出为顺序相关偶发；2 项 browser_move 用例单独运行时修复前后均通过（64 passed），属整套运行负载下的时序偶发 |

新增用例：复用实例在新 Task 中 cwd / workspace 均为 workspace 根而非 `os.getcwd()`；Task 已绑定的 cwd 不被覆盖；
`is_cwd_initialized()` 不把惰性空状态当作已绑定。

## 防错模式与资料同步

- 模式：存放在 Task 局部 ContextVar 中、由一次性初始化写入的状态，在实例被复用到新 Task 时必须重新建立或显式继承，
  不得静默退回进程级默认值。已写入 harness S_02 不变量 17。

## 遗留与后续

- 无。
