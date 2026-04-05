# OpenHarness 从零到一快速启动指南

本文档将带你从一台全新机器开始，一步步完成 OpenHarness 的安装、配置、启动和基础使用。

---

## 目录

- [1. 环境要求](#1-环境要求)
- [2. 获取源代码](#2-获取源代码)
- [3. 安装 Python 依赖](#3-安装-python-依赖)
- [4. 安装前端依赖（可选）](#4-安装前端依赖可选)
- [5. 配置 API Key](#5-配置-api-key)
  - [5.1 Anthropic（默认）](#51-anthropic默认)
  - [5.2 OpenAI 兼容格式](#52-openai-兼容格式)
  - [5.3 国内模型（DashScope / DeepSeek / Kimi）](#53-国内模型dashscope--deepseek--kimi)
  - [5.4 GitHub Copilot](#54-github-copilot)
  - [5.5 本地模型（Ollama）](#55-本地模型ollama)
- [6. 首次启动](#6-首次启动)
- [7. 非交互模式（脚本/CI 集成）](#7-非交互模式脚本ci-集成)
- [8. 常用斜杠命令](#8-常用斜杠命令)
- [9. 配置文件详解](#9-配置文件详解)
- [10. 添加自定义技能](#10-添加自定义技能)
- [11. 安装插件](#11-安装插件)
- [12. 配置 MCP 服务器](#12-配置-mcp-服务器)
- [13. 项目级配置（CLAUDE.md）](#13-项目级配置claudemd)
- [14. 持久化记忆](#14-持久化记忆)
- [15. 会话管理](#15-会话管理)
- [16. 权限模式](#16-权限模式)
- [17. 定时任务（Cron）](#17-定时任务cron)
- [18. 运行测试](#18-运行测试)
- [19. 常见问题排查](#19-常见问题排查)
- [20. 目录结构速查](#20-目录结构速查)

---

## 1. 环境要求

| 依赖 | 最低版本 | 说明 |
|------|----------|------|
| **Python** | 3.10+ | 推荐 3.11 或 3.12 |
| **uv** | 最新版 | Python 包管理器，[安装文档](https://docs.astral.sh/uv/) |
| **Node.js** | 18+ | **可选**，仅 React 终端 UI 需要 |
| **npm** | 随 Node.js | **可选**，前端依赖安装 |
| **Git** | 2.0+ | 克隆仓库 + 部分工具依赖 |

### 安装 uv（如果没有）

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# 或 Homebrew
brew install uv

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 安装 Node.js（可选，交互模式需要）

```bash
# macOS
brew install node

# 或使用 nvm
nvm install 18
```

> **注意**：如果不安装 Node.js，你仍然可以使用 `-p` 非交互模式。交互式 TUI 界面需要 Node.js。

---

## 2. 获取源代码

```bash
git clone https://github.com/HKUDS/OpenHarness.git
cd OpenHarness
```

---

## 3. 安装 Python 依赖

```bash
# 安装核心依赖 + 开发依赖
uv sync --extra dev
```

这会自动创建虚拟环境（`.venv/`）并安装所有依赖包，包括：
- `anthropic` / `openai` — LLM SDK
- `typer` / `rich` / `textual` — CLI 和终端 UI
- `pydantic` — 数据验证
- `mcp` — MCP 协议
- `pytest` — 测试框架（dev 依赖）

### 验证安装

```bash
# 方式一：通过 uv 运行
uv run oh --help

# 方式二：激活虚拟环境后直接运行
source .venv/bin/activate   # macOS/Linux
oh --help
```

你应该看到类似输出：

```
Usage: openharness [OPTIONS] COMMAND [ARGS]

  Oh my Harness! An AI-powered coding assistant.
  ...
```

---

## 4. 安装前端依赖（可选）

交互式 TUI 界面需要安装前端依赖。**首次运行 `oh` 时会自动安装**，你也可以提前手动安装：

```bash
cd frontend/terminal
npm install
cd ../..
```

> 如果不想使用前端 TUI，跳过此步，仅使用 `-p` 非交互模式即可。

---

## 5. 配置 API Key

OpenHarness 支持多种模型 Provider，选择你使用的一种进行配置。

### 5.1 Anthropic（默认）

最简单的方式——设置环境变量：

```bash
export ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx
```

直接运行：

```bash
uv run oh
```

默认使用 `claude-sonnet-4-20250514` 模型。切换模型：

```bash
uv run oh --model claude-opus-4-20250514
uv run oh --model claude-haiku-4-20250514
```

### 5.2 OpenAI 兼容格式

适用于所有支持 `/v1/chat/completions` 的 Provider：

```bash
export OPENHARNESS_API_FORMAT=openai
export OPENAI_API_KEY=sk-xxxxxxxxxxxx
export OPENHARNESS_BASE_URL=https://api.openai.com/v1
export OPENHARNESS_MODEL=gpt-4o

uv run oh
```

或在命令行中指定：

```bash
uv run oh --api-format openai \
  --base-url "https://api.openai.com/v1" \
  --api-key "sk-xxxxxxxxxxxx" \
  --model "gpt-4o"
```

### 5.3 国内模型（DashScope / DeepSeek / Kimi）

#### 阿里 DashScope（通义千问）

```bash
export OPENHARNESS_API_FORMAT=openai
export OPENAI_API_KEY=sk-xxxxxxxxxxxx
export OPENHARNESS_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export OPENHARNESS_MODEL=qwen3.5-flash

uv run oh
```

#### DeepSeek

```bash
export OPENHARNESS_API_FORMAT=openai
export OPENAI_API_KEY=sk-xxxxxxxxxxxx
export OPENHARNESS_BASE_URL=https://api.deepseek.com
export OPENHARNESS_MODEL=deepseek-chat

uv run oh
```

#### Kimi（Moonshot）— Anthropic 兼容格式

```bash
export ANTHROPIC_BASE_URL=https://api.moonshot.cn/anthropic
export ANTHROPIC_API_KEY=your_kimi_api_key
export ANTHROPIC_MODEL=kimi-k2.5

uv run oh
```

#### SiliconFlow

```bash
uv run oh --api-format openai \
  --base-url "https://api.siliconflow.cn/v1" \
  --api-key "sk-xxxxxxxxxxxx" \
  --model "deepseek-ai/DeepSeek-V3"
```

### 5.4 GitHub Copilot

使用已有的 GitHub Copilot 订阅，无需 API Key：

```bash
# 第一步：登录（会打开浏览器进行 OAuth 授权）
uv run oh auth copilot-login

# 第二步：使用 Copilot 作为后端
uv run oh --api-format copilot

# 查看认证状态
uv run oh auth status

# 注销
uv run oh auth copilot-logout
```

支持 GitHub Enterprise：

```bash
uv run oh auth copilot-login
# 选择 "2. GitHub Enterprise" 并输入你的 Enterprise 域名
```

### 5.5 本地模型（Ollama）

```bash
# 先启动 Ollama 并拉取模型
ollama pull qwen2.5-coder:32b

# 使用 OpenAI 兼容格式连接
uv run oh --api-format openai \
  --base-url "http://localhost:11434/v1" \
  --api-key "ollama" \
  --model "qwen2.5-coder:32b"
```

---

## 6. 首次启动

### 交互模式（默认）

```bash
uv run oh
```

启动后你会看到 React TUI 界面，包括：
- **欢迎横幅**
- **输入框** — 输入你的问题或指令
- **键盘提示** — `enter` 发送、`/` 打开命令、`↑↓` 浏览历史、`ctrl+c` 退出

试试输入：

```
Inspect this repository and summarize its architecture.
```

### 工具执行时的权限确认

默认模式下，写操作（编辑文件、执行命令）需要你确认：
- 按 `y` 允许
- 按 `n` 拒绝

### 一键体验（不进入交互界面）

```bash
# 单次提问，打印回答后退出
uv run oh -p "用一段话介绍这个项目"
```

---

## 7. 非交互模式（脚本/CI 集成）

非交互模式（`-p` / `--print`）不需要 Node.js，适合自动化场景：

```bash
# 纯文本输出（默认）
uv run oh -p "Explain the permission system in this project"

# JSON 输出（适合程序化处理）
uv run oh -p "List all tool names" --output-format json

# 流式 JSON（实时事件流）
uv run oh -p "Fix the type error in main.py" --output-format stream-json
```

### 在脚本中使用

```bash
#!/bin/bash
RESULT=$(uv run oh -p "Summarize README.md" --output-format json)
echo "$RESULT" | jq -r '.text'
```

### 限制最大回合数

```bash
# 最多执行 5 轮工具调用
uv run oh -p "Run the tests and fix any failures" --max-turns 5
```

### 跳过权限确认（仅限沙箱环境）

```bash
uv run oh -p "Refactor main.py" --dangerously-skip-permissions
```

---

## 8. 常用斜杠命令

在交互模式中输入 `/` 可触发命令选择器。常用命令：

| 命令 | 功能 |
|------|------|
| `/help` | 显示所有可用命令 |
| `/status` | 显示当前会话状态（模型、权限、Token 用量等） |
| `/model <name>` | 切换模型 |
| `/clear` | 清空对话历史 |
| `/compact` | 手动压缩对话历史 |
| `/save` | 保存当前会话 |
| `/resume` | 恢复之前的会话 |
| `/permissions` | 打开权限模式选择器 |
| `/plan` | 切换 Plan Mode（仅查看，不修改） |
| `/tools` | 列出所有可用工具 |
| `/skills` | 列出所有可用技能 |
| `/plugins` | 列出所有插件 |
| `/mcp` | 查看 MCP 服务器状态 |
| `/memory` | 管理持久化记忆 |
| `/export` | 导出对话为 Markdown |
| `/version` | 显示版本信息 |
| `/exit` | 退出 |

---

## 9. 配置文件详解

全局配置文件位于 `~/.openharness/settings.json`，首次运行时自动创建。

### 完整配置示例

```json
{
  "api_key": "",
  "model": "claude-sonnet-4-20250514",
  "max_tokens": 16384,
  "base_url": null,
  "api_format": "anthropic",
  "max_turns": 200,
  "permission": {
    "mode": "default",
    "allowed_tools": [],
    "denied_tools": [],
    "path_rules": [
      {"pattern": "/etc/*", "allow": false},
      {"pattern": "~/.ssh/*", "allow": false}
    ],
    "denied_commands": ["rm -rf /", "DROP TABLE *"]
  },
  "memory": {
    "enabled": true,
    "max_files": 5,
    "max_entrypoint_lines": 200
  },
  "hooks": {},
  "mcp_servers": {},
  "enabled_plugins": {},
  "theme": "default",
  "vim_mode": false,
  "fast_mode": false,
  "effort": "medium",
  "verbose": false
}
```

### 配置优先级

```
CLI 参数 > 环境变量 > settings.json > 默认值
```

### 支持的环境变量

| 环境变量 | 作用 |
|----------|------|
| `ANTHROPIC_API_KEY` | Anthropic API Key |
| `OPENAI_API_KEY` | OpenAI 格式 API Key |
| `ANTHROPIC_BASE_URL` / `OPENHARNESS_BASE_URL` | API 基础 URL |
| `ANTHROPIC_MODEL` / `OPENHARNESS_MODEL` | 模型名称 |
| `OPENHARNESS_API_FORMAT` | API 格式：`anthropic` / `openai` / `copilot` |
| `OPENHARNESS_MAX_TOKENS` | 最大输出 Token |
| `OPENHARNESS_MAX_TURNS` | 最大工具调用轮数 |
| `OPENHARNESS_CONFIG_DIR` | 自定义配置目录 |
| `OPENHARNESS_DATA_DIR` | 自定义数据目录 |
| `OPENHARNESS_LOGS_DIR` | 自定义日志目录 |

---

## 10. 添加自定义技能

技能是按需加载的 Markdown 知识文件。创建一个 `.md` 文件放到技能目录即可。

### 目录位置

```
~/.openharness/skills/        # 全局技能目录
```

### 创建示例技能

```bash
mkdir -p ~/.openharness/skills
cat > ~/.openharness/skills/my-skill.md << 'EOF'
---
name: my-skill
description: 我的自定义领域专家知识
---

# My Skill

## When to use

当用户询问关于 [你的领域] 的问题时使用。

## Workflow

1. 首先分析用户需求
2. 然后按照以下步骤操作...

## Rules

- 规则一：...
- 规则二：...
EOF
```

### 验证技能加载

在交互模式中输入 `/skills`，应该能看到你的技能。

### 内置技能

项目自带 7 个技能：`commit`、`review`、`debug`、`plan`、`test`、`simplify`、`diagnose`。

---

## 11. 安装插件

OpenHarness 兼容 [claude-code 插件格式](https://github.com/anthropics/claude-code/tree/main/plugins)。

### 通过 CLI 安装

```bash
# 从本地路径安装
uv run oh plugin install /path/to/my-plugin

# 查看已安装插件
uv run oh plugin list

# 卸载
uv run oh plugin uninstall my-plugin
```

### 手动安装

将插件目录放到以下位置之一：

```
~/.openharness/plugins/<plugin-name>/      # 全局插件
.openharness/plugins/<plugin-name>/        # 项目级插件
```

插件目录结构：

```
my-plugin/
├── plugin.json              # 或 .claude-plugin/plugin.json
├── commands/                # 斜杠命令（.md 文件）
├── hooks/
│   └── hooks.json           # 生命周期钩子
└── agents/                  # Agent 定义（.md 文件）
```

---

## 12. 配置 MCP 服务器

MCP（Model Context Protocol）让 Agent 能访问外部工具和数据源。

### 通过 CLI 添加

```bash
# 添加一个 Stdio 类型的 MCP 服务器
uv run oh mcp add my-server '{"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}'

# 查看已配置的服务器
uv run oh mcp list

# 移除
uv run oh mcp remove my-server
```

### 通过配置文件添加

编辑 `~/.openharness/settings.json`：

```json
{
  "mcp_servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    "my-http-server": {
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

### 通过 CLI 参数临时加载

```bash
uv run oh --mcp-config '{"my-server": {"command": "my-mcp-server"}}'
```

---

## 13. 项目级配置（CLAUDE.md）

在项目根目录创建 `CLAUDE.md`，其内容会自动注入到 System Prompt 中，让 Agent 了解项目上下文：

```bash
cat > CLAUDE.md << 'EOF'
# 项目说明

这是一个 Python Web 项目，使用 FastAPI + PostgreSQL。

## 代码规范

- 使用 ruff 进行代码格式化
- 所有 API 需要写单元测试
- 提交信息使用 Conventional Commits 格式

## 重要文件

- `app/main.py` — 应用入口
- `app/models/` — 数据模型
- `tests/` — 测试目录

## 运行命令

- `uv run pytest` — 运行测试
- `uv run ruff check .` — 代码检查
EOF
```

Agent 启动时会自动发现并加载该文件。

---

## 14. 持久化记忆

记忆系统让 Agent 跨会话记住重要信息。

### 创建记忆文件

```bash
mkdir -p .openharness/memory
cat > .openharness/memory/MEMORY.md << 'EOF'
# 项目记忆

## 团队偏好
- 代码风格：Google Python Style
- PR 流程：至少一人 Review

## 重要决策
- 2026-03: 数据库迁移到 PostgreSQL 16
EOF
```

### 在交互模式中管理记忆

```
/memory                    # 查看记忆文件列表
/memory add "重要信息"      # 添加记忆条目
```

记忆文件存储在 `.openharness/memory/` 目录，会自动注入到 System Prompt 并参与相关性检索。

---

## 15. 会话管理

### 继续上次会话

```bash
# 继续当前目录最近的会话
uv run oh --continue
# 或简写
uv run oh -c
```

### 恢复指定会话

```bash
# 打开会话选择器
uv run oh --resume
# 或简写
uv run oh -r

# 按 session ID 恢复
uv run oh --resume abc123def456
```

### 命名会话

```bash
uv run oh --name "重构用户模块"
```

### 在交互模式中

```
/save                     # 保存当前会话
/resume                   # 恢复历史会话（弹出选择器）
/export                   # 导出为 Markdown
```

---

## 16. 权限模式

### 三种权限模式

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| `default` | 读操作自动通过，写操作需确认 | 日常开发（默认） |
| `full_auto` | 允许所有操作 | 沙箱环境、CI |
| `plan` | 阻止所有写操作 | 大型重构前的规划阶段 |

### 切换方式

```bash
# CLI 参数
uv run oh --permission-mode full_auto

# 交互模式中
/permissions              # 打开选择器
/plan                     # 快速切换 Plan Mode
```

---

## 17. 定时任务（Cron）

OpenHarness 内置 Cron 调度器，可以定时执行 Agent 任务：

```bash
# 启动调度器守护进程
uv run oh cron start

# 查看状态
uv run oh cron status

# 列出所有任务
uv run oh cron list

# 查看执行历史
uv run oh cron history

# 查看日志
uv run oh cron logs

# 停止调度器
uv run oh cron stop
```

在交互模式中也可以让 Agent 创建定时任务。

---

## 18. 运行测试

```bash
# 运行所有单元/集成测试（114 个）
uv run pytest -q

# 运行特定模块的测试
uv run pytest tests/test_api/ -v
uv run pytest tests/test_tools/ -v
uv run pytest tests/test_engine/ -v

# 代码检查
uv run ruff check src tests scripts

# 前端类型检查（需要安装前端依赖）
cd frontend/terminal && npx tsc --noEmit && cd ../..

# E2E 测试（需要真实 API Key）
python scripts/test_harness_features.py
python scripts/test_real_skills_plugins.py
```

---

## 19. 常见问题排查

### Q: 启动报错 "No API key found"

**原因**：没有配置 API Key。

**解决**：
```bash
# Anthropic
export ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx

# 或 OpenAI 兼容
export OPENHARNESS_API_FORMAT=openai
export OPENAI_API_KEY=sk-xxxxxxxxxxxx
export OPENHARNESS_BASE_URL=https://your-provider.com/v1
```

### Q: 交互模式启动失败 "React terminal frontend is missing"

**原因**：前端依赖未安装。

**解决**：
```bash
cd frontend/terminal && npm install && cd ../..
```

或跳过前端，使用非交互模式：
```bash
uv run oh -p "Your prompt here"
```

### Q: 交互模式报错 "npm not found"

**原因**：未安装 Node.js。

**解决**：安装 Node.js 18+，或使用非交互模式。

### Q: API 请求超时或 429 错误

**原因**：请求频率过高或网络问题。

OpenHarness 内置自动重试（最多 3 次，指数退避），通常会自动恢复。如果持续出现，检查 API 额度和网络。

### Q: 如何使用代理访问 API？

```bash
export HTTPS_PROXY=http://127.0.0.1:7890
uv run oh
```

### Q: 如何查看 Debug 日志？

```bash
uv run oh --debug
```

### Q: 配置文件在哪里？

```
~/.openharness/
├── settings.json          # 全局配置
├── copilot_auth.json      # Copilot 认证信息
├── skills/                # 自定义技能
├── plugins/               # 全局插件
├── data/
│   ├── sessions/          # 会话快照
│   ├── tasks/             # 后台任务输出
│   └── cron_jobs.json     # Cron 任务注册
└── logs/
    └── cron_scheduler.log # Cron 日志
```

---

## 20. 目录结构速查

```
OpenHarness/
├── src/openharness/       # Python 核心包
│   ├── cli.py             # CLI 入口（oh 命令）
│   ├── api/               # LLM API 客户端（Anthropic/OpenAI/Copilot）
│   ├── engine/            # Agent 循环引擎
│   ├── tools/             # 42+ 内置工具
│   ├── commands/          # 54 个斜杠命令
│   ├── permissions/       # 权限系统
│   ├── config/            # 配置管理
│   ├── prompts/           # System Prompt 构建
│   ├── skills/            # 技能系统（含 7 个内置技能）
│   ├── plugins/           # 插件系统
│   ├── hooks/             # 生命周期钩子
│   ├── memory/            # 持久化记忆
│   ├── mcp/               # MCP 协议客户端
│   ├── services/          # 基础服务（压缩、会话、Cron）
│   ├── swarm/             # 多 Agent 协调
│   ├── tasks/             # 后台任务管理
│   └── ui/                # UI 层（React TUI + 非交互模式）
├── frontend/terminal/     # React/Ink 终端前端
├── tests/                 # 测试（66 个文件）
├── scripts/               # E2E 测试脚本
├── docs/                  # 文档
└── pyproject.toml         # Python 项目配置
```

---

## 快速速查表

```bash
# === 安装 ===
git clone https://github.com/HKUDS/OpenHarness.git && cd OpenHarness
uv sync --extra dev

# === 配置（三选一） ===
export ANTHROPIC_API_KEY=sk-ant-xxx                    # Anthropic
export OPENHARNESS_API_FORMAT=openai                   # OpenAI 兼容
uv run oh auth copilot-login                           # Copilot

# === 运行 ===
uv run oh                                              # 交互模式
uv run oh -p "你的问题"                                # 单次提问
uv run oh -p "Fix bugs" --output-format json           # JSON 输出
uv run oh -c                                           # 继续上次会话
uv run oh --model gpt-4o --api-format openai           # 指定模型

# === 管理 ===
uv run oh plugin list                                  # 查看插件
uv run oh mcp list                                     # 查看 MCP 服务器
uv run oh auth status                                  # 查看认证状态
uv run oh cron status                                  # 查看 Cron 状态

# === 测试 ===
uv run pytest -q                                       # 运行测试
uv run ruff check src tests scripts                    # 代码检查
```

---

*祝你 Harnessing 愉快！🎉*
