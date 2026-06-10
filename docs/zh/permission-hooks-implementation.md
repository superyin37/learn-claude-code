# Permission 与 Hooks 实现逻辑

本文说明当前项目中权限管线与 Hook 扩展机制如何协作。主要实现位于 `s03_permission/code.py`、`s04_hooks/code.py`，综合实现位于 `s20_comprehensive/code.py`。

## 1. 核心定位

权限和 Hook 解决的是两个不同问题：

```text
Permission：这个动作能不能执行？
Hooks：在执行链的某个位置，还要运行哪些扩展逻辑？
```

权限是策略，Hook 是挂载策略和其他扩展的机制。最终结构不是在 `agent_loop` 中硬编码每项检查，而是：

```text
tool_use
  -> PreToolUse hooks
       -> permission
       -> logging
       -> audit
  -> handler
  -> PostToolUse hooks
```

## 2. Permission 的三阶段判断

`s03_permission/code.py` 在工具执行前调用 `check_permission(block)`：

```text
硬拒绝列表
  -> 规则匹配
  -> 必要时询问用户
  -> allow / deny
```

### 2.1 硬拒绝

`check_deny_list()` 检查明显危险的 shell 片段，例如 `rm -rf /`、`sudo`、`shutdown`。命中后直接拒绝，不进入用户审批。

这是教学版的最小实现。字符串包含检查不能完整解析 shell，也无法覆盖变量展开、编码和命令替代，因此不能当作生产级沙箱。

### 2.2 条件规则

`check_rules(tool_name, args)` 处理需要结合工具参数判断的操作，例如：

- `write_file`、`edit_file` 写到工作区外。
- `bash` 包含删除、覆盖系统文件或危险权限修改。

规则返回的是“为什么需要审批”，而不是直接执行。

### 2.3 用户审批

`ask_user()` 暂停当前执行，要求用户决定是否允许。审批结果只作用于本次工具调用，不形成永久规则。

无论允许还是拒绝，harness 都要为对应 `tool_use_id` 返回一个 `tool_result`。拒绝不是丢弃调用，而是把拒绝结果反馈给模型，让模型选择其他路径。

## 3. Hook 注册表

`s04_hooks/code.py` 把扩展点定义为事件到回调列表的映射：

```python
HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}
```

注册和触发接口：

```python
def register_hook(event: str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None
```

当前教学实现采用短路语义：第一个返回非 `None` 的 Hook 结束本次事件分发。

## 4. 四个生命周期事件

### UserPromptSubmit

用户输入进入历史记录和模型调用之前触发。适合：

- 输入审计。
- 注入工作区信息。
- 检查用户请求格式。

教学实现主要打印日志，没有修改用户输入。

### PreToolUse

工具真正执行之前触发。权限检查作为一个 `PreToolUse` Hook 注册：

```python
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
```

返回非 `None` 表示阻止工具执行。主循环仍会生成拒绝型 `tool_result`。

### PostToolUse

handler 返回以后触发，适合：

- 大输出告警。
- 写审计日志。
- 记录执行时长。
- 根据结果执行后处理。

它不应改变 `tool_use` 和 `tool_result` 的一一对应关系。

### Stop

模型不再请求工具、主循环准备退出时触发。可用于：

- 会话统计。
- 清理资源。
- 触发记忆提取。
- 返回一条消息要求 Agent 继续工作。

教学版允许 Stop Hook 返回字符串，将其作为新的用户消息加入历史并继续循环。

## 5. 综合版执行顺序

`s20_comprehensive/code.py` 中权限已经完全作为 Hook 接入：

```text
用户请求
  -> UserPromptSubmit
  -> LLM
  -> tool_use
  -> PreToolUse
       -> permission_hook
       -> log_hook
  -> background dispatch 或 handler
  -> PostToolUse
  -> tool_result
  -> 下一轮
```

综合版还会检查破坏性 MCP 工具。外部工具和内置工具经过同一 `PreToolUse` 入口，因此权限策略不需要散落在每一种 handler 中。

## 6. 为什么权限不应只写在工具函数里

把权限全部放进 `run_bash()` 或 `run_write()` 会带来三个问题：

1. 同一策略会在多个工具中重复。
2. 动态 MCP 工具无法提前写入本地函数。
3. 日志、审批和安全策略与业务执行强耦合。

Hook 把“能否执行”和“如何执行”分开：

```text
PreToolUse 决策
Handler 执行
PostToolUse 观察
```

## 7. 当前实现边界

- deny list 是字符串匹配，不是 shell AST 或系统级隔离。
- 审批只保存在当前调用中，没有会话级 allow rule。
- Hook 没有优先级、并行执行和超时控制。
- 短路后，后续 Hook 不会收到事件。
- PostToolUse 主要用于观察，未定义修改结果的正式协议。
- 真正的安全边界仍需操作系统权限、容器或沙箱提供。

## 8. 总结

Permission 与 Hooks 的组合模式是：

```text
权限负责决策。
Hook 负责把决策挂到稳定执行点。
agent_loop 只负责触发事件，不负责了解每项策略。
```

这让后续的日志、记忆、审计、MCP 权限和 Stop 收尾都可以接入同一条执行链，而不继续膨胀核心循环。
