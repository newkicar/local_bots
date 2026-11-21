"""项目信任（Project Trust，票 05）：项目级 Agent 的安全边界。

设计基线：Local Dots Agent Organization 升级实施方案 §6（项目信任）+
docs/agent-organization/02（projects 表随票 02 已建，catalog 侧
register_project/set_project_trust/get_project 已存在）。
纪律：
- Project Root 与 Task Workspace 是不同概念（§6.2）：project_id 只是
  身份与发现域，任务 workspace 独立保留；
- 项目根路径必须 canonicalize（realpath 解析 symlink + normcase 大小写折叠），
  拒绝 symlink 逃出 / `..` 穿越 / 盘符切换 / UNC（§6.3、§15.3）；
- 信任状态机：pending → trusted → revoked；未信任项目不可使用其项目级 agent；
  撤销信任只挡新任务，历史任务记录保留（catalog 行永不删除）；
- 项目文件不能扩大 Local Dots 全局权限；能力标签不代替权限检查。
"""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Any

from app.agents.catalog import PROJECT_TRUST_STATES, AgentCatalog


class ProjectTrustError(ValueError):
    """项目根路径不合法（逃逸/穿越/盘符切换/UNC/非目录）或非法信任迁移。"""


# 信任迁移守卫：revoked 是终态（重新信任 = 新登记，避免静默恢复）
_TRUST_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"trusted", "revoked"},
    "trusted": {"revoked"},
    "revoked": set(),
}


def normalize_project_root(root: str | Path) -> str:
    """canonicalize 项目根，拒绝一切逃逸向量（§6.3）。

    规则（按序执行，任一命中即 ProjectTrustError）：
    - UNC 路径（//server/share、\\\\?\\ 前缀）拒绝；
    - 相对路径按 CWD 解析为绝对；
    - Windows 无盘符的裸路径（如 D:、/work）拒绝——防盘符切换/歧义；
    - `..` 穿越：拒绝任何含 `..` 段的输入（不静默折叠穿越）；
    - os.path.realpath 解析 symlink，且解析结果必须仍在声明路径前缀之下
      （symlink 逃逸/盘符切换拒绝）；目标必须已存在且为目录；
    - normcase 折叠（Windows 大小写不敏感），与登记值保持同形。
    """
    text = str(root).strip()
    if not text:
        raise ProjectTrustError("项目根路径为空")
    if text.startswith(("\\\\", "//")) or text.startswith("\\\\?\\") or text.startswith("//?/"):
        raise ProjectTrustError(f"UNC 路径拒绝: {text}")
    if ".." in PureWindowsPath(text).parts:
        raise ProjectTrustError(f"路径穿越（`..` 段）拒绝: {text}")
    if os.name == "nt":
        # 根绝对路径但无盘符（如 /work/proj，is_absolute 为假、会被 cwd 拼盘符）→ 拒绝
        if PureWindowsPath(text).root and not PureWindowsPath(text).drive:
            raise ProjectTrustError(f"Windows 项目根必须带盘符（拒绝盘符切换/裸路径）: {text}")
    p = Path(text)
    if not p.is_absolute():
        p = Path.cwd() / p
    norm = os.path.normpath(str(p))
    resolved = os.path.realpath(norm)
    # symlink 逃逸检查：解析结果必须仍在声明路径前缀之下（§6.3/§15.3）；
    # 盘符不匹配（盘符切换）commonpath 抛 ValueError → 同样拒绝
    declared_nc = os.path.normcase(norm)
    resolved_nc = os.path.normcase(resolved)
    try:
        common = os.path.commonpath([declared_nc, resolved_nc])
    except ValueError:
        common = None
    if common != declared_nc:
        raise ProjectTrustError(
            f"symlink 逃逸拒绝（解析到声明路径之外）: {text} -> {resolved}"
        )
    if not os.path.exists(resolved):
        raise ProjectTrustError(f"项目根不存在: {resolved}")
    if not os.path.isdir(resolved):
        raise ProjectTrustError(f"项目根不是目录: {resolved}")
    return os.path.normcase(resolved)


