# 项目缺陷与优化计划

> 在学习源码过程中发现的设计缺陷和可优化点，按模块分类记录。
> 每个条目标注优先级（P0 紧急 / P1 重要 / P2 改进 / P3 优化），逐个攻克。

---

## 记忆系统 (`memory/`)

### 1. 记忆文件无压缩/摘要机制 — P2

**现状**：记忆文件（MEMORY.md + 独立 .md）注入 System Prompt 时只做硬截断（200 行 / 8000 字符），没有 LLM 摘要。

**问题**：当记忆文件很多或很长时，System Prompt 膨胀，挤占对话历史的 Token 空间，导致 auto_compact 更早触发。

**优化方向**：
- 参考 `services/compact/` 的两级压缩策略，为记忆系统增加 LLM 摘要能力
- 或引入简单的 TF-IDF / embedding 检索替代当前的关键词匹配
- 可以在记忆文件超过阈值时自动触发摘要

**涉及文件**：
- `memory/search.py` — 检索算法
- `memory/memdir.py` — MEMORY.md 加载
- `prompts/context.py:163-210` — 注入逻辑

---

## 代码结构

### 2. `commands/registry.py` 单文件过大 — P3

**现状**：1374 行 / 65KB，54 个斜杠命令全部堆在一个文件里。

**问题**：阅读和维护困难，每次修改一个命令都要在大文件中搜索。

**优化方向**：按功能分拆为多个文件（如 `commands/session.py`、`commands/model.py`、`commands/memory.py` 等）。

---

### 3. `coordinator/agent_definitions.py` 单文件过大 — P3

**现状**：44KB 单文件。

**优化方向**：按 Agent 类型拆分。

---

### 4. `swarm/permission_sync.py` (37KB) 和 `swarm/team_lifecycle.py` (28KB) — P3

**现状**：单文件过大。

**优化方向**：拆分为更细粒度的模块。

---

## 类型安全

### 5. mypy strict 未作为 CI 必检项 — P2

**现状**：`pyproject.toml` 中声明了 `mypy strict = true`，但 CI 中未启用，大量延迟导入导致 basedpyright 报错。

**问题**：IDE 中显示大量类型报错，新人容易困惑（已通过 `pyrightconfig.json` 缓解）。

**优化方向**：逐步修复类型标注，最终启用 mypy 作为 CI 检查。

---

## 功能模块

### 6. Voice 模式仅有框架未实现 — P3

**现状**：`voice/` 目录有 `voice_mode.py`、`stream_stt.py`、`keyterms.py`，但都是占位代码。

---

### 7. Vim 模式仅有占位代码 — P3

**现状**：`vim/` 目录仅有 `transitions.py`，功能未实现。

---

## 文档

### 8. 缺少 API 文档和架构图 — P2

**现状**：`docs/` 原有只有 `SHOWCASE.md`，缺少架构详解和模块 API 文档。

**优化方向**：（已部分完成——学习文档系列）继续补充工具系统、API 客户端等模块的文档。

---

## 待发现的问题

> 随着学习深入，后续发现的新问题追加在这里。

---

*最后更新：2026-04-06*
