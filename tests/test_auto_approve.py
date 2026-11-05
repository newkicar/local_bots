"""代审（auto-approval）单元测试：只读命令规则 + 代审策略分支（票 03 延伸）。

纯单元：不建 app、不落库；run 形状与 TaskManager.waiting_runs() 行一致
（pending_op 为 {"pending_op": {...}, "approved_paths": [...]} 外层 wrapper）。
"""
from __future__ import annotations

import pytest

from app.auto_approve import MODES, AutoApprover, extract_actions, is_readonly_command


def _run(wait_reason: str, actions) -> dict:
    """构造 waiting-approval Run 行形状（jarvis_interrupt 才带 actions）。"""
    pending = {"kind": "jarvis_interrupt", "path": r"C:\ws"}
    if actions is not None:
        pending["actions"] = actions
    return {"run_id": "R1", "wait_reason": wait_reason,
            "pending_op": {"pending_op": pending, "approved_paths": []}}


# ---------- 只读命令规则（is_readonly_command） ----------
def test_is_readonly_command_positive():
    for cmd in ("git status", r"Get-ChildItem C:\x", "cmd /c dir",
                "powershell -Command ls", "bash -c pwd", "wsl -e ls", "whoami"):
        assert is_readonly_command(cmd) is True, cmd


def test_is_readonly_command_negative():
    for cmd in ("write_file x", r"Remove-Item C:\x", "git push", "", "   "):
        assert is_readonly_command(cmd) is False, cmd


# ---------- 代审策略分支（AutoApprover.should_auto_approve） ----------
def test_auto_safe_branches():
    ap = AutoApprover("auto_safe")
    # 非 jarvis 引擎挂起 → 一律人工
    assert ap.should_auto_approve(_run("blacklist", [{"tool": "execute", "arg": "git status"}])) is False
    # jarvis 挂起但无结构化动作 → 保守人工
    assert ap.should_auto_approve(_run("jarvis_interrupt", None)) is False
    # 动作非 execute 工具 → 人工
    assert ap.should_auto_approve(_run("jarvis_interrupt",
                                       [{"tool": "write_file", "arg": r"C:\x"}])) is False
    # execute 但命令非只读 → 人工
    assert ap.should_auto_approve(_run("jarvis_interrupt",
                                       [{"tool": "execute", "arg": r"Remove-Item C:\x"}])) is False
    # 混合（一读一写）→ 人工
    assert ap.should_auto_approve(_run("jarvis_interrupt",
                                       [{"tool": "execute", "arg": "git status"},
                                        {"tool": "execute", "arg": "git push"}])) is False
    # 全部只读 execute → 自动放行
    assert ap.should_auto_approve(_run("jarvis_interrupt",
                                       [{"tool": "execute", "arg": "git status"},
                                        {"tool": "execute", "arg": "dir"}])) is True


def test_manual_never_and_yolo_always():
    ap = AutoApprover("manual")
    assert ap.should_auto_approve(_run("jarvis_interrupt",
                                       [{"tool": "execute", "arg": "git status"}])) is False
    assert ap.should_auto_approve(_run("blacklist", [{"tool": "execute", "arg": "dir"}])) is False
    ap.set_mode("yolo")
    assert ap.should_auto_approve(_run("blacklist", [{"tool": "write_file", "arg": "x"}])) is True


def test_set_mode_rejects_unknown():
    ap = AutoApprover()
    for bad in ("auto", "", "YOLO", "AUTO_SAFE", "manual "):
        with pytest.raises(ValueError):
            ap.set_mode(bad)
    for good in MODES:
        ap.set_mode(good)
        assert ap.current_mode() == good
    with pytest.raises(ValueError):
        AutoApprover("bogus")


# ---------- 结构化动作提取（extract_actions 脏输入容忍） ----------
def test_extract_actions_dirt_inputs():
    assert extract_actions(None) == []
    assert extract_actions({}) == []
    assert extract_actions({"pending_op": {}}) == []
    assert extract_actions({"pending_op": {"actions": "not-a-list"}}) == []
    assert extract_actions({"pending_op": {"actions": [
        {"tool": "execute", "arg": "git status"},
        {"tool": ""},
        {"arg": "no-tool"},
        "junk", 42,
    ]}}) == [{"tool": "execute", "arg": "git status"}]
