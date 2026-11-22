# 03 实施计划（第一批 11 票，frontier 顺序）

frontier：01 → 02 → {03,04,05} → 06 → {07,08} → {09,10} → 11

| 票 | 内容 | 交付 |
|---|---|---|
| 01 | Phase-0 审计 | docs/agent-organization/*.md + 设计文档入库 |
| 02 | Catalog 持久化 + API/CLI | app/agents/catalog.py、app/cli.py、三表+projects、内建种子 |
| 03 | 发现框架 + Jarvis provider | app/agents/discovery.py；base.py 可选 discover_agents 默认 [] |
| 04 | Codex 用户级发现 | app/agents/codex_discovery.py（tomllib metadata-only，CODEX_HOME） |
| 05 | 项目信任 | app/agents/project_trust.py + projects 表、root 规范化/逃逸校验 |
| 06 | Codex 项目级发现 | 复用 04 reader，信任门控，codex:project:<pid>:<name> |
| 07 | Sync 编排 | app/agents/sync.py；启动钩子 + POST /api/agents/sync + CLI sync |
| 08 | 任务 Agent 身份/快照 | tasks 加 6 列、create_task/SELECT 全链路、快照冻结、422 规则 |
| 09 | Jarvis 能力面 | capabilities JSON；非默认 profile 显式 EngineError |
| 10 | Codex 调用模式 | capabilities JSON；delegated_only 顶层显式拒绝；不发明旗标 |
| 11 | 路由器 + 回归 | app/agents/router.py、路由 JSON 落库、settings 6 项、全量回归 |

不做（本批明确排除）：DAG/Context Bundle/Agent Session/Plan API/拖拽组织图/
文件 watcher/定时 sync 轮询（LD_AGENT_SYNC_INTERVAL_MIN 留到后续）。
