# 13: 左侧对话栏 + 首轮自动摘要 + 跨对话共享记忆（新需求）

**What to build:** 参考元宝/ChatGPT 侧栏：页面最左侧加历史对话选择栏（标题+单行
摘要+删除+新对话按钮），选中对话即筛选域——该对话的聊天/任务/审批/结果联动
显示；同一对话上下文连续，跨对话不连续。AI 回复第一条消息后后台自动生成
≤50字中文摘要写入侧栏。跨对话共享持久记忆存 `data/memory.json`（机制参考
Codex 共享记忆文件）：给秘书加 `save_memory` 工具 + 系统提示词注入已有条目。
硬约束：不动下方 4 个 tab、不破坏聊天输入框 sticky 冻结、保留深色主题。
- 后端：settings.py +conversations_db_path/memory_path property；新建
  app/conversations.py（ConversationStore，aiosqlite，conversations/
  conversation_messages/conversation_tasks 三表）；新建 app/memory.py
  （MemoryStore 同步 JSON，去重+FIFO 上限 50 条，原子写）；secretary.py
  加 memory kwarg + save_memory 工具 + summarize()（直接调模型，不走 agent）；
  main.py ChatRequest +conversation_id、/api/conversations CRUD、/api/memory
  GET/DELETE、chat 端点串接（append 消息/link 任务/自动 title/首轮摘要
  后台任务+SSE conversation_updated）。
- 前端：index.html 最左侧 aside#conv-sidebar（新对话按钮+#conv-list）+
  桌面汉堡钮 + 移动抽屉 backdrop，main 外包 .layout；app.js 对话列表
  加载/选中/新建/删除、聊天提交带 conversation_id、convTaskIds 筛选
  助手接入任务/结果/审批/徽标、SSE conversation_updated、旧 localStorage
  聊天记录一次性迁移；style.css 深色侧栏（桌面 260px 固定列，<900px 抽屉）。

**Blocked by:** 无（SSE/任务/审批/结果端点均已存在；secretary.py 锁死全文无
chdir/subprocess，chat-form sticky 与 .panel 宽度测试锁死，只追加不改）

**Status:** done

- [x] 左侧对话栏：标题+单行摘要+删除+新对话；选中即该对话的聊天/任务/审批/结果筛选域
- [x] 同一对话上下文连续（history 按对话取），跨对话不连续；旧 localStorage 记录自动迁移
- [x] 首轮 AI 回复后后台生成 ≤50字摘要，侧栏即时更新（SSE 推送）
- [x] 跨对话共享记忆：save_memory 工具 + 系统提示词注入；/api/memory 可查可删
- [x] 硬约束不破：4 tab 功能、输入框 sticky 冻结、深色主题、Node DOM 桩回归全绿
