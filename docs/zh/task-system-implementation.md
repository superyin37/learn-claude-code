# Task System 实现逻辑

本文说明 `agents/s07_task_system.py` 中 TaskSystem 的实现。重点包括任务如何持久化、ID 如何生成、依赖图如何维护，以及它与 TodoList、Context Compact 和后续 AgentTeam 的关系。

## 1. 直观理解

TodoList 更像 agent 当前桌面上的便签：

```text
[ ] 读取文件
[>] 修改代码
[ ] 运行测试
```

TaskSystem 更像项目级任务板：

```text
#1 实现数据库层       completed
#2 实现 API           blocked by #1
#3 编写集成测试       blocked by #2
```

核心区别：

```text
TodoList
  当前会话、内存状态、执行步骤。

TaskSystem
  跨会话、磁盘状态、任务和依赖图。
```

TaskSystem 的目标是让任务状态独立于 `messages`。即使会话被 compact，任务文件仍然存在。

## 2. 文件结构

```text
.tasks/
  task_1.json
  task_2.json
  task_3.json
```

一个任务文件：

```json
{
  "id": 2,
  "subject": "Implement API",
  "description": "Add user endpoints",
  "status": "pending",
  "blockedBy": [1],
  "blocks": [3],
  "owner": ""
}
```

字段：

```text
id
  数字任务 ID。

subject
  简短任务标题。

description
  详细说明。

status
  pending / in_progress / completed。

blockedBy
  当前任务依赖哪些任务。

blocks
  当前任务阻塞哪些任务。

owner
  预留的负责人字段，s07 尚未提供 owner 更新工具。
```

## 3. TaskManager 初始化

```python
class TaskManager:
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1
```

创建实例：

```python
TASKS_DIR = WORKDIR / ".tasks"
TASKS = TaskManager(TASKS_DIR)
```

初始化过程：

```text
确保 .tasks 目录存在。
扫描已有 task_*.json。
找到最大 ID。
下一个任务 ID = 最大 ID + 1。
```

因此程序重启后不会从 1 重新编号。

## 4. _max_id：恢复 ID 序列

```python
def _max_id(self) -> int:
    ids = [
        int(f.stem.split("_")[1])
        for f in self.dir.glob("task_*.json")
    ]
    return max(ids) if ids else 0
```

例如目录里有：

```text
task_1.json
task_4.json
```

那么：

```text
_max_id() = 4
_next_id = 5
```

当前实现不会自动填补中间缺失的 ID。

## 5. _load 和 _save：持久化基础

读取：

```python
def _load(self, task_id: int) -> dict:
    path = self.dir / f"task_{task_id}.json"
    if not path.exists():
        raise ValueError(f"Task {task_id} not found")
    return json.loads(path.read_text())
```

保存：

```python
def _save(self, task: dict):
    path = self.dir / f"task_{task['id']}.json"
    path.write_text(json.dumps(task, indent=2))
```

TaskSystem 没有把所有任务长期保存在一个内存列表里，而是每次从磁盘读取、修改、写回。

因此：

```text
messages 被压缩不会删除任务。
程序重启后仍可恢复任务。
其他进程理论上也能读取这些文件。
```

## 6. create：创建任务

```python
def create(self, subject: str, description: str = "") -> str:
    task = {
        "id": self._next_id,
        "subject": subject,
        "description": description,
        "status": "pending",
        "blockedBy": [],
        "blocks": [],
        "owner": "",
    }
    self._save(task)
    self._next_id += 1
    return json.dumps(task, indent=2)
```

创建时默认：

```text
status = pending
blockedBy = []
blocks = []
owner = ""
```

返回完整 JSON 字符串，作为 `tool_result` 给模型。

## 7. get 和 list_all

获取单个任务：

```python
def get(self, task_id: int) -> str:
    return json.dumps(self._load(task_id), indent=2)
```

列出所有任务：

```python
def list_all(self) -> str:
    tasks = []
    for f in sorted(self.dir.glob("task_*.json")):
        tasks.append(json.loads(f.read_text()))

    if not tasks:
        return "No tasks."

    lines = []
    for t in tasks:
        marker = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
        }.get(t["status"], "[?]")
        blocked = (
            f" (blocked by: {t['blockedBy']})"
            if t.get("blockedBy")
            else ""
        )
        lines.append(
            f"{marker} #{t['id']}: {t['subject']}{blocked}"
        )
    return "\n".join(lines)
```

输出：

```text
[x] #1: Implement database layer
[ ] #2: Implement API (blocked by: [1])
[ ] #3: Write integration tests (blocked by: [2])
```

注意：文件通过文件名字符串排序，所以 `task_10.json` 可能排在 `task_2.json` 前面。这是教学实现的小问题。

## 8. update：更新状态和依赖

```python
def update(
    self,
    task_id: int,
    status: str = None,
    add_blocked_by: list = None,
    add_blocks: list = None,
) -> str:
    task = self._load(task_id)
    ...
```

它支持三类更新：

```text
修改 status。
增加 blockedBy。
增加 blocks。
```

### 状态更新

```python
if status:
    if status not in ("pending", "in_progress", "completed"):
        raise ValueError(f"Invalid status: {status}")
    task["status"] = status

    if status == "completed":
        self._clear_dependency(task_id)
```

当任务完成时，会自动解除它对其他任务的阻塞。

### 增加 blockedBy

```python
if add_blocked_by:
    task["blockedBy"] = list(
        set(task["blockedBy"] + add_blocked_by)
    )
```

使用集合去重，但集合转换回列表后顺序可能不稳定。

注意：直接增加 `blockedBy` 不会反向更新前置任务的 `blocks`。

### 增加 blocks

