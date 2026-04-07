# 工具实现原理深入分析

> 涉及源文件：`tools/file_read_tool.py`(70行) · `tools/file_edit_tool.py`(55行) · `tools/bash_tool.py`(73行) · `utils/shell.py`(78行)
>
> 前置知识：已读完 10（工具系统深度剖析）
>
> 预计阅读时间：25 分钟

---

## 一、本文要解决的 4 个问题

上一篇（10-tool-system.md）讲的是工具系统的**架构层面** —— 三层架构、注册表模式、MCP 适配器。

本篇从架构往下走一层，追问**实现层面**的原理：

| # | 问题 | 对应章节 |
|---|------|---------|
| 1 | 为什么 `ToolInput` 和 `Tool` 要分成两个类？ | 第二章 |
| 2 | 文件读取的技术原理是什么？怎么读到指定行？ | 第三章 |
| 3 | BashTool 的命令到底在哪里执行的？ | 第四章 |
| 4 | 文件修改为什么只是"替换字符串"？不应该更复杂吗？ | 第五章 |

---

## 二、ToolInput 与 Tool 的分离设计

### 2.1 问题：为什么不把参数定义在工具类里？

看 `FileReadTool` 的代码结构，有两个类：

```python
# 类 1：参数模型（Pydantic BaseModel）
class FileReadToolInput(BaseModel):
    path: str = Field(description="Path of the file to read")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=2000)

# 类 2：工具类（BaseTool 子类）
class FileReadTool(BaseTool):
    name = "read_file"
    input_model = FileReadToolInput      # ← 引用类 1
    async def execute(self, arguments: FileReadToolInput, ...) -> ToolResult: ...
```

直觉上觉得可以合并：

```python
# 假设合并后
class FileReadTool(BaseTool):
    name = "read_file"
    path: str               # ← 参数直接放在这里？
    offset: int = 0
    limit: int = 200
```

但实际上**不能合并**，原因是**生命周期不同**。

### 2.2 核心原因：工具是单例，参数是多例

```
启动时（一次性）:
  tool = FileReadTool()                        # 工具实例：全局唯一
  tool.input_model → FileReadToolInput 类       # 类引用：全局唯一
  registry.register(tool)

第 1 次调用:
  tool.input_model.model_validate({"path": "a.py"})
  → FileReadToolInput(path="a.py", offset=0, limit=200)    # 实例 A

第 2 次调用:
  tool.input_model.model_validate({"path": "b.py", "limit": 50})
  → FileReadToolInput(path="b.py", offset=0, limit=50)     # 实例 B（与 A 无关）
```

**类是图纸，`model_validate()` 是按图纸造产品**。图纸只有一份，产品每次都是新的。

### 2.3 `input_model` 被三个地方独立使用

```
FileReadToolInput（Pydantic BaseModel）
       │
       ├─→ ① to_api_schema()           生成 JSON Schema 发给 LLM
       │     tool.input_model.model_json_schema()
       │
       ├─→ ② _execute_tool_call() 关卡3  验证 LLM 返回的参数
       │     tool.input_model.model_validate(tool_input)
       │
       └─→ ③ execute()                 作为类型安全的参数对象传入
              arguments.path / arguments.offset / arguments.limit
```

这三件事需要的是一个**独立的、可序列化的数据模型**，而不是工具本身。

### 2.4 如果强行合并会怎样？

| 问题 | 说明 |
|------|------|
| **单例不能存调用参数** | 工具只实例化一次注册到 Registry，不能拿它来存每次调用的参数 |
| **元类冲突** | 无法同时继承 `ABC`（抽象基类）和 `BaseModel`（Pydantic 的元类冲突） |
| **字段混淆** | `to_api_schema()` 无法区分哪些字段是"参数"（如 `path`）、哪些是"工具自身属性"（如 `name`） |

### 2.5 一句话总结

> **工具是单例（注册一次，调用多次），参数是多例（每次调用不同）**。分开是因为它们的生命周期不同。`input_model` 存的是**类本身**（图纸），不是实例（产品）。

---

## 三、FileReadTool — 文件读取的技术原理

### 3.1 两步读取：先读字节，再解码

```python
# file_read_tool.py 第 46-51 行
raw = path.read_bytes()                        # ① 读原始字节
if b"\x00" in raw:                              # ② 二进制检测
    return ToolResult(output="Binary file ...", is_error=True)
text = raw.decode("utf-8", errors="replace")   # ③ 字节 → 字符串
```

