# Local Dots

Local Dots 的领域术语表：一个跑在 Windows 上的个人秘书系统，把聊天中的任务路由给 Jarvis / Codex 引擎异步执行（设计基线：`###LocalDots｜Wi.txt`）。本文件只定术语；执行纪律在 `AGENTS.md`，决策理由在 `docs/adr/`。

## Language

**Task**:
通过聊天交给秘书的一段工作；管理最小单元，`task_id` 标识，落 SQLite 持久化，拥有独立的 Thread。
_Avoid_: Job, 请求, 工单

**Thread**:
一个 Task 在某个引擎中开启的连续对话会话（`engine_thread_id`，LangGraph checkpointer thread）；后续追问回到原 Thread；同 Thread 的写入与执行串行化。
_Avoid_: Conversation, session, 对话

**Run**:
Thread 内对 Task 的一次执行（`run_id`）；重试产生新 Run 而不覆盖旧 Run；终态为 completed / failed / cancelled / rejected。
_Avoid_: Attempt, execution, 步骤

**Secretary**:
轻量主代理（DeepAgents）：理解意图、定位文件、把 Task 路由给引擎、汇总结果；只读、仅活动于授权目录。
_Avoid_: 助手, LLM, agent

**Engine**:
执行后端。**默认 Jarvis；仅当用户对该 Task 显式指定时才用 Codex**（ADR-0001），不做 LLM 自动路由、不做公司数据自动判定。
_Avoid_: Runner, worker, model

**Adapter**:
Local Dots 与引擎之间的唯一接口层（`app/adapters/`）；`adapters/jarvis.py` 只允许调用 `create_jarvis(workspace, checkpointer)` + `.ainvoke(...)`，禁止 import 引擎内部模块。
_Avoid_: Bridge, wrapper, client

**Workspace**:
任务级工作目录（`workspace/<task_id>/`），引擎的文件读写与命令执行被约束在其中；引擎进程永不 `os.chdir`。这是纪律约定，不是 OS 级沙箱。
_Avoid_: 目录, folder, sandbox

**Queue**:
持久化任务队列 + 溢出等待队列：并发上限 10 个 Run，超出 FIFO 排队（ADR-0003）。
_Avoid_: Pool, scheduler, pipeline

**Approval Gate**:
人工审批点：Run 停在 waiting-approval 态；超时（`LD_APPROVAL_TIMEOUT_MIN`，默认 30 分钟，计时自进入等待态起算）自动拒绝为 `rejected(reason=approval_timeout)`（ADR-0005）。
_Avoid_: 确认框, prompt, 关卡

**Blacklist**:
目录黑名单（初版为空，条目经审核后添加）：只有命中黑名单路径的操作需要走 Approval Gate，其余一律放行（ADR-0004）。
_Avoid_: 白名单, allowlist, 权限列表

**Side-Effect Ledger**:
一次 Run 已产生的副作用（文件、命令、应用）的追加式清单；取消或自动拒绝时照常输出——取消/拒绝只停止后续步骤，从不回滚已发生的副作用。
_Avoid_: 日志, trail, audit

**mimo2codex**:
Codex 引擎背后已有的本地 LLM 网关（`127.0.0.1:8788/v1`，provider `mimo2codex-agnes`，模型 `agnes-3.0-flash`）；Local Dots 原样复用、永不改其配置（ADR-0002）。
_Avoid_: proxy, gateway, API

**Phase**:
方案文档第八节的五个实施阶段；**Phase A** = Jarvis 侧适配边界改造，是 Local Dots 一切引擎任务的前置（清单在 `.scratch/phase-a-jarvis-adapter/spec.md`）。