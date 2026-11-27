---
id: B_20261127_0532_sysop-spec-silent-local-fallback
title: SysOperationSpec 解析失败时静默回退 LOCAL 的修复
type: B
author: alexbhyeo
author_email: yeo.boon.hong@huawei.com
modules:
- openjiuwen/harness
files:
- openjiuwen/harness/schema/deep_agent_spec.py
- tests/unit_tests/harness/test_sys_operation_spec_resolve.py
designs: []
design_note: 缺陷修复，按用户在会话中的确认（2026-11-27，“fix … so that a failed registration raises an error”）直接修复，未建立独立 D 文档。
---

# SysOperationSpec 解析失败时静默回退 LOCAL 的修复

## 现象与影响

Firecracker Playground 的 AgentTeam 会话中，成员的 `write_file` / `bash` 没有在会话的 Firecracker VM 中执行，
而是在宿主（Playground 后端容器）内执行：成员写入的 `/workspace/team-hello.txt` 出现在后端容器中，VM 里没有
（见 [F_20261127_0456_firecracker-playground](../../features/F_20261127_0456_firecracker-playground.md)）。
任何以 `SysOperationSpec(mode=SANDBOX, ...)` 声明沙箱、但注册失败的 DeepAgent / AgentTeam 成员都会这样在宿主上
执行工具，且没有任何错误，属于安全问题。

## 关联设计与当前契约

- `SysOperationSpec.resolve()`（`harness/schema/deep_agent_spec.py`）被 `DeepAgentSpec.build` 与成员装配调用。
- `create_deep_agent` 收到的 `sys_operation` 不是 `SysOperation` 时会新建 LOCAL sys_operation
  （`harness/factory.py`，"mint a LOCAL one"）——这是未提供 sys_operation 时的既有默认行为，本次不改变。
- 契约补充见 [S_01 公开 API 表面与构造流](../../specs/harness/S_01_public-api-and-construction.md) 的
  “SysOperationSpec 解析失败关闭”不变量。

## 根因与修复决策

- 根因：`resolve()` 在 `get_sys_operation` 为空时调用 `add_sys_operation`，却忽略其结果；注册失败（例如隔离 key
  模板与已注册的 SysOperation 重复，资源管理器拒绝）时返回 `None`。调用方把 `None` 当作“未指定”，于是工厂新建
  LOCAL sys_operation。触发场景：同一用户的 DeepAgent 与 AgentTeam 使用相同的 Firecracker 网关配置。
- 修复：`resolve()` 失败关闭——注册后仍取不到实例时抛出 `SYS_OPERATION_MANAGER_PROCESS_ERROR`（附注册失败原因）；
  同一 id 下已注册实例的 `mode` 与 spec 不一致时同样抛出。成功路径（get-or-create、团队恢复时复用同一实例）不变。
- 拒绝的方案：
  - 在 `factory.py` 中区分“未指定”与“解析失败”：需要改变 `create_deep_agent` 的入参语义，影响面更大；在 `resolve()`
    处报错已覆盖全部 spec 路径。
  - 只记录警告继续运行：仍会在宿主上执行工具，违背沙箱声明。
- 兼容影响：以前静默回退 LOCAL 的配置现在会在构建时报错。这类配置原本就没有按声明运行；报错信息指出 id 与原因。

## 回归证据与适用范围

| 执行日期 / 范围 | 环境 | 命令 | 结果 |
| --- | --- | --- | --- |
| 2026-11-27 新增单测 | `fc-e2e-runner:dev`（Python 3.11，uv.lock） | `run_e2e.sh tests/unit_tests/harness/test_sys_operation_spec_resolve.py tests/unit_tests/harness/test_deep_agent_cold_build.py` | 10 passed |
| 2026-11-27 harness 与 agent_teams 单测回归 | 同上 | `run_e2e.sh tests/unit_tests/harness tests/unit_tests/agent_teams --ignore=.../agent_teams/cli -q` | 13903 passed，30 failed，73 skipped；`agent_teams/cli` 因 runner 缺 `prompt_toolkit` 无法收集而排除 |
| 2026-11-27 失败项基线对照 | 同上 | 对失败的 6 个文件分别在修复前（`git show HEAD:` 版本）与修复后运行 | 均为 29 failed、83 passed：28 项 `harness/personal_context` 容量 worker 子进程用例与 `test_spec_ownership` 的导入隔离用例在修复前同样失败，与本次修改无关；全量中第 30 项（observability 导出）单独运行时修复前后均通过，属顺序相关的偶发失败 |

新增用例：LOCAL spec 注册一次并复用同一实例；注册失败抛出且带原因；同 id 模式不一致抛出；模式一致返回已注册实例；
`DeepAgentSpec.build()` 在注册失败时报错而不是得到 LOCAL sys_operation。

## 防错模式与资料同步

- 模式：声明了隔离要求（SANDBOX）的解析路径必须失败关闭，不得以“未指定”的默认值替代失败结果。已写入 harness S_01 不变量。
- Firecracker Playground 中的规避（独立隔离前缀、运行前校验模式）继续保留，作为纵深防御。

## 遗留与后续

- `create_deep_agent(sys_operation=None)` 仍默认新建 LOCAL sys_operation（既有、显式的默认行为）。
