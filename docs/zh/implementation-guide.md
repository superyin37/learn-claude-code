# Implementation 文档导航

本目录按能力板块解释 Agent Harness 的实现。文档不强制与 `s01` 到 `s20` 一一对应：单一机制可以对应一章，跨章节机制则合并为一个板块。

## 核心循环与工具

- [Agent Loop 实现逻辑](agent-loop-implementation.md)
- [Tool System 实现逻辑](tool-system-implementation.md)
- [Permission 与 Hooks 实现逻辑](permission-hooks-implementation.md)

## 计划、任务与委派

- [TodoList 实现逻辑](todolist-implementation.md)
- [Task System 实现逻辑](task-system-implementation.md)
- [Subagent 实现逻辑](subagent-implementation.md)

## 知识与上下文

- [Skill Loading 实现逻辑](skill-loading-implementation.md)
- [Context Compact 实现逻辑](context-compact-implementation.md)
- [Memory 实现逻辑](memory-implementation.md)
- [运行时 Context 与 System Prompt 实现逻辑](runtime-context-prompt-implementation.md)
- [Error Recovery 实现逻辑](error-recovery-implementation.md)

## 持续运行

- [Background Task 实现逻辑](background-task-implementation.md)
- [后台执行、调度与 Autonomous Agent 实现逻辑](scheduling-autonomy-implementation.md)

## 团队与隔离

- [Agent Team 实现逻辑](agent-team-implementation.md)
- [Team Protocol 实现逻辑](team-protocol-implementation.md)
- [Worktree Isolation 实现逻辑](worktree-isolation-implementation.md)

## 外部能力与总装

- [MCP 与综合 Agent Runtime 实现逻辑](mcp-comprehensive-runtime-implementation.md)

## 版本说明

早期 implementation 文档主要解释 `agents/s01_agent_loop.py` 到 `agents/s10_team_protocols.py`。

本次新增文档以当前章节目录中的实现为准：

```text
s03_permission/code.py
s04_hooks/code.py
s09_memory/code.py
s10_system_prompt/code.py
s11_error_recovery/code.py
s13_background_tasks/code.py
s14_cron_scheduler/code.py
s17_autonomous_agents/code.py
s18_worktree_isolation/code.py
s19_mcp_plugin/code.py
s20_comprehensive/code.py
```

旧文档与当前章节发生差异时，应优先以对应 `s*/code.py` 和新板块文档为准。后续会逐步把旧文档中的源码引用从 `agents/` 迁移到当前章节目录。
