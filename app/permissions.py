"""权限兜底（票 02 粗粒度 → 票 03 细粒度）：Blacklist + 提交即挂起策略。

纪律（安全基线）：权限由 Python 服务端强制，不靠 prompt。
- Blacklist 为 JSON 字符串列表文件，**每次检查重读**（运行期追加即时生效）；
- 文件缺席 → 空列表（全放行）；损坏/类型不符 → **fail-closed 全命中** + warn 日志；
- 匹配 = normcase+normpath 边界安全前缀（目录自身或其后代），不做全盘模糊匹配。

coarse_task_policy：任务 workspace 命中黑名单且未被已批准路径覆盖
（ancestor-or-self 边界）→ 提交即 RunSuspended（waiting-approval 粗粒度门禁）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from app.adapters.base import RunSuspended
from app.dispatcher import PolicyFn

logger = logging.getLogger(__name__)

# 损坏时 fail-closed：命中一切路径
_MATCH_ALL = object()


def boundary_match(target: str, root: str) -> bool:
    """normcase+normpath 边界安全前缀：target 等于 root 或是其后代。"""
    t = os.path.normcase(os.path.normpath(target))
    r = os.path.normcase(os.path.normpath(root))
    return t == r or t.startswith(r + os.sep)


class Blacklist:
    """JSON 列表文件；每次检查重读，运行期追加即时生效。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _entries(self) -> list[str] | object:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("blacklist 读取失败（%s），fail-closed 全命中", exc)
            return _MATCH_ALL
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("blacklist 损坏（非 JSON），fail-closed 全命中")
            return _MATCH_ALL
        if not isinstance(data, list) or not all(isinstance(e, str) and e for e in data):
            logger.warning("blacklist 必须是字符串列表，fail-closed 全命中")
            return _MATCH_ALL
        return data

    def matches(self, path: str) -> bool:
        entries = self._entries()
        if entries is _MATCH_ALL:
            return True
        return any(boundary_match(path, e) for e in entries)


def coarse_task_policy(bl: Blacklist) -> PolicyFn:
    """Dispatcher 提交时策略：workspace 命中黑名单 → 挂起等待人工审批。

    已被批准路径（pending_op.approved_paths 的 ancestor-or-self 覆盖）豁免；
    票 03 的审批恢复会写入 approved_paths，恢复执行时不再二次挂起。
    """

    async def policy(task: dict[str, Any], run: dict[str, Any]) -> RunSuspended | None:
        ws = task.get("workspace")
        if not ws or not bl.matches(ws):
            return None
        po = run.get("pending_op") or {}
        approved = po.get("approved_paths") or []
        if any(boundary_match(ws, a) for a in approved):
            return None
        return RunSuspended(
            {"kind": "task_workspace", "path": ws, "detail": "workspace 命中黑名单，等待人工审批"},
            "blacklist",
            approved,
        )

    return policy