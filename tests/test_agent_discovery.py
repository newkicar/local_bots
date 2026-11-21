"""票 03 验收：Discovery 框架 + Jarvis provider（直连 catalog，含 FakeEngine 兼容）。

纪律：EngineAdapter.discover_agents 基类安全默认 []（FakeEngine 不 override）；
Jarvis provider 不 import 内部模块、不发明参数——如实上报 discovery_unsupported，
catalog 保留内建 jarvis:default。本机 pytest WinError-5：自建 tests/tmp_workspace/。
"""
from __future__ import annotations

import os
import shutil

from app.adapters.base import EngineAdapter
from app.adapters.jarvis import JarvisAdapter
from app.agents.catalog import AgentCatalog
from app.agents.discovery import (
    JARVIS_MINIMAL_API_ASK,
    AgentDiscoveryService,
    DiscoveryResult,
    build_default_discovery,
)
from tests.fakes import FakeEngine

BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace"
)


def _db(name: str) -> str:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return os.path.join(d, "tasks.db")


def _desc(agent_id: str, *, scope: str = "user", hash_: str = "h1") -> dict:
    return {
        "agent_id": agent_id, "engine": "codex",
        "engine_profile_id": agent_id.split(":")[-1],
        "name": agent_id, "description": None, "source_scope": scope,
        "source_id": agent_id.split(":")[-1], "source_path": None, "capabilities": {},
        "invocation_mode": "delegated_only", "profile_version": "v1",
        "profile_hash": hash_, "metadata": {},
    }


class _FakeProvider:
    def __init__(self, engine: str, agents: list[dict] | None = None,
                 *, fail: bool = False) -> None:
        self.engine = engine
        self._agents = list(agents or [])
        self._fail = fail
        self.calls: list[str] = []

    async def discover(self, scope: str) -> DiscoveryResult:
        self.calls.append(scope)
        if self._fail:
            raise RuntimeError(f"{self.engine} boom")
        return DiscoveryResult(self.engine, "ok", self._agents)


async def test_base_default_discover_agents_and_fake_engine_untouched():
    # 基类安全默认 []，FakeEngine 不 override（tests/fakes.py 未改）
    assert FakeEngine.discover_agents is EngineAdapter.discover_agents
    assert await FakeEngine().discover_agents("user") == []
    assert await FakeEngine().discover_agents("project") == []
    # JarvisAdapter 亦不 override（无 profile API），构造不触盘
    j = JarvisAdapter(os.path.join(BASE, "jarvis_placeholder.db"))
    assert await j.discover_agents("user") == []


async def test_discovery_service_upserts_metadata_and_initial_status():
    db = _db("disc_upsert")
    cat = AgentCatalog(db)
    await cat.open()
    svc = AgentDiscoveryService(cat)
    prov = _FakeProvider("codex", [_desc("codex:alpha"), _desc("codex:beta")])
    svc.register_provider(prov)
    report = await svc.discover("user")
    assert report["upserted"] == 2 and report["errors"] == [] and report["unsupported"] == []
    assert prov.calls == ["user"]
    a = await cat.get_agent("codex:alpha")
    assert a["discovery_status"] == "pending_review"  # user scope 初态（auto 默认关）
    assert len(await cat.list_agents()) == 6  # 4 内建 + 2 新发现
    await cat.close()


async def test_discovery_rerun_same_hash_keeps_status():
    db = _db("disc_rerun")
    cat = AgentCatalog(db)
    await cat.open()
    svc = AgentDiscoveryService(cat)
    svc.register_provider(_FakeProvider("codex", [_desc("codex:gamma")]))
    await svc.discover("user")
    await cat.transition_agent("codex:gamma", "available")
    await svc.discover("user")  # 同 profile_hash、metadata-only → 不拉回 pending_review
    assert (await cat.get_agent("codex:gamma"))["discovery_status"] == "available"
    await cat.close()


async def test_jarvis_provider_reports_unsupported_and_builtins_preserved():
    db = _db("disc_jarvis")
    cat = AgentCatalog(db)
    await cat.open()
    svc = build_default_discovery(cat)
    report = await svc.discover("user")
    assert report["upserted"] == 0 and report["errors"] == []
    assert report["unsupported"] == [{"engine": "jarvis", "detail": JARVIS_MINIMAL_API_ASK}]
    b = await cat.get_agent("jarvis:default")
    assert b is not None and b["discovery_status"] == "available"
    await cat.close()


async def test_provider_error_isolated_from_other_providers():
    db = _db("disc_err")
    cat = AgentCatalog(db)
    await cat.open()
    svc = AgentDiscoveryService(cat)
    svc.register_provider(_FakeProvider("alpha", fail=True))
    svc.register_provider(_FakeProvider("codex", [_desc("codex:ok1")]))
    report = await svc.discover("user")
    assert report["upserted"] == 1
    assert {"engine": "alpha", "detail": "RuntimeError: alpha boom"} in report["errors"]
    statuses = {e["engine"]: e["status"] for e in report["engines"]}
    assert statuses == {"alpha": "error", "codex": "ok"}
    await cat.close()
