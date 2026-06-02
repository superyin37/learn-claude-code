# Agent 系统组件总览：s01 到 s10

本文总结本仓库从 `s01` 到 `s10` 逐步构建出的 agent harness。重点不是“模型如何变聪明”，而是一个工程系统如何把模型、工具、上下文、记忆、任务、后台执行、团队协作和协议组织起来。

一句话概括：

```text
Agent 系统 = 模型 + messages + agent_loop + TOOLS + TOOL_HANDLERS + 外部状态
```

模型负责判断。Harness 负责提供能力、执行动作、记录状态、管理边界。

## 1. 最小 Agent 内核

最小 agent 包含：

```python
client = Anthropic(...)
MODEL = os.environ["MODEL_ID"]
SYSTEM = "..."
TOOLS = [...]
TOOL_HANDLERS = {...}
history = []
agent_loop(history)
```

它们的关系是：

```text
User input
  -> history/messages
  -> client.messages.create(model, system, messages, tools)
  -> model response
  -> 如果 stop_reason == tool_use
       根据 tool name 找 TOOL_HANDLERS
       执行真实函数
       把 tool_result 追加回 history
       继续 loop
     否则结束
```

### TOOLS

`TOOLS` 是给模型看的能力声明。它告诉模型：

- 有哪些工具可以调用
- 每个工具的用途是什么
- 每个工具需要什么参数

示例：

```python
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]
```

### TOOL_HANDLERS

`TOOL_HANDLERS` 是给 harness 执行用的路由表。模型只会说“我要调用 `bash`”，真正执行哪个 Python 函数由 `TOOL_HANDLERS` 决定。

```python
TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
}
```

`TOOLS` 和 `TOOL_HANDLERS` 通过工具名对应：

```text
TOOLS[*].name == TOOL_HANDLERS 的 key
```

### 工具函数

工具函数是真正接触外部世界的代码，例如：

```python
run_bash()
run_read()
run_write()
run_edit()
safe_path()
```

模型不能直接执行 shell 或写文件。模型只能发起 tool call，harness 再调用这些函数。

### history / messages

`history` 或 `messages` 是 agent 的短期记忆，里面包含：

- 用户消息
- 模型回复
- 工具调用结果

模型每次推理看到的上下文就是它。

### agent_loop

`agent_loop` 是系统心脏。它不做智能决策，只做闭环：

```python
def agent_loop(messages):
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS[block.name]
                output = handler(**block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

        messages.append({"role": "user", "content": results})
```

核心原则：

```text
模型决定何时调用工具。
Harness 执行工具。
工具结果回到 messages。
模型继续推理。
```

## 2. s01：Agent Loop

`s01` 建立最小闭环。

组件：

```text
client
MODEL
SYSTEM
TOOLS = [bash]
run_bash()
agent_loop()
history
```

关系：

```text
用户输入
  -> history
  -> 模型
  -> bash tool_use
  -> run_bash
  -> tool_result
  -> history
  -> 模型继续
```

作用：证明 coding agent 的核心不是复杂框架，而是“模型 + 一个工具 + 一个循环”。

## 3. s02：Tool Dispatch

`s02` 把单工具扩展为工具系统。

新增组件：

```python
safe_path()
run_read()
run_write()
run_edit()
TOOL_HANDLERS
```

此时工具层变成：

```text
TOOLS
  -> 给模型看的工具说明

TOOL_HANDLERS
  -> 给 harness 执行的函数映射

工具函数
  -> 真实执行 shell、读文件、写文件、编辑文件

safe_path
  -> 防止路径逃逸 workspace
```

典型结构：

```python
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}
```

核心变化：

```text
agent_loop 不变。
新增工具只需要：
1. 在 TOOLS 里声明
2. 在 TOOL_HANDLERS 里注册 handler
3. 实现对应函数
```

## 4. s03：TodoWrite

`s03` 加入计划状态。

新增组件：

```python
class TodoManager
TODO = TodoManager()
todo tool
```

`TodoManager` 维护结构化 todo：

```python
[
    {"id": "1", "text": "Read files", "status": "completed"},
    {"id": "2", "text": "Edit code", "status": "in_progress"},
    {"id": "3", "text": "Run tests", "status": "pending"},
]
```

它和 `history` 的关系：

```text
history 是对话上下文。
TODO 是结构化进度状态。

模型调用 todo 工具更新 TODO。
TODO.render() 作为 tool_result 回到 history。
模型下一轮看到自己的计划状态。
```

