"""代审（auto-approval）：审批门禁按模式自动放行（票 03 延伸）。

三种模式（LD_APPROVAL_MODE，运行期可经 /api/approval-mode 切换）：
- manual   手动审批（缺省，行为与现状一致：等人工点批准）
- auto_safe 有限代审：仅当挂起来源为 jarvis_interrupt 且全部结构化动作
  均为"只读 execute 命令"时自动放行；其余（文件写/删、非只读命令、
  无法解析的结构化动作、非 jarvis 挂起）一律交人工
- yolo     无限代审（YOLO）：全部审批请求自动放行

纪律（安全基线）：自动放行与手动批准走同一条 ApprovalGate.approve 路径
（resume_wait_for_approved_path + decide_run + dispatcher.resume_run），
Side-Effect Ledger 照常追加输出；无法结构化解析的旧数据保守交人工。

只读命令规则在本模块本地实现（镜像 Jarvis 的 HEURISTIC_SAFE_COMMANDS 语义，
不 import Jarvis 任何模块）：大小写不敏感，取前 1-2 个 token 判前缀。
"""
from __future__ import annotations

from typing import Any

MODES: tuple[str, ...] = ("manual", "auto_safe", "yolo")

# 只读命令前缀表（1 token 或 2 token，全小写；大小写不敏感匹配）
_READONLY_COMMANDS: frozenset[str] = frozenset(
    [
        "get-date",
        "whoami",
        "hostname",
        "get-location",
        "pwd",
        "ls",
        "dir",
        "get-childitem",
        "get-content",
        "get-process",
        "tasklist",
        "echo",
        "git status",
        "git log",
        "git diff",
    ]
)

# shell 包装剥离：包装器（小写）→ 其后引导参数
_SHELL_WRAPPERS: dict[str, tuple[str, ...]] = {
    "cmd": ("/c", "/k"),
    "powershell": ("-command", "-c", "-noprofile", "-nop"),
    "pwsh": ("-command", "-c", "-noprofile", "-nop"),
    "bash": ("-c",),
    "sh": ("-c",),
    "wsl": ("-e",),
}


def _strip_shell_wrapper(tokens: list[str]) -> list[str]:
    """剥离一层 shell 包装（cmd /c、powershell -Command、bash -c、wsl -e…）。"""
    if not tokens:
        return tokens
    guides = _SHELL_WRAPPERS.get(tokens[0].lower())
    if not guides:
        return tokens
    rest = tokens[1:]
    for i in range(min(len(rest), 3)):
        if rest[i].lower() in guides:
            return rest[i + 1:]
    return []


def is_readonly_command(command: str) -> bool:
    """判断一条 shell 命令是否为安全只读命令（前缀匹配，不执行）。"""
    tokens = [t for t in (command or "").split() if t.strip()]
    if not tokens:
        return False
    tokens = _strip_shell_wrapper(tokens)
    if not tokens:
        return False
    first = tokens[0].lower()
    two = f"{first} {tokens[1].lower()}" if len(tokens) >= 2 else ""
    return two in _READONLY_COMMANDS or first in _READONLY_COMMANDS


def extract_actions(pending_op: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从 run.pending_op 提取结构化动作列表（jarvis_interrupt 由适配器写入 actions）。

    返回 [] 表示不可解析（旧数据 / 非结构化挂起）——调用方应保守交人工。
    """
    po = (pending_op or {}).get("pending_op") or {}
    actions = po.get("actions")
    if not isinstance(actions, list):
        return []
    out: list[dict[str, Any]] = []
    for a in actions:
        if isinstance(a, dict) and a.get("tool"):
            out.append({"tool": str(a.get("tool")), "arg": str(a.get("arg") or "")})
    return out


class AutoApprover:
    """代审策略：按当前模式决定一条 waiting-approval Run 是否自动放行。"""

    def __init__(self, initial_mode: str = "manual") -> None:
        if initial_mode not in MODES:
            raise ValueError(f"unknown approval mode: {initial_mode!r}")
        self._mode = initial_mode

    def current_mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown approval mode: {mode!r}")
        self._mode = mode

    def should_auto_approve(self, run: dict[str, Any], mode: str | None = None) -> bool:
        """该 Run 在给定（或当前）模式下是否应自动放行。"""
        m = mode or self._mode
        if m == "manual":
            return False
        if m == "yolo":
            return True
        # auto_safe：仅 jarvis 引擎挂起 + 全部动作都是只读 execute 命令
        if run.get("wait_reason") != "jarvis_interrupt":
            return False
        actions = extract_actions(run.get("pending_op"))
        if not actions:
            return False  # 不可解析 → 保守交人工
        for a in actions:
            if a["tool"] != "execute":
                return False
            if not is_readonly_command(a.get("arg", "")):
                return False
        return True
