# Agent Team 实现逻辑

本文说明 `agents/s09_agent_teams.py` 中 AgentTeam 的实现：队友如何创建、每个队友如何运行独立 agent loop、团队配置如何持久化，以及成员如何通过 JSONL inbox 通信。

## 1. 直观理解

s04 的 Subagent 是一次性委派：

```text
spawn
  -> 执行子任务
  -> 返回 summary
  -> 上下文销毁
```

s09 的 Teammate 是有身份的团队成员：

```text
spawn
  -> working
  -> 完成一轮工作
  -> idle
  -> 可以再次 spawn
```

每个 teammate 有：

```text
名字
角色
状态
自己的 messages
自己的 inbox
自己的后台线程
```

团队成员共享：

```text
同一个模型配置
同一个文件系统 WORKDIR
同一套基础工具实现
文件式消息总线
```

## 2. 文件结构

```text
.team/
  config.json
  inbox/
    lead.jsonl
    alice.jsonl
    bob.jsonl
```

`config.json`：

```json
{
  "team_name": "default",
  "members": [
    {
      "name": "alice",
      "role": "coder",
      "status": "idle"
    }
  ]
}
```

Inbox 文件是一行一条 JSON：

```json
{"type":"message","from":"lead","content":"Fix auth bug","timestamp":...}
{"type":"broadcast","from":"lead","content":"Run tests","timestamp":...}
```

## 3. 核心组件

```python
class MessageBus
class TeammateManager

BUS = MessageBus(INBOX_DIR)
TEAM = TeammateManager(TEAM_DIR)
```

职责分工：

```text
MessageBus
  负责消息持久化和传输。

TeammateManager
  负责成员配置、状态和线程生命周期。

Lead agent_loop
  负责主 agent 的决策和工具调用。

Teammate _teammate_loop
  负责每个队友自己的模型循环。
```

## 4. VALID_MSG_TYPES

```python
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}
```

s09 真正使用的主要是：

```text
message
broadcast
```

其他类型是为 s10 协议预留。

消息类型的作用不是执行逻辑，而是给消息附加语义，方便接收者和后续协议代码区分。

## 5. MessageBus 初始化

```python
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)
```

它确保：

```text
.team/inbox 目录存在。
```

每个成员不需要提前创建 inbox 文件。第一次发送消息时自动产生。

## 6. send：发送消息

```python
def send(
    self,
    sender: str,
    to: str,
    content: str,
    msg_type: str = "message",
    extra: dict = None,
) -> str:
    if msg_type not in VALID_MSG_TYPES:
        return f"Error: Invalid type '{msg_type}'..."

    msg = {
        "type": msg_type,
        "from": sender,
        "content": content,
        "timestamp": time.time(),
    }

    if extra:
        msg.update(extra)

    inbox_path = self.dir / f"{to}.jsonl"
    with open(inbox_path, "a") as f:
        f.write(json.dumps(msg) + "\n")

    return f"Sent {msg_type} to {to}"
```

流程：

```text
校验消息类型。
构建消息对象。
合并额外协议字段。
以 append 模式写入目标成员 inbox。
返回发送结果。
```

使用 append-only JSONL 的好处：

```text
实现简单。
一条消息一行。
容易追加。
容易人工查看。
进程重启后未读消息仍在磁盘。
```

## 7. read_inbox：读取并清空

```python
def read_inbox(self, name: str) -> list:
    inbox_path = self.dir / f"{name}.jsonl"
    if not inbox_path.exists():
        return []

    messages = []
    for line in inbox_path.read_text().strip().splitlines():
        if line:
            messages.append(json.loads(line))

    inbox_path.write_text("")
    return messages
```

这是一个 drain 操作：

```text
读取全部消息。
解析 JSON。
清空 inbox 文件。
返回消息列表。
```

同一条消息只会通过正常读取返回一次。

注意：读取和清空不是原子操作。并发发送时可能出现竞争。

## 8. broadcast：广播

```python
def broadcast(
    self,
    sender: str,
    content: str,
    teammates: list,
) -> str:
    count = 0
    for name in teammates:
        if name != sender:
            self.send(
                sender,
                name,
                content,
                "broadcast",
            )
            count += 1
    return f"Broadcast to {count} teammates"
```

广播不是特殊共享频道，而是循环调用 `send`，给每个成员 inbox 分别写一条消息。

