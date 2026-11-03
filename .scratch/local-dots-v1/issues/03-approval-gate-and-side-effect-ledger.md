# 03: Approval Gate——黑名单命中审批、30 分钟超时、Side-Effect Ledger

**What to build:** 任务要动到 Blacklist 目录时，Run 停在 waiting-approval 并推送审批事件；
用户在 API/Web 上批准则继续执行、拒绝则停止当前步；30 分钟（LD_APPROVAL_TIMEOUT_MIN）未处理
自动拒绝为 rejected(reason=approval_timeout)，计时自进入等待态起算。取消、拒绝、超时三条路径
都输出 Side-Effect Ledger——已发生的副作用（文件、命令、应用）如实列出、从不静默回滚，
用户据此手动决定撤销什么。Blacklist 初版为空，条目经审核流程添加（数据而非代码）。

**Blocked by:** 02 (Codex 适配 + Dispatcher)

**Status:** done（commit 7c6866f；拒绝路径与 Side-Effect Ledger 见 ../acceptance-record.md）

- [ ] 命中 Blacklist 目录的操作进 waiting-approval；未命中一律放行（黑名单制，非白名单）
- [ ] 批准后 Run 继续；拒绝后当前步即停、后续步骤不再执行、副作用不回滚且 Ledger 完整输出
- [ ] 30 分钟未处理自动落 rejected(reason=approval_timeout)（测试用可注入时钟/短超时参数）
- [ ] 取消任务同样即停 + Ledger，语义与拒绝一致
- [ ] Blacklist 条目（如系统目录、公司数据目录）可经审核添加而无需改代码
- [ ] 审批流转全程有 SSE 状态事件可观察