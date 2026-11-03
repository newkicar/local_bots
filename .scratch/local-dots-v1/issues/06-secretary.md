# 06: Secretary——意图路由、只读文件发现与任务工具接线

**What to build:** 用户在聊天里说人话派活：Secretary（独立 DeepAgents 主代理，模型独立配置）
理解意图，在授权目录内只读定位文件（ls/glob/grep/read 类，带超时与结果数量上限），
把 Task 路由给引擎（缺省 Jarvis，用户写明"用 Codex"才走 Codex，ADR-0001），
接好任务管理六件工具（submit_task / list_tasks / get_task / followup_task / cancel_task /
start_agent_session），委派后不等待引擎完成，继续接受新任务，并汇总结果汇报。

**Blocked by:** 02 (Codex 适配 + Dispatcher), 05 (Jarvis 适配)

**Status:** ready-for-agent

- [ ] 自然语言"让 Codex 分析项目 A，Jarvis 整理项目 B"→ 创建两个后台任务并立即继续对话
- [ ] 未指定引擎时路由到 Jarvis；显式指定 Codex 才走 Codex（无 LLM 路由、无公司数据自动判定）
- [ ] 文件发现只在授权目录内且只读；目录搜索有超时与结果上限，不扫全盘失控
- [ ] 秘书委派后本轮即结束，不等引擎（"假异步"问题根治）
- [ ] 任务完成/失败/待审批结果可由秘书汇总，用户追问回到原 Thread
- [ ] 权限不靠 prompt：文件访问由服务端 + Windows 账户强制，prompt 注入不能扩权