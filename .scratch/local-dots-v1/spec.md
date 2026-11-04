# Spec: Local Dots V1.0（个人秘书 + 双引擎异步任务系统）

Status: ready-for-agent

## Problem Statement

从用户视角：我现在只能坐在办公室电脑前、盯着一个 Agent 的 TUI 等它干完活，才能安排下一件事。
我想要从手机/远端给这台 Windows 电脑安排任务：不必等前一项完成、关掉网页不丢任务、
任务完成或需要审批时能收到通知并处理，而不需要单独开发手机 App，也不需要把公司电脑整体暴露到公网。

## Solution

在 Windows 本机跑一套 Local Dots 服务（FastAPI，仅监听 127.0.0.1:8000，登录计划任务常驻）：

- **Secretary（DeepAgents 轻量主代理）**：聊天里理解意图、在授权目录内只读定位文件、
  把 Task 路由给引擎（**默认 Jarvis，仅用户对该 Task 显式指定才用 Codex**，ADR-0001），
  汇总结果；秘书的工具调用不等待引擎完成。
- **Task Manager（SQLite 持久化 + 异步 Dispatcher）**：Task/Thread/Run 三层分离，
  并发上限 10 个 Run、超出 FIFO 排队（ADR-0003），重启不丢任务、不盲目重试。
- **Engines / Adapters**：Codex 走本机 `codex exec` 子进程复用 mimo2codex 网关（ADR-0002）；
  Jarvis 走 `create_jarvis(workspace, checkpointer)` + `.ainvoke(...)`，checkpointer 由本项目注入。
- **Permission Layer**：目录黑名单审核制（ADR-0004）——命中黑名单路径的操作停在
  Approval Gate（waiting-approval），30 分钟超时自动拒绝（ADR-0005），取消/拒绝输出
  Side-Effect Ledger，其余操作一律放行。
- **Web UI**：原生 HTML/CSS/JS 响应式四件套——聊天、任务列表、审批、结果；SSE 只推状态迁移。

## User Stories

1. 作为用户，我在手机聊天里提交任务后立即拿到任务编号并继续安排下一件事，好让多任务并行不被阻塞。
2. 作为用户，我连续提交三项任务且无需等待前一项完成（验收：多任务执行）。
3. 作为用户，我可以分别让 Jarvis 和 Codex 保持它们现有的模型与 Skills 执行任务（验收：多引擎复用）。
4. 作为用户，我提交任务时默认由 Jarvis 执行，写明"用 Codex 跑"时才用 Codex，好让路由规则永远可预期。
5. 作为用户，我让秘书"在 D 盘找到某文件夹并在其中启动指定引擎"，它先只读定位、检查授权、再创建绑定该工作目录的 Thread（验收：动态工作目录）。
6. 作为用户，我对原任务追问时回到原 Thread，上下文不混入其他任务（验收：上下文隔离）。
7. 作为用户，关掉网页或浏览器标签，后台任务照常执行（验收：可靠性）。
8. 作为用户，服务重启后未完成任务不丢失、不会盲目重试；无法确认副作用的 Run 暂停并请求我核实（验收：可靠性）。
9. 作为用户，任务完成/失败/需要审批时结果被保存并通知我，回来后能查看（主动汇报）。
10. 作为用户，任务要动到黑名单目录时我收到审批请求，可批准或拒绝；批准才继续，拒绝即停。
11. 作为用户，我在 30 分钟未处理审批时它自动被拒（rejected，reason=approval_timeout），系统不会无限挂着。
12. 作为用户，我取消任务时当前步立即停止，已发生的副作用不回滚，但拿到一份 Side-Effect Ledger 知道要手动撤销什么。
13. 作为用户，10 个 Run 同时跑满后新任务进入 FIFO 排队，我知道它还在队列里而不会丢。
14. 作为用户，我用 followup 沿用原 Thread 继续某任务，而不需要重新交代背景。
15. 作为用户，我用 start_agent_session 在某目录"打开一个 Agent 准备对话"，与"交付一项工作让它做完"（submit_task）明确区分。
16. 作为用户，我查看任务列表、日志与结果，知道每个 Task 当前处于什么状态。
17. 作为用户，我以单用户 Bearer Token 认证访问 API，密钥与凭据永不出现在回环地址之外。
18. 作为用户，电脑重启后 Local Dots 随登录自动恢复服务，队列与历史都在。
19. 作为用户，维护黑名单时走审核流程添加条目（如系统目录、公司数据目录），不需要改代码。
20. 作为用户，经授权的手机浏览器（Cloudflare 通道）可以查看任务、发送指令并接收结果（验收：远程操作）。
21. 作为用户，Secretary 的目录搜索有超时与结果数量上限，不会失控扫全盘。
22. 作为用户，同一 Thread 的并发写与执行被串行化，不产生状态冲突。
23. 作为用户，引擎进程永不 os.chdir 全进程目录，每个任务的文件操作被约束在自己的 Workspace。

## Implementation Decisions

- 模块落位严格按方案文档第七节（app/ 下 secretary、task_manager、dispatcher、session_manager、permissions、adapters/）；
  本 Spec 新增顶层目录需在 PR 说明理由。
- 引擎选择固定策略（ADR-0001）：submit_task 的 engine 参数缺省 jarvis；"用 Codex"由用户逐任务显式表达；
  不做 LLM 路由、不做公司数据自动判定。
- Codex 适配绑定本机 `codex exec` 子进程读 JSON 事件流（ADR-0002），继承 ~/.codex/config.toml 的
  mimo2codex-agnes 网关，零额外配置；沙箱默认 read_only，任务授权写才切 workspace_write；
  引擎 Thread 用 CLI 的 Session ID 恢复；日后 SDK 支持本地网关证实后可升级，切换局限在 codex adapter 一个文件。
  （方案文档 3.1 节"SDK 优先"条款被 ADR-0002 取代。）
