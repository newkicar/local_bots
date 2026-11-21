"""Local Dots 最小 CLI（python -m app.cli）：Agent 目录只读检视。

票 02 落地：agents list [--status X] / agents inspect <agent_id>；
输出 JSON（ensure_ascii=False, indent=2），供人工核对目录快照。
票 07 将扩展 agents sync（同步编排入口）。
纪律：CLI 只读 catalog，不改状态；配置走 .env/环境变量（get_settings）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.agents.catalog import AgentCatalog
from app.settings import get_settings


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


async def _run(action: str, ns: argparse.Namespace) -> int:
    settings = get_settings()
    catalog = AgentCatalog(settings.tasks_db_path)
    await catalog.open()
    try:
        if action == "list":
            _dump({"agents": await catalog.list_agents(ns.status)})
        elif action == "inspect":
            agent = await catalog.get_agent(ns.agent_id)
            if agent is None:
                print(f"agent not found: {ns.agent_id}", file=sys.stderr)
                return 1
            _dump(agent)
        return 0
    finally:
        await catalog.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-dots", description="Local Dots 最小 CLI")
    sub = parser.add_subparsers(dest="group")
    agents = sub.add_parser("agents", help="Agent 目录")
    asub = agents.add_subparsers(dest="action")
    p_list = asub.add_parser("list", help="列出目录中的 agent（快照 + override 合并）")
    p_list.add_argument("--status", default=None, help="按 discovery_status 过滤")
    p_inspect = asub.add_parser("inspect", help="检视单个 agent")
    p_inspect.add_argument("agent_id", help="agent_id，如 jarvis:default / codex:worker")
    ns = parser.parse_args(argv)
    if ns.group == "agents" and ns.action in ("list", "inspect"):
        return asyncio.run(_run(ns.action, ns))
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
