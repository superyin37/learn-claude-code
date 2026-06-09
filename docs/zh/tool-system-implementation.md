# Tool System 实现逻辑

本文说明 `agents/s02_tool_use.py` 中工具系统的实现：`TOOLS`、`TOOL_HANDLERS` 和工具函数如何分工，模型调用如何被路由，以及如何安全地增加新工具。

## 1. 直观理解

工具系统可以分为三层：

```text
TOOLS
  模型看到的能力说明书

TOOL_HANDLERS
  harness 使用的路由表

run_xxx 函数
  真正执行动作的代码
```

关系：

```text
模型读取 TOOLS
  -> 发出 tool_use(name, input)
  -> agent_loop 用 name 查 TOOL_HANDLERS
  -> handler 调用 run_xxx
  -> 返回 tool_result
```

核心原则：

```text
添加工具不需要改 agent_loop 的结构。
只需要声明、注册和实现。
```

## 2. 为什么从单一 bash 扩展专用工具

s01 只有 `bash`。理论上模型可以通过 shell 完成所有操作，但存在问题：

```text
命令格式不稳定。
文件内容截断不可控。
特殊字符处理脆弱。
所有能力都暴露在一个高权限入口上。
参数难以结构化校验。
```

s02 增加：

```text
read_file
write_file
edit_file
```

专用工具的价值：

```text
输入更结构化。
行为更容易预测。
权限边界更清楚。
错误信息更稳定。
模型更容易选择正确动作。
```

## 3. TOOLS：模型侧接口定义

例如 `read_file`：

```python
{
    "name": "read_file",
    "description": "Read file contents.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
    },
}
```

每个声明包含：

```text
name
  工具协议名，也是路由键。

description
  模型用来判断何时调用它。

input_schema
  模型调用时应生成的参数结构。
```

`required` 表示必须提供的字段。

注意：

```text
input_schema 主要约束模型输出格式。
业务级校验仍应由 Python handler 完成。
```

## 4. TOOL_HANDLERS：执行侧路由表

```python
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(
        kw["path"],
        kw["old_text"],
        kw["new_text"],
    ),
}
```

它把模型协议参数转换为 Python 函数参数。

例如模型返回：

```json
{
  "name": "read_file",
  "input": {
    "path": "README.md",
    "limit": 100
  }
}
```

路由过程：

```python
handler = TOOL_HANDLERS.get(block.name)
output = handler(**block.input)
```

等价于：

```python
output = run_read("README.md", 100)
```

## 5. TOOLS 与 TOOL_HANDLERS 的关系

二者必须通过名称对应：

```text
TOOLS[*]["name"] == TOOL_HANDLERS 的 key
```

但它们服务于不同对象：

```text
TOOLS 给模型看。
TOOL_HANDLERS 给 Python harness 用。
```

如果只在 TOOLS 中声明，没有 handler：

```text
模型会调用工具。
Harness 找不到执行函数。
返回 Unknown tool。
```

如果只注册 handler，没有放进 TOOLS：

```text
Harness 理论上能执行。
但模型不知道这个能力存在，通常不会调用。
```

## 6. safe_path：文件工具的统一边界

```python
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path
```

流程：

```text
把用户/模型给出的相对路径拼到 WORKDIR。
resolve 消除 .. 和符号路径影响。
检查最终路径是否仍在 WORKDIR 内。
超出则拒绝。
```

例如：

```text
README.md        -> 允许
src/app.py       -> 允许
../../secret.txt -> 拒绝
```

`safe_path` 是 read/write/edit 共享的安全基础。

## 7. run_read

```python
def run_read(path: str, limit: int = None) -> str:
    try:
        text = safe_path(path).read_text()
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [
                f"... ({len(lines) - limit} more lines)"
            ]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"
```

逻辑：

```text
验证路径。
读取文本。
按可选 limit 截断行数。
最大返回 50000 字符。
异常转换为字符串。
```

输出截断可以避免一次工具调用把整个上下文塞满。