加入工具系统：

```python
TOOL_HANDLERS["todo"] = lambda **kw: TODO.update(kw["items"])
```

作用：

```text
让模型先列步骤，再执行。
让用户可以看到 agent 当前进度。
减少长任务漂移。
```

Todo 是短期执行计划，主要服务于当前会话。

## 5. s04：Subagent

`s04` 加入上下文隔离。

新增组件：

```python
run_subagent(prompt)
task tool
sub_msgs = []
sub_tools = [...]
```

Subagent 不是新的模型类，而是：

```text
同一个模型
+ 新的 messages
+ 一组受限工具
+ 一个独立 loop
```

关系：

```text
主 agent history
  -> 模型调用 task 工具
  -> run_subagent(prompt)
      -> 创建 sub_msgs
      -> 调用模型
      -> 子 agent 自己读文件、跑命令
      -> 返回 summary
  -> summary 作为 tool_result 回主 history
```

作用：

```text
隔离探索噪声。
保护主上下文清晰。
让大任务可以拆给干净上下文执行。
```

Todo 和 Subagent 的区别：

```text
Todo 管“我要做什么”。
Subagent 管“这部分让另一个上下文去查或做”。

Todo 是计划状态。
Subagent 是隔离执行机制。
```

## 6. s05：Skill Loading

`s05` 加入按需知识加载。

新增组件：

```python
class SkillLoader
SKILLS = SkillLoader(SKILLS_DIR)
load_skill tool
```

`SkillLoader` 扫描：

```text
skills/**/SKILL.md
```

解析每个 skill 的元数据和正文。

Skill 和 Tool 的关系：

```text
Tool 是行动能力。
Skill 是知识能力。
```

Skill 本身不是动作函数。它通过 `load_skill` 工具把知识注入 `messages`：

```text
模型看到 SYSTEM 里的可用 skill 列表
  -> 判断需要某个 skill
  -> 调用 load_skill(name)
  -> SkillLoader 返回 SKILL.md 内容
  -> 内容作为 tool_result 进入 history
  -> 模型基于该知识继续行动
```

为什么不把所有 skill 一开始塞进 system？

```text
因为上下文昂贵。
因为无关知识会干扰模型。
因为模型应该按需拉取领域知识。
```

作用：

```text
把知识变成可发现、可加载、可组合的资源。
让模型在需要时扩展上下文，而不是一开始背负全部内容。
```

## 7. s06：Context Compact

`s06` 加入上下文压缩。

新增组件：

```python
estimate_tokens(messages)
micro_compact(messages)
auto_compact(messages)
compact tool
TRANSCRIPT_DIR
TOKEN_THRESHOLD
```

它和 `history/messages` 的关系最直接。

`messages` 是工作记忆，但它会不断增长：

```text
用户消息
模型回复
工具结果
skill 内容
子 agent summary
错误日志
测试输出
```

压缩分三层：

```text
micro_compact
  -> 清理旧的大型 tool_result

auto_compact
  -> 让模型总结旧上下文
  -> 用 summary 替换长历史

transcript archive
  -> 把完整历史写入 .transcripts
  -> 保留可追溯性
```

流程：

```text
每次模型调用前
  -> estimate_tokens(messages)
  -> 如果接近阈值：
       micro_compact(messages)
  -> 如果仍然过大：
       auto_compact(messages)
       写 transcript
       用 summary 初始化新的 messages
```

Compact 与其他组件的关系：

```text
Skill 内容会进入 messages，因此可能被 compact。
Subagent summary 会进入 messages，因此也可能被 compact。
Todo 如果只存在 messages 里，可能受 compact 影响。
因此后续 s07 引入磁盘持久化 TaskSystem。
```

作用：

```text
让 agent 能进行长会话。
避免上下文窗口被工具输出撑爆。
在保留连续性的同时腾出上下文空间。
```

## 8. s07：Task System

`s07` 把短期 todo 升级为持久化任务系统。

新增组件：

```python
class TaskManager
TASKS_DIR = WORKDIR / ".tasks"
task_create
task_update
task_list
task_get
```

TaskSystem 和 Todo 的关系：

```text
Todo 是会话内计划。
TaskSystem 是跨会话任务板。

Todo 管当前几步。
TaskSystem 管长期目标、状态、归属和依赖。
```

TaskSystem 把任务从 `history` 中拿出来，放到磁盘：

```text
.tasks/
  task files or task state
```

典型调用：

