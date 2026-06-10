# 运行时 Context 与 System Prompt 实现逻辑

本文说明 System Prompt 如何从固定字符串演进为运行时组装结果。核心实现位于 `s10_system_prompt/code.py`，综合集成位于 `s20_comprehensive/code.py`。

## 1. 核心问题

固定 `SYSTEM` 在能力较少时足够：

```python
SYSTEM = f"You are a coding agent at {WORKDIR}."
```

随着工具、Skills、Memory、MCP 和团队状态增加，固定字符串会产生三个问题：

- 所有能力互相耦合。
- 不需要的内容也占用 token。
- 运行状态变化后 prompt 不能同步更新。

因此 prompt 应该是运行时状态的投影：

```text
System Prompt = stable sections + current context sections
```

## 2. Section 拆分

`PROMPT_SECTIONS` 把身份、工具和工作区拆成独立段落：

```python
PROMPT_SECTIONS = {
    "identity": "...",
    "tools": "...",
    "workspace": "...",
    "memory": "...",
}
```

Section 的价值不是少写字符串，而是明确每一段的拥有者：

| Section | 数据来源 |
|---|---|
| identity | Agent 固定行为 |
| tools | 当前工具注册表 |
| workspace | 当前工作目录 |
| skills | Skills registry |
| memory | `.memory/MEMORY.md` |
| MCP | 已连接 server |
| teammates | 当前活跃成员 |

## 3. Context 是运行状态

`update_context(context, messages)` 从真实系统状态生成 context：

```python
{
    "enabled_tools": ...,
    "workspace": ...,
    "memories": ...,
}
```

判断 Memory 是否加载，不是搜索用户消息里有没有“记忆”关键词，而是检查 `.memory/MEMORY.md` 是否存在且有内容。

同理，MCP section 应来自连接表，团队 section 应来自活跃成员表。

## 4. 按需组装

`assemble_system_prompt(context)` 始终加入核心段落，再按状态加入动态段落：

```text
identity
tools
workspace
  + memory（存在时）
  + skills catalog（存在时）
  + MCP servers（已连接时）
  + teammates（活跃时）
```

拼接使用空行分隔，使模型容易识别不同职责的上下文来源。

## 5. 缓存

`s10_system_prompt/code.py` 用确定性 JSON 序列化生成 context key：

```python
key = json.dumps(
    context,
    sort_keys=True,
    ensure_ascii=False,
    default=str,
)
```

如果 key 没变，就返回上一次组装结果。

这只是本地字符串缓存：

```text
避免重复执行 Python 拼接
≠
模型 API 的 prompt cache
```

动态工具池场景要谨慎使用缓存。`connect_mcp()` 后工具定义发生变化，旧缓存必须失效或重新组装。

## 6. 每轮更新时机

基础实现的循环：

```text
update_context
  -> get_system_prompt
  -> LLM
  -> tools
  -> update_context
  -> 下一轮
```

这样工具或外部状态变化后，下一轮模型调用能看到新能力。

S20 每轮调用 `assemble_tool_pool()` 和 `update_context()`，然后组装 prompt，确保动态 MCP 工具和当前运行态一致。

## 7. Skills 的注入策略

Skills 不把完整正文全部放入 system prompt。运行时只加入 catalog：

```text
- code-review: Review code changes
- pdf: Process PDF documents
```

模型确定相关后调用 `load_skill(name)` 获取完整正文。

这与 Memory 的两层加载相似：

```text
Skills：目录常驻，正文按需
Memory：索引常驻，相关文件按需
```

## 8. Memory 的注入策略

`s10` 演示直接把 `MEMORY.md` 内容作为动态 section：

```text
Relevant memories:
...
```

完整 Memory 实现还会进行相关性选择并把具体文件临时注入 user turn。Prompt 组装负责暴露索引和能力，不负责替代 Memory 检索器。

## 9. MCP 与动态工具

MCP 连接后同时改变两项状态：

1. 模型可见的工具 schema。
2. System Prompt 中已连接 server 的说明。

因此工具池和 prompt 必须来自同一轮 context，不能一个更新、另一个继续使用旧缓存。

## 10. Context 与 messages 的职责

```text
System Prompt / Context
  -> 稳定规则、能力、环境和长期状态

messages
  -> 当前会话、模型输出和 tool_result

临时注入
  -> cron、后台通知、相关 Memory 等当前事件
```

把所有信息都放进 system prompt 会破坏缓存和可调试性；把所有信息都放进 messages 又会让历史快速膨胀。

## 11. 可调试性

Section 化后，可以明确回答：

- 哪一段告诉模型可用工具？
- Memory 从哪个文件加载？
- MCP 工具为何没有出现？
- Worktree 路径由谁提供？

每块上下文有明确来源，是大型 Agent 保持可调试性的关键。

## 12. 当前实现边界

- `s10` 的缓存是单进程全局变量，不适合多个并发 session。
- context key 包含大文本时，序列化本身也有成本。
- S20 的 Memory 只读取索引前 2000 字符。
- 工具说明有部分手工字符串，可能和实际注册表漂移。
- 没有正式的 section 优先级和冲突检测。
- 动态 section 变化可能降低 API prompt cache 命中率。

## 13. 总结

运行时 Prompt 的核心不是“写一段更好的提示词”，而是建立组装管线：

```text
读取真实状态
  -> 选择 section
  -> 保持静态内容稳定
  -> 注入必要动态内容
  -> 与当前工具池同步
```

模型看到的 system prompt 应当是当前 harness 状态的准确、最小化表示。
