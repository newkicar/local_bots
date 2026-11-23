# 14: Jarvis HITL 恢复路径静默丢弃 resume_note（审批备注两段式注入）

**What to build:** 现状（`app/adapters/jarvis.py` `execute()`）：`resume_note`
只拼进新轮 payload 的 `text`；pending interrupt + `decision="approved"` 时走
`_resume_payload` → `Command(resume={"decisions":[{"type":"approve"}*n]})`，
langchain HITL 的 approve 决策不携带 message → **备注被静默丢弃**；
decision != approved 的防御路径（重新挂起）同样丢弃。修复为两段式语义：

- resume ainvoke 干净返回（无新 `__interrupt__`）且 `resume_note` 非空 →
  同 thread 第二次 `ainvoke({"messages":[{"role":"user","content":note}]},
  config)` 注入审批备注，最终回复取第二次调用；
- 第二次调用再 interrupt → 走既有 RunSuspended 路径（备注已进入线程历史，
  不丢；下次审批放行时同一备注随新 execute 再注入，天然重试语义）；
- `resume_note` 为空 → 零额外调用（成本中性，行为与现状一致）；
- 新轮路径（无 pending interrupt）行为完全不变；
- 模块 docstring（HITL 段）+ `_resume_payload` docstring 更新，说清
  两段式语义与"再挂起时备注随线程历史保留"的保证。

**Blocked by:** 无

**Status:** done

- [x] resume 干净返回 + note 非空 → 同 thread 二次 ainvoke 注入备注，最终回复取第二次
- [x] 二次调用再挂起 → RunSuspended（备注已入历史不丢）
- [x] note 为空 → 单次调用（回归锁，成本中性）
- [x] 新轮路径字节级不变（现有测试锁死）
- [x] fake agent_factory 断言调用次数与 payload 形状（三分支覆盖）
- [x] docstring 同步 + 全量回归绿（WinError 5 纪律：自建临时目录）+ 提交

## Comments
- 2026-11-23 完成（票 14 落地，全量回归绿）：
  - `app/adapters/jarvis.py`：`execute()` 两段式注入块 L133-144（resume 干净返回且
    `resume_note` 非空 → 同 thread 第二次 `ainvoke` 用户消息注入，最终回复取第二次；
    第二次再挂起 → 既有 `RunSuspended` 路径）；新增静态方法
    `_interrupt_suspended` L170-182（`__interrupt__` → RunSuspended(kind=jarvis_interrupt,
    path=workspace, detail≤300, actions 清洗)）；模块 docstring HITL 段 L15-20 与
    `_resume_payload` docstring L185-200 同步两段式语义（HITL approve 协议无自由文本，
    备注只走第二次调用；再挂起时备注已入线程历史，下次放行随新 execute 再注入）。
  - `tests/test_jarvis_adapter.py`：HTTP 端到端 `test_hitl_interrupt_maps_to_approval_gate`
    与 `test_resume_none_input_does_not_release_hitl_interrupt` 断言备注经第二次
    普通用户消息注入；`FakeJarvisAgent` interrupt 分支区分 Command（resume）与
    dict（备注）两类载荷；新增四个单元直驱测试
    `test_two_stage_note_injected_after_clean_resume` /
    `test_two_stage_note_reinterrupt_keeps_suspended` /
    `test_two_stage_empty_note_cost_neutral` /
    `test_two_stage_fresh_turn_payload_unchanged`（`_unit_env` 自建
    `tests/tmp_workspace/`，守 WinError 5 纪律）。
  - 回归：本机全量 `python -m pytest -q` 181 passed（沙箱内 codex 两例
    subprocess/命名管道受限 WinError 5 属环境限制，非回归；沙箱外单独复跑通过）。

- 2026-11-22 建单：来自"多身份调用"需求校对汇报。证据：
  `app/adapters/jarvis.py:99-135`（execute：note 只拼 text；pending+approved
  走 _resume_payload 后 note 未进任何 payload）、`:172-186`（_resume_payload
  构造 Command(resume=...)，approve 决策无 message 字段）；langchain
  HumanInTheLoopMiddleware 的 decisions 协议仅 type/approve/reject +
  updated_args，不携带自由文本。