## 9. TeammateManager 初始化

```python
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}
```

内部状态：

```text
config
  团队名和成员状态，落盘。

threads
  当前进程中 name -> Thread 的映射，不落盘。
```

所以成员身份可以跨进程保留，但线程不能恢复。

## 10. 配置加载与保存

```python
def _load_config(self) -> dict:
    if self.config_path.exists():
        return json.loads(self.config_path.read_text())
    return {"team_name": "default", "members": []}

def _save_config(self):
    self.config_path.write_text(
        json.dumps(self.config, indent=2)
    )
```

如果没有配置文件：

```text
创建默认内存配置。
真正保存发生在第一次 spawn 或状态变化时。
```

## 11. _find_member

```python
def _find_member(self, name: str) -> dict:
    for m in self.config["members"]:
        if m["name"] == name:
            return m
    return None
```

成员名是团队内部身份键。

当前实现没有单独成员 ID，也没有检查大小写冲突。

## 12. spawn：创建或重新启动队友

```python
def spawn(
    self,
    name: str,
    role: str,
    prompt: str,
) -> str:
    member = self._find_member(name)

    if member:
        if member["status"] not in ("idle", "shutdown"):
            return (
                f"Error: '{name}' is currently "
                f"{member['status']}"
            )
        member["status"] = "working"
        member["role"] = role
    else:
        member = {
            "name": name,
            "role": role,
            "status": "working",
        }
        self.config["members"].append(member)

    self._save_config()

    thread = threading.Thread(
        target=self._teammate_loop,
        args=(name, role, prompt),
        daemon=True,
    )
    self.threads[name] = thread
    thread.start()

    return f"Spawned '{name}' (role: {role})"
```

分两种情况：

```text
新成员
  写入 name / role / working。

已有成员
  只有 idle 或 shutdown 状态可以重新启动。
  working 状态不能重复 spawn。
```

然后创建 daemon thread，线程中运行 `_teammate_loop`。

## 13. teammate 的 messages 如何初始化

```python
def _teammate_loop(
    self,
    name: str,
    role: str,
    prompt: str,
):
    sys_prompt = (
        f"You are '{name}', role: {role}, at {WORKDIR}. "
        "Use send_message to communicate. "
        "Complete your task."
    )

    messages = [{
        "role": "user",
        "content": prompt,
    }]
```

每次 spawn 都创建新的 `messages`：

```text
队友不会继承 lead history。
也不会恢复同名队友上一次运行的 messages。
```

持久的是成员元数据和 inbox，不是会话上下文。

因此这里的“persistent teammate”更准确地说是：

```text
身份持久。
消息邮箱持久。
单次运行上下文不持久。
```

## 14. teammate loop

```python
tools = self._teammate_tools()

for _ in range(50):
    inbox = BUS.read_inbox(name)
    for msg in inbox:
        messages.append({
            "role": "user",
            "content": json.dumps(msg),
        })

    response = client.messages.create(
        model=MODEL,
        system=sys_prompt,
        messages=messages,
        tools=tools,
        max_tokens=8000,
    )

    messages.append({
        "role": "assistant",
        "content": response.content,
    })

    if response.stop_reason != "tool_use":
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            output = self._exec(
                name,
                block.name,
                block.input,
            )
            results.append({...})

    messages.append({
        "role": "user",
        "content": results,
    })
```

每轮先 drain inbox，再调用模型。

Inbox 消息被作为普通 user message JSON 字符串追加：

```json
{
  "type": "message",
  "from": "lead",
  "content": "Please inspect auth.py",
  "timestamp": 1234567890
}
```

最多运行 50 轮，避免无限循环。

## 15. teammate 可用工具

```text
bash
read_file
write_file
edit_file
send_message
read_inbox
```

没有：

```text
spawn_teammate
list_teammates
broadcast
```

这些团队管理能力只暴露给 lead。

## 16. _exec：队友工具分发

```python
def _exec(
    self,
    sender: str,
    tool_name: str,
    args: dict,
) -> str:
    if tool_name == "bash":
        return _run_bash(args["command"])
    if tool_name == "read_file":
        return _run_read(args["path"])
    if tool_name == "write_file":
        return _run_write(
            args["path"],
            args["content"],
        )
    if tool_name == "edit_file":
        return _run_edit(
            args["path"],
            args["old_text"],
            args["new_text"],
        )
    if tool_name == "send_message":
        return BUS.send(
            sender,
            args["to"],
            args["content"],
            args.get("msg_type", "message"),
        )
    if tool_name == "read_inbox":
        return json.dumps(
            BUS.read_inbox(sender),
            indent=2,
        )
    return f"Unknown tool: {tool_name}"
```

