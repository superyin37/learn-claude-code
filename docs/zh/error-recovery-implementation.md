# Error Recovery 实现逻辑

本文说明 Agent 如何处理输出截断、上下文超限、限流和服务过载。独立实现位于 `s11_error_recovery/code.py`，综合实现位于 `s20_comprehensive/code.py`。

## 1. 恢复机制的位置

错误恢复包裹的是模型调用，不是工具业务逻辑：

```text
prepare context
  -> call LLM
       -> transient retry
       -> prompt-too-long recovery
       -> max-tokens recovery
  -> process response
```

错误必须分类。所有异常统一重试会放大不可恢复错误，完全不重试又会让 Agent 因短暂波动退出。

## 2. RecoveryState

`RecoveryState` 保存当前恢复过程：

```text
current_model
consecutive_529
has_escalated
recovery_count
has_attempted_reactive_compact
```

这些字段防止恢复逻辑无限循环，并记录是否已经用过某条恢复路径。

## 3. 429 与 529

`with_retry()` 处理临时 API 故障：

- 429：请求过多。
- 529：服务过载。

重试采用指数退避和随机抖动：

```text
delay = min(base * 2^attempt, upper_bound) + jitter
```

抖动避免多个请求在同一时间再次冲击服务。

如果连续出现足够多次 529，且配置了 `FALLBACK_MODEL_ID`，状态会切换到备用模型。

## 4. 为什么重试要有上限

综合版通过 `MAX_RETRIES`、`MAX_CONSECUTIVE_529` 控制重试：

```text
短暂错误 -> 自动恢复
持续错误 -> 切备用模型或返回失败
```

无限重试会占住线程、隐藏真实故障并持续产生费用。

## 5. 输出截断

模型返回 `stop_reason == "max_tokens"` 时，输出可能没有完成。

恢复分两阶段：

### 第一次：提高上限

第一次截断不把不完整输出加入 messages，而是提高 `max_tokens`，对原请求重新调用。

这样模型获得更大输出空间，也避免历史中出现一份半成品。

### 后续：续写

如果提高上限后仍然截断：

1. 保存本次 assistant 输出。
2. 注入 continuation 提示。
3. 要求从中断位置继续，不重复总结。

续写次数受 `MAX_RECOVERY_RETRIES` 限制。

## 6. Prompt Too Long

`is_prompt_too_long_error()` 根据异常内容识别上下文超限。

第一次命中时：

```text
messages
  -> reactive_compact
  -> 保留摘要和最近完整消息
  -> 重新调用模型
```

如果已经执行过 reactive compact 仍然超限，则停止恢复，避免重复压缩造成信息继续损失。

## 7. Reactive Compact 与 Auto Compact

两者触发时机不同：

| 机制 | 触发 |
|---|---|
| auto compact | 调用前估算已超过阈值 |
| reactive compact | API 已明确返回上下文过长 |

Auto Compact 是预防，Reactive Compact 是故障恢复。

Reactive Compact 会保存 transcript，并保留近期消息，尤其避免把 `tool_use` 和对应 `tool_result` 拆开。

## 8. 恢复后的控制流

恢复动作完成后使用 `continue` 回到循环开头：

```text
异常
  -> 改 RecoveryState / messages / model / max_tokens
  -> continue
  -> 重新准备同一轮调用
```

这比在异常分支中复制一套模型调用逻辑更安全。

## 9. 工具错误与模型错误

模型 API 错误由 Error Recovery 处理。

工具 handler 错误通常转换为普通 `tool_result`：

```text
Error: ...
```

让模型看到失败并决定是否换命令、换工具或终止。只有 harness 自身无法继续的异常才应逃出工具执行层。

## 10. 与 Context Compact 的连接

完整顺序：

```text
tool_result_budget
  -> snip_compact
  -> micro_compact
  -> 必要时 compact_history
  -> with_retry(call_llm)
  -> 若 prompt too long，再 reactive_compact
```

压缩负责控制输入体积，Recovery 负责处理压缩后仍可能发生的 API 故障。

## 11. 与动态 Prompt、MCP 的连接

重试同一轮时，模型、工具池和 system prompt 必须保持一致，除非恢复动作明确改变了它们，例如：

- fallback model 改变模型。
- reactive compact 改变 messages。
- MCP 重连改变工具池。

否则重试可能不再是同一个请求语义。

## 12. 当前实现边界

- 通过异常字符串识别 prompt-too-long，依赖 SDK 错误文本。
- 教学版没有完整解析 `Retry-After`。
- fallback model 只处理模型选择，没有能力差异适配。
- continuation 可能重复部分内容。
- 工具失败没有统一重试策略。
- 没有 circuit breaker、全局限流和请求预算。
- transcript 和错误日志没有统一关联 ID。

## 13. 总结

Error Recovery 的核心模式是：

```text
先分类
  -> 临时错误：退避重试
  -> 服务过载：必要时切模型
  -> 输出截断：提高上限或续写
  -> 上下文超限：reactive compact
  -> 超过恢复预算：停止
```

恢复系统不是让任何错误都“成功”，而是让可恢复故障自动恢复，让不可恢复故障有限、明确地结束。
