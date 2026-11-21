"""Codex 用户级发现（票 04）：读 $CODEX_HOME/agents/*.toml，仅 tomllib 元数据，绝不执行。

设计基线：docs/agent-organization/02-agent-catalog-design.md（方案 §5）。
纪律：
- CODEX_HOME 环境变量优先，默认 ~/.codex；目录缺失 → 空结果（非错误）；
- 仅 tomllib 解析（stdlib，不执行 TOML 内任何东西，TOML 本身无执行语义）；
- 坏 TOML / 缺必填字段（name/description/developer_instructions）→ 跳过并记录
  error，不阻塞其余文件；
- 自定义 agent 一律 delegated_only；user scope 的 agent_id = codex:<name>；
  与内建 profile 名（default/worker/explorer）冲突时跳过（agent_id 是 PK）；
- 内建 codex:default/worker/explorer 已随票 02 播种，此票只补用户自定义发现；
- 不发明任何 codex CLI 参数；project scope 由本模块 CodexProjectDiscoveryProvider（票 06）实现。
"""
from __future__ import annotations

import hashlib
import os
import tomllib
from pathlib import Path
from typing import Any

from app.agents.discovery import DISCOVERY_OK, DISCOVERY_UNSUPPORTED, DiscoveryResult
from app.agents.catalog import AgentCatalog  # type: ignore

# 必填键（存在且非空才算合法 agent 定义）；developer_instructions 是行为性内容，
# 只记录存在性 + 全文 hash，不把原文写进 catalog。
REQUIRED_KEYS = ("name", "description", "developer_instructions")
# 内建 profile 名（票 02 种子）：user agent 同名会撞 agent_id 主键 → 跳过
_BUILTIN_NAMES = ("default", "worker", "explorer")


def _default_codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


class CodexUserDiscoveryProvider:
    """$CODEX_HOME/agents/*.toml → user scope agent 描述符（metadata-only）。

    codex_home 可注入（测试用）；None → 尊重 $CODEX_HOME / ~/.codex。
    """

    engine = "codex"

    def __init__(self, codex_home: str | Path | None = None) -> None:
        self._codex_home = Path(codex_home) if codex_home else None

    def _agents_dir(self) -> Path:
        home = self._codex_home or _default_codex_home()
        return home / "agents"

    async def discover(self, scope: str) -> DiscoveryResult:
        if scope != "user":
            return DiscoveryResult(
                self.engine, DISCOVERY_UNSUPPORTED, [],
                "codex user-scope 发现；project scope 属票 06",
            )
        agents_dir = self._agents_dir()
        if not agents_dir.is_dir():
            # 目录缺失 = 无自定义 agent，非错误
            return DiscoveryResult(self.engine, DISCOVERY_OK, [], "agents 目录缺失 → 空结果")
        agents: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for f in sorted(agents_dir.glob("*.toml")):
            desc = self._read_one(f, errors)
            if desc is not None:
                agents.append(desc)
        detail = "; ".join(e["detail"] for e in errors) if errors else ""
        return DiscoveryResult(
            self.engine, DISCOVERY_OK, agents, detail, errors=errors, change_kind="behavior"
        )

    # ---------- 单文件读取（委托公共 reader） ----------
    def _read_one(self, f: Path, errors: list[dict[str, str]]) -> dict[str, Any] | None:
        parsed = parse_agent_toml(f, errors)
        if parsed is None:
            return None
        name = parsed["name"]
        if name in _BUILTIN_NAMES:
            errors.append(
                {"agent": f.name,
                 "detail": f"agent 名 {name!r} 与内建 profile 冲突（codex:{name} 已播种）"}
            )
            return None
        return {
            "agent_id": f"codex:{name}",
            "engine": "codex",
            "engine_profile_id": name,
            "name": name,
            "description": parsed["description"],
            "source_scope": "user",
            "source_id": name,
            "source_path": parsed["source_path"],
            "capabilities": {},
            "invocation_mode": "delegated_only",
            "profile_version": None,
            # 行为 hash 覆盖全文（developer_instructions 是 prompt，属行为）
            "profile_hash": parsed["profile_hash"],
            "metadata": {
                "source_file": parsed["source_file"],
                "has_developer_instructions": True,
            },
        }