```python
if add_blocks:
    task["blocks"] = list(
        set(task["blocks"] + add_blocks)
    )

    for blocked_id in add_blocks:
        try:
            blocked = self._load(blocked_id)
            if task_id not in blocked["blockedBy"]:
                blocked["blockedBy"].append(task_id)
                self._save(blocked)
        except ValueError:
            pass
```

这个方向会维护双向关系：

```text
task #1 blocks #2
  -> #1.blocks 加入 2
  -> #2.blockedBy 加入 1
```

如果被阻塞任务不存在，当前实现直接忽略。

## 9. _clear_dependency：完成后解除阻塞

```python
def _clear_dependency(self, completed_id: int):
    for f in self.dir.glob("task_*.json"):
        task = json.loads(f.read_text())
        if completed_id in task.get("blockedBy", []):
            task["blockedBy"].remove(completed_id)
            self._save(task)
```

例如：

```text
#2.blockedBy = [1]
```

当 #1 更新为 completed：

```text
扫描全部任务。
找到 blockedBy 中包含 1 的任务。
移除 1。
保存任务。
```

结果：

```text
#2.blockedBy = []
```

这表示 #2 已满足开工条件。

## 10. Ready 如何判断

s07 没有显式的 `ready` 字段或 `task_ready` 函数。

可以从状态推导：

```text
status == pending
且 blockedBy 为空
```

也就是：

```python
ready = (
    task["status"] == "pending"
    and not task["blockedBy"]
)
```

当前代码只是通过 `task_list` 显示 blockedBy，让模型自行判断哪些任务可执行。

## 11. 工具接口

```python
TOOL_HANDLERS = {
    "task_create": lambda **kw: TASKS.create(
        kw["subject"],
        kw.get("description", ""),
    ),
    "task_update": lambda **kw: TASKS.update(
        kw["task_id"],
        kw.get("status"),
        kw.get("addBlockedBy"),
        kw.get("addBlocks"),
    ),
    "task_list": lambda **kw: TASKS.list_all(),
    "task_get": lambda **kw: TASKS.get(kw["task_id"]),
}
```

模型可用工具：

```text
task_create
task_update
task_list
task_get
```

工具结果仍然会进入 `messages`，但真实任务状态以 `.tasks/*.json` 为准。

## 12. 与 messages 的联动

例如：

```text
assistant:
  tool_use task_create(subject="Implement API")

harness:
  TASKS.create(...)
  写入 .tasks/task_1.json

user:
  tool_result:
    {
      "id": 1,
      "status": "pending",
      ...
    }
```

这里存在两个层次：

```text
messages 中的任务 JSON
  是调用时的状态快照。

.tasks/task_1.json
  是当前真实状态。
```

旧会话里看到的 JSON 可能已经过时，因此模型需要使用 `task_get` 或 `task_list` 重新读取当前状态。

## 13. 与 Context Compact 的关系

TaskSystem 的核心价值是状态在会话之外：

```text
auto_compact 可以替换全部 messages。
.tasks 文件不会受影响。
压缩后模型调用 task_list，即可恢复任务板状态。
```

因此：

```text
messages 负责当前思考过程。
TaskSystem 负责长期任务事实。
```

## 14. 与 TodoList 的关系

从教学演进看，TaskSystem 是 TodoList 的升级方向。

从真实架构看，两者可以并列：

```text
TaskSystem 管“有哪些任务需要完成”。
TodoList 管“我现在如何完成手上的任务”。
```

例如：

```text
TaskSystem:
  #12 修复登录 bug

TodoList:
  [x] 阅读 auth.py
  [>] 修复 token 校验
  [ ] 运行测试
  [ ] 更新 task #12 为 completed
```

典型联动：

```text
task_get #12
task_update #12 status=in_progress
  -> 创建当前会话 TodoList
  -> 执行具体步骤
task_update #12 status=completed
```

## 15. 与 AgentTeam 的关系

s07 尚未接入 AgentTeam，但 TaskSystem 已具备共享状态基础：

```text
多个 agent 可以读取同一个 .tasks 目录。
任务中预留 owner 字段。
任务依赖可用于并行调度。
```

后续系统可以增加：

```text
owner 更新。
原子认领。
ready task 扫描。
并发写锁。
任务完成通知。
```

## 16. 如何判断任务完成

单任务完成：

```text
status == completed
```

整个任务图完成：

```text
所有任务 status 都是 completed。
```

但 s07 不会自动终止 `agent_loop`。模型需要：

```text
调用 task_list 检查状态。
完成工作后更新任务。
认为目标完成时停止调用工具。
```

最终循环仍由：

```python
if response.stop_reason != "tool_use":
    return
```

决定结束。

## 17. 当前实现边界

教学版存在这些限制：

```text
没有文件锁，并发更新可能覆盖。
保存不是原子替换。
没有删除任务。
没有移除依赖的独立 API。
addBlockedBy 不维护反向 blocks。
不存在的 addBlocks 目标被静默忽略。
没有循环依赖检测。
没有显式 ready 查询。
owner 字段无法通过工具更新。
文件名是字符串排序。
```

生产系统应增加：

```text
事务或锁。
依赖图一致性校验。
循环检测。
原子认领。
事件日志。
任务版本号。
失败和取消状态。
```

## 总结

TaskSystem 把任务从会话内状态升级为磁盘任务图：

```text
TaskManager 负责 CRUD。
.tasks/*.json 保存真实状态。
blockedBy / blocks 表达依赖。
任务完成时自动解除下游阻塞。
task_* tools 让模型读写任务板。
```

它让任务状态可以跨会话、跨 compact，并为后续多 agent 协作提供共享协调基础。
