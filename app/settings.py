"""运行配置（.env / 环境变量，LD_ 前缀）。

纪律：密钥与凭据只出现在 .env（不回环外、不入库）；
所有可调参数集中在此，不散落硬编码（ADR-0003 并发上限可配）。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 认证：单用户 Bearer Token
    ld_api_token: str = "local-dots-dev-token"

    # 并发与排队（ADR-0003）
    ld_max_concurrent_runs: int = 10

    # 审批超时（ADR-0005，分钟；计时自进入 waiting-approval 起算）
    ld_approval_timeout_min: float = 30.0

    # 代审模式：manual 手动（缺省）/ auto_safe 有限代审（只读命令自动放行）/ yolo 全放行
    # 运行期可经 /api/approval-mode 切换（本启动周期内生效，重启回落此配置）
    ld_approval_mode: Literal["manual", "auto_safe", "yolo"] = "manual"

    # 目录与数据
    ld_data_dir: Path = Path("data")
    ld_workspace_dir: Path = Path("workspace")

    # 秘书模型（独立配置，不依赖 Codex 模型；默认指 mimo2codex 网关）
    ld_secretary_model: str = "agnes-3.0-flash"
    ld_secretary_base_url: str = "http://127.0.0.1:8788/v1"
    ld_secretary_api_key: str = "mimo2codex"
    # 秘书只读文件发现授权目录（分号分隔；不扫全盘）
    ld_secretary_authorized_dirs: str = "D:\\"

    # Codex CLI（ADR-0002：子进程，继承 ~/.codex/config.toml，绝不改该文件）
    ld_codex_cli: str = "codex"

    @property
    def tasks_db_path(self) -> Path:
        return self.ld_data_dir / "tasks.db"

    @property
    def checkpoints_db_path(self) -> Path:
        return self.ld_data_dir / "checkpoints.db"

    @property
    def blacklist_path(self) -> Path:
        return self.ld_data_dir / "blacklist.json"

    @property
    def authorized_dirs(self) -> list[Path]:
        parts = [p.strip() for p in self.ld_secretary_authorized_dirs.split(";") if p.strip()]
        return [Path(p) for p in parts]


@lru_cache
def get_settings() -> Settings:
    return Settings()
