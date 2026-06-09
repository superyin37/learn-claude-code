# TodoList 实现逻辑

本文说明 `agents/s03_todo_write.py` 中 TodoList 的实现逻辑：如何初始化、如何与会话联动、如何维护状态、如何提醒模型更新进度，以及如何判断任务结束。

## 核心定位

TodoList 不是外部调度器。它是一个模型可写入的结构化状态。

模型负责决定任务拆分和进度更新，harness 负责：

- 暴露 `todo` 工具
- 校验 todo 数据结构
- 保存当前 todo 状态
- 把 todo 渲染结果写回 `messages`
- 在模型长时间不更新 todo 时注入提醒

整体关系：

```text
SYSTEM 提醒模型使用 todo
  -> 模型调用 todo 工具
  -> TOOL_HANDLERS["todo"]
  -> TODO.update(items)
  -> TodoManager 校验并保存状态
  -> render() 返回文本
  -> tool_result 写回 messages
  -> 模型下一轮看到 todo 状态
```

## 1. 初始化

TodoList 的初始化分两层。

第一层是系统提示：

```python
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use the todo tool to plan multi-step tasks. Mark in_progress before starting, completed when done.
Prefer tools over prose."""
```

这段提示告诉模型：

```text
多步任务要用 todo 工具规划。
开始做某项前标记 in_progress。
做完后标记 completed。
优先使用工具，而不是只用自然语言解释。
```

第二层是创建 `TodoManager` 实例：

```python
TODO = TodoManager()
```

`TodoManager.__init__` 很简单：

```python
def __init__(self):
    self.items = []
```

也就是说，每次启动脚本时，todo 初始为空。它是进程内内存状态，不是落盘状态。退出程序后会丢失。

## 2. Todo 数据结构

每个 todo item 是一个字典：

```python
{
    "id": "1",
    "text": "Read files",
    "status": "pending",
}
```

字段含义：

```text
id      任务 ID，字符串
text    任务描述
status  当前状态
```

状态只有三种：

```text
pending       尚未开始
in_progress   当前正在做
completed     已完成
```

内部状态保存在：

```python
self.items
```

也就是 `TodoManager` 的内存列表。

## 3. 如何作为工具暴露给模型

Todo 被注册成一个标准 tool。

执行侧：

```python
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo":       lambda **kw: TODO.update(kw["items"]),
}
```

模型可见侧：

```python
{
    "name": "todo",
    "description": "Update task list. Track progress on multi-step tasks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["id", "text", "status"],
                },
            },
        },
        "required": ["items"],
    },
}
```

所以它和普通工具一样：

```text
TOOLS 告诉模型怎么调用 todo。
TOOL_HANDLERS 告诉 Python 怎么执行 todo。
TODO.update 是实际状态更新函数。
```

## 4. update 如何维护状态

核心函数是：

```python
def update(self, items: list) -> str:
    ...
```

它不是增量 patch，而是整表替换。

流程：

```text
模型每次传入完整 items 列表
  -> TodoManager 校验
  -> 校验通过后 self.items = validated
  -> 返回 render()
```

校验逻辑：

```text
1. 最多 20 个任务。
2. 每个任务 text 不能为空。
3. status 必须是 pending / in_progress / completed。
4. 如果没有 id，则用列表序号生成 id。
5. 同一时间最多只能有一个 in_progress。
```

关键约束：

```python
if in_progress_count > 1:
    raise ValueError("Only one task can be in_progress at a time")
```

这个约束让 todo 不只是显示列表，还能限制模型的执行节奏：

```text
一次只能明确推进一项任务。
```

## 5. render 如何回显给模型

更新成功后调用：

```python
return self.render()
```

`render()` 把结构化状态转成模型和用户都容易读的文本：

```text
[x] #1: Read files
[>] #2: Edit code
[ ] #3: Run tests

(1/3 completed)
```

标记规则：

```text
pending      -> [ ]
in_progress  -> [>]
completed    -> [x]
```

统计完成数：

```python
done = sum(1 for t in self.items if t["status"] == "completed")
lines.append(f"\n({done}/{len(self.items)} completed)")
```

这个文本会作为 `tool_result` 进入 `messages`，所以模型下一轮能看到自己刚刚更新后的计划。

## 6. 与 history/messages 如何联动

用户输入先进入 `history`：

```python
history.append({"role": "user", "content": query})
agent_loop(history)
```

然后 `agent_loop` 每轮把 `messages` 传给模型：

```python
response = client.messages.create(
    model=MODEL,
    system=SYSTEM,
    messages=messages,
    tools=TOOLS,
)
```

