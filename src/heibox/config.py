"""配置与数据目录。

默认数据目录：%LOCALAPPDATA%\\heibox-history（可用 --data-dir 或 HEIBOX_DATA_DIR 覆盖）。
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError


@dataclass
class Config:
    userid: int
    request_interval: float = 3.0
    # 可选：浏览器里通过人机验证后的 Cookie，仅在接口要求验证时需要
    cookie: str = ""


def data_dir() -> Path:
    override = os.environ.get("HEIBOX_DATA_DIR")
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return base / "heibox-history"


def config_path(directory: Path) -> Path:
    return directory / "config.toml"


def db_path(directory: Path) -> Path:
    return directory / "heibox.db"


def normalize_cookie(text: str) -> str:
    """容忍从 DevTools 复制来的几种形式：裸 Cookie 头、带 Cookie: 前缀、带引号。"""
    value = text.strip().strip('"').strip("'")
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    return " ".join(value.split())


def load_config(directory: Path) -> Config | None:
    path = config_path(directory)
    if not path.exists():
        return None

    # 记事本 / PowerShell 5 可能写入 UTF-8 BOM，先剥掉再解析
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]

    try:
        raw = tomllib.loads(data.decode("utf-8"))
        return Config(
            userid=int(raw["userid"]),
            request_interval=float(raw.get("request_interval", 3.0)),
            cookie=str(raw.get("cookie", "")),
        )
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"配置文件无法解析：{path}（{exc}）") from exc


def save_config(directory: Path, config: Config) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        f"userid = {config.userid}",
        f"request_interval = {config.request_interval}",
    ]
    if config.cookie:
        lines.append(f"cookie = {json.dumps(config.cookie)}")
    config_path(directory).write_text("\n".join(lines) + "\n", encoding="utf-8")
