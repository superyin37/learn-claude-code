# Skill Loading 实现逻辑

本文说明 `agents/s05_skill_loading.py` 中 Skill Loading 的实现。重点包括 Skill 如何发现、如何解析、元数据如何进入 system prompt，以及完整正文如何通过 `load_skill` 工具按需进入会话。

## 1. 直观理解

Skill 可以理解成：

```text
围绕某一类任务编写的可复用说明书。
```

例如：

```text
PDF 处理步骤
代码审查规范
MCP 服务创建指南
项目部署流程
```

Skill 不直接执行动作。它告诉模型做这类任务时应该注意什么、按什么步骤做。

核心区分：

```text
Tool 让模型能做事。
Skill 让模型知道怎样做得更好。
```

## 2. 为什么不把所有知识放进 SYSTEM

如果所有 Skill 正文都放入 system prompt：

```text
上下文会迅速膨胀。
大量无关知识干扰当前任务。
每轮模型调用都重复支付这些 token。
Skill 越多，系统越难扩展。
```

s05 使用两层加载：

```text
Layer 1: Discovery
  system prompt 只放名称和简短描述。

Layer 2: Loading
  模型需要时调用 load_skill。
  完整正文作为 tool_result 进入 messages。
```

关系：

```text
skills/**/SKILL.md
  -> SkillLoader 扫描
  -> 元数据进入 SYSTEM
  -> 模型发现可用 Skill
  -> 调用 load_skill(name)
  -> 正文进入 messages
```

## 3. Skill 文件结构

典型 `SKILL.md`：

```markdown
---
name: code-review
description: Review code for bugs, regressions, and missing tests.
tags: review, quality
---

# Code Review

1. Read the diff.
2. Find correctness risks.
3. Report findings by severity.
```

文件分为两部分：

```text
frontmatter
  name、description、tags 等轻量元数据。

body
  完整任务说明、步骤和注意事项。
```

项目目录约定：

```text
skills/
  code-review/
    SKILL.md
  pdf/
    SKILL.md
```

## 4. SkillLoader 初始化

```python
class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
        self._load_all()
```

创建实例：

```python
SKILLS_DIR = WORKDIR / "skills"
SKILL_LOADER = SkillLoader(SKILLS_DIR)
```

初始化时立即扫描所有 Skill，并缓存到内存：

```python
self.skills = {
    "code-review": {
        "meta": {...},
        "body": "...",
        "path": ".../skills/code-review/SKILL.md",
    },
}
```

## 5. _load_all：发现所有 Skill

```python
def _load_all(self):
    if not self.skills_dir.exists():
        return

    for f in sorted(self.skills_dir.rglob("SKILL.md")):
        text = f.read_text()
        meta, body = self._parse_frontmatter(text)
        name = meta.get("name", f.parent.name)
        self.skills[name] = {
            "meta": meta,
            "body": body,
            "path": str(f),
        }
```

逻辑：

```text
检查 skills 目录是否存在。
递归查找所有 SKILL.md。
读取文本。
解析 frontmatter 和 body。
优先使用 meta.name 作为技能名。
没有 name 时使用父目录名。
保存到 self.skills。
```

这是 discovery 的数据准备阶段。

## 6. _parse_frontmatter：解析元数据

```python
def _parse_frontmatter(self, text: str) -> tuple:
    match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)

    if not match:
        return {}, text

    meta = {}
    for line in match.group(1).strip().splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()

    return meta, match.group(2).strip()
```

逻辑：

```text
匹配文件开头的 --- frontmatter ---。
逐行按第一个冒号拆分 key/value。
剩余部分作为正文。
```

如果没有 frontmatter：

```text
meta = {}
body = 完整文件文本
```

注意：这是简化解析器，不是真正的 YAML parser。复杂数组、多行字符串和嵌套对象不能可靠处理。

## 7. get_descriptions：Discovery 层

```python
def get_descriptions(self) -> str:
    if not self.skills:
        return "(no skills available)"

    lines = []
    for name, skill in self.skills.items():
        desc = skill["meta"].get("description", "No description")
        tags = skill["meta"].get("tags", "")
        line = f"  - {name}: {desc}"
        if tags:
            line += f" [{tags}]"
        lines.append(line)
    return "\n".join(lines)
```

输出类似：

```text
  - pdf: Process PDF files safely [documents]
  - code-review: Review code for bugs and regressions [quality]
```

然后注入 system prompt：

```python
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.

Skills available:
{SKILL_LOADER.get_descriptions()}"""
```

