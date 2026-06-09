# Agent Loop 实现逻辑

本文说明 `agents/s01_agent_loop.py` 中最小 Agent Loop 的实现。它是后续 Todo、Subagent、Skill、Compact、TaskSystem 和 AgentTeam 的共同内核。

## 1. 直观理解

最小 agent 可以理解为：

```text
模型负责决定下一步。
工具负责接触真实世界。
messages 负责保存过程。
agent_loop 负责让三者持续循环。
```

核心闭环：

```text
用户请求
  -> 模型判断
  -> 如果需要行动，发出 tool_use
  -> harness 执行工具
  -> tool_result 返回模型
  -> 模型继续判断
  -> 不再调用工具时结束
```

最小组成：

```python
client
MODEL
SYSTEM
TOOLS
run_bash()
history
agent_loop()
```

## 2. 初始化模型环境

```python
load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
```

这里完成三件事：

```text
加载 .env 环境变量。
创建 Anthropic 客户端。
读取要调用的模型 ID。
```

`client` 不是 agent 本身，它只是模型 API 的调用入口。

## 3. SYSTEM 的作用

```python
SYSTEM = (
    f"You are a coding agent at {os.getcwd()}. "
    "Use bash to solve tasks. Act, don't explain."
)
```

SYSTEM 提供稳定的运行身份和环境信息：

```text
你是 coding agent。
当前工作目录是什么。
可以使用 bash 行动。
优先行动而不是只解释。
```

SYSTEM 每次模型调用都会传入，但不存放在 `history` 里。

模型每轮看到的输入可以理解为：

```text
SYSTEM + history/messages + TOOLS
```

## 4. TOOLS：模型可见的能力声明

```python
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
        },
        "required": ["command"],
    },
}]
```

TOOLS 告诉模型：

```text
工具名是什么。
工具可以做什么。
调用时需要哪些参数。
```

TOOLS 只是一份声明，不会执行命令。真实执行发生在 Python 工具函数中。

## 5. run_bash：真实动作接口

```python
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
```

职责：

```text
接收模型产生的 command。
进行最小危险命令过滤。
在当前工作目录执行命令。
捕获 stdout 和 stderr。
限制执行时间和返回长度。
把结果转换为字符串返回。
```

模型不能直接运行 shell。它只能产生一个结构化的 `tool_use`，由 harness 决定是否执行。

## 6. history/messages：会话工作记忆

主程序初始化：

```python
history = []
```

用户每次输入后：

```python
history.append({"role": "user", "content": query})
agent_loop(history)
```

`history` 和 `agent_loop` 中的 `messages` 是同一个列表对象。

它会不断追加：

```text
用户请求
模型文本
模型 tool_use
工具 tool_result
后续用户请求
```

因此在没有 compact 的版本里，每次模型默认会看到之前的全部会话历史。

## 7. agent_loop 的核心流程

```python
def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        messages.append({
            "role": "assistant",
            "content": response.content,
        })

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = run_bash(block.input["command"])
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

        messages.append({
            "role": "user",
            "content": results,
        })
```

### 第一步：调用模型

```python
response = client.messages.create(...)
```

传入：

```text
MODEL    使用哪个模型
SYSTEM   稳定行为规则
messages 当前完整会话
TOOLS    可用能力
```

### 第二步：保存模型回复

```python
messages.append({
    "role": "assistant",
    "content": response.content,
})
```

必须先保存完整 assistant 回复，因为里面包含：

```text
普通文本块
tool_use 块
tool_use_id
工具参数
```

后续 `tool_result` 需要通过 `tool_use_id` 与它对应。

### 第三步：判断是否结束

```python
if response.stop_reason != "tool_use":
    return
```

结束权在模型：

```text
模型需要继续行动 -> stop_reason == "tool_use"
模型认为可以回答 -> stop_reason 不再是 "tool_use"
```

Harness 不通过固定步骤数判断任务完成。

### 第四步：执行所有工具调用

```python
for block in response.content:
    if block.type == "tool_use":
        output = run_bash(block.input["command"])
```

一个模型回复可以包含多个 tool call，因此代码遍历所有 content block。

### 第五步：返回 tool_result

```python
results.append({
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": output,
})
```

`tool_use_id` 是工具调用和工具结果之间的关联键：

```text
assistant tool_use id=abc
user tool_result tool_use_id=abc
```

### 第六步：继续下一轮

```python
messages.append({"role": "user", "content": results})
```

然后 `while True` 回到顶部，模型看到工具结果并决定下一步。

## 8. 一次完整消息序列

```text
user:
  请查看项目并修复错误

assistant:
  tool_use:
    id: tool_1
    name: bash
    input:
      command: "ls"

user:
  tool_result:
    tool_use_id: tool_1
    content: "README.md\nagents\n..."

assistant:
  tool_use:
    id: tool_2
    name: bash
    input:
      command: "python -m pytest"

user:
  tool_result:
    tool_use_id: tool_2
    content: "1 failed..."

assistant:
  最终文本回复
```

最后一轮没有 `tool_use`，`agent_loop` 返回。

## 9. 多轮用户会话

外层 REPL 不会在一次 `agent_loop` 结束后清空 history：

```python
while True:
    query = input(...)
    history.append({"role": "user", "content": query})
    agent_loop(history)
```

所以：

```text
一次 agent_loop 结束，只代表当前请求完成。
整个 history 仍然保留。
用户继续输入后，模型能看到前面的会话。
```

程序退出条件：

```python
if query.strip().lower() in ("q", "exit", ""):
    break
```

## 10. 异常和安全边界

当前实现只有非常轻量的边界：

```text
危险字符串过滤。
命令执行超时。
工具输出长度截断。
```

它没有：

```text
真正的进程沙箱。
命令审批。
细粒度文件权限。
退出码结构化处理。
完整异常捕获。
调用轮数上限。
上下文压缩。
```

所以这是教学内核，不是完整生产运行时。

## 11. 后续组件如何接入

后续组件都不需要改变核心模式：

```text
Todo       新增一个状态工具。
Subagent   新增一个背后运行子 loop 的 task 工具。
Skill      新增 load_skill 工具。
Compact    在模型调用前维护 messages。
TaskSystem 新增持久化任务工具。
Background 在模型调用前注入异步结果。
AgentTeam  新增队友和消息工具。
Protocol   新增结构化请求响应工具。
```

稳定内核始终是：

```text
messages -> model -> tool_use -> execute -> tool_result -> messages
```

## 总结

Agent Loop 只负责四件事：

```text
调用模型。
保存模型回复。
执行模型请求的工具。
把结果送回模型。
```

真正的任务决策由模型完成。后续所有 harness 能力，都是围绕这个循环扩展模型可以观察和采取的行动。
