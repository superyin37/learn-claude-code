#!/usr/bin/env python3
# Harness: planning -- keeping the model on course without scripting the route.
"""
s03_todo_write.py - TodoWrite

The model tracks its own progress via a TodoManager. A nag reminder
forces it to keep updating when it forgets.

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> | Tools   |
    |  prompt  |      |       |      | + todo  |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                                |
                    +-----------+-----------+
                    | TodoManager state     |
                    | [ ] task A            |
                    | [>] task B <- doing   |
                    | [x] task C            |
                    +-----------------------+
                                |
                    if rounds_since_todo >= 3:
                      inject <reminder>

Key insight: "The agent can track its own progress -- and I can see it."
"""


'''
多步任务中, 模型会丢失进度 -- 重复做过的事、跳步、跑偏。对话越长越严重: 
工具结果不断填满上下文, 系统提示的影响力逐渐被稀释。
一个 10 步重构可能做完 1-3 步就开始即兴发挥, 因为 4-10 步已经被挤出注意力了。

这个文件展示了如何让模型通过一个结构化的 TodoManager 来跟踪自己的任务进度。
模型可以使用 "todo" 工具来更新任务列表，每个任务都有一个状态（pending、in_progress、completed）。
代理循环中还添加了一个机制，如果模型连续多轮没有更新 todo 状态，就会注入一个提醒，促使模型保持对任务进度的关注。
这种设计让模型能够更好地管理和跟踪多步骤任务的完成情况。
'''

