# 剩余 37 项 ruff 人工清偿清单

- 状态：open
- 标签：ready-for-human
- 日期：2026-11-25
- 基线：commit 9aa0115 之后，`ruff check .` 剩 37 项（全部需人工判断；另有 16 项 unsafe 自动修复候选）

## 分组（按规则）

### BLE001 裸 except（10）——逐个确认该吞什么异常
- app\agents\discovery.py:113
- app\main.py:386
- app\secretary.py:75 / 233 / 280 / 595
- tests\acceptance08.py:417
- tests\test_api_skeleton.py:220 / 265
- tests\test_web_ui.py:82

### PLW1510 subprocess.run 缺 check=True（5）
- app\adapters\codex.py:264
- tests\test_web_ui.py:425 / 456 / 490 / 625

### RUF059 解包变量未用（7）——改 `_` 或补使用
- tests\test_approval.py:84 / 135 / 233（变量 `t`）
- tests\test_followup.py:75 / 95 / 115 / 177（变量 `fake`）

### UP031 % 格式化（5）——ruff 有 unsafe 自动建议，逐处核对 assert 消息
- tests\test_web_ui.py:427 / 458 / 492 / 524 / 627

### SIM102 嵌套 if 合并（3）
- app\agents\project_trust.py:55
- app\dispatcher.py:173 / 213

### 其余零星（7）
- app\agents\catalog.py:129 SIM118 `in dict.keys()` → `in dict`
- app\agents\project_trust.py:51 PIE810 startswith 传 tuple
- app\main.py:386 S110 try-except-pass 加日志
- tests\test_agent_sync.py:158 ASYNC230 async 里阻塞 open
- tests\test_api_skeleton.py:227 F841 `fake` 未用
- tests\test_codex_adapter.py:224 F841 `cur` 未用
- tests\test_router.py:155 RUF012 类属性可变默认值

## 约定
- 每组一个 commit，Conventional Commits 中文描述
- BLE001/PLW1510 涉及 dispatcher/secretary/adapters 的异常与子进程语义，改前确认行为意图，必要时附测试
- 全部完成后 `ruff check .` 应为 0 项（或显式 noqa 留痕）