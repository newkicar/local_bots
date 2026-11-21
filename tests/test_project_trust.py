"""票 05 验收：项目信任（root 规范化/逃逸校验 + 信任状态机 + agent 门控）。

纪律：Project Root ≠ Task Workspace（§6.2）；canonicalize = realpath + normcase；
拒绝 symlink 逃逸 / `..` 穿越 / 盘符切换 / UNC；撤销信任只挡新任务、历史行保留
（catalog 行永不删除）；能力标签不代替权限检查。
本机 pytest WinError-5 中毒：自建 tests/tmp_workspace/。
"""
from __future__ import annotations

import os
import shutil

import pytest

from app.agents.catalog import AgentCatalog
from app.agents.project_trust import (
    ProjectTrustError,
    ProjectTrustService,
    normalize_project_root,
)

BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace"
)


def _db(name: str) -> str:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return os.path.join(d, "tasks.db")


def _dir(name: str) -> str:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


def _norm(root: str) -> str:
    return os.path.normcase(os.path.realpath(root))


# ---------- root 规范化 / 逃逸向量 ----------
def test_normalize_absolute_identity():
    d = _dir("pt_abs")
    assert normalize_project_root(d) == _norm(d)


def test_normalize_relative_resolves_to_absolute(monkeypatch):
    d = _dir("pt_rel_parent")
    proj = os.path.join(d, "proj")
    os.makedirs(proj)
    monkeypatch.chdir(d)
    assert normalize_project_root("proj") == _norm(proj)


def test_normalize_rejects_dotdot_traversal(monkeypatch):
    d = _dir("pt_trav")
    outside = os.path.join(d, "outside")
    os.makedirs(outside)
    monkeypatch.chdir(d)
    with pytest.raises(ProjectTrustError, match="穿越"):
        normalize_project_root("../outside")


def test_normalize_rejects_unc_bare_and_empty():
    with pytest.raises(ProjectTrustError, match="UNC"):
        normalize_project_root("\\\\server\\share\\proj")
    with pytest.raises(ProjectTrustError, match="UNC"):
        normalize_project_root("//server/share/proj")
    if os.name == "nt":
        with pytest.raises(ProjectTrustError, match="盘符"):
            normalize_project_root("/work/proj")  # 无盘符裸绝对路径
    with pytest.raises(ProjectTrustError, match="为空"):
        normalize_project_root("   ")


def test_normalize_rejects_symlink_escape(monkeypatch):
    """模拟：项目根是 symlink、realpath 解析到声明前缀之外 → 拒绝。"""
    d = _dir("pt_symlink")
    inner = os.path.join(d, "inner")
    outside = os.path.join(d, "outside")
    os.makedirs(inner)
    os.makedirs(outside)
    real = os.path.realpath

    def fake_realpath(path):
        if os.path.normcase(os.path.normpath(str(path))) == os.path.normcase(
            os.path.normpath(inner)
        ):
            return outside
        return real(path)

    monkeypatch.setattr("os.path.realpath", fake_realpath)
    with pytest.raises(ProjectTrustError, match="逃逸"):
        normalize_project_root(inner)


