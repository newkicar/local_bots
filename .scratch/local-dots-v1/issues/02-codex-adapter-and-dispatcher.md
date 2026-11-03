# 02: Codex 适配 + Dispatcher——真实引擎首条垂直切片

**What to build:** 用户提交 engine=codex 的 Task 后，Dispatcher 通过 Codex 适配层起 `codex exec`
子进程（JSON 事件流，继承本机 mimo2codex 网关，零额外配置），Run 真的执行、结果落库并产生
reported 状态与通知记录；同 Thread 追问（followup_task）复用引擎 Session ID 继续上下文。
沙箱默认 read_only，任务显式授权写才切 workspace_write；命中 Blacklist 的目录暂以
"提交即 waiting-approval"粗粒度兜底（细粒度审批由票 03 完善）。

**Blocked by:** 01 (应用骨架)

**Status:** ready-for-agent

- [ ] 提交 codex 任务 → running → completed，结果可经 get_task 读取（验收：多任务执行，可连提三项不互相等待）
- [ ] Codex 保持现有模型与 Skills（不改 ~/.codex/config.toml，连通性来自 mimo2codex 网关）
- [ ] followup 沿用原 engine_thread_id，上下文隔离——不混入其他任务（验收：上下文隔离）
- [ ] 10 个 Run 同时跑满后新任务进 FIFO 持久等待队列按序补位（上限可配，.env）
- [ ] 同一 Thread 的并发写与执行被串行化
- [ ] Codex CLI 缺席或报错时 Run 落 failed 并留日志，不影响其他 Run
- [ ] 引擎真机连通性为可跳过的集成测试；单测用假引擎验证调度与状态机