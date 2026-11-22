# 10: 聊天面板任务通知卡 + 无依赖 Markdown 结果渲染（新需求）

**What to build:** 任务终态/挂起时聊天页（秘书）应主动给反馈——SSE notification 事件
（task_manager.add_notification 已有，`{type,notification_id,task_id,kind,payload}`）
前端不渲染；结果里的 Markdown（如查天气）被 `esc()` 挤成一块。纯前端改动：
- web/app.js 零依赖 `mdToHtml(src)`（先 esc 再转块级：``` 围栏→pre.md-pre、#~####→h3-h6、
  -/*/N. 列表→ul/ol、段落 <br>；行内 code/加粗/斜体/链接，链接 URL 白名单
  http(s)/mailto/#/相对路径，mdSafeUrl 兜底 "#"）。
- `es.onmessage` 增 `notification` 分支 → `chatTaskNotice(ev)`：状态药丸（completed=ok、
  failed/rejected/cancelled=bad、approval_requested=warn）+ task_id + 正文（completed→
  mdToHtml(result) 可滚动 max-height 14em；failed→esc(error)；rejected→esc(reject_reason)；
  cancelled→ledger 条数；待审批→run_id+wait_reason）+「查看结果/去审批」按钮（跳结果面板
  滚到 `res-<task_id>` 卡或审批面板）。`state.seenNotifs` Set（上限 200 FIFO 淘汰）按
  notification_id 去重，防 SSE 重连/刷新重放重复插卡。
- resultCard/detailHtml 结果改 `<div class="md">`+mdToHtml（error 保持 esc + pre-wrap）；
  resultCard 设 `card.id = "res-" + task_id` 供通知卡跳转。
- style.css：.task-notice（border-left 3px ok/bad/warn）、.tn-* 、.md 块样式、
  .status.ok/.bad/.warn、.kv white-space:pre-wrap。
- tests/test_web_ui.py：静态 marker 断言（mdToHtml/task-notice/seenNotifs/mdSafeUrl in js，
  .task-notice/pre.md-pre in css）。

**Blocked by:** 无（SSE notification 事件已存在）

**Status:** done

- [x] 任务完成/失败/取消/待审批时聊天面板主动出现任务通知卡（无需手动切任务/结果页）
- [x] 按 notification_id 去重（SSE 重连/刷新重放不重复插卡）
- [x] Markdown 结果（标题/列表/代码块/链接/加粗斜体）结构化渲染；零依赖、无 CDN
- [x] 结果面板与任务详情结果同用 Markdown 渲染；错误文本保持纯文本 pre-wrap
- [x] XSS 安全：markdown 源先 esc 再转换；链接 URL 白名单（http/https/mailto/#/相对）
- [x] tests/test_web_ui.py 静态 marker 断言新增