# ---------- 公共 reader（票 04 落地、票 06 复用） ----------
def parse_agent_toml(f: Path, errors: list[dict[str, str]]) -> dict[str, Any] | None:
    """读并校验单个 agents/*.toml（tomllib 元数据，绝不执行 TOML）。

    成功返回原始字段 {name, description, profile_hash, source_file, source_path}；
    失败（读盘/解码/坏 TOML/缺必填）记入 errors 并返回 None，不阻塞同目录其余文件。
    """
    try:
        raw = f.read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        errors.append({"agent": f.name, "detail": f"{type(exc).__name__}: {exc}"})
        return None
    missing = [
        k for k in REQUIRED_KEYS
        if not str(data.get(k) or "").strip()
    ]
    if missing:
        errors.append(
            {"agent": f.name, "detail": "缺必填字段: " + ",".join(missing)}
        )
        return None
    return {
        "name": str(data["name"]).strip(),
        "description": str(data["description"]).strip(),
        "source_file": f.name,
        "source_path": str(f),
        # 行为 hash 覆盖全文（developer_instructions 是 prompt，属行为）
        "profile_hash": hashlib.sha256(raw).hexdigest(),
    }


# ---------- 项目级发现（票 06） ----------
class CodexProjectDiscoveryProvider:
    """项目级发现：<trusted-project-root>/.codex/agents/*.toml → project scope。

    纪律：
    - 信任门控：只枚举 trust_state=trusted 的项目（pending/revoked → 不激活、不读）；
    - agent_id = codex:project:<project_id>:<name>；source_scope=project；
      source_id=<project_id>:<name>（UNIQUE 键按项目隔离）；
    - 项目级 agent 一律先 pending_review（catalog._initial_status：
      auto_enable_project_profiles 默认 False；ticket 11 可经 LD_AGENT_AUTO_ENABLE_PROJECT_PROFILES 开启）；
    - 撤销项目信任由 ProjectTrustService.revoke_project 触发
      catalog.demote_project_agents → stale（行永不删）；
    - 复用票 04 reader（parse_agent_toml），绝不执行 TOML。
    """

    engine = "codex"

    def __init__(self, catalog: "AgentCatalog") -> None:
        self._catalog = catalog

    async def discover(self, scope: str) -> DiscoveryResult:
        if scope != "project":
            return DiscoveryResult(
                self.engine, DISCOVERY_UNSUPPORTED, [],
                "codex project-scope 发现；需 scope=project",
            )
        projects = await self._catalog.list_projects(trust_state="trusted")
        if not projects:
            return DiscoveryResult(self.engine, DISCOVERY_OK, [], "无受信任项目 → 空结果")
        agents: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for proj in projects:
            pid = proj["project_id"]
            root = proj.get("canonical_root") or proj["root_path"]
            agents_dir = Path(root) / ".codex" / "agents"
            if not agents_dir.is_dir():
                continue  # 目录缺失 = 该项目无自定义 agent，非错误
            start = len(errors)
            for f in sorted(agents_dir.glob("*.toml")):
                parsed = parse_agent_toml(f, errors)
                if parsed is None:
                    continue
                for e in errors[start:]:
                    e["project"] = pid
                    start += 1
                agents.append({
                    "agent_id": f"codex:project:{pid}:{parsed['name']}",
                    "engine": "codex",
                    "engine_profile_id": parsed["name"],
                    "name": f"{proj['name']}/{parsed['name']}",
                    "description": parsed["description"],
                    "source_scope": "project",
                    "source_id": f"{pid}:{parsed['name']}",
                    "source_path": parsed["source_path"],
                    "capabilities": {},
                    "invocation_mode": "delegated_only",
                    "profile_version": None,
                    "profile_hash": parsed["profile_hash"],
                    "metadata": {
                        "source_file": parsed["source_file"],
                        "has_developer_instructions": True,
                        "project_id": pid,
                    },
                })
        detail = "; ".join(e["detail"] for e in errors) if errors else ""
        return DiscoveryResult(
            self.engine, DISCOVERY_OK, agents, detail, errors=errors, change_kind="behavior"
        )


def build_codex_discovery(catalog: "AgentCatalog"):
    """Codex provider 集合（票 04 user + 票 06 project）。

    完整 provider 集合（jarvis + codex）由票 07 sync orchestrator 组装。
    """
    from app.agents.discovery import AgentDiscoveryService
    svc = AgentDiscoveryService(catalog)
    svc.register_provider(CodexUserDiscoveryProvider())
    svc.register_provider(CodexProjectDiscoveryProvider(catalog))
    return svc
