# 学习进度与后续方向

> 最后更新：2026-04-07

---

## 已完成

```
✅ 外壳层（怎么启动的）
   01 启动全景 → 02 CLI 入口 → 03 UI 路由

✅ 通信层（前后端怎么配合的）
   04 前端启动器 → 05 双进程通信协议

✅ 核心层（Agent 怎么工作的）
   06 运行时装配 + Agent 循环 → 07 engine 包全景
   → 08 对话压缩与成本追踪

✅ 子系统层（工具怎么工作的）
   10 工具系统深度剖析
   11 工具实现原理深入分析

✅ 专题
   09 为什么用 yield

✅ 已添加代码注释的文件
   cli.py        — Typer 初始化
   ui/runtime.py — build_runtime + handle_line
   ui/backend_host.py — 4 组方法分类
   prompts/context.py — System Prompt 8 个片段
   engine/query.py — Agent 循环 + 6 道关卡
   engine/query_engine.py — 门面模式 + 热更新方法
```

---

## 当前位置

**已完全看透主线 + 两大子系统**：`oh` 命令 → CLI 解析 → 模式路由 → 运行时装配 → Agent 循环 → LLM 调用 → 工具执行 → 流式返回。

工具系统（三层架构 + Pydantic 一举三得）和 API 客户端（Protocol 策略模式 + 三种客户端适配 + 流式传输 + 重试机制）均已深入理解。

---

## 后续 5 个方向（按推荐优先级排列）

### A. 工具系统 ✅ 已完成（→ 10-tool-system.md）

**为什么**：刚看完 6 道关卡的关卡 5（tool.execute），自然下一步。最轻量（76+60 行），学完能自己写工具。

| 文件 | 行数 | 内容 |
|------|------|------|
| `tools/base.py` | 76 | BaseTool 抽象类 + ToolRegistry + ToolResult |
| `tools/__init__.py` | 104 | 42 个工具的注册清单 |
| `tools/file_read_tool.py` | ~60 | 最简单的工具实现范例 |
| `tools/bash_tool.py` | ~60 | Shell 命令执行 |
| `tools/file_edit_tool.py` | ~50 | 文件编辑 |
| `tools/web_fetch_tool.py` | ~60 | 网页抓取 |

**学习目标**：理解 BaseTool 接口 → 看 2-3 个具体实现 → 能自己写一个新工具

### B. API 客户端 ✅ 已完成（→ 12-api-client.md）

**为什么**：理解 LLM 调用的底层细节，包括流式传输原理和多 Provider 适配。

| 文件 | 行数 | 内容 |
|------|------|------|
| `api/client.py` | 186 | Anthropic 客户端 + SupportsStreamingMessages Protocol + 重试 |
| `api/openai_client.py` | 343 | OpenAI 兼容客户端 + Anthropic↔OpenAI 格式转换 |
| `api/copilot_client.py` | 131 | Copilot 客户端（包装 OpenAI 客户端） |
| `api/provider.py` | 97 | Provider 自动检测 |
| `api/errors.py` | ~20 | 错误类型 |

**学习目标**：理解 3 种客户端的适配方式 → 消息格式转换 → 流式 SSE 原理

### C. 权限系统 ⭐⭐⭐

**为什么**：只有 107 行，query.py 注释中已有概要，快速收割。

| 文件 | 行数 | 内容 |
|------|------|------|
| `permissions/checker.py` | 107 | 完整的权限决策链 |
| `permissions/modes.py` | ~10 | 3 种模式枚举 |

**学习目标**：理解 DEFAULT / PLAN / FULL_AUTO 的完整判断流程

### D. 命令系统 ⭐⭐

**为什么**：了解 54 个斜杠命令怎么注册和执行。文件较大（1374 行），适合按需查阅而非通读。

| 文件 | 行数 | 内容 |
|------|------|------|
| `commands/registry.py` | 1374 | 所有斜杠命令的实现 |
| `commands/__init__.py` | 18 | 导出 |

### E. 扩展系统 ⭐⭐

**为什么**：了解插件/技能/MCP/多 Agent 的扩展能力。模块较多，适合挑感兴趣的看。

| 模块 | 核心文件 | 内容 |
|------|---------|------|
| `plugins/` | `loader.py` (170行) | 插件发现/加载/兼容 claude-code 格式 |
| `skills/` | `loader.py` (97行) | 技能加载 (.md 文件) |
| `mcp/` | `client.py` (190行) | MCP 服务器连接管理 |
| `swarm/` | `in_process.py` (650行) | 多 Agent 协调 |
| `hooks/` | `executor.py` (220行) | 生命周期钩子执行 |

---

## 已记录的项目缺陷

见 [improvements/TODO.md](../improvements/TODO.md)，共 8 项。

---

*下次学习时，从方向 C（权限系统）开始，只有 107 行，最快收割。*
