# 01: 应用骨架——认证、任务持久化与状态机、SSE 状态推送

**What to build:** Local Dots 服务跑起来（uvicorn，仅 127.0.0.1:8000，Bearer Token 认证），
用户提交一个 Task 立即拿到 task_id 且任务落库进入 queued；Task/Thread/Run 三层记录可查询，
状态迁移（queued → running → completed → reported 及 failed/cancelled/rejected/interrupted 分支）
通过 SSE 状态事件推送给任意已认证客户端。本票用假引擎（测试替身）驱动状态机，不接真实引擎。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] 无 Token / 错 Token 的 API 请求被拒（401），正确 Token 放行
- [ ] 提交任务立即返回 task_id，任务记录含 task_id、engine、engine_thread_id（引擎建 Thread 后立即写库）、workspace、status、parent_task_id
- [ ] 对 Task 的状态查询返回当前状态与 Run 历史（重试产生新 Run 不覆盖旧 Run）
- [ ] SSE 通道只推状态迁移事件（不逐字流式），客户端断开不影响后台状态机
- [ ] 关闭客户端（模拟关网页）后任务状态机照常推进
- [ ] 测试全部走 HTTP API 外部行为，假引擎不 import 任何真实引擎模块