# 05: Jarvis 适配——create_jarvis 公开入口 + checkpointer 注入

**What to build:** 用户提交 engine=jarvis（缺省）的 Task，Dispatcher 经 Jarvis 适配层调用外部仓
Phase A 暴露的 `create_jarvis(workspace, checkpointer)` 工厂 + `.ainvoke(...)`，Task 在用户指定的
Workspace 内真实执行（验收：多引擎复用——Jarvis 保持现有模型与 Skills）；追问沿用原 Thread；
同一 Workspace 内"找到目录 → 在其中启动 Jarvis 会话"（start_agent_session）可用（验收：动态工作目录）。

**Blocked by:** 01 (应用骨架), 02 (Codex 适配 + Dispatcher), 外部前置：truly-jarvis-main 的 Phase A 完成
（.scratch/phase-a-jarvis-adapter/spec.md 五项全绿；其仓 ADR 0012+）

**Status:** ready-for-agent

- [ ] adapters/jarvis.py 只 import 公开入口，零 import Jarvis 内部模块（评审必查）
- [ ] checkpointer 由 Local Dots 注入独立 SQLite 库（task_id 作 thread_id），无模块级单例
- [ ] 引擎永不 os.chdir；Jarvis 文件后端与 shell 工具显式收任务级 Workspace
- [ ] jarvis 任务完整走通 queued → running → completed → reported 与 followup
- [ ] 未提交任务缺省 engine 为 jarvis（ADR-0001）