```text
模型调用 task_create
  -> TaskManager 创建任务
  -> 返回 task id

模型调用 task_update
  -> 修改任务状态或 owner

模型调用 task_list
  -> 读取任务板

模型调用 task_get
  -> 查看任务详情
```

作用：

```text
让任务状态不依赖当前 messages。
让任务可以跨 session 存在。
为后续多 agent 协作提供共享状态。
```

## 9. s08：Background Tasks

`s08` 加入后台执行。

新增组件：

```python
class BackgroundManager
background_run
check_background
threading.Thread
notification queue
```

以前的 `bash` 是阻塞式：

```text
模型调用 bash
  -> harness 等命令完成
  -> 返回结果
  -> 模型才能继续
```

后台任务模式：

```text
模型调用 background_run(command)
  -> BackgroundManager 创建后台 thread
  -> 立即返回 job_id
  -> agent_loop 继续

后台命令完成
  -> 结果进入 notification queue

模型之后调用 check_background
或 agent_loop 注入通知
  -> 模型看到后台结果
```

它和 `history` 的关系：

```text
后台任务的结果不是马上作为当前 tool_result 返回。
它稍后以通知形式进入 messages。
```

作用：

```text
长时间运行的命令不阻塞模型。
模型可以一边等测试，一边继续读代码、规划或处理其他任务。
```

## 10. s09：Agent Teams

`s09` 从单 agent 扩展到团队。

新增组件：

```python
class MessageBus
class TeammateManager
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
spawn_teammate
list_teammates
send_message
read_inbox
broadcast
```

### MessageBus

`MessageBus` 是通信层。

每个成员一个 JSONL inbox：

```text
.team/inbox/lead.jsonl
.team/inbox/alice.jsonl
.team/inbox/bob.jsonl
```

核心函数：

```python
send(sender, to, content, msg_type)
read_inbox(name)
broadcast(sender, content, teammates)
```

关系：

```text
send
  -> 追加一行 JSON 到目标 inbox

read_inbox
  -> 读取目标 inbox
  -> 清空 inbox

broadcast
  -> 给所有队友 send
```

### TeammateManager

`TeammateManager` 管队友生命周期。

核心函数：

```python
spawn(name, role, prompt)
_teammate_loop(name, role, prompt)
list_all()
member_names()
```

关系：

```text
Lead agent
  -> 调 spawn_teammate
  -> TeammateManager.spawn
  -> 创建后台 thread
  -> thread 里运行 _teammate_loop
  -> teammate 有自己的 messages
  -> teammate 通过 MessageBus 收发消息
```

Subagent 和 Teammate 的区别：

```text
Subagent：
  一次性
  返回 summary
  偏上下文隔离

Teammate：
  持久存在
  有名字、角色、状态、inbox
  可持续通信
  偏团队协作
```

作用：

```text
让一个主 agent 可以派生多个持久队友。
队友之间不共享 messages，而是通过 inbox 通信。
团队状态落在 .team/config.json 和 .team/inbox/*.jsonl。
```

## 11. s10：Team Protocols

`s10` 在自由消息之上加入结构化协议。

新增组件：

```python
VALID_MSG_TYPES
shutdown_requests = {}
plan_requests = {}
_tracker_lock
handle_shutdown_request()
handle_plan_review()
_check_shutdown_status()
shutdown_request tool
shutdown_response tool
plan_approval tool
```

s09 的消息是自由文本，s10 加入可追踪状态机。

核心模式：

```text
request_id correlation
```

也就是：凡是需要请求、响应、审批、确认的事情，都生成一个 `request_id`，双方围绕这个 id 更新状态。

### Shutdown Protocol

状态机：

```text
pending -> approved | rejected
```

流程：

```text
Lead
  -> 调 shutdown_request(teammate)
  -> 生成 request_id
  -> shutdown_requests[request_id] = pending
  -> MessageBus 发 shutdown_request 给 teammate

Teammate
  -> read_inbox
  -> 看到 shutdown_request
  -> 调 shutdown_response(request_id, approve)
  -> 更新 shutdown_requests
  -> 发 shutdown_response 给 lead

Lead
  -> 调 shutdown_response(request_id)
  -> 查询该 request_id 的状态
```

相关函数：

```python
handle_shutdown_request(teammate)
_check_shutdown_status(request_id)
```

### Plan Approval Protocol

状态机：

```text
pending -> approved | rejected
```

流程：

