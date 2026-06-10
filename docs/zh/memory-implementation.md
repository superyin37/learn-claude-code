# Memory 实现逻辑

本文说明项目的长期记忆板块：记忆如何持久化、检索、注入、提取和整理。完整教学实现位于 `s09_memory/code.py`。

## 1. Memory 与会话历史的区别

`messages` 是当前会话的工作记忆，Context Compact 会对它裁剪和摘要。

Memory 是跨压缩、跨会话的长期知识：

```text
messages
  -> 当前会话
  -> 会增长
  -> 可压缩

.memory/*.md
  -> 跨会话
  -> 文件持久化
  -> 按需加载
```

适合保存用户偏好、长期反馈、项目事实和外部参考位置，不适合保存每一步临时工具输出。

## 2. 文件结构

```text
.memory/
├── MEMORY.md
├── user-preference-tabs.md
├── project-auth-rewrite.md
└── reference-ci-ticket.md
```

每条记忆是一个 Markdown 文件：

```markdown
---
name: user-preference-tabs
description: User prefers tabs for indentation
type: user
---

Use tabs when editing source files.
```

支持四种类型：

| 类型 | 内容 |
|---|---|
| `user` | 用户身份、偏好和稳定习惯 |
| `feedback` | 用户对 Agent 工作方式的指导 |
| `project` | 项目背景、架构事实和长期约束 |
| `reference` | issue、文档和排查入口 |

## 3. 写入与索引

`write_memory_file()` 把名称转换为文件名，写入 frontmatter 和正文，然后调用 `_rebuild_index()`。

索引文件 `MEMORY.md` 只保存名称、链接和一句描述：

```markdown
- [user-preference-tabs](user-preference-tabs.md) - User prefers tabs
```

设计目的是让索引常驻上下文，而完整文件只在相关时加载。

```text
小索引：便宜、常驻
完整文件：较贵、按需
```

## 4. 读取与目录扫描

`list_memory_files()` 扫描 `.memory/*.md`，排除 `MEMORY.md`，解析：

```python
{
    "filename": ...,
    "name": ...,
    "description": ...,
    "type": ...,
    "body": ...,
}
```

`read_memory_index()` 给 system prompt 使用，`read_memory_file()` 在相关性选择后读取完整正文。

## 5. 相关记忆选择

`select_relevant_memories(messages, max_items=5)` 只读取最近三个用户消息，截取最多 2000 字符作为当前查询。

然后构造轻量目录：

```text
0: user-preference-tabs - User prefers tabs
1: project-auth-rewrite - Auth rewrite is compliance-driven
```

一次 side-query 要求模型只返回相关条目的 JSON 索引：

```json
[0, 1]
```

选择结果最多五条。这样避免把所有长期记忆塞进每一次请求。

如果 API 调用、JSON 提取或解析失败，代码会退化为名称和描述的关键词匹配。

## 6. 注入当前请求

`load_memories()` 把选中的文件包装为：

```xml
<relevant_memories>
...
</relevant_memories>
```

`agent_loop()` 不直接修改原始用户消息，而是创建 `request_messages` 副本，将记忆内容临时前置到当前用户 turn：

```text
原始 messages 保持不变
  -> request_messages 副本
  -> 当前用户消息前添加 relevant_memories
  -> 调用 LLM
```

这避免同一段记忆永久进入历史，并在后续轮次反复复制。

## 7. 与 Context Compact 的顺序

每轮模型调用前：

```text
保存 pre_compress 快照
  -> tool_result_budget
  -> snip_compact
  -> micro_compact
  -> 必要时 compact_history
  -> 临时注入相关 Memory
  -> LLM
```

Memory 不依赖压缩后的摘要进行提取。`pre_compress` 保存压缩前对话，防止用户偏好在摘要中被弱化。

## 8. 回合结束后的自动提取

当模型不再返回 `tool_use` 时：

```python
extract_memories(pre_compress)
consolidate_memories()
```

`extract_memories()`：

1. 收集最近十条消息中的文本。
2. 列出已有记忆名称和描述。
3. 要求模型返回 `{name, type, description, body}` 数组。
4. 忽略空内容。
5. 将有效结果写成独立文件并重建索引。

提取放在任务结束后，而不是每个工具步骤后，避免记忆维护干扰主要工作循环。

## 9. Consolidation

当记忆文件数达到 `CONSOLIDATE_THRESHOLD = 10` 时，`consolidate_memories()` 要求模型：

- 合并重复项。
- 删除过时或冲突内容。
- 优先保留用户偏好。
- 将总量控制在 30 条以内。

教学实现会删除旧记忆文件，再按返回结果重建。这是 Dream 式整理的简化版本。

## 10. System Prompt 中的索引

`build_system()` 每个用户 turn 开始时读取 `MEMORY.md`：

```text
Memories available:
- [...]
```

索引告诉主模型长期记忆的存在；真正相关的完整内容由 `load_memories()` 注入当前请求。两条路径分别承担“发现”和“使用”。

## 11. 综合版中的差异

`s20_comprehensive/code.py` 只读取 `.memory/MEMORY.md` 的前 2000 字符并加入运行时 context。

它保留了 Memory 的 prompt 集成，但没有完整复用 `s09` 的：

- side-query 选择。
- 回合后自动提取。
- consolidation。

因此 Memory 的完整行为应以 `s09_memory/code.py` 为准，S20 展示的是轻量总装。

## 12. 当前实现边界

- frontmatter 解析是简化实现，不是完整 YAML parser。
- slug 同名时可能覆盖已有记忆。
- 相关性选择需要额外模型调用。
- 关键词降级对中文分词和同义词不敏感。
- consolidation 是整体替换，没有事务和版本回滚。
- 异常大多被静默忽略，不利于诊断提取失败。
- 没有文件锁，多进程写入可能竞争。

## 13. 总结

Memory 的核心闭环是：

```text
文件持久化
  -> 小索引发现
  -> 相关性选择
  -> 完整内容按需注入
  -> 回合后提取
  -> 低频合并整理
```

它不是把所有历史永久塞进上下文，而是把长期知识移到外部存储，需要时再借回当前请求。
