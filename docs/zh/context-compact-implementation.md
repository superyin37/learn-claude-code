# Context Compact 实现逻辑

本文说明 `agents/s06_context_compact.py` 中 Context Compact 的实现逻辑：什么时候触发、压缩哪些内容、如何归档完整历史、如何替换当前 `messages`，以及手动 compact tool 和自动 compact 的关系。

## 核心定位

Context Compact 的本质是：

```text
用“旧历史归档 + 当前摘要”替代“无限增长的完整 messages”。
```

它不是外部长期记忆系统，而是一个会话瘦身机制。

目标：

```text
保留继续工作需要的信息。
删除或压缩低价值细节。
完整原文归档到磁盘。
当前上下文换成短 summary。
```

整体流程：

```text
每轮 agent_loop 开始
  -> micro_compact(messages)
  -> estimate_tokens(messages)
  -> 如果超过 THRESHOLD
       auto_compact(messages)
       用 summary 替换 messages
  -> 调模型
  -> 执行工具
  -> 如果模型调用 compact
       auto_compact(messages)
```

## 1. 为什么需要 Compact

在 s01-s05 中，`messages/history` 会一直追加：

```text
用户请求
模型回复
tool_use
tool_result
文件内容
shell 输出
skill 内容
子 agent summary
错误日志
```

问题是：

```text
messages 越来越大。
旧工具结果占据大量上下文。
模型容易被旧状态干扰。
最终超过上下文窗口。
```

Compact 的目标不是清空历史，而是把旧上下文压缩成继续工作所需的摘要。

## 2. 核心常量

```python
THRESHOLD = 50000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
KEEP_RECENT = 3
```

含义：

```text
THRESHOLD
  自动压缩阈值。估算 token 数超过它时触发 auto_compact。

TRANSCRIPT_DIR
  完整对话历史归档目录。

KEEP_RECENT
  micro_compact 保留最近几个完整 tool_result。
```

## 3. estimate_tokens：粗略估算上下文大小

实现：

```python
def estimate_tokens(messages: list) -> int:
    """Rough token count: ~4 chars per token."""
    return len(str(messages)) // 4
```

这是一个粗略估算：

```text
把 messages 转成字符串。
字符数 / 4 约等于 token 数。
```

它不精确，但足够做教学版阈值判断。

## 4. micro_compact：每轮静默压缩旧工具结果

`micro_compact(messages)` 是第一层压缩，每次调用模型前都会执行：

```python
micro_compact(messages)
```

它做的事：

```text
遍历 messages。
找到所有 user 消息里的 tool_result。
保留最近 KEEP_RECENT 个。
旧的 tool_result 如果内容很长，就替换成占位符。
```

识别逻辑：

```python
tool_results = []
for msg_idx, msg in enumerate(messages):
    if msg["role"] == "user" and isinstance(msg.get("content"), list):
        for part_idx, part in enumerate(msg["content"]):
            if isinstance(part, dict) and part.get("type") == "tool_result":
                tool_results.append((msg_idx, part_idx, part))
```

它只压缩这种结构：

```python
{
    "role": "user",
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": "...",
            "content": "很长的工具输出",
        }
    ],
}
```

如果工具结果数量不超过 `KEEP_RECENT`，直接返回：

```python
if len(tool_results) <= KEEP_RECENT:
    return messages
```

否则保留最近几个：

```python
to_clear = tool_results[:-KEEP_RECENT]
```

旧结果替换成：

```python
result["content"] = f"[Previous: used {tool_name}]"
```

例如原来是：

```text
tool_result read_file:
  5000 行文件内容...
```

会变成：

```text
[Previous: used read_file]
```

作用：

```text
保留“曾经做过什么”。
删除“旧的长输出细节”。
最近 3 个工具结果完整保留。
```

## 5. micro_compact 如何知道工具名

`tool_result` 里只有 `tool_use_id`，没有工具名。所以它先扫描 assistant 消息中的 tool_use：

