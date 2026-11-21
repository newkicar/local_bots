"""票 06 验收：Codex 项目级发现（<trusted-root>/.codex/agents/*.toml）+ 信任门控 + 撤销降级。

纪律：
- 信任门控：只读 trust_state=trusted 的项目（pending/revoked 不激活、不读盘）；
- agent_id = codex:project:<project_id>:<name>，source_scope=project，一律先 pending_review；
- 撤销项目信任 → 该项目 project agent 降 stale（available/pending_review/disabled），
  discovered → unavailable；行永不删；
- 复用票 04 reader（parse_agent_toml），坏 TOML 记 error 不阻塞其余。
本机 pytest WinError-5 中毒：自建 tests/tmp_workspace/。
"""
from __future__ import annotations

import hashlib
import os
import shutil

from app.agents.catalog import AgentCatalog
from app.agents.codex_discovery import CodexProjectDiscoveryProvider, CodexUserDiscoveryProvider
from app.agents.discovery import (
    DISCOVERY_OK,
    DISCOVERY_UNSUPPORTED,
    AgentDiscoveryService,
)
from app.agents.project_trust import ProjectTrustService

BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace"
)

_VALID = (
    "name = " + chr(34) + "alpha" + chr(34) + chr(10)
    + "description = " + chr(34) + "Project helper" + chr(34) + chr(10)
    + "developer_instructions = " + chr(34) + "Stay in scope." + chr(34) + chr(10)
)


def _db(name: str) -> str:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return os.path.join(d, "tasks.db")


def _proj_dir(name: str) -> str:
    d = os.path.join(BASE, f"proj_{name}")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


async def _cat(db: str) -> AgentCatalog:
    c = AgentCatalog(db)
    await c.open()
    return c


def _agent_dir(root: str) -> str:
    d = os.path.join(root, ".codex", "agents")
    os.makedirs(d, exist_ok=True)
    return d


def _write(adir: str, fname: str, content: str) -> None:
    with open(os.path.join(adir, fname), "w", encoding="utf-8", newline="") as f:
        f.write(content)


async def test_untrusted_project_not_discovered():
    db = _db("cp_untrusted")
    cat = await _cat(db)
    svc = ProjectTrustService(cat)
    root = _proj_dir("untrusted")
    _write(_agent_dir(root), "alpha.toml", _VALID)
    p = await svc.register_project("untrusted", root)  # pending → 不激活
    prov = CodexProjectDiscoveryProvider(cat)
    res = await prov.discover("project")
    assert res.status == DISCOVERY_OK and res.agents == []
    assert (await cat.list_agents()) and all(a["source_scope"] != "project" for a in await cat.list_agents())
    await svc.trust_project(p["project_id"])  # 信任后才可读
    res = await prov.discover("project")
    assert len(res.agents) == 1 and res.agents[0]["agent_id"] == f"codex:project:{p["project_id"]}:alpha"
    await cat.close()


async def test_project_agent_descriptor_shape_and_pending_review():
    db = _db("cp_shape")
    cat = await _cat(db)
    svc = ProjectTrustService(cat)
    root = _proj_dir("shape")
    _write(_agent_dir(root), "alpha.toml", _VALID)
    p = await svc.register_project("shapeproj", root)
    await svc.trust_project(p["project_id"])
    svc2 = AgentDiscoveryService(cat)
    svc2.register_provider(CodexProjectDiscoveryProvider(cat))
    rep = await svc2.discover("project")
    assert rep["upserted"] == 1 and rep["errors"] == []
    a = await cat.get_agent(f"codex:project:{p["project_id"]}:alpha")
    assert a["discovery_status"] == "pending_review"  # 项目级一律先审
    assert a["source_scope"] == "project"
    assert a["source_id"] == f"{p["project_id"]}:alpha"
    assert a["invocation_mode"] == "delegated_only"
    assert a["name"] == "shapeproj/alpha"
    assert a["metadata"]["project_id"] == p["project_id"]
    assert a["profile_hash"] == hashlib.sha256(_VALID.encode("utf-8")).hexdigest()
    await cat.close()


async def test_revoked_project_agents_demoted_to_stale_rows_kept():
    db = _db("cp_revoke")
    cat = await _cat(db)
    svc = ProjectTrustService(cat)
    root = _proj_dir("revoke")
    _write(_agent_dir(root), "alpha.toml", _VALID)
    p = await svc.register_project("revproj", root)
    pid = p["project_id"]
    await svc.trust_project(pid)
    prov = CodexProjectDiscoveryProvider(cat)
    res = await prov.discover("project")
    # upsert 是 AgentDiscoveryService 的职责：provider 直调结果需先写入 catalog
    for d in res.agents:
        await cat.upsert_agent(d, change_kind=res.change_kind)
    aid = res.agents[0]["agent_id"]
    await cat.transition_agent(aid, "available")  # 审批通过
    out = await svc.revoke_project(pid)
    assert out["trust_state"] == "revoked"
    assert [a["agent_id"] for a in out["demoted_agents"]] == [aid]
    row = await cat.get_agent(aid)
    assert row is not None  # 行保留
    assert row["discovery_status"] == "stale"
    # 撤销后不再发现
    assert (await prov.discover("project")).agents == []
    await cat.close()


async def test_bad_toml_recorded_valid_still_found():
    db = _db("cp_mixed")
    cat = await _cat(db)
    svc = ProjectTrustService(cat)
    root = _proj_dir("mixed")
    adir = _agent_dir(root)
    _write(adir, "broken.toml", "name = [unclosed")
    _write(adir, "ok.toml", _VALID.replace("alpha", "beta"))
    p = await svc.register_project("mixed", root)
    await svc.trust_project(p["project_id"])
    res = await CodexProjectDiscoveryProvider(cat).discover("project")
    assert [a["engine_profile_id"] for a in res.agents] == ["beta"]
    assert len(res.errors) == 1
    assert res.errors[0]["agent"] == "broken.toml"
    assert res.errors[0]["project"] == p["project_id"]
    assert "TOMLDecodeError" in res.errors[0]["detail"]
    await cat.close()


async def test_trusted_project_missing_agents_dir_is_ok_empty():
    db = _db("cp_empty")
    cat = await _cat(db)
    svc = ProjectTrustService(cat)
    root = _proj_dir("empty")  # 不建 .codex/agents
    p = await svc.register_project("empty", root)
    await svc.trust_project(p["project_id"])
    res = await CodexProjectDiscoveryProvider(cat).discover("project")
    assert res.status == DISCOVERY_OK and res.agents == [] and res.errors == []
    await cat.close()


async def test_project_provider_rejects_non_project_scope():
    db = _db("cp_scope")
    cat = await _cat(db)
    res = await CodexProjectDiscoveryProvider(cat).discover("user")
    assert res.status == DISCOVERY_UNSUPPORTED
    await cat.close()


async def test_user_provider_still_unsupported_on_project_scope():
    # 票 04 契约不回退：user provider 的 project scope 报 unsupported
    res = await CodexUserDiscoveryProvider(codex_home=_proj_dir("user_scope")).discover("project")
    assert res.status == DISCOVERY_UNSUPPORTED