#### ① `path.read_bytes()` 底层调用链

```
Path.read_bytes()
  → open(path, "rb")       # 以二进制模式打开
  → f.read()               # 一次性读取全部字节到内存（返回 bytes）
  → f.close()
```

操作系统层面：`Python` → C 标准库 `fopen`/`fread` → 系统调用 `open(2)`/`read(2)` → 内核从磁盘（或页缓存）拷贝数据到用户空间缓冲区。

#### ② 为什么先 `read_bytes` 而不是直接 `read_text`？

因为中间夹了一步**二进制检测** —— 用 `b"\x00" in raw` 判断是否包含空字节。如果直接用 `path.read_text("utf-8")`，就没机会做这个检测了（而且二进制文件解码可能产生大量乱码浪费 Token）。

#### ③ `.decode("utf-8", errors="replace")`

把原始字节按 UTF-8 编码规则解码为 Python `str`（Unicode 字符串）。`errors="replace"` 表示遇到无法解码的字节用 `�`（U+FFFD）替代，而不是抛异常。

### 3.2 指定行数读取：全量读入 + 列表切片

这是一个常被误解的点 —— **并没有操作系统级别的"按行读取"，而是全量读入后用 Python 列表切片截取**。

```python
# file_read_tool.py 第 52-57 行
lines = text.splitlines()
# 例如：["import os", "import sys", "", "def main():", "    print('hello')"]
#  索引：    0            1           2       3              4

selected = lines[arguments.offset : arguments.offset + arguments.limit]
#                ^^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#                起始行（从0开始）     起始行 + 要取的行数 = 结束位置
```

#### 具体例子

文件有 1000 行，`offset=10, limit=50`：

```python
lines[10 : 10 + 50]  →  lines[10:60]  →  第 11~60 行（共 50 行）
```

#### 为什么不用逐行读取或 seek 跳转？

对一般的源代码文件（几百 KB）全量读入完全够用。只有遇到 GB 级文件时才需要逐行迭代或 `seek` 跳转，但那种文件早在二进制检测或 Token 限制处就被拦截了。

### 3.3 行号格式化 — 存在的理由

```python
numbered = [
    f"{arguments.offset + index + 1:>6}\t{line}"
    for index, line in enumerate(selected)
]
```

输出效果：

```
     1	import os
     2	import sys
     3	
     4	def main():
     5	    print("hello")
```

**为什么行号这么重要？** 因为下游的 `FileEditTool` 用的是字符串匹配（`old_str`），LLM 需要看到精确内容才能构造正确的替换参数。行号帮助 LLM 定位上下文，`offset + limit` 让它能分页读大文件而不撑爆 Token。

### 3.4 FileReadTool 完整流程图

```
execute(arguments={path:"main.py", offset:0, limit:200}, context)
  │
  ├─ 1. _resolve_path(cwd, "main.py")
  │     → Path("main.py").expanduser()     # 展开 ~
  │     → 不是绝对路径？拼接 cwd
  │     → .resolve()                        # 消除 .., symlink
  │     → /project/main.py
  │
  ├─ 2. path.exists()? path.is_dir()?      # 防御性检查
  │
  ├─ 3. path.read_bytes()                  # 一次性读入全部字节
  │     → b"\x00" in raw?                  # 二进制检测
  │
  ├─ 4. raw.decode("utf-8", errors="replace")  # 字节 → 字符串
  │     → text.splitlines()                     # 按换行拆行
  │     → lines[0:200]                          # 列表切片取范围
  │     → 添加行号格式化
  │
  └─ 5. ToolResult(output="     1\timport os\n     2\t...")
```

> **一句话**：读字节 → 检测二进制 → 解码 → 拆行 → 切片 → 加行号。分两步走（先字节后字符串）是为了在解码前做安全拦截。

---

## 四、BashTool — 命令执行链路

### 4.1 BashTool 本身不执行命令

`BashTool.execute()` 只做三件事：**超时控制、输出收集、错误判断**。真正的命令执行委托给了 `utils/shell.py`。

### 4.2 完整执行链

