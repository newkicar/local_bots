# 10: Codex adapter 调用模式

**What to build:** codex.py capabilities() 如实 JSON（default=direct；worker/explorer/
自定义=delegated_only；agent_discovery=true）；delegated_only agent 被当顶层 worker 直接执行
→显式 EngineError/4xx（不发明 --agent 旗标，不读 ~/.codex/config.toml）；stdin/JSONL/thread
绑定/`--ephemeral`/resume/取消树/副作用台账行为原样保留（现有测试锁死）。

**Blocked by:** 08

**Status:** done

- [x] capabilities + 调用模式守卫
- [x] delegated_only 顶层调用显式失败测试
- [x] 回归 + 提交