import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 定义系统提示，告诉模型它是一个编码代理，并鼓励它使用工具来解决任务。
# 强调使用 todo 工具来规划多步骤任务
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use the todo tool to plan multi-step tasks. Mark in_progress before starting, completed when done.
Prefer tools over prose."""

'''
TodoManager: structured state the LLM writes to --
TodoManager 维护一个任务列表，每个任务有一个文本描述和一个状态（pending、in_progress、completed）。
模型通过调用 "todo" 工具来更新这个列表，代理循环会根据模型是否使用了 "todo" 工具来决定是否注入提醒，促使模型保持对任务进度的关注。
'''


class TodoManager:
    # 管理一个 todo 列表，限制最多 20 个任务，每个任务有 id、文本和状态
    # 初始化一个空的 todo 列表
    '''
    items是一个列表，存储当前的任务项，每个项是一个字典，包含 
    id: 任务的唯一标识符（字符串）
    text: 任务的描述文本（字符串）
    status: 任务的状态（字符串，必须是 "pending"、"in_progress" 或 "completed"）

    update方法接受一个新的任务列表，验证输入的合法性（文本不能为空，状态必须是 pending、in_progress 或 completed），并确保一次只能有一个任务处于 in_progress 状态。
    验证通过后，更新 items 列表并返回渲染后的文本。

    items格式：
    [
    {"id": "1", "text": "Refactor function A", "status": "pending"},
    {"id": "2", "text": "Write tests for B", "status": "in_progress"},
    {"id": "3", "text": "Update documentation", "status": "completed"},
    ]
    '''
    def __init__(self):
        self.items = []

    # 更新 todo 列表，验证输入并返回渲染后的文本
    def update(self, items: list) -> str:
        if len(items) > 20:
            raise ValueError("Max 20 todos allowed")
        
        # 验证每个任务项，确保有文本和合法的状态，并统计 in_progress 的数量
        # validated 列表存储验证后的任务项，in_progress_count 统计正在进行的任务数量
        validated = []
        in_progress_count = 0

        # 遍历输入的任务项，验证文本和状态是否合法，并构建验证后的任务列表
        # enumerate(items) 用于获取每个任务项的索引 i 和内容 item，如果 item 中没有提供 id，则使用索引作为默认 id
        # 例如，如果输入的 items 是 
        # [{"text": "Task A", "status": "pending"}, {"text": "Task B", "status": "in_progress"}]
        # 则验证后的 validated 列表将是 [{"id": "1", "text": "Task A", "status": "pending"}, {"id": "2", "text": "Task B", "status": "in_progress"}]。
        for i, item in enumerate(items):

            text = str(item.get("text", "")).strip()
            # status 默认为 pending，如果 item 中没有提供 status，则使用 pending 作为默认值
            status = str(item.get("status", "pending")).lower()
            # 任务的 id 可以由模型提供，也可以默认使用索引加1的字符串形式，例如 "1"、"2" 等
            item_id = str(item.get("id", str(i + 1)))

            # 验证文本不能为空，状态必须是 pending、in_progress 或 completed。
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            
            # 如果状态是 in_progress，增加计数器，确保一次只能有一个任务在进行中
            if status == "in_progress":
                in_progress_count += 1
            # 将验证后的任务项添加到 validated 列表中，使用提供的 id 或默认的索引作为 id
            validated.append({"id": item_id, "text": text, "status": status})

        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time")
        self.items = validated
        return self.render()

    # 将当前的 todo 列表渲染成文本格式，显示每个任务的状态和描述，并统计完成的任务数量
    def render(self) -> str:
        if not self.items:
            return "No todos."
        
        # 根据每个任务的状态添加不同的标记（[ ]、[>]、[x]），并构建显示文本列表
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)


TODO = TodoManager()


# -- Tool implementations --
# 工具函数与之前相同
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

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


'''
关于TOOL_HANDLERS和TOOLS的定义：
- TOOL_HANDLERS 是一个字典，映射工具名称到对应的处理函数。
当模型调用一个工具时，代理循环会使用这个字典来找到对应的函数并执行。

- TOOLS 是一个列表，定义了每个工具的名称、描述和输入模式（input_schema）。
这个列表会传递给模型，让模型知道有哪些工具可用，以及如何调用它们。
每个工具的 input_schema 定义了工具需要的参数和它们的类型，这样模型在调用工具时就知道应该提供哪些参数。

TOOL_HANDLERS是PYTHON侧的工具函数映射，
而TOOLS是传递给LLM的工具定义列表。
两者配合使用，实现了模型调用工具并由Python执行的机制。
'''

# 定义工具处理函数的映射，方便在代理循环中调用对应的函数来执行工具
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo":       lambda **kw: TODO.update(kw["items"]),
}

# 定义工具列表，包含工具的名称、描述和输入模式，供模型调用
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "todo", "description": "Update task list. Track progress on multi-step tasks.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "text", "status"]}}}, "required": ["items"]}},
]



def agent_loop(messages: list):
    '''
    -- Agent loop with nag reminder injection --
    代理循环中添加了一个计数器 rounds_since_todo，用来跟踪模型连续多少轮没有使用 "todo" 工具。
    每当模型使用 "todo" 工具更新任务列表时，计数器重置为0；如果模型连续多轮没有使用 "todo" 工具，计数器就会增加。
    当计数器达到一定值（例如3）时，代理循环会在下一轮调用模型之前，注入一个提醒消息，提示模型更新它的任务列表。
    '''
    rounds_since_todo = 0
    while True:
        # Nag reminder is injected below, alongside tool results
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []

        # used_todo 标志用来检测本轮是否使用了 "todo" 工具，如果使用了就重置 rounds_since_todo，否则增加计数器
        used_todo = False
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                
                # 如果本轮使用了 "todo" 工具，设置 used_todo 标志为 True
                if block.name == "todo":
                    used_todo = True
                    
        # 根据 used_todo 的值更新 rounds_since_todo，如果本轮使用了 "todo" 工具，重置为0，否则增加计数器
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        # 如果连续多轮没有使用 "todo" 工具，注入一个提醒消息，提示模型更新它的任务列表
        if rounds_since_todo >= 3:
            results.append({"type": "text", "text": "<reminder>Update your todos.</reminder>"})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