def test_normalize_rejects_missing_and_non_directory():
    missing = os.path.join(BASE, "pt_missing_never_created")
    if os.path.exists(missing):
        shutil.rmtree(missing)
    with pytest.raises(ProjectTrustError, match="不存在"):
        normalize_project_root(missing)
    f = _dir("pt_file")
    target = os.path.join(f, "a.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("x")
    with pytest.raises(ProjectTrustError, match="不是目录"):
        normalize_project_root(target)


# ---------- 登记 + 信任状态机（挂 catalog.projects） ----------
async def _svc(db: str) -> tuple[ProjectTrustService, AgentCatalog]:
    cat = AgentCatalog(db)
    await cat.open()
    return ProjectTrustService(cat), cat


async def test_register_project_persists_canonical_root():
    root = _dir("pt_reg")
    svc, cat = await _svc(_db("pt_reg_db"))
    project = await svc.register_project("myproj", root)
    assert project["trust_state"] == "pending"
    assert project["root_path"] == root
    assert project["canonical_root"] == _norm(root)
    got = await cat.get_project(project["project_id"])
    assert got["canonical_root"] == _norm(root)  # 写库
    await cat.close()


async def test_trust_revoke_transitions_and_terminal():
    db = _db("pt_trust_db")  # 存变量：重开时不得再 rmtree 抹库
    svc, cat = await _svc(db)
    p = await svc.register_project("p", _dir("pt_t1"))
    pid = p["project_id"]
    await svc.trust_project(pid)
    assert (await cat.get_project(pid))["trust_state"] == "trusted"
    with pytest.raises(ProjectTrustError):  # trusted → trusted 非法
        await svc.trust_project(pid)
    await svc.revoke_project(pid)
    assert (await cat.get_project(pid))["trust_state"] == "revoked"
    with pytest.raises(ProjectTrustError):  # revoked 终态，不可静默恢复
        await svc.trust_project(pid)
    await cat.close()
    cat2 = AgentCatalog(db)
    await cat2.open()
    assert (await cat2.get_project(pid))["trust_state"] == "revoked"  # 持久
    await cat2.close()


async def test_revoke_pending_project_allowed():
    svc, cat = await _svc(_db("pt_revoke_pending"))
    p = await svc.register_project("p", _dir("pt_rp"))
    await svc.revoke_project(p["project_id"])
    assert (await cat.get_project(p["project_id"]))["trust_state"] == "revoked"
    await cat.close()


# ---------- 项目级 agent 门控 ----------
async def test_can_use_project_agent_gating():
    svc, cat = await _svc(_db("pt_gate"))
    root = _dir("pt_gate_root")
    p = await svc.register_project("g", root)
    pid = p["project_id"]
    assert not await svc.can_use_project_agent(pid, "project")  # pending 未信任
    assert await svc.can_use_project_agent(pid, "user")  # user scope 不走项目门控
    await svc.trust_project(pid)
    assert await svc.can_use_project_agent(pid, "project")
    await svc.revoke_project(pid)
    assert not await svc.can_use_project_agent(pid, "project")
    assert not await svc.can_use_project_agent("P_does_not_exist", "project")
    loose = ProjectTrustService(cat, trust_required=False)
    assert await loose.can_use_project_agent(pid, "project")  # 门控整体关闭 → 放行
    await cat.close()


async def test_revocation_preserves_history_rows():
    """撤销信任只挡新任务：catalog 行与状态原样保留（永不删行，§6.3）。"""
    svc, cat = await _svc(_db("pt_history"))
    p = await svc.register_project("h", _dir("pt_hist_root"))
    pid = p["project_id"]
    desc = {
        "agent_id": f"codex:project:{pid}:alpha", "engine": "codex",
        "engine_profile_id": "alpha", "name": "Alpha", "description": None,
        "source_scope": "project", "source_id": "alpha", "source_path": None,
        "capabilities": {}, "invocation_mode": "delegated_only",
        "profile_version": None, "profile_hash": "h1", "metadata": {},
    }
    await cat.upsert_agent(desc)
    await cat.transition_agent(desc["agent_id"], "available")
    await svc.revoke_project(pid)
    row = await cat.get_agent(desc["agent_id"])
    assert row is not None and row["discovery_status"] == "available"  # 记录保留
    assert not await svc.can_use_project_agent(pid, "project")  # 但新任务被挡
    await cat.close()


# ---------- 包含性检查（不跟随逃出项目根的链接） ----------
async def test_root_contains_within_sibling_and_escape():
    svc, cat = await _svc(_db("pt_contains"))
    root = _dir("pt_contains_root")
    sub = os.path.join(root, "sub")
    os.makedirs(sub)
    sibling = os.path.join(BASE, "pt_contains_sibling")
    p = await svc.register_project("c", root)
    pid = p["project_id"]
    assert await svc.root_contains(pid, root)  # 根自身
    assert await svc.root_contains(pid, sub)
    assert not await svc.root_contains(pid, sibling)  # 兄弟目录
    assert not await svc.root_contains(pid, os.path.join(root, "..", "sibling"))  # 穿越
    assert not await svc.root_contains(pid, os.path.join(root, "a", "..", ".."))  # 穿越
    assert not await svc.root_contains(pid, "P_nope")  # 未知项目
    await cat.close()
