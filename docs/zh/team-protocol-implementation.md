# Team Protocol 实现逻辑

本文说明 `agents/s10_team_protocols.py` 中团队协议的实现。重点包括为什么普通消息不足、`request_id` 如何关联请求与响应，以及 Shutdown 和 Plan Approval 两个状态机如何建立。

## 1. 直观理解

s09 已经支持自由消息：

```text
lead -> alice: 请停止工作
alice -> lead: 好的
```

但这种文本存在问题：

```text
“好的”对应哪一个请求？
请求是否仍在等待？
对方是同意还是拒绝？
多条并发请求如何区分？
系统如何自动更新成员状态？
```

s10 把自由文本通信升级为结构化协议：

```text
请求有 request_id。
响应携带同一个 request_id。
状态保存在 tracker。
协议有明确状态转换。
```

核心模式：

```text
request -> pending -> response -> approved/rejected
```

## 2. 在 s09 之上新增的组件

```python
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()

handle_shutdown_request()
handle_plan_review()
_check_shutdown_status()
```

同时扩展：

```text
MessageBus 消息字段。
TeammateManager 的工具集合。
TeammateManager._exec。
Lead 的工具集合。
队友结束状态处理。
```

## 3. VALID_MSG_TYPES

```python
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}
```

协议消息仍然通过同一个 MessageBus 传输。

区别在于消息中增加额外字段：

```json
{
  "type": "shutdown_request",
  "from": "lead",
  "content": "Please shut down gracefully.",
  "timestamp": 123,
  "request_id": "a1b2c3d4"
}
```

## 4. Tracker：协议的内存状态

```python
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()
```

Shutdown tracker 示例：

```python
{
    "a1b2c3d4": {
        "target": "alice",
        "status": "pending",
    }
}
```

Plan tracker 示例：

```python
{
    "e5f6g7h8": {
        "from": "alice",
        "plan": "1. Read auth.py\n2. Add tests",
        "status": "pending",
    }
}
```

`_tracker_lock` 用于保护多个线程同时更新 tracker。

注意：这些 tracker 只存在内存中，进程退出后丢失。

## 5. 为什么 MessageBus 和 Tracker 都需要

```text
MessageBus
  负责把协议消息送给对方。

Tracker
  负责保存协议当前状态，供程序查询和更新。
```

只用 MessageBus：

```text
消息送到了，但当前状态需要重新扫描历史推导。
```

只用 Tracker：

```text
状态存在，但对方不知道有请求。
```

二者结合：

```text
消息负责通知。
Tracker 负责状态。
```

## 6. Shutdown Protocol

目标：

```text
Lead 请求 teammate 优雅停止。
Teammate 可以同意或拒绝。
Lead 可以按 request_id 查询结果。
```

状态机：

```text
pending
  -> approved
  -> rejected
```

## 7. handle_shutdown_request：Lead 发起关机请求

```python
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]

    with _tracker_lock:
        shutdown_requests[req_id] = {
            "target": teammate,
            "status": "pending",
        }

    BUS.send(
        "lead",
        teammate,
        "Please shut down gracefully.",
        "shutdown_request",
        {"request_id": req_id},
    )

    return (
        f"Shutdown request {req_id} sent to "
        f"'{teammate}' (status: pending)"
    )
```

流程：

```text
生成 request_id。
在 tracker 中创建 pending 记录。
向 teammate inbox 写 shutdown_request。
返回 request_id 给 lead 模型。
```

Lead 必须保存或从 messages 中读取该 ID，后续用它查询状态。

## 8. Teammate 如何接收关机请求

队友 loop 每轮先 drain inbox：

```python
inbox = BUS.read_inbox(name)
for msg in inbox:
    messages.append({
        "role": "user",
        "content": json.dumps(msg),
    })
```

队友 system prompt 明确要求：

```text
Respond to shutdown_request with shutdown_response.
```

因此模型看到：

```json
{
  "type": "shutdown_request",
  "request_id": "a1b2c3d4",
  "content": "Please shut down gracefully."
}
```

然后决定调用：

```text
shutdown_response(
  request_id="a1b2c3d4",
  approve=true,
  reason="Work completed"
)
```

## 9. Teammate 的 shutdown_response 处理