```
BashTool.execute(arguments={command: "ls -la"})
  │
  └→ create_shell_subprocess("ls -la", cwd=...)          # shell.py:39
       │
       ├─ resolve_shell_command("ls -la")                  # shell.py:16
       │    → 找到系统 shell（macOS/Linux → bash, Windows → powershell/cmd）
       │    → ["/bin/bash", "-lc", "ls -la"]               # 拼成 argv
       │
       ├─ wrap_command_for_sandbox(argv, settings)         # 可选的沙箱包装
       │
       └─ asyncio.create_subprocess_exec(                  # ← 真正的执行点
              "/bin/bash", "-lc", "ls -la",
              cwd="/project",
              stdout=PIPE, stderr=PIPE,
          )
          → 操作系统 fork + exec → 子进程运行 bash → bash 执行 "ls -la"
```

### 4.3 `resolve_shell_command` — 跨平台 Shell 选择

```python
# utils/shell.py 第 16-36 行
def resolve_shell_command(command: str, *, platform_name=None) -> list[str]:
    # macOS / Linux
    bash = shutil.which("bash")        # 在 PATH 中找 bash
    if bash:
        return [bash, "-lc", command]  # -l = login shell, -c = 执行字符串命令
    shell = shutil.which("sh") or os.environ.get("SHELL") or "/bin/sh"
    return [shell, "-lc", command]

    # Windows（优先级：bash > powershell > cmd）
    # ...
```

`-lc` 参数说明：
- `-l`（login）：加载用户的 `.bash_profile` / `.bashrc`，确保 PATH 等环境变量完整
- `-c`（command）：把后面的字符串当命令执行

### 4.4 `asyncio.create_subprocess_exec` — 操作系统级别

```python
process = await asyncio.create_subprocess_exec(
    "/bin/bash", "-lc", "ls -la",
    cwd="/project",
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
```

底层做的事：
1. **`fork()`** — 创建当前进程的副本（子进程）
2. **`exec()`** — 在子进程中用 `/bin/bash` 替换当前程序映像
3. **`PIPE`** — 创建管道，子进程的 stdout/stderr 通过管道传回父进程
4. **异步** — `asyncio` 把等待子进程的操作注册到事件循环，不阻塞其他协程

### 4.5 超时控制和输出处理

```python
# bash_tool.py 第 42-53 行 — 超时控制
try:
    stdout, stderr = await asyncio.wait_for(
        process.communicate(),           # 等待子进程完成并收集输出
        timeout=arguments.timeout_seconds,  # 默认 120 秒
    )
except asyncio.TimeoutError:
    process.kill()                       # 超时则杀死进程
    await process.wait()                 # 等待进程真正退出
    return ToolResult(output="Command timed out ...", is_error=True)

# bash_tool.py 第 55-66 行 — 输出处理
parts = []
if stdout:
    parts.append(stdout.decode("utf-8", errors="replace").rstrip())
if stderr:
    parts.append(stderr.decode("utf-8", errors="replace").rstrip())

text = "\n".join(part for part in parts if part).strip() or "(no output)"

# 截断过长输出（>12000 字符），防止撑爆 Token
if len(text) > 12000:
    text = f"{text[:12000]}\n...[truncated]..."
```

### 4.6 一句话总结

> 用户命令经过 `resolve_shell_command` 变成 `[bash, "-lc", command]`，经过可选沙箱包装，由 `asyncio.create_subprocess_exec` fork 出子进程执行。BashTool 自身只负责超时、收集输出、截断。

---

## 五、FileEditTool — 为什么"只是"字符串替换？

### 5.1 核心代码只有两行

```python
# file_edit_tool.py 第 37-46 行
original = path.read_text(encoding="utf-8")         # 读全文
updated = original.replace(old_str, new_str, 1)      # Python 字符串替换
path.write_text(updated, encoding="utf-8")           # 写回
```

初看会觉得：**这也太简单了吧？AI 编辑文件不应该用 diff/patch 或者生成整个文件再对比吗？**

答案是：**试过复杂方案后，发现简单方案最好用。**

### 5.2 三种方案对比

| | **方案 A：整文件重写** | **方案 B：diff/patch** | **方案 C：字符串替换** ✅ |
|---|---|---|---|
| LLM 输出 | 整个文件新内容 | unified diff 格式 | 只输出 `old_str` + `new_str` |
| Token 消耗 | 500行文件改1行 → 输出500行 | 输出 diff 头部+改动 | 只输出被改的几行 |
| 出错概率 | 499行都可能"幻觉"改错 | 行号偏移、`@@`格式、空白字符任一出错就失败 | 只动精确匹配片段，其余一字不碰 |
| 实现复杂度 | 低 | 高（需要 diff 解析器） | 极低（`str.replace`） |
| 安全性 | 最差 — 文件被整体覆盖 | 中等 | 最好 — 未匹配部分物理上不可能被修改 |

