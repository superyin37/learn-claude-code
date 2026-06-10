#!/usr/bin/env python3
# Harness: context isolation -- protecting the model's clarity of thought.
"""
s04_subagent.py - Subagents

Spawn a child agent with fresh messages=[]. The child works in its own
context, sharing the filesystem, then returns only a summary to the parent.

    Parent agent                     Subagent
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- fresh
    |                  |  dispatch   |                  |
    | tool: task       | ---------->| while tool_use:  |
    |   prompt="..."   |            |   call tools     |
    |   description="" |            |   append results |
    |                  |  summary   |                  |
    |   result = "..." | <--------- | return last text |
    +------------------+             +------------------+
              |
    Parent context stays clean.
    Subagent context is discarded.

Key insight: "Process isolation gives context isolation for free."
"""

'''
这个文件展示了如何实现一个子代理（subagent）系统。

主代理可以通过调用一个特殊的工具（task）来创建一个子代理。
子代理在一个全新的上下文中运行，拥有自己的消息历史和工具集，但共享同一个文件系统。
子代理完成它的任务后，只返回一个总结性的文本结果给主代理，而不暴露它的内部对话历史。
这种设计允许主代理保持清晰的上下文，同时让子代理独立处理复杂的子任务。

实现细节：
- 定义了一个 run_subagent 函数，负责创建和运行子代理。
- 子代理使用一个简化的工具集（CHILD_TOOLS），不包含递归调用 task 的能力，以避免无限嵌套。
- 主代理在处理工具调用时，如果遇到 task 工具，就调用 run_subagent 来执行子代理，并将子代理的总结结果作为工具结果返回给主代理。
- 其他工具（bash、read_file、write_file、edit_file）在主代理和子代理中都可用，子代理直接调用这些工具来完成任务。
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

SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."

# subagent 的 system prompt，强调它是一个子代理，完成任务后总结发现。
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."


# -- Tool implementations shared by parent and child --
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
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

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


TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# Child gets all base tools except task (no recursive spawning)
# 定义子代理的工具列表，包含基本工具但不包含 task 工具，以避免递归调用。
CHILD_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]


# -- Subagent: fresh context, filtered tools, summary-only return --
# 定义一个函数 run_subagent 来运行子代理，接受一个 prompt 作为输入，返回一个总结性的文本结果。
def run_subagent(prompt: str) -> str:
    # 子代理的消息历史从一个新的用户消息开始，内容是传入的 prompt。子代理使用 SUBAGENT_SYSTEM 作为系统提示，CHILD_TOOLS 作为工具列表。
    sub_messages = [{"role": "user", "content": prompt}]  # fresh context

    # 子代理循环，最多运行30轮，调用模型并处理工具调用，直到模型停止或达到轮数限制。子代理的结果只返回最后的文本内容。
    for _ in range(30):  # safety limit
        response = client.messages.create(
            model=MODEL, system=SUBAGENT_SYSTEM, messages=sub_messages,
            tools=CHILD_TOOLS, max_tokens=8000,
        )
        sub_messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
        sub_messages.append({"role": "user", "content": results})
    # Only the final text returns to the parent -- child context is discarded
    # 注意这里与之前的 agent_loop 不同，子代理在完成后只返回最后的文本内容，而不返回工具调用结果或消息历史。
    # 这实现了上下文隔离，主代理无法访问子代理的内部对话历史，只能得到一个总结性的结果。
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"


# -- Parent tools: base tools + task dispatcher --
# 定义主代理的工具列表，包含子代理工具列表中的所有工具，以及一个额外的 task 工具，用于创建子代理。
'''
task工具格式：
name："task" -- 工具名称
description：工具描述，提示模型这个工具是用来创建子代理的。
input_schema：工具的输入模式，要求输入一个对象，包含两个属性：
- prompt：字符串类型，子代理的任务描述。
- description：字符串类型，子任务的简短描述（可选）。

调用task工具时，模型需要提供一个 prompt 来描述子代理的任务，以及一个可选的 description 来简要说明这个任务的内容。
主代理在处理工具调用时，如果遇到 task 工具，就会调用 run_subagent 函数来执行子代理，并将子代理的总结结果作为工具结果返回给主代理。
'''

PARENT_TOOLS = CHILD_TOOLS + [
    {"name": "task", "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "description": {"type": "string", "description": "Short description of the task"}}, "required": ["prompt"]}},
]


def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=PARENT_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 如果工具调用是 task，调用 run_subagent 来执行子代理，并将子代理的总结结果作为工具结果返回给主代理。
                if block.name == "task":
                    
                    desc = block.input.get("description", "subtask")
                    prompt = block.input.get("prompt", "")
                    print(f"> task ({desc}): {prompt[:80]}")
                    output = run_subagent(prompt)
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                print(f"  {str(output)[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
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
