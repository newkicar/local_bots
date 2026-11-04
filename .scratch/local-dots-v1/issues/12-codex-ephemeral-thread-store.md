# 12: Codex 一次性任务与用户全局线程存储冲突失败——新任务带 --ephemeral（bug）

**What to build:** 票 11 E2E 实测中传给 codex 的天气任务仍 failed：CLI 在会话初始化时
把新 thread 持久化到用户全局 ~/.codex 线程存储（thread_history_1.sqlite +
thread-writer-locks），与用户自己的交互会话 / app-server 共抢 active writer，
报 `thread-store conflict: ... already has an active writer` 且退出码 1（同任务 Jarvis
引擎正常）。修复：`app/adapters/codex.py` 新任务命令加 --ephemeral（一次性后台
任务不需要全局存储可见性），会话完全在子进程内运行；resume 路径不带
（需读已持久化线程）。thread.started 仍发 thread_id（审计/写库不受影），
但 ephemeral 线程不可 resume，该限制在适配器 docstring 与本票说明。

**Blocked by:** 票 11

**Status:** done

- [x] 真实 CLI 冒烟：codex exec --ephemeral 新会话 rc=0 且产出 agent_message
- [x] 单测：新任务命令含 --ephemeral；resume 命令不含（test_codex_new_task_ephemeral_resume_persisted）
- [x] 全量 pytest 绿（63 passed）
- [x] E2E：Web 提交真实 codex 天气任务 → 终态成功，未再出现 thread-store conflict