```python
tool_name_map = {}
for msg in messages:
    if msg["role"] == "assistant":
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if hasattr(block, "type") and block.type == "tool_use":
                    tool_name_map[block.id] = block.name
```

然后用：

```python
tool_id = result.get("tool_use_id", "")
tool_name = tool_name_map.get(tool_id, "unknown")
```

这样占位符能写成：

```text
[Previous: used bash]
[Previous: used read_file]
[Previous: used edit_file]
```

而不是只写“删掉了一个工具结果”。

## 6. auto_compact：完整会话归档 + LLM 总结

`auto_compact(messages)` 是第二层压缩。

它做三件事：

```text
1. 保存完整 transcript。
2. 让模型总结当前对话。
3. 用 summary 替换整个 messages。
```

### 6.1 保存完整 transcript

```python
TRANSCRIPT_DIR.mkdir(exist_ok=True)
transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"

with open(transcript_path, "w") as f:
    for msg in messages:
        f.write(json.dumps(msg, default=str) + "\n")
```

保存到：

```text
.transcripts/transcript_<timestamp>.jsonl
```

这是完整 `messages` 的归档，方便以后追溯。

### 6.2 让模型总结当前对话

```python
conversation_text = json.dumps(messages, default=str)[:80000]
response = client.messages.create(
    model=MODEL,
    messages=[{
        "role": "user",
        "content": (
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "
            "Be concise but preserve critical details.\n\n"
            + conversation_text
        ),
    }],
    max_tokens=2000,
)
summary = response.content[0].text
```

总结要求包含：

```text
1. 已完成什么
2. 当前状态是什么
3. 做过哪些关键决策
```

注意：

```text
conversation_text 只取前 80000 字符。
summary 最多 2000 tokens。
```

### 6.3 用 summary 替换 messages

```python
return [
    {
        "role": "user",
        "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}",
    },
    {
        "role": "assistant",
        "content": "Understood. I have the context from the summary. Continuing.",
    },
]
```

这一步很关键。

它不是在原 `messages` 后面追加 summary，而是把整个 `messages` 替换成两条消息：

```text
user:
  [Conversation compressed. Transcript: ...]
  summary...

assistant:
  Understood. I have the context from the summary. Continuing.
```

这样下一轮模型看到的不是完整旧历史，而是压缩摘要。

## 7. 自动触发逻辑

自动 compact 在每轮模型调用前触发：

```python
micro_compact(messages)

if estimate_tokens(messages) > THRESHOLD:
    print("[auto_compact triggered]")
    messages[:] = auto_compact(messages)
```

注意这里用的是：

```python
messages[:] = auto_compact(messages)
```

而不是：

```python
messages = auto_compact(messages)
```

原因是 `messages` 是外部传进来的列表，通常就是 `history`。用切片替换会原地修改同一个列表对象。

关系：

```text
history 传入 agent_loop。
messages 指向同一个 list。
messages[:] 替换内容。
外部 history 也随之变成压缩后的内容。
```

如果写 `messages = ...`，只是局部变量重新绑定，外部 `history` 不会变。

## 8. compact 工具：手动触发压缩

除了自动阈值触发，模型还可以主动调用 `compact` 工具。

工具 handler：

```python
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "compact":    lambda **kw: "Manual compression requested.",
}
```

工具声明：

```python
{
    "name": "compact",
    "description": "Trigger manual conversation compression.",
    "input_schema": {
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "description": "What to preserve in the summary",
            },
        },
    },
}
```

不过实际处理时，`compact` 被特殊分支拦截：

```python
manual_compact = False

if block.name == "compact":
    manual_compact = True
    output = "Compressing..."
else:
    handler = TOOL_HANDLERS.get(block.name)
    ...
```

先把 `compact` 的工具结果写入 `messages`：

```python
messages.append({"role": "user", "content": results})
```

然后触发压缩：

```python
if manual_compact:
    print("[manual compact]")
    messages[:] = auto_compact(messages)
```

手动 compact 的流程：