```text
Teammate
  -> 调 plan_approval(plan)
  -> 生成 request_id
  -> plan_requests[request_id] = pending
  -> MessageBus 发 plan_approval_response 给 lead

Lead
  -> read_inbox
  -> 看到计划和 request_id
  -> 调 plan_approval(request_id, approve, feedback)
  -> 更新 plan_requests
  -> 发审批结果给 teammate
```

相关函数：

```python
handle_plan_review(request_id, approve, feedback)
```

注意命名：

```text
plan_approval 是工具名。
plan_approval_response 是消息类型。
```

工具负责触发动作，消息类型负责在 inbox 中表达协议语义。

作用：

```text
把自由文本协作升级为可追踪协议。
让 lead 和 teammate 能围绕 request_id 达成一致。
为更复杂的团队治理、审批和生命周期控制打基础。
```

## 12. 截止 s10 的整体关系图

```text
client / MODEL / SYSTEM
        |
        v
agent_loop(messages/history)
        |
        +-- before model call
        |     read inbox
        |     drain background notifications
        |     compact messages
        |
        +-- model sees
        |     SYSTEM
        |     messages
        |     TOOLS
        |
        +-- model returns
              text or tool_use
                    |
                    v
              TOOL_HANDLERS[name]
                    |
                    +-- base tools
                    |     bash
                    |     read_file
                    |     write_file
                    |     edit_file
                    |
                    +-- planning
                    |     TodoManager
                    |
                    +-- context isolation
                    |     run_subagent
                    |
                    +-- knowledge
                    |     SkillLoader
                    |
                    +-- memory
                    |     micro_compact
                    |     auto_compact
                    |
                    +-- persistent tasks
                    |     TaskManager
                    |
                    +-- async execution
                    |     BackgroundManager
                    |
                    +-- team
                    |     MessageBus
                    |     TeammateManager
                    |
                    +-- protocols
                          shutdown_requests
                          plan_requests
                          request_id trackers
```

## 13. 分层理解

可以把 s01 到 s10 的系统分成这些层：

```text
1. Model layer
   client, MODEL, SYSTEM

2. Conversation layer
   history/messages, agent_loop

3. Tool declaration layer
   TOOLS

4. Tool execution layer
   TOOL_HANDLERS, run_bash, run_read, run_write, run_edit

5. Planning layer
   TodoManager

6. Context isolation layer
   run_subagent, sub_msgs

7. Knowledge layer
   SkillLoader, load_skill

8. Memory layer
   estimate_tokens, micro_compact, auto_compact, transcript archive

9. Persistent coordination layer
   TaskManager, .tasks

10. Async layer
   BackgroundManager, threads, notification queue

11. Team layer
   MessageBus, TeammateManager, .team/inbox/*.jsonl

12. Protocol layer
   request_id, shutdown_requests, plan_requests, approval/shutdown FSM
```

## 14. 关键组件一句话总结

`TOOLS` 是模型可见的能力说明。

`TOOL_HANDLERS` 是 harness 的真实执行路由。

`agent_loop` 把模型、工具和 messages 串成闭环。

`history/messages` 是模型的短期工作记忆。

`TodoManager` 给单会话任务一个结构化计划。

`run_subagent` 用独立 messages 隔离复杂探索。

`SkillLoader` 把知识按需作为 tool_result 注入上下文。

`compact` 机制管理 messages 的大小和长期连续性。

`TaskManager` 把任务从短期 todo 升级为磁盘持久状态。

`BackgroundManager` 把慢操作移出阻塞 loop。

`MessageBus` 让多个 agent 用 inbox 通信。

`TeammateManager` 管持久队友的线程、状态和循环。

`Protocols` 用 `request_id` 把自由通信升级为可追踪状态机。

## 15. 总结

从 `s01` 到 `s10`，agent 系统的演进不是不断改写核心循环，而是在核心循环之外增加 harness 能力。

最小内核始终是：

```text
messages -> model -> tool_use -> handler -> tool_result -> messages
```

后续所有机制都是围绕这个内核展开：

```text
Todo 让它有计划。
Subagent 让它能隔离上下文。
Skill 让它能按需加载知识。
Compact 让它能长时间运行。
TaskSystem 让目标能持久化。
Background 让慢任务不阻塞。
AgentTeam 让多个模型协作。
Protocol 让协作可追踪、可治理。
```

所以一个成熟 agent harness 的工程重点不是替模型写死流程，而是提供清晰的能力、干净的上下文、稳定的状态、可控的权限和可追踪的协作边界。
