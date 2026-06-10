#!/usr/bin/env python3
# Harness: on-demand knowledge -- domain expertise, loaded when the model asks.
"""
s05_skill_loading.py - Skills

Two-layer skill injection that avoids bloating the system prompt:

    Layer 1 (cheap): skill names in system prompt (~100 tokens/skill)
    Layer 2 (on demand): full skill body in tool_result

    skills/
      pdf/
        SKILL.md          <-- frontmatter (name, description) + body
      code-review/
        SKILL.md

    System prompt:
    +--------------------------------------+
    | You are a coding agent.              |
    | Skills available:                    |
    |   - pdf: Process PDF files...        |  <-- Layer 1: metadata only
    |   - code-review: Review code...      |
    +--------------------------------------+

    When model calls load_skill("pdf"):
    +--------------------------------------+
    | tool_result:                         |
    | <skill>                              |
    |   Full PDF processing instructions   |  <-- Layer 2: full body
    |   Step 1: ...                        |
    |   Step 2: ...                        |
    | </skill>                             |
    +--------------------------------------+

Key insight: "Don't put everything in the system prompt. Load on demand."
"""

'''
这个文件展示了一个技能加载系统，允许模型在需要时动态加载特定领域的知识，而不是将所有信息都放在系统提示中。
实现细节：
- 定义了一个 SkillLoader 类，负责扫描指定目录下的技能文件（SKILL.md），解析其中的 YAML 前置内容（如技能名称和描述）以及技能主体内容。
- 在系统提示中只列出技能的名称和简短描述，避免过度膨胀系统提示。
- 当模型调用 load_skill 工具时，SkillLoader 会返回对应技能的完整内容，供模型参考。
这种设计使得模型能够在需要时获取详细信息，同时保持系统提示的简洁和清晰。

tool 是注册在系统里的可调用函数
skill 是一个md文件

skill 不是 tool，但必须通过 tool 才能进入上下文。   


什么是 skill
这里的 skill 可以先简单理解成一份围绕某类任务的可复用说明书。
它通常会告诉 agent：
- 什么时候该用它
- 做这类任务时有哪些步骤
- 有哪些注意事项

什么是 discovery
discovery 指“发现有哪些 skill 可用”。
这一层只需要很轻量的信息，例如：
- skill 名字
- 一句描述

什么是 loading
loading 指“把某个 skill 的完整正文真正读进来”。
这一层才是昂贵的，因为它会把完整内容放进当前上下文。
'''


import os
import re
import subprocess
import yaml
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv


# 加载环境
load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
SKILLS_DIR = WORKDIR / "skills"


# -- SkillLoader: scan skills/<name>/SKILL.md with YAML frontmatter --

# 设计一个 SkillLoader 类，负责扫描指定目录下的技能文件（SKILL.md），解析其中的 YAML 前置内容（如技能名称和描述）以及技能主体内容。
# 在系统提示中只列出技能的名称和简短描述，避免过度膨胀系统提示。
class SkillLoader:
    def __init__(self, skills_dir: Path):
        '''
        - skills_dir 是一个 Path 对象，指向存放技能文件的目录。
        - skills 是一个字典，键是技能名称，值是一个包含技能元信息（meta）、技能正文（body）和文件路径（path）的字典。
        - _load_all 方法会扫描 skills_dir 目录下的所有 SKILL.md 文件，解析它们的内容，并将结果存储在 skills 字典中。
        '''
        self.skills_dir = skills_dir
        self.skills = {}
        self._load_all()

    # _load_all 方法会扫描 skills_dir 目录下的所有 SKILL.md 文件，解析它们的内容，并将结果存储在 skills 字典中。
    def _load_all(self):
        # 如果技能目录不存在，直接返回。
        if not self.skills_dir.exists():
            return
        
        # 遍历技能目录下的所有 SKILL.md 文件，解析它们的内容，并将结果存储在 skills 字典中。
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text()
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}


    # _parse_frontmatter 方法使用正则表达式解析技能文件中的 YAML 前置内容，提取出技能的元信息和正文内容。
    def _parse_frontmatter(self, text: str) -> tuple:
        """Parse YAML frontmatter between --- delimiters."""
        # 使用正则表达式匹配技能文件中的 YAML 前置内容，提取出技能的元信息和正文内容。
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)

        # 如果没有匹配到 YAML 前置内容，返回一个空的元信息字典和原始文本作为正文。
        if not match:
            return {}, text
        
        # 否则，解析 YAML 前置内容，将其转换为一个字典，并返回该字典和正文内容。
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        return meta, match.group(2).strip()

    # get_descriptions 方法返回一个字符串，包含所有技能的名称和简短描述，供系统提示使用。
    def get_descriptions(self) -> str:
        """Layer 1: short descriptions for the system prompt."""
        # 如果没有技能可用，返回一个提示信息。
        if not self.skills:
            return "(no skills available)"
        # 否则，遍历所有技能，提取它们的名称和描述，格式化成一个列表，并返回该列表的字符串表示。
        lines = []
        # 遍历所有技能，提取它们的名称和描述，格式化成一个列表，并返回该列表的字符串表示。
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)


    # get_content 方法接受一个技能名称作为输入，返回该技能的完整内容，供模型在需要时加载。
    # 并作为 tool_result 返回给模型。
    def get_content(self, name: str) -> str:
        """Layer 2: full skill body returned in tool_result."""
        # 根据技能名称从 skills 字典中获取对应的技能信息，如果该技能不存在，返回一个错误提示。
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"

# 加载技能
SKILL_LOADER = SkillLoader(SKILLS_DIR)

# Layer 1: skill metadata injected into system prompt
# 在系统提示中注入技能的元信息（名称和描述），供模型参考。
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.

Skills available:
{SKILL_LOADER.get_descriptions()}"""


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


TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
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
    {"name": "load_skill", "description": "Load specialized knowledge by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "Skill name to load"}}, "required": ["name"]}},
]


def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
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
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms05 >> \033[0m")
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