## 8. run_write

```python
def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"
```

逻辑：

```text
验证目标路径。
创建父目录。
完整覆盖写入。
返回写入结果。
```

这是覆盖写入，不是追加写入。

## 9. run_edit

```python
def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"
```

逻辑：

```text
验证路径。
读取完整文件。
检查 old_text 是否存在。
只替换第一次出现的位置。
覆盖写回文件。
```

精确文本替换比让模型拼复杂 shell 命令更稳定。

当前实现没有检查 `old_text` 是否出现多次，所以在生产系统中通常还会增加唯一性校验。

## 10. run_bash

```python
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
```

它仍然作为通用逃生口存在，用于：

```text
运行测试。
执行构建。
使用 git。
搜索代码。
调用项目 CLI。
```

专用工具和 bash 并不是互斥关系：

```text
专用工具处理高频、结构明确的动作。
bash 处理长尾命令和项目工具链。
```

## 11. agent_loop 如何统一执行工具

```python
for block in response.content:
    if block.type == "tool_use":
        handler = TOOL_HANDLERS.get(block.name)
        output = (
            handler(**block.input)
            if handler
            else f"Unknown tool: {block.name}"
        )
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        })
```

这个分发器不关心具体工具是什么。

它只依赖统一协议：

```text
输入：
  block.name
  block.input

输出：
  字符串 tool_result
```

因此后续 Todo、Skill、TaskSystem 等都能以同样方式接入。

## 12. 如何新增一个工具

假设增加 `list_directory`。

第一步，实现函数：

```python
def run_list_directory(path: str = ".") -> str:
    try:
        fp = safe_path(path)
        return "\n".join(p.name for p in fp.iterdir())
    except Exception as e:
        return f"Error: {e}"
```

第二步，注册 handler：

```python
TOOL_HANDLERS["list_directory"] = (
    lambda **kw: run_list_directory(kw.get("path", "."))
)
```

第三步，加入模型工具声明：

```python
TOOLS.append({
    "name": "list_directory",
    "description": "List entries in a directory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
    },
})
```

`agent_loop` 不需要修改。

## 13. 错误如何反馈给模型

工具函数通常不直接抛出异常，而是返回：

```text
Error: ...
```

这个字符串作为 `tool_result` 写回 messages。

模型下一轮可以：

```text
修正参数。
换一种工具。
读取其他文件。
向用户说明失败。
```

后续版本还会在 dispatch 外层增加统一 `try/except`，防止单个 handler 异常让整个 agent 进程退出。

## 14. Tool 与其他组件的区别

### Tool 与 Skill

```text
Tool 是行动接口。
Skill 是知识内容。
Skill 通常通过 load_skill 这个 Tool 进入上下文。
```

### Tool 与 Subagent

```text
普通 Tool 背后是一个函数。
task Tool 背后是另一个 agent loop。
```

### Tool 与 Todo/TaskSystem

```text
Todo 和 TaskSystem 是状态组件。
模型通过 todo/task_* 工具读写这些状态。
```

### Tool 与 Protocol

```text
Protocol 定义协作语义和状态机。
协议动作仍然通过工具暴露给模型。
```

## 15. 当前实现边界

教学版工具系统缺少：

```text
严格 JSON Schema 运行时校验。
权限审批。
每个工具独立权限配置。
审计日志。
取消机制。
幂等性设计。
结构化错误对象。
并发冲突控制。
真正 shell 沙箱。
```

但它已经建立了最重要的扩展模式：

```text
声明和执行分离。
工具通过名称注册。
agent_loop 保持稳定。
```

## 总结

工具系统的核心结构是：

```text
TOOLS
  定义模型可以做什么。

TOOL_HANDLERS
  定义工具名如何路由。

run_xxx
  定义动作如何真正执行。

agent_loop
  统一接收 tool_use 并返回 tool_result。
```

工具系统是整个 harness 的行动层。后续所有高级组件，最终都需要通过工具接口让模型读取、更新或控制。
