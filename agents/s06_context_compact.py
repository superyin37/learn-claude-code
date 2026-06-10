#!/usr/bin/env python3
# Harness: compression -- clean memory for infinite sessions.
"""
s06_context_compact.py - Compact

Three-layer compression pipeline so the agent can work forever:

    Every turn:
    +------------------+
    | Tool call result |
    +------------------+
            |
            v
    [Layer 1: micro_compact]        (silent, every turn)
      Replace non-read_file tool_result content older than last 3
      with "[Previous: used {tool_name}]"
            |
            v
    [Check: tokens > 50000?]
       |               |
       no              yes
       |               |
       v               v
    continue    [Layer 2: auto_compact]
                  Save full transcript to .transcripts/
                  Ask LLM to summarize conversation.
                  Replace all messages with [summary].
                        |
                        v
                [Layer 3: compact tool]
                  Model calls compact -> immediate summarization.
                  Same as auto, triggered manually.

Key insight: "The agent can forget strategically and keep working forever."
"""


'''
这个文件展示了一个三层压缩管道的实现，旨在帮助代理在无限会话中保持上下文清晰。

目标：怎样在不丢掉主线连续性的前提下，把活跃上下文重新腾出空间。

压缩的意思：
用更短的表示方式，保留继续工作真正需要的信息。

例如：
- 大输出只保留预览，全文写到磁盘
- 很久以前的工具结果改成占位提示
- 整段长历史总结成一份摘要

每当代理调用工具并获得结果时，首先进行微观压缩（micro_compact），将旧的工具结果替换为简短的占位符。
然后检查消息的估计令牌数是否超过阈值，如果超过，则进行自动压缩（auto_compact），
将完整的对话记录保存到磁盘，并让语言模型总结对话内容，最后用总结替换所有消息。

此外，代理还可以通过调用一个特殊的工具（compact）来手动触发压缩。
这种设计允许代理在处理复杂任务时保持上下文的清晰和相关性，同时避免上下文过长导致的性能问题。
关键的洞察是，代理可以通过战略性地“忘记”旧信息来持续工作，而不丢失对当前任务的理解。

实现细节：
- 定义了一个估计令牌数的函数（estimate_tokens），用于判断何时触发自动压缩。
- micro_compact 函数遍历消息，找到旧的工具结果，并将其替换为简短的占位符，保留最近的几个结果。
- auto_compact 函数将完整的对话记录保存到磁盘，并调用语言模型生成一个总结，然后用这个总结替换所有消息。
- 在主代理的工具处理逻辑中，如果检测到调用了 compact 工具，则立即触发 auto_compact 进行压缩。
- 其他工具（bash、read_file、write_file、edit_file）在主代理和子代理中都可用，子代理直接调用这些工具来完成任务。

'''

import json
import os
import subprocess
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."

THRESHOLD = 50000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
KEEP_RECENT = 3
PRESERVE_RESULT_TOOLS = {"read_file"}

# estimate_tokens 函数是一个粗略的令牌计数器，假设平均每个令牌占用4个字符。
# 它通过将消息列表转换为字符串并计算其长度来估计总的令牌数，然后除以4得到一个近似值。
def estimate_tokens(messages: list) -> int:
    """Rough token count: ~4 chars per token."""
    return len(str(messages)) // 4


# -- Layer 1: micro_compact - replace old tool results with placeholders --
# micro_compact 函数遍历消息列表，找到所有的工具结果，并将除了最近几个之外的旧结果替换为一个简短的占位符。
def micro_compact(messages: list) -> list:
    # Collect (msg_index, part_index, tool_result_dict) for all tool_result entries
    # 遍历消息列表，找到所有工具结果的索引和内容，并将它们保存在 tool_results 列表中。
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        # 只检查用户消息中的工具结果块，假设它们在用户消息的 content 列表中以特定格式出现。
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                # 如果该部分是一个工具结果（type == "tool_result"），则将其索引和内容保存到 tool_results 列表中。
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))

    # 如果工具结果的数量不超过 KEEP_RECENT，则不进行压缩，直接返回原始消息列表。
    if len(tool_results) <= KEEP_RECENT:
        return messages

    # Find tool_name for each result by matching tool_use_id in prior assistant messages
    # 遍历消息列表，找到所有助手消息中的工具使用块，并建立一个工具使用 ID 到工具名称的映射（tool_name_map）。
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name

    # Clear old results (keep last KEEP_RECENT). Preserve read_file outputs because
    # they are reference material; compacting them forces the agent to re-read files.
    to_clear = tool_results[:-KEEP_RECENT]
    for _, _, result in to_clear:
        if not isinstance(result.get("content"), str) or len(result["content"]) <= 100:
            continue
        tool_id = result.get("tool_use_id", "")
        tool_name = tool_name_map.get(tool_id, "unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue
        result["content"] = f"[Previous: used {tool_name}]"
    return messages


# -- Layer 2: auto_compact - save transcript, summarize, replace messages --
# auto_compact 函数首先将完整的对话记录保存到磁盘上的一个文件中，然后调用语言模型生成一个总结性的文本，
def auto_compact(messages: list, focus: str = "") -> list:
    # Save full transcript to disk
    # 将完整的对话记录保存到磁盘上的一个文件中，文件名包含当前的时间戳，以便后续参考。
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    # 将消息列表中的每条消息转换为 JSON 格式，并逐行写入到 transcript_path 指定的文件中。
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"[transcript saved: {transcript_path}]")
    
    # Ask LLM to summarize
    # 调用语言模型生成一个总结性的文本，提示模型总结对话内容，并保留关键细节。将消息列表转换为字符串并附加到提示中，以供模型参考。
    conversation_text = json.dumps(messages, default=str)[-80000:]
    focus_instruction = ""
    if focus:
        focus_instruction = f" Pay special attention to preserving details about: {focus}."
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content":
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "
            "Be concise but preserve critical details."
            f"{focus_instruction}\n\n" + conversation_text}],
        max_tokens=2000,
    )
    summary = next((block.text for block in response.content if hasattr(block, "text")), "")
    if not summary:
        summary = "No summary generated."
    # Replace all messages with compressed summary
    return [
        {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
    ]


# -- Tool implementations --
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

# 定义一个新的工具 "compact"，当模型调用这个工具时，会触发手动压缩的逻辑。
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "compact":    lambda **kw: "Manual compression requested.",
}

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "compact", "description": "Trigger manual conversation compression.",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "What to preserve in the summary"}}}},
]


def agent_loop(messages: list):
    while True:
        # Layer 1: micro_compact before each LLM call
        micro_compact(messages)
        # Layer 2: auto_compact if token estimate exceeds threshold
        if estimate_tokens(messages) > THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = auto_compact(messages)
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        manual_compact = False
        compact_focus = ""
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    manual_compact = True
                    compact_focus = block.input.get("focus", "")
                    output = "Compressing..."
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})
        # Layer 3: manual compact triggered by the compact tool
        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compact(messages, focus=compact_focus)
            return


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms06 >> \033[0m")
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