```text
模型调用 compact
  -> harness 返回 Compressing...
  -> 这次 compact 调用也被写入 messages
  -> auto_compact 归档并总结完整 messages
  -> 用 summary 替换 messages
```

## 9. 三层压缩关系

这个文件中的三层压缩是：

```text
Layer 1: micro_compact
Layer 2: auto_compact
Layer 3: compact tool
```

更准确地说：

```text
micro_compact:
  每轮自动执行。
  只替换旧 tool_result 内容。
  不调用模型总结。
  不改变 messages 总体结构。

auto_compact:
  超过阈值自动执行。
  保存完整 transcript。
  调用模型总结。
  整体替换 messages。

compact tool:
  模型主动触发。
  本质上也是调用 auto_compact。
```

关系图：

```text
messages
  |
  | 每轮
  v
micro_compact
  |
  | if estimate_tokens > THRESHOLD
  v
auto_compact
  |
  v
messages[:] = [
  compressed summary,
  continuation ack
]

模型也可以：
  tool_use compact
    -> auto_compact
```

## 10. Compact 后模型还能看到什么

压缩前模型可能看到：

```text
user: 最初需求
assistant: tool_use read_file
user: 大量文件内容
assistant: tool_use edit_file
user: Edited ...
assistant: tool_use bash
user: 测试输出...
...
```

压缩后模型看到：

```text
SYSTEM:
  You are a coding agent...

messages:
  user:
    [Conversation compressed. Transcript: .transcripts/transcript_....jsonl]

    Summary:
    - 已完成...
    - 当前状态...
    - 关键决策...
    - 下一步...

  assistant:
    Understood. I have the context from the summary. Continuing.
```

它不再直接看到旧工具结果全文，但知道 transcript 文件路径。

## 11. 和 Todo / Skill / Subagent 的关系

Compact 是对 `messages` 动手，所以任何进入 `messages` 的内容都会被影响。

### Todo

```text
todo render 结果在 messages 里。
旧 todo 快照可能被保留或总结。
如果没有外部 TodoManager 当前状态补充，模型依赖 summary 理解当前任务状态。
```

### Skill

```text
load_skill 返回的 SKILL.md 内容作为 tool_result 进入 messages。
旧 skill 内容可能被 micro_compact 替换。
auto_compact 后只剩 summary 中保留的 skill 重点。
```

### Subagent

```text
subagent 内部历史本来就不进入父 messages。
只有 subagent summary 进入父 messages。
compact 只压缩这个 summary，不知道子 agent 内部细节。
```

### TaskSystem

```text
s06 还没有 TaskSystem。
所以任务状态主要还在 messages 里。
s07 引入 .tasks 后，长期任务状态就不再完全依赖 messages。
```

## 12. 当前实现的局限

这个 compact 是教学版，有几个边界：

```text
estimate_tokens 很粗糙。
micro_compact 只处理 user content list 里的 tool_result。
不会压缩普通长文本 user message。
不会压缩 assistant 文本。
auto_compact 只截取前 80000 字符让模型总结。
compact 工具的 focus 参数声明了，但实际没有传给 auto_compact 使用。
summary 质量完全依赖模型。
归档 transcript 写入 .transcripts，但没有读取工具。
没有分层长期记忆索引。
```

尤其是 `focus`：

```python
"focus": "What to preserve in the summary"
```

当前实现只是声明给模型看，实际 `auto_compact(messages)` 没接收 focus 参数。因此它没有真正影响总结 prompt。

## 总结

Context Compact 的实现逻辑是：

```text
每轮先 micro_compact，清掉旧工具结果的大内容。
如果 messages 估算 token 超阈值，就 auto_compact：
  保存完整 transcript 到 .transcripts
  让模型总结关键上下文
  用 summary 替换整个 messages。

模型也可以调用 compact 工具，手动触发同样的 auto_compact。
```

它的本质是：

```text
让 agent 战略性遗忘旧细节，同时保留继续工作的主线。
```