在 `_exec` 中：

```python
if tool_name == "shutdown_response":
    req_id = args["request_id"]
    approve = args["approve"]

    with _tracker_lock:
        if req_id in shutdown_requests:
            shutdown_requests[req_id]["status"] = (
                "approved" if approve else "rejected"
            )

    BUS.send(
        sender,
        "lead",
        args.get("reason", ""),
        "shutdown_response",
        {
            "request_id": req_id,
            "approve": approve,
        },
    )

    return (
        "Shutdown approved"
        if approve
        else "Shutdown rejected"
    )
```

它做两件事：

```text
更新 shutdown tracker。
向 lead inbox 发送结构化响应。
```

如果 `request_id` 不存在，当前代码仍然会给 lead 发响应，只是不更新 tracker。

## 10. Teammate 如何真正退出

队友 loop 中：

```python
should_exit = False
```

执行工具后：

```python
if (
    block.name == "shutdown_response"
    and block.input.get("approve")
):
    should_exit = True
```

下一轮循环顶部：

```python
if should_exit:
    break
```

最后更新成员状态：

```python
member["status"] = (
    "shutdown" if should_exit else "idle"
)
```

所以：

```text
approve=true
  -> tracker approved
  -> 回复 lead
  -> teammate loop 退出
  -> member.status = shutdown

approve=false
  -> tracker rejected
  -> 回复 lead
  -> teammate 继续运行
```

## 11. Lead 如何查询关机状态

```python
def _check_shutdown_status(request_id: str) -> str:
    with _tracker_lock:
        return json.dumps(
            shutdown_requests.get(
                request_id,
                {"error": "not found"},
            )
        )
```

Lead 工具名是：

```text
shutdown_response
```

但在 lead 侧它实际执行的是“查询 shutdown request 状态”：

```python
"shutdown_response": lambda **kw: (
    _check_shutdown_status(kw.get("request_id", ""))
)
```

这个命名容易混淆。更清晰的名字应是：

```text
check_shutdown_request
```

## 12. Shutdown 完整时间线

```text
Lead:
  shutdown_request(teammate="alice")

Harness:
  request_id = a1b2c3d4
  tracker[a1b2c3d4] = pending
  inbox/alice.jsonl += shutdown_request

Alice:
  read inbox
  shutdown_response(
    request_id=a1b2c3d4,
    approve=true
  )

Harness:
  tracker[a1b2c3d4] = approved
  inbox/lead.jsonl += shutdown_response
  alice should_exit = true
  alice status = shutdown

Lead:
  read inbox
  或查询 shutdown_response(request_id)
```

## 13. Plan Approval Protocol

目标：

```text
Teammate 在进行重要工作前提交计划。
Lead 审核计划。
Lead 返回 approve/reject 和 feedback。
```

状态机：

```text
pending
  -> approved
  -> rejected
```

与 Shutdown 的方向相反：

```text
Shutdown:
  lead 发请求，teammate 响应。

Plan Approval:
  teammate 发请求，lead 响应。
```

## 14. Teammate 提交计划

队友调用：

```text
plan_approval(plan="...")
```

`_exec` 中：

```python
if tool_name == "plan_approval":
    plan_text = args.get("plan", "")
    req_id = str(uuid.uuid4())[:8]

    with _tracker_lock:
        plan_requests[req_id] = {
            "from": sender,
            "plan": plan_text,
            "status": "pending",
        }

    BUS.send(
        sender,
        "lead",
        plan_text,
        "plan_approval_response",
        {
            "request_id": req_id,
            "plan": plan_text,
        },
    )

    return (
        f"Plan submitted (request_id={req_id}). "
        "Waiting for lead approval."
    )
```

流程：

```text
生成 request_id。
在 plan_requests 中保存 pending。
向 lead inbox 发送计划。
返回等待审批提示。
```

这里的消息类型叫 `plan_approval_response`，但实际发送的是审批请求。命名并不理想。

更清晰的协议应区分：

```text
plan_approval_request
plan_approval_response
```

## 15. Lead 审批计划

Lead 看到 inbox 后调用：

```text
plan_approval(
  request_id="e5f6g7h8",
  approve=true,
  feedback="Proceed with focused tests"
)
```

处理函数：