模型如果调用 `todo`：

```text
assistant message:
  tool_use todo(items=[...])

Python:
  TODO.update(items)

user message:
  tool_result:
    [x] #1: ...
    [>] #2: ...
```

所以有两份状态：

```text
TODO.items
  真实结构化状态，存在 Python 内存里。

messages
  对话历史，保存模型调用 todo 的过程和 render 后文本。
```

二者联动方式：

```text
模型通过 messages 看到 todo 文本。
模型通过 todo tool 改 TODO.items。
TODO.render 再写回 messages。
```

## 7. 模型每轮会看到哪些 todo 信息

在当前实现中，每次模型调用都会看到整个 `messages/history`，因为 s03 还没有 compact。

如果几轮之前模型调用过 todo，后来又调用了几个工具，那么模型会看到：

```text
1. 最初用户任务
2. 当时模型发出的 todo tool_use
3. todo 工具返回的渲染结果
4. 后续 read_file / edit_file / bash 的 tool_use
5. 后续工具结果
6. 如果触发过 reminder，也会看到 reminder
```

例如：

```text
user:
  重构这个模块

assistant:
  tool_use todo(items=[...])

user:
  tool_result for todo:
    [x] #1: Read target files
    [>] #2: Edit implementation
    [ ] #3: Run tests

    (1/3 completed)

assistant:
  tool_use read_file(path="...")

user:
  tool_result for read_file:
    文件内容...

assistant:
  tool_use edit_file(...)

user:
  tool_result for edit_file:
    Edited path...
```

TodoManager 自己的 `TODO.items` 是当前结构化状态，但模型真正看到的是历史里每次 todo `tool_result` 的文本快照。

这些旧快照都会留在 `messages` 里。模型需要靠时间顺序理解：

```text
最后一个 todo 结果才是当前状态。
```

这也是后续需要 compact 的原因。

## 8. 如何提醒模型维护 todo

`agent_loop` 中维护一个计数器：

```python
rounds_since_todo = 0
```

每轮检查模型有没有调用 `todo`：

```python
used_todo = False

for block in response.content:
    if block.type == "tool_use":
        ...
        if block.name == "todo":
            used_todo = True
```

然后更新计数：

```python
rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
```

如果连续 3 轮没用 todo：

```python
if rounds_since_todo >= 3:
    results.insert(0, {
        "type": "text",
        "text": "<reminder>Update your todos.</reminder>",
    })
```

这条 reminder 会和 tool results 一起作为用户消息进入 `messages`。

作用：

```text
不是强制模型必须改 todo。
而是在模型长时间忘记维护计划时，把注意力拉回来。
```

## 9. 如何判断结束

这里要区分两个“结束”。

`agent_loop` 的结束条件是模型停止调用工具：

```python
if response.stop_reason != "tool_use":
    return
```

也就是说，从 harness 角度：

```text
模型不再请求工具，agent_loop 就结束。
```

Todo 本身没有独立的强制结束判断。它只是记录：

```text
completed 数量 / 总数量
```

因此当前实现里：

```text
所有 todo 都 completed
  -> 这是模型可见的完成信号

模型看到全部 completed 后
  -> 应该停止调用工具并给最终回复

harness 检测到 stop_reason != tool_use
  -> 退出 agent_loop
```

换句话说：

```text
Todo 不直接终止 loop。
模型根据 todo 状态决定是否结束。
agent_loop 根据模型是否继续 tool_use 来结束。
```

## 10. 实现边界

当前 `TodoManager` 是教学版，有几个明显边界：

```text
不落盘，退出进程丢失。
不支持任务依赖。
不支持 owner。
不支持跨 agent 共享。
不自动判断所有 completed 后强制停止。
不做增量 patch，每次是整表替换。
```

这些边界正是后续 `s07 TaskSystem` 要解决的方向：

```text
TodoManager 适合当前会话的短期计划。
TaskManager 适合跨会话、可共享、可持久化的任务板。
```

## 总结

TodoList 的实现逻辑是：

```text
用 SYSTEM 引导模型规划。
用 TOOLS 暴露 todo 写入接口。
用 TOOL_HANDLERS 路由到 TODO.update。
用 TodoManager 校验并维护结构化状态。
用 render 把状态写回 messages。
用 rounds_since_todo 提醒模型持续维护。
用模型停止 tool_use 作为 agent_loop 结束条件。
```

它的本质不是外部程序替模型安排流程，而是给模型一个结构化白板，让模型自己写计划、更新进度，并让 harness 保证这个白板格式稳定、状态可见。
