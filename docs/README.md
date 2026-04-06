# docs 文档目录

## overview/ — 项目总览
- [PROJECT_ANALYSIS.md](overview/PROJECT_ANALYSIS.md) — 完整项目分析文档
- [QUICKSTART.md](overview/QUICKSTART.md) — 从零到一快速启动指南

## learning/ — 源码学习（按阅读顺序排列）

### 外壳层
1. [STARTUP_FLOW.md](learning/STARTUP_FLOW.md) — 后端启动流程全景（7 层调用栈）
2. [MAIN_ENTRY_DEEP_DIVE.md](learning/MAIN_ENTRY_DEEP_DIVE.md) — `cli.py` main() 入口函数深度剖析
3. [APP_UI_ROUTING.md](learning/APP_UI_ROUTING.md) — `ui/app.py` UI 路由层深度剖析

### 通信层
4. [REACT_LAUNCHER.md](learning/REACT_LAUNCHER.md) — `ui/react_launcher.py` 前端启动器
5. [FRONTEND_BACKEND_ARCHITECTURE.md](learning/FRONTEND_BACKEND_ARCHITECTURE.md) — 前后端双进程通信架构（7 文件联合分析）

### 核心层
6. [RUNTIME_AND_AGENT_LOOP.md](learning/RUNTIME_AND_AGENT_LOOP.md) — `runtime.py` + `query_engine.py` + `query.py` 核心运行时与 Agent 循环
7. [ENGINE_PACKAGE.md](learning/ENGINE_PACKAGE.md) — `engine/` 包全景分析（6 文件：模型、事件、成本、引擎、循环）
8. [ENGINE_DEEP_DIVE.md](learning/ENGINE_DEEP_DIVE.md) — 消息模型、对话压缩与成本追踪深入

### 专题
9. [WHY_YIELD.md](learning/WHY_YIELD.md) — 为什么整个项目都在用 `yield`？流式数据管道解析

## improvements/ — 缺陷记录与优化计划
- [TODO.md](improvements/TODO.md) — 项目缺陷与优化 TODO 列表

## 原有文档
- [SHOWCASE.md](SHOWCASE.md) — 使用案例展示