```python
def handle_plan_review(
    request_id: str,
    approve: bool,
    feedback: str = "",
) -> str:
    with _tracker_lock:
        req = plan_requests.get(request_id)

    if not req:
        return (
            f"Error: Unknown plan request_id "
            f"'{request_id}'"
        )

    with _tracker_lock:
        req["status"] = (
            "approved" if approve else "rejected"
        )

    BUS.send(
        "lead",
        req["from"],
        feedback,
        "plan_approval_response",
        {
            "request_id": request_id,
            "approve": approve,
            "feedback": feedback,
        },
    )

    return (
        f"Plan {req['status']} for '{req['from']}'"
    )
```

它做两件事：

```text
更新 plan tracker。
向提交者 inbox 发送审批结果。
```

## 16. Teammate 如何看到审批结果

下一轮 `_teammate_loop`：

```text
drain inbox
  -> 把 plan_approval_response JSON 加入 messages
  -> 模型看到 approve 和 feedback
  -> 决定继续工作或修改计划
```

当前代码没有在 harness 中强制阻塞其他工具。

也就是说：

```text
“等待批准后再工作”主要依赖 system prompt 和模型遵守。
```

这不是严格的 plan gate。

生产实现若要强制审批，应在 harness 中记录成员是否有 pending plan，并拒绝高风险工具调用。

## 17. 两个协议的共同模式

```text
1. 发起方生成 request_id。
2. Tracker 写入 pending。
3. MessageBus 把请求送给接收方。
4. 接收方携带 request_id 响应。
5. Tracker 更新 approved/rejected。
6. MessageBus 把结果送回发起方。
```

抽象后可以写成：

```text
ProtocolRequest {
  request_id
  type
  sender
  target
  payload
  status
}
```

这套模式可以扩展到：

```text
文件修改审批。
部署审批。
预算审批。
权限升级。
任务移交。
冲突仲裁。
```

## 18. 工具与协议的关系

协议不是独立于 Tool System 的另一套执行机制。

它仍通过工具暴露给模型：

```text
shutdown_request tool
shutdown_response tool
plan_approval tool
```

工具负责：

```text
创建请求。
提交响应。
查询状态。
```

MessageBus 负责：

```text
传输协议消息。
```

Tracker 负责：

```text
保存状态机当前状态。
```

## 19. 线程锁的作用

Lead 和多个 teammate 在不同线程运行，可能同时更新：

```python
shutdown_requests
plan_requests
```

所以使用：

```python
_tracker_lock = threading.Lock()
```

典型写法：

```python
with _tracker_lock:
    plan_requests[req_id] = {...}
```

当前锁只保护内存 tracker，不保护：

```text
MessageBus inbox 文件。
config.json。
成员状态。
```

## 20. 与 TaskSystem 的关系

TaskSystem 表达工作状态：

```text
pending / in_progress / completed
blockedBy / blocks
```

Protocol 表达协商状态：

```text
请求是否被接受。
计划是否获批。
成员是否同意退出。
```

两者可以联动：

```text
计划获批
  -> task status 改为 in_progress。

成员关机前
  -> 移交未完成 task。

任务完成
  -> lead 请求 teammate shutdown。
```

s10 教学代码尚未把它们组合起来。

## 21. 当前实现边界

教学版协议存在这些限制：

```text
Tracker 不持久化，进程退出后丢失。
没有请求超时。
没有重复响应保护。
没有取消请求。
没有统一协议对象。
消息类型命名有混淆。
Plan approval 没有 harness 级强制 gate。
未知 shutdown request 仍可发送响应。
状态和 inbox 可能不一致。
没有协议事件日志。
```

生产系统需要：

```text
持久化状态。
请求生命周期和超时。
幂等响应。
发送者/接收者身份验证。
严格状态转换校验。
审批结果的执行约束。
审计日志。
```

## 总结

Team Protocol 在 MessageBus 之上增加三样东西：

```text
request_id
  关联请求和响应。

tracker
  保存 pending / approved / rejected。

状态机
  规定协议如何推进。
```

Shutdown 和 Plan Approval 虽然业务方向不同，但共享同一个请求响应模式。这是从“agent 可以互相说话”升级到“agent 可以按明确规则协商”的关键一步。
