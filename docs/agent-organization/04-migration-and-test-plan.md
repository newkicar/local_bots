# 04 迁移与测试计划

## 数据库迁移（幂等 add-only，先例 TaskManager.open()）

- catalog 四表用 CREATE TABLE IF NOT EXISTS；projects 表同库。
- tasks 表新列（票 08）：PRAGMA table_info(tasks) 缺列才 ALTER TABLE ADD COLUMN：
  agent_id, engine_profile_id, engine_profile_version, engine_profile_hash,
  agent_snapshot_json, project_id。旧行全 NULL，按旧 engine 列语义解读。
- 测试必须含"旧库升级"用例：先建 V1 schema（14 列 tasks，无 catalog 表）→
  再跑 open()/迁移 → 旧行可读（新列 NULL）→ 再跑一次（幂等）。

## 测试纪律

- 禁用 tmp_path / tempfile.mkdtemp（本机 WinError 5 中毒）；
  自建 tests/tmp_workspace/（gitignore）。
- pytest asyncio_mode=auto，异步测试直接 async def。
- FakeEngine 不得修改：base.py 新增可选方法必须给安全默认。
- 全量回归门槛：基线 100 passed，每票完成后全量绿才提交。

## §21 十条端到端验收清单（票 11 执行）

1 新 Jarvis profile（如未来支持）→ sync → catalog → 审批 → 可用；
2 Codex 自定义 agent → delegated_only 且展示限制；
3 任务记录 agent 身份 + profile hash；
4 旧 Codex/Jarvis 任务执行、审批、取消、恢复、followup 全部照常；
5 路由决策 JSON 可审计；6 撤销项目信任只挡新任务；7 同步幂等；
8 坏 TOML 不影响整体；9 缺失源不删 catalog 行；10 默认行为（不带 agent_id）
与 V1 完全一致。
