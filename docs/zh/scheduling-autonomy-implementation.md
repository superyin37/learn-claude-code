# 后台执行、调度与 Autonomous Agent 实现逻辑

本文按“持续运行”板块说明三个相互连接的机制：

- `s13_background_tasks/code.py`：慢操作后台执行。
- `s14_cron_scheduler/code.py`：按时间触发工作。
- `s17_autonomous_agents/code.py`：队友空闲时主动认领工作。

三者分别回答：

```text
任务很慢怎么办？
未来某个时间再执行怎么办？
没有人分配时，Agent 如何自己找活？
```

## 1. 共同架构

三个机制都避免让主 `agent_loop` 承担等待：

```text
生产者
  -> 共享状态或队列
  -> Agent 空闲时消费
  -> 注入 messages
```

后台线程、cron 线程和 idle polling 都不替代模型决策。它们只负责让新的工作信号在正确时间进入上下文。

## 2. Background Task

### 2.1 判断是否后台执行

`is_slow_operation()` 根据工具名和参数识别慢操作，例如安装依赖、运行测试或显式要求后台运行。

`should_run_background()` 决定：

- 当前工具是否适合后台。
- 用户或模型是否要求后台。
- 是否应继续阻塞主循环。

### 2.2 启动线程

`start_background_task(block)`：

1. 生成 task id。
2. 在内存中记录 running 状态。
3. 创建 daemon thread。
4. 立即返回 placeholder `tool_result`。

```text
tool_use bash
  -> start_background_task
  -> "[Background task started...]"
  -> 主 Agent 继续下一轮
```

后台线程完成后，不直接修改正在运行的模型请求，而是把结果写入完成队列。

### 2.3 完成通知

`collect_background_results()` 排空已完成结果。综合版把它们包装为：

```xml
<task_notification>
...
</task_notification>
```

并在下一轮 LLM 调用前加入 `messages`。

关键点是通知只注入一次。读取队列后必须删除已消费项，否则模型会反复收到同一个完成事件。

## 3. Cron Scheduler

### 3.1 CronJob

`CronJob` 保存：

```text
id
cron
prompt
recurring
durable
```

`recurring=False` 表示一次性任务；`durable=True` 表示定义写入 `.scheduled_tasks.json`，进程重启后重新加载。

Durable 只持久化任务定义。Agent 进程关闭时，daemon scheduler 不会继续运行。

### 3.2 表达式匹配

五段 cron：

```text
minute hour day-of-month month day-of-week
```

支持：

- `*`
- `*/N`
- 单值
- 范围
- 逗号列表

`validate_cron()` 在注册和加载时检查表达式，避免坏任务进入调度循环。

### 3.3 调度线程

`cron_scheduler_loop()` 每秒运行一次：

```text
读取当前时间
  -> 遍历 scheduled_jobs
  -> cron_matches
  -> cron_queue.append(job)
```

`minute_marker` 记录某 job 最近触发的具体日期和分钟，防止轮询线程在同一分钟内重复入队。

一次性任务触发后从注册表删除；durable job 同时更新磁盘文件。

### 3.4 Queue Processor

时间匹配和 Agent 执行是两个阶段：

```text
Scheduler：只负责到点入队
Queue Processor：只负责 Agent 空闲时交付
Agent Loop：只负责消费并执行 prompt
```

`agent_lock.acquire(blocking=False)` 避免 cron 在用户交互正在执行时并发进入同一个历史记录。

交付时加入：

```text
[Scheduled] <job.prompt>
```

然后像普通用户请求一样运行一轮 Agent。

## 4. Autonomous Agent

### 4.1 为什么需要 IDLE 阶段

普通队友完成当前任务后退出。Autonomous Agent 增加：

```text
WORK
  -> IDLE
       -> inbox 有消息：回 WORK
       -> 看板有任务：claim 后回 WORK
       -> 超时：SHUTDOWN
```

因此 Lead 只需要建立任务看板和启动成员，不必逐项手工分配。

### 4.2 scan_unclaimed_tasks

可认领任务必须满足：

```text
status == pending
owner 为空
blockedBy 全部完成
```

`can_start(task_id)` 检查依赖状态。被依赖阻塞的任务不会提前认领。

### 4.3 idle_poll

默认参数：

```text
IDLE_POLL_INTERVAL = 5 秒
IDLE_TIMEOUT = 60 秒
```

每次轮询顺序固定：

1. 先检查 inbox。
2. 处理 shutdown 等协议消息。
3. 再扫描可认领任务。
4. claim 成功后返回 WORK。

Inbox 优先于任务板，因为控制消息和关机请求比新任务更紧急。

### 4.4 Claim 的竞争

`claim_task()` 检查任务状态和 owner，再执行：

```text
owner = agent_name
status = in_progress
```

教学版没有跨进程文件锁，因此两个 Agent 仍可能同时读到 pending 并竞争写入。owner 检查减少问题，但没有消除 read-modify-write race。

## 5. 三种信号如何进入 Agent

| 来源 | 产生方式 | 进入上下文 |
|---|---|---|
| Background | 工作线程完成 | `<task_notification>` |
| Cron | 调度线程时间匹配 | `[Scheduled] ...` |
| Autonomous | idle poll 发现任务 | claim 结果和任务描述 |

这些信号最后都转化为 `messages` 中的事件，模型仍通过相同 agent loop 决定下一步。

## 6. 与 Task System 的连接

Background task 是一次工具执行状态，不等于持久 Task。

```text
Task System：目标、依赖、owner、完成状态
Background Task：某次慢工具调用的运行状态
Cron Job：未来产生工作请求的时间规则
Autonomous Agent：从 Task System 主动消费工作
```

不要用后台 task id 替代任务图中的 task id。

## 7. 与 Team Protocol 的连接

Autonomous Agent 在 WORK 和 IDLE 阶段都必须处理 shutdown request。

协议消息通过 inbox 到达；状态机决定它是普通工作、计划审批还是关机。自治不意味着忽略 Lead 控制。

## 8. 与 Worktree 的连接

任务可以绑定 worktree。队友自动 claim 后读取任务的 `worktree` 字段，并把 bash/read/write 的 cwd 切换到对应目录。

因此完整路径是：

```text
idle poll
  -> claim task
  -> load task.worktree
  -> set teammate cwd
  -> WORK
```

## 9. 当前实现边界

- Background 状态主要在内存中，进程退出后丢失。
- daemon thread 不提供强制取消和资源配额。
- Cron 每秒轮询，不适合超大任务量。
- Durable cron 不代表进程关闭时仍会触发。
- Queue 和任务文件的并发控制较轻。
- Autonomous Agent 使用固定轮询间隔，可能有延迟和空转。
- 自动认领没有优先级、能力匹配和公平调度。
- 多 Agent claim 缺少可靠文件锁。

## 10. 总结

持续运行板块的共同模式是：

```text
不要让主循环等待。
把完成、时间和新任务转换为事件。
在 Agent 可执行时，把事件注入 messages。
```

后台执行解决“等待”，Cron 解决“时间”，Autonomous Agent 解决“分配”。它们共同让 Agent 从一次性问答程序变成可持续工作的运行时。