这里没有使用统一字典，而是 `if` 分支，因为发送者身份 `sender` 需要动态注入。

## 17. 队友如何结束一轮工作

如果模型不再调用工具：

```python
if response.stop_reason != "tool_use":
    break
```

退出循环后：

```python
member = self._find_member(name)
if member and member["status"] != "shutdown":
    member["status"] = "idle"
    self._save_config()
```

s09 还没有真正的 shutdown 协议，因此正常完成后变为 `idle`。

Lead 后续可以用相同名字再次 `spawn`，给它新的 prompt 和新的 messages。

## 18. Lead 的团队工具

Lead 的 `TOOL_HANDLERS` 增加：

```text
spawn_teammate
list_teammates
send_message
read_inbox
broadcast
```

关系：

```python
"spawn_teammate": lambda **kw: TEAM.spawn(...),
"list_teammates": lambda **kw: TEAM.list_all(),
"send_message": lambda **kw: BUS.send("lead", ...),
"read_inbox": lambda **kw: json.dumps(
    BUS.read_inbox("lead"),
    indent=2,
),
"broadcast": lambda **kw: BUS.broadcast(
    "lead",
    kw["content"],
    TEAM.member_names(),
),
```

Lead 通过这些工具管理团队，不直接操作线程或文件。

## 19. Lead 如何自动接收 inbox

主 `agent_loop` 每轮模型调用前：

```python
inbox = BUS.read_inbox("lead")

if inbox:
    messages.append({
        "role": "user",
        "content": (
            f"<inbox>{json.dumps(inbox, indent=2)}</inbox>"
        ),
    })
    messages.append({
        "role": "assistant",
        "content": "Noted inbox messages.",
    })
```

所以 teammate 给 lead 发消息后，lead 会在下一次模型调用前看到。

和 BackgroundTask 一样，如果 lead 的 agent_loop 已经返回，消息不会主动唤醒模型，要等下一次进入 loop。

## 20. Subagent 与 Teammate 的区别

```text
Subagent
  一次性函数调用。
  父 agent 阻塞等待。
  返回一个 summary。
  没有名字和 inbox。

Teammate
  后台线程运行。
  有名字、角色、状态。
  通过 inbox 异步通信。
  完成后变为 idle，可再次启动。
```

但 s09 teammate 仍没有长期恢复自己的对话上下文。

## 21. 与 TaskSystem 的关系

s09 代码没有直接集成 s07 TaskSystem。

真实组合系统中：

```text
TaskSystem
  保存共享工作板。

AgentTeam
  提供执行者和通信。

Lead
  创建任务、分配任务或通知成员。

Teammate
  更新任务状态并发回结果。
```

到 s11 才进一步加入队友自主扫描和认领任务。

## 22. 并发风险

所有 teammate 共享同一个 `WORKDIR`：

```text
alice 可能编辑 auth.py。
bob 也可能编辑 auth.py。
lead 同时也可能编辑 auth.py。
```

风险：

```text
写入覆盖。
读取到中间状态。
测试互相影响。
git 工作区冲突。
```

s09 只有上下文隔离和线程并发，没有目录隔离。s12 才使用 worktree 解决这个问题。

## 23. 当前实现边界

教学版缺少：

```text
Inbox 文件锁和原子 drain。
线程恢复。
队友 messages 持久化。
真正的 shutdown。
成员删除。
心跳和健康检查。
错误状态记录。
并发数量限制。
独立工作目录。
消息确认和重试。
```

此外，配置状态可能与真实线程状态不一致，例如进程异常退出后 `config.json` 仍显示 working。

## 总结

AgentTeam 由两个核心对象组成：

```text
MessageBus
  用 JSONL inbox 提供异步通信。

TeammateManager
  用 config.json 保存身份状态，
  用线程运行每个成员自己的 agent loop。
```

它把一次性 Subagent 扩展为有名字、有角色、有邮箱的协作成员，但仍共享文件系统，且单次会话上下文不会跨 spawn 恢复。