### 5.3 为什么不用"生成整个文件"？

```
修改 main.py（500行）中的第 237 行

方案 A - 整文件重写：
  LLM 输出：~500 行 → ~2000 tokens → ~$0.06（GPT-4 输出价）
  风险：其余 499 行可能出现"幻觉修改"（删掉注释、改变缩进、重命名变量……）

方案 C - 字符串替换：
  LLM 输出：old_str(3行) + new_str(3行) → ~30 tokens → ~$0.001
  风险：只改匹配的部分，其余 497 行物理上不可能被修改
```

**省钱 60 倍 + 安全性碾压**。

### 5.4 为什么不用 diff/patch？

diff 方案理论上可行（LLM 生成 unified diff，工具端用 `patch` 应用），但有两个致命问题：

1. **LLM 生成 diff 格式容易错** — 行号偏移、`@@` 头部格式、空白字符，任何一处出错 patch 就失败
2. **调试困难** — diff 是给人看的格式，不是给 LLM 生成的格式

而 `old_str → new_str` 这个心智模型极其简单：**"找到这段话，换成那段话"**。LLM 非常擅长这种任务。

### 5.5 能工作的隐含前提

看起来简单，但有一个精妙的**系统配合**让它能工作：

```
FileReadTool 输出带行号的精确内容
        ↓
LLM 看到了文件的精确内容（一字不差）
        ↓
LLM 从上下文中精确复制出 old_str（因为内容就在对话历史里）
        ↓
str.replace() 精确匹配 → 替换成功
```

**`FileReadTool` 和 `FileEditTool` 是一对协作组件**：
- 读工具保证 LLM 看到精确内容
- 改工具利用这个精确性做字符串匹配

如果读的时候内容就不准确，替换就会失败（`old_str was not found`）—— 这恰好是一种**安全保护**。

### 5.6 唯一性保障

```python
# file_edit_tool.py 第 41-44 行
if arguments.replace_all:
    updated = original.replace(arguments.old_str, arguments.new_str)       # 替换所有
else:
    updated = original.replace(arguments.old_str, arguments.new_str, 1)    # 只替换第一个
```

默认 `replace_all=False`，只替换第一个匹配。这是防止文件中有多处相同内容时误改。

所以 System Prompt 通常会要求 LLM "**提供足够多的上下文让 old_str 唯一**" —— 不是只写要改的那一行，而是把上下几行也包含进 `old_str`，确保全文件只有一处匹配。

### 5.7 行业验证 — 主流工具都这么做

这不是 OpenHarness 独创的，**几乎所有主流 AI coding 工具都用这个方案**：

| 产品 | 编辑方式 |
|------|---------|
| **Anthropic Claude Code** | `old_str + new_str`（和这里一模一样） |
| **Cursor** | 字符串替换 + 少量 diff |
| **Aider** | search/replace block 格式（本质一样） |
| **OpenAI Codex CLI** | 字符串替换 |

大家不约而同选了最简单的方案，因为它在 **Token 效率、准确性、实现复杂度**之间取得了最佳平衡。

### 5.8 FileEditTool 完整流程图

```
execute(arguments={path:"main.py", old_str:"def foo():", new_str:"def bar():"})
  │
  ├─ 1. _resolve_path(cwd, "main.py") → /project/main.py
  │
  ├─ 2. path.exists()? → True ✓
  │
  ├─ 3. path.read_text("utf-8")
  │     → "import os\n\ndef foo():\n    pass\n"
  │
  ├─ 4. "def foo():" in original? → True ✓
  │     （如果找不到 → 报错，LLM 会重新读文件再尝试）
  │
  ├─ 5. original.replace("def foo():", "def bar():", 1)
  │     → "import os\n\ndef bar():\n    pass\n"
  │     只替换第一个匹配，其余内容一字不动
  │
  ├─ 6. path.write_text(updated, "utf-8")   # 全文写回
  │
  └─ 7. ToolResult(output="Updated /project/main.py")
```

### 5.9 一句话总结

