# 00 现状审计（Phase 0）

日期：2026-11-21 · 仓库 HEAD：1cfaace · 测试基线：100 passed（& "D:\python_env\python.exe" -m pytest -q，约 115s）

## 调用链地图

用户(Web/CLI 聊天) → app/main.py（FastAPI，Bearer LD_API_TOKEN，仅 127.0.0.1）
  → app/secretary.py（DeepAgents 主代理：submit_task / start_agent_session /
     list_tasks / get_task / followup_task / cancel_task + 只读目录发现 + save_memory）
  → app/task_manager.py（TaskManager：tasks/runs/notifications 三表 SQLite，
     状态机守卫 transition_task，Task/Thread/Run 三层分离）
  → app/dispatcher.py（Dispatcher：后台泵，并发上限 10，同 Thread 串行化，
     审批策略钩子 → ApprovalGate）
  → app/adapters/base.py（EngineAdapter.ensure_thread/execute + RunHooks +
     RunSuspended/EngineError）
      ├─ app/adapters/codex.py（codex exec 子进程 + JSONL 事件流）
      └─ app/adapters/jarvis.py（create_jarvis + AsyncSqliteSaver，HITL interrupt）

## 关键代码事实（实施依据）

- TaskManager._SCHEMA：tasks 14 列；open() 已有幂等 add-only 迁移先例
  （PRAGMA table_info + ALTER TABLE ADD COLUMN）。get_task/list_tasks/
  incomplete_tasks/_task_row 全部硬编码 SELECT 列清单——新增任务列须全链路扩展。
- create_task(*, instruction, engine="jarvis", workspace, parent_task_id,
  writes_authorized)；transition_task 支持 **fields 动态写列（路由 JSON 落库复用）。
- main.py lifespan 顺序：tm.open → conv_store.open → reconcile_on_startup →
  dispatcher.start → gate.start（关停反向）。启动 sync 钩子插在 tm.open() 之后。
- adapters/base.py：EngineAdapter 抽象方法 ensure_thread/execute；可选
  reconcile_running 默认 {"safe": False}。扩展可选方法必须给安全默认（FakeEngine 不动）。
- secretary.py：_engine_names 来自 dispatcher._engines；SYSTEM_PROMPT 默认 jarvis；
  LLM 建议的 agent 必须服务端校验（见 11）。

## 与 V1 的衔接

本批（Agent Organization 第一批）不改变上述调用链，只做：catalog/发现/sync/信任/
路由身份快照 + 两个 adapter 的能力面与调用模式守卫。
