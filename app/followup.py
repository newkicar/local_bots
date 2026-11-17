"""票 14：任务终态/待审批的确定性跟进报告。

任务到达终态（completed/failed/rejected/cancelled）或 waiting-approval 时，
自动向所有链接了该任务的对话推入一条 role="report" 的"任务跟进"消息并经
SSE（conversation_updated）实时刷新聊天面板；completed 顺势迁移 reported，
补上"秘书口头承诺跟进但实际无回路"的断点（原 GET /api/tasks/{id} 惰性
迁移降为兜底，不再是主链路）。

设计点：
- 触发：TaskManager notify 回调（dispatcher/gate 主链），无 LLM 参与，确定性；
- 分发面：ConversationStore.find_conversations_for_task（conversation_tasks 反查）；
- 无环：handler 忽略 thread-bound/queued/running/reported/interrupted，
  completed→reported 的二次 task_state 与 notification 事件均不触发；
- 失败隔离：整体 try/except，绝不拖垮 notify 主链（任务状态机先行落库）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("local_dots.followup")

#: 触发集：终态 + 待审批；中间态（thread-bound/queued/running/interrupted）与
#: reported（二次迁移）一律忽略，防环。
TRIGGER_STATES = {"completed", "failed", "rejected", "cancelled", "waiting-approval"}

_STATE_META: dict[str, tuple[str, str]] = {
    "completed": ("✅", "任务完成"),
    "failed": ("❌", "任务失败"),
    "rejected": ("⛔", "任务被拒绝"),
    "cancelled": ("⊘", "任务已取消"),
    "waiting-approval": ("⏳", "任务待审批"),
}


def _excerpt(text: Any, limit: int) -> str:
    """空白折叠摘录：所有空白折为单空格，超 limit 截断加 …。"""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat[:limit] + "…" if len(flat) > limit else flat


async def handle_task_event(event: dict[str, Any], tm: Any, conv_store: Any, hub: Any) -> None:
    """task_state 事件 → 向链接对话推跟进报告；completed → reported 闭环。

    tm/conv_store/hub 鸭子类型（不 import TaskManager/ConversationStore 运行时，
    免循环依赖、可测）；全程 try/except 失败隔离。
    """
    try:
        if event.get("type") != "task_state":
            return
        to = event.get("to")
        if to not in TRIGGER_STATES:
            return
        task_id = event.get("task_id")
        task = await tm.get_task(task_id)
        if task is None:
            return
        cids = await conv_store.find_conversations_for_task(task_id)
        if not cids:
            return

        icon, label = _STATE_META[to]
        lines = [f"{icon} {label}（{task_id}）", "指令：" + _excerpt(task.get("instruction"), 60)]
        if to == "completed":
            lines.append("结果：" + _excerpt(task.get("result"), 400))
        elif to == "failed":
            lines.append("错误：" + _excerpt(task.get("error"), 400))
        elif to == "rejected":
            lines.append("拒绝原因：" + _excerpt(task.get("reject_reason"), 400))
        elif to == "cancelled":
            lines.append("任务已取消（Side-Effect Ledger 只追加不回滚）")
        else:  # waiting-approval：原因在 RUN 上（取最新 attempt 的 wait_reason）
            reason = "未知"
            runs = await tm.get_runs(task_id)
            for r in runs:
                if r.get("state") == "waiting-approval" and r.get("wait_reason"):
                    reason = r["wait_reason"]
            lines.append("待审批原因：" + _excerpt(reason, 400))
        text = "\n".join(lines)

        for cid in cids:
            await conv_store.append_message(cid, "report", text)
            await hub.publish({"type": "conversation_updated", "conversation_id": cid})

        if to == "completed":
            # 确定性闭环：不再依赖 GET /api/tasks/{id} 的惰性迁移（保留为兜底）
            await tm.transition_task(task_id, "reported")
    except Exception:
        logger.exception("任务跟进报告推送失败（不影响 notify 主链）")