> **不是"原理简单所以用了"，而是"试过复杂方案后发现简单方案最好用"**。字符串替换让 LLM 只输出变化的部分，省 Token、防幻觉、实现简单。`FileReadTool` 的精确输出是它能工作的前提。

---

## 六、三个工具的技术原理对比

| 维度 | FileReadTool | BashTool | FileEditTool |
|------|-------------|----------|-------------|
| **核心操作** | `Path.read_bytes()` + `decode()` | `asyncio.create_subprocess_exec()` | `str.replace()` |
| **IO 模式** | 同步读（文件小） | 异步子进程 | 同步读写（文件小） |
| **安全机制** | 二进制检测 `b"\x00"` | 沙箱包装 + 超时杀进程 | `old_str not in original` 报错 |
| **输出截断** | 无（靠 `limit` 参数控制） | 12000 字符 | 无 |
| **与 LLM 配合** | 带行号输出 → LLM 精确定位 | 返回 stdout+stderr → LLM 判断结果 | 需要 LLM 从读取结果中精确复制 `old_str` |
| **is_read_only** | 始终 `True` | 默认 `False` | 默认 `False` |

---

## 七、设计洞察

### 7.1 工具间的协作关系

```
FileReadTool ──精确内容──→ LLM ──精确 old_str──→ FileEditTool
     │                                                │
     └─── 带行号格式 ─── 帮助定位 ───── 构造替换 ────┘
```

这不是两个独立工具，而是一个**读-改工作流**的两半。`FileReadTool` 的行号格式化不是"锦上添花"，而是 `FileEditTool` 能正确工作的**前提条件**。

### 7.2 防御性编程贯穿始终

每个工具的 `execute()` 前半段都是各种检查：

```python
# FileReadTool：4 个检查
if not path.exists(): ...       # 文件不存在
if path.is_dir(): ...           # 是目录
if b"\x00" in raw: ...          # 是二进制
if not numbered: ...            # 选取范围为空

# FileEditTool：2 个检查
if not path.exists(): ...       # 文件不存在
if old_str not in original: ... # 匹配失败

# BashTool：2 个检查
except SandboxUnavailableError: ...  # 沙箱不可用
except asyncio.TimeoutError: ...     # 超时
```

所有检查失败都返回 `ToolResult(is_error=True)` 而不是抛异常 —— **错误是反馈给 LLM 的信息**，不是要中断的致命问题。LLM 看到错误后会自行调整策略（比如换个路径重试）。

### 7.3 "足够简单"就是最好的设计

三个工具的核心逻辑分别是：
- 读文件：`read_bytes` + `decode` + `splitlines` + 切片
- 执行命令：`create_subprocess_exec` + `communicate` + `wait_for`
- 改文件：`read_text` + `str.replace` + `write_text`

没有自定义的解析器、没有复杂的状态机、没有第三方依赖（除了 Pydantic 和 httpx）。每个工具 50-70 行代码，任何 Python 程序员 10 分钟就能读懂。

**这正是架构的目标**：把复杂性放在抽象层（base.py 的接口设计 + 注册表模式 + MCP 适配器），让实现层尽可能简单。

---

## 八、关键要点回顾

1. **ToolInput 和 Tool 分离**是因为生命周期不同 — 工具是单例（注册一次），参数是多例（每次调用都是新实例）
2. **文件读取**是全量读入内存再切片，不是操作系统级别的按行读取；先读字节后解码是为了在解码前做二进制检测
3. **BashTool 不执行命令**，它委托给 `create_shell_subprocess`，后者通过 `asyncio.create_subprocess_exec` fork 子进程
4. **文件编辑用字符串替换**是业界共识 — 省 Token 60 倍、防幻觉、实现简单，`FileReadTool` 的精确输出是其能工作的前提
5. **防御性编程**贯穿每个工具 — 所有错误都优雅地返回 `ToolResult(is_error=True)`，让 LLM 自行调整

---

*阅读完本文档后，你应该能够：*
1. *解释为什么 ToolInput 和 Tool 必须分成两个类*
2. *说出文件读取从磁盘到 LLM 的完整数据路径*
3. *画出 BashTool 从用户命令到子进程执行的调用链*
4. *向别人解释为什么"字符串替换"是 AI 编辑文件的最佳方案*

---

*下一步建议：方向 B（API 客户端）— 理解 LLM 调用的底层细节。*
