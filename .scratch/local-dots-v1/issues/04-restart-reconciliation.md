# 04: 重启核对——关机不丢任务、不盲目重试

**What to build:** 服务（或电脑）重启后，Task Manager 重读未结束任务并逐一核对引擎 Thread
与实际执行状态：可安全继续的恢复执行；无法确认是否已产生外部副作用的 Run 暂停、转
waiting-approval 请求用户核实；尚未汇报的结果保留（验收：可靠性——服务重启不丢记录、不盲目重试）。

**Blocked by:** 02 (Codex 适配 + Dispatcher)

**Status:** done（commit 813b046；验收⑤b 重启核对不盲目重试见 ../acceptance-record.md）

- [ ] 杀掉服务再启动：queued 任务继续排队，running 任务按核对结果恢复或转等待核实
- [ ] 无法确认副作用的 Run 落 waiting-approval（含重启核对来源标记），而非直接重跑
- [ ] 已完成未汇报的结果重启后可查、可汇报
- [ ] checkpointer 持久化数据（独立 SQLite 库）在重启后可恢复会话上下文
- [ ] 行为测试以持久化状态为输入（外部行为），不测恢复逻辑实现细节