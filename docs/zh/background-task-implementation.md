# Background Task 实现逻辑

本文说明 `agents/s08_background_tasks.py` 中后台任务的实现：命令如何在线程中执行、状态如何维护、完成通知如何进入主会话，以及阻塞工具与后台工具的区别。

## 1. 直观理解

普通 `bash` 是阻塞执行：

```text
模型调用 bash
  -> harness 等命令结束
  -> 返回结果
  -> 模型才能继续
```

后台任务是异步执行：

```text
模型调用 background_run
  -> harness 启动线程
  -> 立即返回 task_id
  -> 模型继续处理其他工作

后台命令完成
  -> 写入通知队列
  -> 下一轮模型调用前注入结果
```

核心价值：

```text
等待慢命令时，agent 不必停止思考和行动。
```

## 2. 核心组件

```python
class BackgroundManager

BG = BackgroundManager()

background_run
check_background

tasks
_notification_queue
_lock
```

关系：

```text
background_run tool
  -> BG.run(command)
  -> 创建 daemon thread
  -> BG._execute(task_id, command)
  -> 更新 tasks
  -> 推送 notification

agent_loop
  -> BG.drain_notifications()
  -> 结果注入 messages
```

## 3. 初始化

```python
class BackgroundManager:
    def __init__(self):
        self.tasks = {}
        self._notification_queue = []
        self._lock = threading.Lock()
```

`tasks` 保存全部后台任务状态：

```python
{
    "a1b2c3d4": {
        "status": "running",
        "result": None,
        "command": "python -m pytest",
    }
}
```

`_notification_queue` 保存尚未注入主会话的完成通知。

`_lock` 保护通知队列的并发访问。

## 4. run：启动后台任务

```python
def run(self, command: str) -> str:
    task_id = str(uuid.uuid4())[:8]
    self.tasks[task_id] = {
        "status": "running",
        "result": None,
        "command": command,
    }

    thread = threading.Thread(
        target=self._execute,
        args=(task_id, command),
        daemon=True,
    )
    thread.start()

    return (
        f"Background task {task_id} started: "
        f"{command[:80]}"
    )
```

流程：

```text
生成 8 位 task_id。
在 tasks 中记录 running 状态。
创建后台线程。
线程目标是 _execute。
立即启动线程。
不等待命令完成，马上返回 task_id。
```

`daemon=True` 表示主进程退出时，不会等待后台线程完成。

## 5. _execute：线程中的真实执行

```python
def _execute(self, task_id: str, command: str):
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = (r.stdout + r.stderr).strip()[:50000]
        status = "completed"
    except subprocess.TimeoutExpired:
        output = "Error: Timeout (300s)"
        status = "timeout"
    except Exception as e:
        output = f"Error: {e}"
        status = "error"

    self.tasks[task_id]["status"] = status
    self.tasks[task_id]["result"] = output or "(no output)"

    with self._lock:
        self._notification_queue.append({...})
```

状态：

```text
running
completed
timeout
error
```

注意：当前代码只要 `subprocess.run` 正常返回，就标记为 `completed`，即使命令退出码非 0。

也就是说：

```text
completed 表示进程执行结束。
不一定表示业务成功。
```

命令结果完整保存在 `tasks[task_id]["result"]`，最多 50000 字符。

通知队列只保存前 500 字符：

```python
{
    "task_id": task_id,
    "status": status,
    "command": command[:80],
    "result": result[:500],
}
```

## 6. 为什么需要两个结果存储位置

```text
tasks
  保存完整任务状态和较完整结果。

_notification_queue
  保存等待注入会话的简短通知。
```

这样既能：

```text
自动告诉模型任务完成了。
又能让模型通过 check_background 获取完整结果。
```

不过当前自动通知只截断到 500 字符，模型看到预览后需要主动查询详细结果。

## 7. check：主动查询状态

```python
def check(self, task_id: str = None) -> str:
    if task_id:
        t = self.tasks.get(task_id)
        if not t:
            return f"Error: Unknown task {task_id}"
        return (
            f"[{t['status']}] {t['command'][:60]}\n"
            f"{t.get('result') or '(running)'}"
        )

    lines = []
    for tid, t in self.tasks.items():
        lines.append(
            f"{tid}: [{t['status']}] {t['command'][:60]}"
        )
    return "\n".join(lines) if lines else "No background tasks."
```

两种模式：

```text
传 task_id
  返回单个任务状态和完整结果。

不传 task_id
  返回所有后台任务摘要。
```

## 8. drain_notifications：消费完成通知

```python
def drain_notifications(self) -> list:
    with self._lock:
        notifs = list(self._notification_queue)
        self._notification_queue.clear()
    return notifs
```

“drain” 表示：

