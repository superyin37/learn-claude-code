# MCP 与综合 Agent Runtime 实现逻辑

本文说明外部 MCP 工具如何进入 Agent，以及 `s20_comprehensive/code.py` 如何把工具、权限、上下文、恢复、调度和团队机制组装到同一个循环。

MCP 独立实现位于 `s19_mcp_plugin/code.py`，完整总装位于 `s20_comprehensive/code.py`。

## 1. MCP 解决什么问题

内置工具由项目直接实现：

```text
bash
read_file
write_file
create_task
```

MCP 让外部服务通过统一协议提供工具：

```text
docs server -> search
deploy server -> deploy, logs
```

Agent 不需要知道 server 使用什么语言或内部 API，只需要获得工具 schema 和调用入口。

## 2. MCPClient

教学版 `MCPClient` 保存：

```text
name
tools
handlers
```

提供两个核心动作：

```text
register：模拟 tools/list
call_tool：模拟 tools/call
```

当前代码用 Python mock server 演示协议边界，没有启动真实子进程或网络连接。

## 3. connect_mcp

`connect_mcp(name)`：

1. 检查是否已经连接。
2. 从 mock server factory 查找配置。
3. 创建 MCPClient。
4. 注册 server 工具。
5. 放入 `mcp_clients`。

连接完成只改变运行时注册表。真正让模型看到新工具的是下一次 `assemble_tool_pool()`。

## 4. 工具命名空间

不同 server 可能都提供 `search`。统一名称格式：

```text
mcp__<server>__<tool>
```

例如：

```text
mcp__docs__search
mcp__issues__search
```

`normalize_mcp_name()` 把不允许的字符替换为下划线，防止名称破坏 schema 或发生不可预测冲突。

## 5. assemble_tool_pool

每轮从两个来源组装：

```text
BUILTIN_TOOLS
  + connected MCP tool definitions

BUILTIN_HANDLERS
  + generated MCP handlers
```

生成 handler 时必须正确捕获当前 client 和原始 tool name。代码使用 lambda 默认参数绑定，避免循环变量晚绑定导致所有 handler 指向最后一个工具。

## 6. 动态工具池与 Prompt

`connect_mcp` 之后：

```text
mcp_clients 改变
  -> assemble_tool_pool 改变
  -> update_context 改变
  -> system prompt 改变
```

因此工具池、handler map 和 system prompt 必须在同一轮重新构建。只更新工具 schema 而不更新 prompt，或只更新 prompt 而不更新 handler，都会造成运行态不一致。

## 7. MCP 与权限

综合版把 MCP 工具送入和内置工具相同的 `PreToolUse` Hook。

工具 description 可标记 read-only 或 destructive。教学版权限判断较简化，但架构上已经具备统一入口：

```text
mcp tool_use
  -> permission_hook
  -> generated MCP handler
  -> PostToolUse
```

外部工具不能绕过本地执行策略。

## 8. 综合 Runtime 的准备阶段

`prepare_context(messages)` 在 LLM 前运行压缩管线：

```text
tool_result_budget
  -> snip_compact
  -> micro_compact
  -> 超限时 compact_history
```

随后 `update_context()` 收集：

- Memory 索引。
- 已连接 MCP server。
- 活跃 teammates。

`assemble_system_prompt()` 再加入 skills catalog 和工作区等 section。

## 9. call_llm

`call_llm(messages, context, tools, state, max_tokens)` 统一模型调用：

```text
assemble system prompt
  -> with_retry
  -> client.messages.create
```

`RecoveryState` 管理当前模型、连续 529、输出升级和 reactive compact 状态。

把模型调用封装成单独函数，使重试逻辑不需要复制整个 agent loop。

## 10. 主 agent_loop

综合执行链：

```text
1. inject background notifications
2. consume cron queue
3. prepare_context
4. update_context
5. assemble_tool_pool
6. call_llm
7. 处理 max_tokens / prompt-too-long
8. 将 assistant content 加入 messages
9. 检查是否实际包含 tool_use
10. 执行每个工具
11. 把 tool_result 加入 messages
12. 回到循环
```

判断是否继续工具轮时使用 `has_tool_use(response.content)`，而不是只依赖 `stop_reason`。实际内容中的 tool block 才是执行依据。

## 11. 工具执行链

每个 `tool_use`：

```text
PreToolUse hooks
  -> 若拒绝：生成拒绝 tool_result
  -> 若慢操作：background dispatch
  -> 否则：handler
  -> PostToolUse hooks
  -> tool_result
```

`call_tool_handler()` 统一参数调用和异常转换，避免单个 handler 异常破坏整轮其他工具结果。

## 12. 两种 Delegation

综合版保留两种不同语义：

### task

一次性 subagent：

- 独立 messages。
- 完成明确子任务。
- 只返回最终摘要。

### spawn_teammate

持久队友：

- 独立线程。
- MessageBus inbox。
- Team Protocol。
- idle polling 和自动 claim。
- 可绑定 worktree。

前者减少主会话噪音，后者支持持续并行协作。

## 13. 两种计划状态

```text
todo_write
  -> 当前 session
  -> 内存状态
  -> 防止当前 Agent 漂移

Task System
  -> 跨 session
  -> JSON 文件
  -> 依赖、owner、团队协调
```

综合版同时保留两者，因为它们解决的时间尺度不同。

## 14. 异步事件统一进入 messages

综合 Runtime 中，多种外部事件最终使用相同通道：

| 事件 | 注入形式 |
|---|---|
| 后台完成 | `<task_notification>` |
| Cron 触发 | `[Scheduled] ...` |
| teammate 消息 | inbox 消息 |
| Memory | system section 或相关内容 |
| compact | 压缩摘要 |

这样模型仍只需要理解 system prompt、messages 和 tools，不需要直接操作线程或队列。

## 15. 为什么“机制很多，循环一个”

各组件没有各自发明新的 Agent：

```text
Memory 改变上下文
Permission 改变工具是否执行
Background 改变工具何时返回完整结果
Cron 改变请求何时产生
Team 改变谁执行任务
Worktree 改变在哪里执行
MCP 改变有哪些工具
Recovery 改变失败后如何继续
```

核心循环仍然是：

```python
while True:
    response = LLM(messages, tools)
    if not has_tool_use(response.content):
        return
    results = execute_tools(response.content)
    messages.append(tool_results)
```

## 16. 当前实现边界

- MCP server 是 mock，不包含真实 transport、认证和重连。
- 综合版 Memory 只加载索引，没有完整提取闭环。
- 多种共享状态使用线程内存，进程恢复不完整。
- Task、inbox、worktree 日志的文件锁较简化。
- 后台、cron 和 teammate 线程没有统一 supervisor。
- 内置工具说明存在手工维护部分。
- S20 是教学总装，不是生产级安全边界。

## 17. 总结

MCP 与综合 Runtime 的关键是“统一”：

```text
外部工具统一进入 tool pool
权限统一进入 PreToolUse
异步事件统一进入 messages
运行状态统一进入 context
模型错误统一进入 recovery
所有能力统一围绕一个 agent_loop
```

复杂 Agent 的工程重点不是增加更多决策树，而是让每种能力在循环中拥有清晰、稳定的位置。
