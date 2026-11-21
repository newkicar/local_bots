# 09: Jarvis adapter 能力面

**What to build:** EngineAdapter.capabilities() 可选默认 + jarvis.py 如实上报
{"agent_discovery": false, "direct_profile_invocation": false, ...}；显式请求非默认
jarvis profile（engine_profile_id != "default"）→显式 EngineError（绝不静默回退 default）；
默认路径行为字节级不变（现有测试锁死）。

**Blocked by:** 08

**Status:** open

- [ ] capabilities 默认 + jarvis 如实 JSON
- [ ] 非默认 profile 显式失败测试
- [ ] 回归 + 提交