```text
读取全部待处理通知。
同时清空队列。
同一通知只自动注入一次。
```

使用锁是因为：

```text
后台线程可能正在 append。
主线程可能正在读取和 clear。
```

当前锁只保护通知队列，没有保护 `tasks` 字典。

## 9. 工具接口

```python
TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    ...
    "background_run": (
        lambda **kw: BG.run(kw["command"])
    ),
    "check_background": (
        lambda **kw: BG.check(kw.get("task_id"))
    ),
}
```

模型看到的声明：

```text
bash
  Run a shell command (blocking).

background_run
  Run command in background thread.
  Returns task_id immediately.

check_background
  Check one task or list all.
```

模型需要根据命令类型选择：

```text
短命令、立即需要结果 -> bash
测试、构建、长任务     -> background_run
```

## 10. agent_loop 如何注入通知

每次模型调用前：

```python
notifs = BG.drain_notifications()

if notifs and messages:
    notif_text = "\n".join(
        f"[bg:{n['task_id']}] "
        f"{n['status']}: {n['result']}"
        for n in notifs
    )

    messages.append({
        "role": "user",
        "content": (
            "<background-results>\n"
            f"{notif_text}\n"
            "</background-results>"
        ),
    })

    messages.append({
        "role": "assistant",
        "content": "Noted background results.",
    })
```

注入结果类似：

```text
<background-results>
[bg:a1b2c3d4] completed: 42 tests passed...
</background-results>
```

然后加一条 assistant acknowledgement：

```text
Noted background results.
```

这使消息角色保持交替，同时告诉后续模型这条通知已进入上下文。

## 11. 通知什么时候能被模型看到

通知只在 `agent_loop` 准备发起下一次模型调用时被 drain。

存在两种情况：

### 当前 agent_loop 仍在运行

如果模型持续调用其他工具：

```text
后台任务完成
  -> 下一次 while 循环顶部
  -> drain_notifications
  -> 立即注入
```

### 当前 agent_loop 已经返回

如果模型先给出最终文本，agent_loop 已结束：

```text
后台任务稍后完成
  -> 不会主动唤醒模型
  -> 等用户下一次输入并再次进入 agent_loop
  -> 通知才被注入
```

所以当前实现不是事件驱动唤醒，而是“下一轮调用前检查”。

## 12. 一次完整时间线

```text
模型:
  background_run("python -m pytest")

harness:
  创建 task a1b2c3d4
  启动线程
  立即返回 task_id

模型:
  read_file("README.md")

后台线程:
  pytest 执行完成
  tasks[a1b2c3d4] = completed
  notification_queue.append(...)

agent_loop 下一轮:
  drain_notifications
  注入 background-results

模型:
  根据测试结果继续修复或总结
```

## 13. 与 TaskSystem 的区别

两者都叫 task，但职责不同：

```text
TaskSystem task
  项目工作单元。
  保存在 .tasks。
  跨会话持久化。

Background task
  一个正在后台运行的命令。
  保存在进程内存。
  进程退出后丢失。
```

更好的命名可以是：

```text
TaskSystem: work item
BackgroundManager: job
```

## 14. 与 messages 的关系

后台状态有三种表现：

```text
background_run 的立即结果
  "Background task ... started"

自动 completion notification
  <background-results>...</background-results>

check_background 的查询结果
  完整状态和输出
```

这些都会进入 `messages`，但 `BG.tasks` 才是当前进程中的真实任务状态。

## 15. 并发和共享文件系统风险

多个后台命令可能并行运行：

```text
线程 A 跑测试。
线程 B 跑构建。
主 agent 同时编辑文件。
```

这可能造成：

```text
测试读取到编辑中间状态。
两个命令同时写同一文件。
输出目录互相覆盖。
```

s08 只提供执行并发，没有工作区隔离。后续 s12 的 worktree 才解决目录级并行冲突。

## 16. 当前实现边界

教学版缺少：

```text
任务取消。
线程池和并发上限。
进程退出后的任务恢复。
任务状态落盘。
退出码区分成功/失败。
持续日志流。
主动唤醒模型。
tasks 字典完整加锁。
危险命令过滤。
独立工作目录。
```

另外 `background_run` 直接调用 `_execute`，没有复用 `run_bash` 的危险字符串检查。

生产系统需要更严格的命令权限和资源治理。

## 总结

Background Task 的核心结构：

```text
BG.run
  创建 ID、记录 running、启动线程、立即返回。

BG._execute
  在线程中执行命令、更新状态、写通知队列。

BG.check
  主动查询任务。

BG.drain_notifications
  在模型调用前把完成结果注入 messages。
```

它把“等待慢命令”从主 agent loop 中拆出去，让模型可以继续处理其他工作，但并没有自动解决并发文件冲突和持久化恢复问题。
