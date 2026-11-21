"""票 07 验收：Agent Sync 编排（full 集 sync / 幂等 / 缺源降级 / 失败隔离 / API / CLI）。

锚点模式沿用 tests/test_agent_api.py（_settings/_home 自建 tmp_workspace，禁 tmp_path）。
覆盖：
- user 基本 + 幂等；非法 scope；行为 hash 变化回 pending_review；
- 缺源降级 stale 不删行；项目撤销降级豁免 missing；override 永不被 sync 覆盖；
- provider 失败隔离（同 engine 放弃 missing 断言）；build_full_discovery 形状；
- 启动钩子 + POST /api/agents/sync + CLI sync + 开关关闭。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from app.agents.catalog import AgentCatalog
from app.agents.codex_discovery import CodexUserDiscoveryProvider
from app.agents.discovery import DISCOVERY_UNSUPPORTED
from app.agents.project_trust import ProjectTrustService
from app.agents.sync import AgentSyncOrchestrator, build_full_discovery
from app.main import create_app
from app.settings import Settings
from tests.fakes import FakeEngine

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace")

_VALID = 'name = "alpha"\ndescription = "A"\ndeveloper_instructions = "x"\n'
_VALID_A2 = 'name = "alpha"\ndescription = "A2"\ndeveloper_instructions = "x2"\n'  # 全文 sha256 变化


def _settings(name: str, **kw) -> Settings:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "data"))
    os.makedirs(os.path.join(d, "workspace"))
    return Settings(
        ld_data_dir=os.path.join(d, "data"),
        ld_workspace_dir=os.path.join(d, "workspace"),
        ld_api_token="test-token",
        _env_file=None,
        **kw,
    )


def _home(name: str, agents: dict[str, str] | None) -> str:
    """$CODEX_HOME 隔离目录；agents 为 {文件名: 内容}，None/空 = 空 agents 目录。"""
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    agents_dir = os.path.join(d, "agents")
    os.makedirs(agents_dir)
    for fn, content in (agents or {}).items():
        with open(os.path.join(agents_dir, fn), "w", encoding="utf-8", newline="") as f:
            f.write(content)
    return d


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _write_alpha(home: str, content: str) -> None:
    with open(os.path.join(home, "agents", "alpha.toml"), "w", encoding="utf-8", newline="") as f:
        f.write(content)


# ---------- orchestrator 直测 ----------

async def test_sync_user_basic_and_idempotent():
    settings = _settings("sync_basic")
    home = _home("sync_basic_home", {"alpha.toml": _VALID})
    cat = AgentCatalog(settings.tasks_db_path)
    await cat.open()
    try:
        r1 = await AgentSyncOrchestrator(cat, codex_home=home).run_sync("user")
        assert r1["status"] == "ok"
        assert r1["discovered"] == 1
        assert r1["changed"] == 1
        assert r1["missing"] == 0
        assert r1["pending_review"] == 1
        assert r1["sync_id"].startswith("S")
        assert r1["errors"] == []
        # jarvis（无公开清单 API）+ codex project reader（user scope 不适用）→ 2 条 unsupported
        assert len(r1["unsupported"]) == 2

        await cat.transition_agent("codex:alpha", "available")
        assert len(await cat.sync_runs()) == 1

        r2 = await AgentSyncOrchestrator(cat, codex_home=home).run_sync("user")
        assert r2["status"] == "ok" and r2["discovered"] == 1
        assert r2["changed"] == 0 and r2["missing"] == 0 and r2["pending_review"] == 0
        a = await cat.get_agent("codex:alpha")
        assert a["discovery_status"] == "available"
        assert len(await cat.sync_runs()) == 2
    finally:
        await cat.close()


async def test_sync_invalid_scope_raises():
    settings = _settings("sync_bad")
    cat = AgentCatalog(settings.tasks_db_path)
    await cat.open()
    try:
        with pytest.raises(ValueError):
            await AgentSyncOrchestrator(cat).run_sync("nope")
    finally:
        await cat.close()


async def test_sync_behavior_hash_change_back_to_pending_review():
    settings = _settings("sync_behavior")
    home = _home("sync_behavior_home", {"alpha.toml": _VALID})
    cat = AgentCatalog(settings.tasks_db_path)
    await cat.open()
    try:
        await AgentSyncOrchestrator(cat, codex_home=home).run_sync("user")
        await cat.transition_agent("codex:alpha", "available")
        _write_alpha(home, _VALID_A2)
        r2 = await AgentSyncOrchestrator(cat, codex_home=home).run_sync("user")
        assert r2["changed"] >= 1
        a = await cat.get_agent("codex:alpha")
        assert a["discovery_status"] == "pending_review"
        assert a["description"] == "A2"
    finally:
        await cat.close()


async def test_sync_missing_source_downgrades_not_deletes():
    settings = _settings("sync_missing")
    home = _home("sync_missing_home", {"alpha.toml": _VALID})
    cat = AgentCatalog(settings.tasks_db_path)
    await cat.open()
    try:
        await AgentSyncOrchestrator(cat, codex_home=home).run_sync("user")
        await cat.transition_agent("codex:alpha", "available")
        os.remove(os.path.join(home, "agents", "alpha.toml"))
        r2 = await AgentSyncOrchestrator(cat, codex_home=home).run_sync("user")
        assert r2["missing"] == 1
        assert r2["status"] == "ok"
        a = await cat.get_agent("codex:alpha")
        assert a is not None
        assert a["discovery_status"] == "stale"
    finally:
        await cat.close()


async def test_project_revoke_downgrade_never_escalates():
    settings = _settings("sync_revoke")
    root = os.path.join(BASE, "sync_revoke_root")
    shutil.rmtree(root, ignore_errors=True)
    agents_dir = os.path.join(root, ".codex", "agents")
    os.makedirs(agents_dir)
    with open(os.path.join(agents_dir, "alpha.toml"), "w", encoding="utf-8", newline="") as f:
        f.write(_VALID)
    cat = AgentCatalog(settings.tasks_db_path)
    await cat.open()
    try:
        svc = ProjectTrustService(cat)
        proj = await svc.register_project("p1", root)
        pid = proj["project_id"]
        await svc.trust_project(pid)

        r1 = await AgentSyncOrchestrator(cat).run_sync("project")
        assert r1["status"] == "ok" and r1["discovered"] == 1
        aid = f"codex:project:{pid}:alpha"
        a1 = await cat.get_agent(aid)
        assert a1["source_scope"] == "project"
        await cat.transition_agent(aid, "available")

        await svc.revoke_project(pid)
        r2 = await AgentSyncOrchestrator(cat).run_sync("project")
        assert r2["missing"] == 0  # 撤销降级的行豁免 missing 断言
        a2 = await cat.get_agent(aid)
        assert a2["discovery_status"] == "stale"  # 保持 stale，永不升级 unavailable
    finally:
        await cat.close()


async def test_sync_never_overrides_local_overrides():
    settings = _settings("sync_override")
    home = _home("sync_override_home", {"alpha.toml": _VALID})
    cat = AgentCatalog(settings.tasks_db_path)
    await cat.open()
    try:
        await AgentSyncOrchestrator(cat, codex_home=home).run_sync("user")
        await cat.set_override("codex:alpha", alias="myalias", routing_priority=7, auto_route_allowed=True)
        await cat.transition_agent("codex:alpha", "available")
        _write_alpha(home, _VALID_A2)
        await AgentSyncOrchestrator(cat, codex_home=home).run_sync("user")
        a = await cat.get_agent("codex:alpha")
        assert a["description"] == "A2"  # 快照更新
        ov = a["override"]
        assert set(ov) == {"enabled", "alias", "extra_capabilities", "routing_priority", "auto_route_allowed"}
        assert ov["alias"] == "myalias"
        assert ov["routing_priority"] == 7
        assert ov["auto_route_allowed"] is True
        assert a["discovery_status"] == "pending_review"  # 行为变化拉回重新审批
    finally:
        await cat.close()


async def test_sync_provider_failure_isolated():
    settings = _settings("sync_fail")
    home = _home("sync_fail_home", {"alpha.toml": _VALID})

    class _BoomProvider:
        engine = "codex"

        async def discover(self, scope: str):
            raise RuntimeError("boom")

    cat = AgentCatalog(settings.tasks_db_path)
    await cat.open()
    try:
        orch = AgentSyncOrchestrator(cat, providers=[_BoomProvider(), CodexUserDiscoveryProvider(home)])
        r = await orch.run_sync("user")
        assert r["status"] == "error"
        assert any("RuntimeError" in e and "boom" in e for e in r["errors"])
        assert r["discovered"] == 1  # ok 的 provider 仍照常 upsert
        assert r["missing"] == 0  # codex engine 整体放弃 missing 断言
        runs = await cat.sync_runs()
        assert runs and runs[0]["status"] == "error"
    finally:
        await cat.close()


async def test_build_full_discovery_shape():
    settings = _settings("sync_fullset")
    home = _home("sync_fullset_home", {"alpha.toml": _VALID})
    cat = AgentCatalog(settings.tasks_db_path)
    await cat.open()
    try:
        svc = build_full_discovery(cat, codex_home=home)
        # engine 名升序 + engine 内注册顺序：codex(user, project) 先于 jarvis
        assert [p.engine for p in svc.providers()] == ["codex", "codex", "jarvis"]

        rep = await svc.discover("user")
        assert rep["upserted"] == 1
        codex_reports = [e for e in rep["engines"] if e["engine"] == "codex"]
        assert sum(1 for e in codex_reports if e["status"] == "ok") == 1
        assert sum(1 for e in codex_reports if e["status"] == DISCOVERY_UNSUPPORTED) == 1
        jarvis_reports = [e for e in rep["engines"] if e["engine"] == "jarvis"]
        assert len(jarvis_reports) == 1
        assert jarvis_reports[0]["status"] == DISCOVERY_UNSUPPORTED

        rep2 = await svc.discover("project")
        assert rep2["upserted"] == 0  # 无受信任项目 → project reader 空结果（非错误）
    finally:
        await cat.close()


# ---------- 启动钩子 + API + CLI + 开关 ----------

def test_agent_sync_api_and_startup_hook(monkeypatch):
    settings = _settings("sync_api")
    monkeypatch.setenv("CODEX_HOME", _home("sync_api_home", None))
    with TestClient(create_app(settings, {"fake": FakeEngine()})) as c:
        # 启动钩子：user + project 各一轮（默认开）
        runs = c.portal.call(c.app.state.catalog.sync_runs)
        assert len(runs) == 2
        assert {r["scope"] for r in runs} == {"user", "project"}

        r = c.post("/api/agents/sync", json={}, headers=_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("sync_id", "scope", "status", "discovered", "changed",
                  "missing", "pending_review", "errors", "unsupported"):
            assert k in body
        assert body["scope"] == "user" and body["status"] == "ok"

        bad = c.post("/api/agents/sync", json={"scope": "nope"}, headers=_headers())
        assert bad.status_code == 400
        assert c.post("/api/agents/sync", json={}).status_code == 401

        assert len(c.portal.call(c.app.state.catalog.sync_runs)) == 3


def test_agent_sync_cli_writes_one_run(monkeypatch):
    import app.cli as cli_mod

    settings = _settings("sync_cli")
    monkeypatch.setenv("CODEX_HOME", _home("sync_cli_home", None))
    monkeypatch.setattr(cli_mod, "get_settings", lambda: settings)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_mod.main(["agents", "sync", "--scope", "user"])
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert set(out) == {"user"}
    assert out["user"]["status"] == "ok"
    assert out["user"]["sync_id"].startswith("S")

    async def _rows():
        cat = AgentCatalog(settings.tasks_db_path)
        await cat.open()
        try:
            return await cat.sync_runs()
        finally:
            await cat.close()

    assert len(asyncio.run(_rows())) == 1


def test_startup_sync_disabled_writes_no_runs(monkeypatch):
    settings = _settings("sync_disabled", ld_agent_sync_on_startup=False)
    monkeypatch.setenv("CODEX_HOME", _home("sync_disabled_home", None))
    with TestClient(create_app(settings, {"fake": FakeEngine()})) as c:
        assert c.portal.call(c.app.state.catalog.sync_runs) == []