# Subagent 实现逻辑

本文说明 `agents/s04_subagent.py` 中 subagent 的实现逻辑：父 agent 如何通过 `task` 工具创建子 agent，子 agent 如何拥有独立上下文，如何使用工具完成任务，以及如何只把总结返回给父会话。

## 核心定位

Subagent 的本质是：

```text
一个工具背后挂了另一个 agent loop。
```

父 agent 通过 `task` 工具，把一段子任务 prompt 交给一个全新的 agent loop。子 agent 使用独立 `messages` 执行任务，最后只把总结返回给父 agent。

它解决的问题是：

```text
复杂探索会污染主会话上下文。
```

例如读取大量文件、搜索代码、跑命令、分析日志，这些中间过程不一定需要全部进入父 agent 的 `messages`。父 agent 只需要最终结论。

整体关系：

```text
Parent agent messages
  -> model decides to call task
  -> agent_loop catches task
  -> run_subagent(prompt)
      -> create fresh sub_messages
      -> call model with SUBAGENT_SYSTEM + CHILD_TOOLS
      -> child uses tools
      -> child finishes with text summary
  -> summary becomes tool_result
  -> parent messages only receive summary
```

## 1. 父 agent 和子 agent 的 system prompt

父 agent 的系统提示：

```python
SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."
```

它的重点是：

```text
可以用 task 工具委派探索或子任务。
```

子 agent 的系统提示：

```python
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."
```

它的重点是：

```text
完成给定任务，然后总结发现。
```

所以父 agent 是调度者，子 agent 是一次性执行者。

## 2. 父子共享基础工具函数

基础工具函数还是这些：

```python
safe_path()
run_bash()
run_read()
run_write()
run_edit()
```

工具执行映射：

```python
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}
```

父 agent 和子 agent 共享这些 handler，也共享同一个 `WORKDIR` 文件系统。

因此 s04 的隔离是：

```text
上下文隔离。
文件系统共享。
工具执行函数共享。
```

也就是说，子 agent 看不到父 agent 的历史消息，但它可以读写同一个项目目录。

## 3. CHILD_TOOLS：子 agent 能用哪些工具

子 agent 的工具列表是：

```python
CHILD_TOOLS = [
    bash,
    read_file,
    write_file,
    edit_file,
]
```

注意：子 agent 没有 `task` 工具。

原因是避免递归创建子 agent：

```text
Parent 可以 spawn child。
Child 不可以继续 spawn grandchild。
```

这是一个简单的安全限制，防止无限嵌套。

## 4. PARENT_TOOLS：父 agent 多一个 task 工具

父 agent 的工具列表：

```python
PARENT_TOOLS = CHILD_TOOLS + [
    {
        "name": "task",
        "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "description": {
                    "type": "string",
                    "description": "Short description of the task",
                },
            },
            "required": ["prompt"],
        },
    },
]
```

所以：

```text
父 agent 能用：
  bash / read_file / write_file / edit_file / task

子 agent 能用：
  bash / read_file / write_file / edit_file
```

`task` 工具的输入：

```text
prompt       子 agent 的完整任务描述
description 子任务短描述，可选，用于打印日志
```

## 5. run_subagent 如何初始化

核心函数：

```python
def run_subagent(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]
    ...
```

这里最关键的是：

```python
sub_messages = [{"role": "user", "content": prompt}]
```

它没有继承父 agent 的 `messages`，而是从一条新的 user prompt 开始。

如果父 agent 当前 history 是：

```text
user: 做一个重构
assistant: 调了 todo
user: todo result
assistant: read_file
user: file contents
...
```

子 agent 看不到这些。子 agent 只看到：

```text
SYSTEM: SUBAGENT_SYSTEM
user: prompt
tools: CHILD_TOOLS
```

这就是上下文隔离。

## 6. 子 agent 的 loop 如何运行

`run_subagent` 内部有一个独立循环：

```python
for _ in range(30):
    response = client.messages.create(
        model=MODEL,
        system=SUBAGENT_SYSTEM,
        messages=sub_messages,
        tools=CHILD_TOOLS,
        max_tokens=8000,
    )
    sub_messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason != "tool_use":
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)[:50000],
            })

    sub_messages.append({"role": "user", "content": results})
```

它和主 `agent_loop` 结构一样：

```text
call model
append assistant response
if tool_use:
  execute tools
  append tool_result
repeat
```