模型每轮都能看到有哪些 Skill，但看不到完整正文。

## 8. get_content：Loading 层

```python
def get_content(self, name: str) -> str:
    skill = self.skills.get(name)
    if not skill:
        return (
            f"Error: Unknown skill '{name}'. "
            f"Available: {', '.join(self.skills.keys())}"
        )

    return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"
```

职责：

```text
按名称查找 Skill。
不存在时返回可用列表。
存在时返回完整正文。
用 <skill> 标签明确内容边界。
```

## 9. load_skill 如何接入工具系统

执行映射：

```python
TOOL_HANDLERS = {
    ...
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
}
```

模型声明：

```python
{
    "name": "load_skill",
    "description": "Load specialized knowledge by name.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name to load",
            },
        },
        "required": ["name"],
    },
}
```

所以 Skill 本身不是 Tool，但必须通过 Tool 才能进入当前会话。

## 10. 一次完整加载流程

假设用户要求审查代码：

```text
SYSTEM:
  Skills available:
    - code-review: Review code for bugs...

user:
  请审查这次改动

assistant:
  tool_use:
    name: load_skill
    input:
      name: code-review

harness:
  SKILL_LOADER.get_content("code-review")

user:
  tool_result:
    <skill name="code-review">
      完整审查步骤...
    </skill>

assistant:
  基于 Skill 调用 read_file、bash 等行动工具
```

这里有两种完全不同的 tool：

```text
load_skill
  观察/知识工具，给模型补充说明。

read_file/bash/edit_file
  行动工具，让模型操作环境。
```

## 11. Skill 如何与 messages 联动

完整 Skill 正文作为 `tool_result` 追加到 `messages`：

```python
results.append({
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": str(output),
})

messages.append({
    "role": "user",
    "content": results,
})
```

因此后续每轮模型都能看到已经加载的 Skill，直到：

```text
会话结束。
messages 被 compact。
历史被其他机制裁剪。
```

SkillLoader 本身只缓存文件内容，不记录模型已经加载过哪些 Skill。是否重复调用由模型自行判断。

## 12. Skill 与 Compact 的关系

Skill 正文进入 `messages` 后，会像普通工具结果一样占用上下文。

在 s06 中：

```text
旧 load_skill tool_result 可能被 micro_compact 替换为占位符。
auto_compact 后只保留 summary 中概括的 Skill 要点。
```

所以 Skill 的两层加载减少了初始成本，但加载后的正文仍需由上下文管理机制维护。

## 13. Skill 与 Subagent 的关系

当前 s04 和 s05 是分别演示的，s04 子 agent 工具集中没有 `load_skill`。

组合系统中可以选择：

```text
只允许父 agent 加载 Skill，再把要点写进子任务 prompt。
或者把 load_skill 也暴露给子 agent，让子 agent 自己按需加载。
```

后者通常更符合上下文隔离，因为子 agent 可以只加载自己任务需要的知识。

## 14. 如何新增一个 Skill

创建：

```text
skills/testing/SKILL.md
```

内容：

```markdown
---
name: testing
description: Plan and run focused tests for code changes.
tags: test, quality
---

# Testing

1. Identify changed behavior.
2. Run the narrowest relevant tests.
3. Expand coverage based on risk.
4. Report failures clearly.
```

下次启动程序时：

```text
SkillLoader 自动扫描。
testing 出现在 SYSTEM 的 Skills available。
模型可调用 load_skill(name="testing")。
```

无需修改 `agent_loop`。

## 15. 当前实现边界

教学版 SkillLoader 有这些限制：

```text
只在进程启动时扫描，运行中新增文件不会自动刷新。
frontmatter 不是完整 YAML 解析。
同名 Skill 会被后扫描的文件覆盖。
没有版本和依赖管理。
没有权限或信任级别。
没有记录 Skill 是否已经加载。
没有按任务或 agent 隔离缓存。
完整正文没有长度限制。
```

生产系统通常还需要：

```text
严格元数据 schema。
Skill 来源和签名校验。
按需读取而不是启动时加载全部正文。
缓存和热更新。
适用条件和优先级。
加载审计。
```

## 总结

Skill Loading 的核心是两层注入：

```text
Discovery:
  名称和描述进入 SYSTEM。
  模型知道有哪些知识可用。

Loading:
  模型调用 load_skill。
  完整正文通过 tool_result 进入 messages。
```

它实现了知识与行动的分离：

```text
Skill 提供方法和规范。
Tool 提供真实行动能力。
Agent 根据当前任务决定何时加载知识、何时采取行动。
```
