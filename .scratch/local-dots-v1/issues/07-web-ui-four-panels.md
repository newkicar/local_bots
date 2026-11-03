# 07: Web UI 四件套——聊天、任务列表、审批、结果（原生响应式）

**What to build:** 一套响应式原生 HTML/CSS/JS 网页（电脑/手机共用，无框架），四个面板：
聊天（与 Secretary 对话、提交/追问/取消任务）、任务列表（全部 Task 与状态、Run 历史、日志）、
审批（waiting-approval 的 Run 列表，批准/拒绝，含超时来源与重启核对来源）、结果（completed/
failed/rejected/cancelled 结果与 Side-Effect Ledger 查看）。SSE 状态推送驱动实时刷新。
至此手机端（经回环或既有网络）可完成"查任务、发指令、审批、收结果"（验收：远程操作，除公网通道外）。

**Blocked by:** 03 (Approval Gate——审批面板), 06 (Secretary——聊天面板)

**Status:** ready-for-agent

- [ ] 手机宽度下四面板可用（响应式），Bearer Token 登录
- [ ] 聊天提交任务 → 任务列表实时（SSE）出现且状态迁移可见
- [ ] 审批面板可批准/拒绝，操作结果即时反映到任务状态
- [ ] 结果面板可查看结果与 Side-Effect Ledger；关掉页面重进任务仍在
- [ ] 前端不直连引擎；一切经 API，密钥不回环外