不同点：

```text
messages 是 sub_messages。
system 是 SUBAGENT_SYSTEM。
tools 是 CHILD_TOOLS。
最多循环 30 轮。
```

30 轮限制是 safety limit，防止子 agent 无限运行。

## 7. 子 agent 如何返回结果

最后：

```python
return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"
```

它只返回最后一次模型回复里的文本内容。

不会返回：

```text
子 agent 的完整 messages。
子 agent 调过哪些工具。
子 agent 的中间工具结果。
子 agent 的内部对话历史。
```

父 agent 只拿到一个 summary 字符串。

这是 s04 的核心设计：

```text
child context is discarded。
parent context receives only summary。
```

## 8. 父 agent 如何调用 subagent

父 agent 的 `agent_loop` 处理工具调用时，对 `task` 做特殊分支：

```python
if block.name == "task":
    desc = block.input.get("description", "subtask")
    print(f"> task ({desc}): {block.input['prompt'][:80]}")
    output = run_subagent(block.input["prompt"])
else:
    handler = TOOL_HANDLERS.get(block.name)
    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
```

也就是说，`task` 是特殊工具，不走普通 `TOOL_HANDLERS`，而是在父循环里直接调用 `run_subagent`。

然后把子 agent 的结果放回父 agent：

```python
results.append({
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": str(output),
})
```

最后：

```python
messages.append({"role": "user", "content": results})
```

所以父 agent 的 messages 里会看到：

```text
assistant:
  tool_use task(prompt="调查 X 的实现", description="explore X")

user:
  tool_result:
    子 agent 的总结文本
```

但不会看到子 agent 内部读了哪些文件、跑了哪些命令。

## 9. 父子会话看到的信息差异

假设父 agent 当前会话是：

```text
user: 请分析项目并修改登录逻辑
assistant: tool_use read_file(...)
user: tool_result 大量文件内容
assistant: tool_use task(prompt="调查认证模块结构")
```

子 agent 看到：

```text
SYSTEM:
  You are a coding subagent...

user:
  调查认证模块结构

tools:
  bash / read_file / write_file / edit_file
```

子 agent 看不到父 agent 之前的文件内容、todo、工具输出。

父 agent 最后看到：

```text
user:
  tool_result for task:
    认证模块主要在 auth.py 和 middleware.py...
    登录逻辑入口是 ...
    建议修改 ...
```

这样主上下文保持干净。

## 10. Subagent 和普通工具的关系

从模型角度，`task` 就是一个工具：

```text
模型调用 task(prompt=...)
```

从 harness 角度，`task` 是一个高级工具：

```text
普通工具：
  tool_use -> Python 函数 -> 字符串结果

task 工具：
  tool_use -> run_subagent -> 子 agent loop -> summary 字符串结果
```

所以 subagent 可以理解为：

```text
一个工具背后挂了另一个 agent loop。
```

## 11. Subagent 和 Todo 的区别

Todo 是状态工具：

```text
模型写计划。
harness 保存结构化状态。
结果回到 messages。
```

Subagent 是执行工具：

```text
模型委派子任务。
harness 创建独立上下文。
子 agent 完成探索。
summary 回到 messages。
```

Todo 解决任务跟踪。

Subagent 解决上下文污染。

## 12. 当前实现的限制

这个 s04 是教学版，有几个边界：

```text
子 agent 不持久，跑完即丢。
子 agent 没有名字、角色、邮箱。
子 agent 不能继续 spawn subagent。
子 agent 与父 agent 共享文件系统，没有独立工作区。
父 agent 只收到 summary，无法追溯子 agent 内部完整日志。
run_subagent 是阻塞调用，没有并发。
没有权限分级，父子共享同一批基础工具函数。
```

后续演进方向：

```text
s08 解决后台/非阻塞执行。
s09 把一次性 subagent 升级为持久 teammate。
s12 用 worktree 做目录级隔离。
```

## 总结

`s04` 的 subagent 实现逻辑是：

```text
父 agent 多一个 task 工具。
模型调用 task 时，harness 创建一个新的 sub_messages。
子 agent 用 SUBAGENT_SYSTEM 和 CHILD_TOOLS 独立运行 agent loop。
子 agent 的中间历史全部丢弃。
最后只把总结作为 tool_result 返回给父 agent。
```

它的本质是：

```text
用一个新的 messages 数组换取上下文隔离。
```