class ProjectTrustService:
    """项目登记 + 信任迁移 + 边界检查（挂 AgentCatalog 的 projects 表）。

    - register_project：canonicalize 后登记（trust_state=pending）；
    - trust/voke：走迁移守卫，写 catalog.set_project_trust；
    - can_use_project_agent：信任门控（未信任项目的项目级 agent 不可用）；
    - root_contains：symlink 解析后的包含性检查（不跟随逃出项目根）。
    """

    def __init__(self, catalog: AgentCatalog, *, trust_required: bool = True) -> None:
        self._catalog = catalog
        self.trust_required = trust_required

    # ---------- 登记 ----------
    async def register_project(self, name: str, root: str | Path) -> dict[str, Any]:
        """登记项目：canonicalize root，root_path 存原始输入、canonical_root 存规范化值。"""
        canonical = normalize_project_root(root)
        project = await self._catalog.register_project(name, str(Path(root)))
        await self._catalog.set_project_canonical_root(project["project_id"], canonical)
        return await self._catalog.get_project(project["project_id"])

    # ---------- 信任迁移 ----------
    async def trust_project(self, project_id: str) -> dict[str, Any]:
        """pending → trusted。"""
        return await self._transition(project_id, "trusted")

    async def revoke_project(self, project_id: str) -> dict[str, Any]:
        """pending/trusted → revoked（终态；只挡新任务，历史行保留）。

        同时把该项目 project-scope agent 降 stale（票 06：撤销信任→stale，不删行）。
        返回 dict 附 demoted_agents 列表。
        """
        project = await self._transition(project_id, "revoked")
        project["demoted_agents"] = await self._catalog.demote_project_agents(project_id)
        return project

    async def _transition(self, project_id: str, to: str) -> dict[str, Any]:
        project = await self._catalog.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        allowed = _TRUST_TRANSITIONS.get(project["trust_state"], set())
        if to not in allowed:
            raise ProjectTrustError(
                f"项目 {project_id}: {project['trust_state']} → {to} 非法迁移"
            )
        return await self._catalog.set_project_trust(project_id, to)

    # ---------- 门控与包含性 ----------
    def is_trusted(self, project: dict[str, Any] | None) -> bool:
        """项目级 agent 信任门控：trust_required 时仅 trusted 放行。"""
        if project is None:
            return False
        if not self.trust_required:
            return True
        return project.get("trust_state") == "trusted"

    async def can_use_project_agent(self, project_id: str, agent_scope: str) -> bool:
        """项目级 agent（source_scope=project）是否允许新任务使用。

        - user/builtin scope 不走项目信任门控（各自规则）；
        - 项目 revoked/未知 → 拒绝；
        - 撤销信任只影响新任务（本函数），历史任务快照（票 08）保留。
        """
        if agent_scope != "project":
            return True
        if not self.trust_required:
            return True  # 信任检查整体关闭（LD_PROJECT_TRUST_REQUIRED=false）→ 不强制
        project = await self._catalog.get_project(project_id)
        return self.is_trusted(project)

    async def root_contains(self, project_id: str, candidate: str | Path) -> bool:
        """symlink 解析后的包含性检查：candidate 是否落在项目根内（含根自身）。

        不跟随逃出项目根的链接（§15.3：不允许通过 symlink 逃出项目根）；
        逃逸/穿越向量（`..`、盘符切换）直接 False，不抛错。
        """
        project = await self._catalog.get_project(project_id)
        if project is None:
            return False
        root = project.get("canonical_root") or normalize_project_root(project["root_path"])
        root = os.path.normcase(os.path.realpath(root))
        try:
            cand = os.path.normcase(os.path.realpath(os.path.normpath(str(candidate))))
        except OSError:
            return False
        if ".." in Path(cand).parts:
            return False
        rel = os.path.relpath(cand, root)
        return not rel.startswith("..")