- Jarvis 适配只用公开入口 `create_jarvis(workspace, checkpointer)` + `.ainvoke(...)`，禁止 import 内部模块；
  checkpointer 由 Local Dots 注入独立 SQLite 库（task_id 作 thread_id），不用模块级单例；
  前置是外部仓（truly-jarvis-main）的 Phase A 改造（.scratch/phase-a-jarvis-adapter/spec.md，ADR 用 0012+ 编号）。
- Task/Thread/Run 三层分离，任务记录形状（原型，取自方案文档第五节）：

  ```
  { task_id, engine, engine_thread_id, workspace, status, parent_task_id }
  ```

  engine_thread_id 在引擎创建 Thread 后立即写库，不等任务结束。
- 状态机（原型，取自方案文档第五节 + ADR-0005）：
  `queued → running → completed → reported`，分支 `waiting-approval`、`failed`、`cancelled`、`rejected(reason=...)`、`interrupted`；
  waiting-approval 的进入来源：命中 Blacklist 的操作、重启核对时无法确认副作用的 Run。
- 并发与排队（ADR-0003）：同时最多 10 个 Run（可配参数，.env），溢出进持久等待队列（SQLite）FIFO 补位。
- 目录安全（ADR-0004）：Blacklist 初版为空、审核添加；命中→Approval Gate，未命中一律放行。
- 审批超时（ADR-0005）：`LD_APPROVAL_TIMEOUT_MIN`（默认 30），计时自 Run 进入等待审批态起算；
  超时落 rejected(reason=approval_timeout)；拒绝/取消/超时均只停止后续步骤，已发生副作用照写 Side-Effect Ledger，永不静默回滚。
- 重启恢复：服务重启重读未结束任务，核对引擎 Thread 实际执行状态；可安全继续的恢复，
  不能确认副作用的暂停并请求核实（进 waiting-approval）；保留尚未汇报的结果。
- Secretary 是独立 DeepAgents 主代理，模型独立配置（不依赖 Codex 模型）；
  工具 = 任务管理六件（submit_task / list_tasks / get_task / followup_task / cancel_task / start_agent_session）
  + 受限只读文件发现（仅授权目录；glob 类搜索带超时与结果上限；内置 glob 不可靠时再加只读目录索引工具，不重写文件系统）。
- Web UI：原生响应式（无框架），四件套聊天 + 任务列表 + 审批 + 结果；SSE 只推状态迁移事件，不做逐字流式。
- 部署：Windows 登录计划任务跑 uvicorn（仅 127.0.0.1:8000），数据落 data/ + workspace/，不打包；
  单用户 Bearer Token（.env）。
- 安全基线：权限由 Python 服务端 + Windows 账户强制，不靠 prompt；FilesystemBackend / virtual_mode 均非 OS 沙箱；
  API 密钥、Codex 凭据、mimo2codex 端口不回环外。
- Cloudflare Access + Tunnel 是最后一步（Phase 5），须在本地验收通过后实施，不进 V1.0 验收门禁。

## Testing Decisions

- 主接缝（自上而下，优先复用而非新开）：
  1. **HTTP API + SSE 面**（FastAPI 应用级）：任务提交/查询/取消、状态迁移事件、审批流转、认证——全部按外部行为测。
  2. **Adapter 基类接口**：Dispatcher/Task Manager 对引擎的依赖面向 adapters 接口；用假引擎验证排队、并发、串行化、状态机；
     Codex CLI 真机连通性单独做集成测试（可标记跳过），不在单测里起子进程。
  3. **重启核对行为**：以持久化状态为输入测"哪些 Run 恢复 / 哪些转 waiting-approval / 哪些保留结果"，不测实现细节。
- 好测试的标准：只测外部行为（API 响应、状态迁移、队列顺序、超时语义、Ledger 内容），不测私有函数与实现结构。
- 计时类行为（审批 30 分钟超时、SSE）用可注入时钟/短超时参数验证，不用真实 sleep。
- 本机 pytest 临时目录 WinError 5 中毒纪律：禁用 tmp_path / tempfile.mkdtemp，自建 tests/tmp_workspace/（gitignore）。
- 先例：姊妹仓（Jarvis / 数字主持人）的 pytest `asyncio_mode = "auto"` 惯例；异步测试直接写 async def。
- 每个引擎任务票验收对照方案文档第九节六条，对应哪条写在票面。

## Out of Scope

- Cloudflare 公网通道（Phase 5；最后做，见方案文档第八节第⑤步）。
- 语音、多个固定专家 Agent、复杂长期记忆/向量库、企业级分布式（Redis/Celery/多用户）。
- 独立手机 App、实时看屏幕/接管键鼠的远程桌面能力。
- Jarvis 仓自身的 Core/TUI 分离改造（Phase A，在其仓 tracker 实施）。
- Docker 化（Windows 原生 Python 环境）。

## Further Notes

- 本 Spec 的词表以 CONTEXT.md 为准（Task/Thread/Run/Secretary/Engine/Adapter/Workspace/Queue/Approval Gate/
  Blacklist/Side-Effect Ledger/mimo2codex/Phase）；决策理由见 docs/adr 0001–0005。
- 黑名单初版为空：种子候选（系统目录、公司数据目录）待用户审核后添加。
- 依赖外部前置：truly-jarvis-main 的 Phase A 完成后 Jarvis 适配票才可开工；在此之前 Codex 链路先行。