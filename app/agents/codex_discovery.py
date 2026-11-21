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
- 不发明任何 codex CLI 参数；project scope 属票 06。
"""
from __future__ import annotations

import hashlib
import os
import tomllib
from pathlib import Path
from typing import Any

from app.agents.discovery import DISCOVERY_OK, DISCOVERY_UNSUPPORTED, DiscoveryResult

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

    # ---------- 单文件读取 ----------
    def _read_one(self, f: Path, errors: list[dict[str, str]]) -> dict[str, Any] | None:
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
        name = str(data["name"]).strip()
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
            "description": str(data["description"]).strip(),
            "source_scope": "user",
            "source_id": name,
            "source_path": str(f),
            "capabilities": {},
            "invocation_mode": "delegated_only",
            "profile_version": None,
            # 行为 hash 覆盖全文（developer_instructions 是 prompt，属行为）
            "profile_hash": hashlib.sha256(raw).hexdigest(),
            "metadata": {
                "source_file": f.name,
                "has_developer_instructions": True,
            },
        }
