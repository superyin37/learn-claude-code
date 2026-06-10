#!/usr/bin/env python3
# Harness: the loop -- the model's first connection to the real world.
"""
s01_agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.
"""

import os
import subprocess

try:
    import readline
    # #143 UTF-8 backspace fix for macOS libedit
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(override=True)

# 如果使用自定义的 Anthropic API URL，移除默认的认证令牌以避免冲突
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化 Anthropic 客户端和模型 ID
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

# 从环境变量获取模型 ID （例如 "claude-sonnet-4-6"）
MODEL = os.environ["MODEL_ID"]

# 定义system prompt，告诉模型它是一个编码代理，并鼓励它使用工具来解决任务
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# 定义工具列表，目前只有一个工具：bash
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


# 定义一个函数来安全地执行 bash 命令，避免执行危险命令，并捕获输出和错误
def run_bash(command: str) -> str:

    # 简单的安全检查，阻止一些常见的危险命令
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    
    # 尝试执行命令，并捕获输出和错误，设置超时时间为120秒
    try:
        # subprocess.run() 是 Python 用来“启动并控制外部程序”的标准库函数，
        # 是一个更高级的接口，推荐使用它来替代 os.system()，因为它提供了更多的控制和安全性。
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        
        # 将标准输出和标准错误合并，并限制返回的长度
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# -- The core pattern: a while loop that calls tools until the model stops --
def agent_loop(messages: list):
    while True:

        # 调用模型，传入当前的消息历史和工具列表
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        
        # Append assistant turn
        # 将模型的回复添加到消息历史中
        messages.append({"role": "assistant", "content": response.content})


        # If the model didn't call a tool, we're done
        # 如果模型的回复不是工具调用类型，说明它已经完成了任务，退出循环
        if response.stop_reason != "tool_use":
            return
        
        # Execute each tool call, collect results
        # 执行每个工具调用，收集结果
        results = []
        for block in response.content:
            if block.type == "tool_use":

                # 打印工具调用的命令，使用 ANSI 转义码让它变成黄色
                print(f"\033[33m$ {block.input['command']}\033[0m")

                # 调用 run_bash 函数执行命令，并打印输出的前200个字符
                # 使用 ANSI 转义码让输出变成绿色
                output = run_bash(block.input["command"])

                print(f"\033[32m{output[:200]}\033[0m")

                # 将工具结果添加到 results 列表中，关联到对应的工具调用 ID
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
                
        # 将工具结果作为用户消息添加到消息历史中，供模型下一轮使用                        
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":

    # 初始化一个空的消息历史列表
    history = []

    while True:
        try:
            # 从用户输入获取查询，使用 ANSI 转义码让提示符变成青色
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        # 如果用户输入 "q", "exit" 或者空字符串，退出循环
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 将用户查询添加到消息历史中，并调用 agent_loop 进入代理循环
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # 回复history的最后一条消息的内容，如果它是一个列表（包含工具结果），则打印其中的文本块
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
