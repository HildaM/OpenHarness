"""Higher-level system prompt assembly.

本文件是 System Prompt 的「总装车间」，将 8 个不同来源的内容片段
按固定顺序拼接成一个完整的 System Prompt 字符串，发送给 LLM。

调用链路：
    runtime.py handle_line()
        → build_runtime_system_prompt()     ← 本文件的核心函数
            → build_system_prompt()         ← prompts/system_prompt.py（基础角色定义 + 环境信息）
            → _build_skills_section()       ← 本文件（技能列表）
            → load_claude_md_prompt()       ← prompts/claudemd.py（项目指令）
            → load_memory_prompt()          ← memory/memdir.py（MEMORY.md）
            → find_relevant_memories()      ← memory/search.py（基于输入的记忆检索）

关键设计：
    - 每次用户输入都会重新调用 build_runtime_system_prompt()
    - 因为 latest_user_prompt 不同会导致记忆检索结果不同
    - 配置也可能被斜杠命令修改（如 /model、/effort）
"""

from __future__ import annotations

from pathlib import Path

from openharness.config.paths import get_project_issue_file, get_project_pr_comments_file
from openharness.config.settings import Settings
from openharness.memory import find_relevant_memories, load_memory_prompt
from openharness.prompts.claudemd import load_claude_md_prompt
from openharness.prompts.system_prompt import build_system_prompt
from openharness.skills.loader import load_skill_registry


