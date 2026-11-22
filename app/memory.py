"""跨对话共享持久记忆（JSON 文件，同步）。

机制参考 Codex 式共享记忆文件：秘书可经 save_memory 工具写入长期结论，
系统提示词在后续每个对话中注入已有条目——多对话共享，跨重启持久。

纪律：原子写（同目录临时文件 + os.replace）；坏 JSON 视为空；FIFO 上限。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MAX_ENTRIES = 50


class MemoryStore:
    def __init__(self, path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> list[dict[str, str]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return []
        items = data["items"]
        result: list[dict[str, str]] = []
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("content"), str):
                result.append({"content": it["content"], "created_at": str(it.get("created_at", ""))})
        return result

    def _write(self, items: list[dict[str, str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def list(self) -> list[dict[str, str]]:
        return self._read()

    def add(self, content: str) -> dict[str, bool]:
        content = (content or "").strip()
        if not content:
            return {"ok": False, "deduped": False}
        items = self._read()
        if any(it["content"] == content for it in items):
            return {"ok": True, "deduped": True}
        items.append({"content": content, "created_at": ""})
        del items[:-MAX_ENTRIES]
        self._write(items)
        return {"ok": True, "deduped": False}

    def remove(self, index: int) -> bool:
        items = self._read()
        if not isinstance(index, int) or index < 0 or index >= len(items):
            return False
        items.pop(index)
        self._write(items)
        return True