def _build_skills_section(cwd: str | Path) -> str | None:
    """构建技能列表片段，列出所有可用技能供 LLM 知晓。

    加载来源：
        1. 内置技能: src/openharness/skills/bundled/content/*.md（7 个）
        2. 用户技能: ~/.openharness/skills/*.md
        3. 插件技能: 各插件目录下的 .md 文件

    生成的内容示例：
        # Available Skills
        - **commit**: Create clean, well-structured git commits
        - **review**: Review code for bugs, security issues
        - **debug**: Diagnose and fix bugs systematically
        ...
    """
    registry = load_skill_registry(cwd)
    skills = registry.list_skills()
    if not skills:
        return None
    lines = [
        "# Available Skills",
        "",
        "The following skills are available via the `skill` tool. "
        "When a user's request matches a skill, invoke it with `skill(name=\"<skill_name>\")` "
        "to load detailed instructions before proceeding.",
        "",
    ]
    for skill in skills:
        lines.append(f"- **{skill.name}**: {skill.description}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# build_runtime_system_prompt() — System Prompt 的总装函数
#
# 最终输出的 Prompt 结构（8 个片段按 "\n\n" 拼接）：
#
#   ┌─────────────────────────────────────────────┐
#   │ ① 基础角色定义（~55 行固定文本）              │  ← build_system_prompt()
#   │    "You are OpenHarness..."                  │
#   │    + 系统规则 + 任务准则 + 工具使用指南        │
#   │    + # Environment                           │
#   │      OS: macOS, Git: yes (branch: main)      │
#   ├─────────────────────────────────────────────┤
#   │ ② Fast Mode 提示（可选）                      │  ← settings.fast_mode
#   ├─────────────────────────────────────────────┤
#   │ ③ Effort/Passes 设置                         │  ← settings.effort + passes
#   ├─────────────────────────────────────────────┤
#   │ ④ 可用技能列表                                │  ← _build_skills_section()
#   │    "- commit: Create clean git commits"       │
#   ├─────────────────────────────────────────────┤
#   │ ⑤ CLAUDE.md 项目指令                         │  ← load_claude_md_prompt()
#   │    从 cwd 向上搜索 CLAUDE.md 文件             │
#   ├─────────────────────────────────────────────┤
#   │ ⑥ Issue / PR 上下文（可选）                   │  ← .openharness/issue.md
#   ├─────────────────────────────────────────────┤
#   │ ⑦ MEMORY.md 持久化记忆                       │  ← load_memory_prompt()
#   ├─────────────────────────────────────────────┤
#   │ ⑧ 相关记忆（基于用户输入动态检索）             │  ← find_relevant_memories()
#   │    每次用户输入不同，检索结果也不同             │
#   └─────────────────────────────────────────────┘
#
# ═══════════════════════════════════════════════════════════════════════════
def build_runtime_system_prompt(
    settings: Settings,
    *,
    cwd: str | Path,
    # latest_user_prompt: 当前用户输入的文本
    # 用于片段 ⑧ 的记忆检索——不同的输入会匹配到不同的记忆文件
    # 这就是为什么 handle_line() 每次都要重新调用本函数
    latest_user_prompt: str | None = None,
) -> str:
    """Build the runtime system prompt with project instructions and memory."""

    # ──── 片段 ①：基础角色定义 + 环境信息 ────
    # build_system_prompt() 内部逻辑：
    #   - 如果 settings.system_prompt 不为 None → 完全替换基础 Prompt（用户自定义）
    #   - 否则使用内置的 _BASE_SYSTEM_PROMPT（~55 行的角色定义和行为准则）
    #   - 拼接 # Environment 段落（OS、Shell、Git、Python 版本、日期等）
    sections = [build_system_prompt(custom_prompt=settings.system_prompt, cwd=str(cwd))]

    # ──── 片段 ②：Fast Mode 提示（可选） ────
    # 当用户在 settings.json 中设置 fast_mode: true 时生效
    # 告诉 LLM 偏向简短回复、减少工具调用
    if settings.fast_mode:
        sections.append(
            "# Session Mode\nFast mode is enabled. Prefer concise replies, minimal tool use, and quicker progress over exhaustive exploration."
        )

    # ──── 片段 ③：Effort / Passes 设置 ────
    # 告诉 LLM 当前的推理深度要求
    # effort: low/medium/high/max — 控制回答的详细程度
    # passes: 迭代次数 — 控制多遍检查
    sections.append(
        "# Reasoning Settings\n"
        f"- Effort: {settings.effort}\n"
        f"- Passes: {settings.passes}\n"
        "Adjust depth and iteration count to match these settings while still completing the task."
    )

    # ──── 片段 ④：可用技能列表 ────
    # 让 LLM 知道有哪些技能可以按需加载
    # 技能是 .md 文件，只在 LLM 主动调用 skill 工具时才注入完整内容
    # 这里只列出名称和描述，节省 Token
    skills_section = _build_skills_section(cwd)
    if skills_section:
        sections.append(skills_section)

    # ──── 片段 ⑤：CLAUDE.md 项目指令 ────
    # 从 cwd 向上逐级搜索 CLAUDE.md 文件（类似 .gitignore 的查找方式）
    # 搜索路径：cwd/CLAUDE.md → cwd/.claude/CLAUDE.md → parent/CLAUDE.md → ...
    # 也会加载 .claude/rules/*.md 中的规则文件
    # 每个文件内容截断到 12000 字符，防止超长
    claude_md = load_claude_md_prompt(cwd)
    if claude_md:
        sections.append(claude_md)

    # ──── 片段 ⑥：Issue / PR 上下文（可选） ────
    # 读取 .openharness/issue.md 和 .openharness/pr_comments.md
    # 这些文件通常由外部工具写入（如 CI/CD 流水线），为 Agent 提供任务上下文
    # 内容截断到 12000 字符
    for title, path in (
        ("Issue Context", get_project_issue_file(cwd)),
        ("Pull Request Comments", get_project_pr_comments_file(cwd)),
    ):
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                sections.append(f"# {title}\n\n```md\n{content[:12000]}\n```")

    # ──── 片段 ⑦ + ⑧：记忆系统 ────
    if settings.memory.enabled:
        # 片段 ⑦：MEMORY.md 入口文件
        # 读取 .openharness/memory/MEMORY.md（如果存在）
        # 这是项目级的持久化记忆，跨会话保留
        # max_entrypoint_lines 限制读取行数（默认 200 行）
        memory_section = load_memory_prompt(
            cwd,
            max_entrypoint_lines=settings.memory.max_entrypoint_lines,
        )
        if memory_section:
            sections.append(memory_section)

        # 片段 ⑧：基于用户输入的动态记忆检索
        # 这是 System Prompt 需要每次重建的核心原因：
        #   - 用户输入 "Fix the database bug" → 检索到数据库相关的记忆文件
        #   - 用户输入 "Add a REST API" → 检索到 API 设计相关的记忆文件
        #
        # find_relevant_memories() 的检索算法：
        #   1. 将用户输入分词（ASCII 3+ 字符 + 中文逐字）
        #   2. 扫描 .openharness/memory/ 下所有 .md 文件
        #   3. 匹配 token：元数据（name/description）命中权重 2x，正文命中权重 1x
        #   4. 按得分排序，取 top N（默认 max_results=5）
        #
        # 每个匹配到的记忆文件内容截断到 8000 字符
        if latest_user_prompt:
            relevant = find_relevant_memories(
                latest_user_prompt,
                cwd,
                max_results=settings.memory.max_files,
            )
            if relevant:
                lines = ["# Relevant Memories"]
                for header in relevant:
                    content = header.path.read_text(encoding="utf-8", errors="replace").strip()
                    lines.extend(
                        [
                            "",
                            f"## {header.path.name}",
                            "```md",
                            content[:8000],
                            "```",
                        ]
                    )
                sections.append("\n".join(lines))

    # 最终拼接：所有非空片段用 "\n\n" 连接成一个完整的 System Prompt
    return "\n\n".join(section for section in sections if section.strip())
