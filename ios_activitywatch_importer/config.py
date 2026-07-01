from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


APPLE_SCREEN_TIME_EPOCH = 978307200
KNOWLEDGE_DB_SHA1 = "4f108665e5a33b7330c9b0ed930162ef7b756290"
DEFAULT_CONFIG_EXAMPLE = {
    "backup_base_dir": "C:\\Users\\<USERNAME>\\AppData\\Roaming\\Apple Computer\\MobileSync\\Backup\\",
    "backup_password": "",
    "aw_api_url": "http://localhost:5600/api/0",
    "bucket_id": "aw-watcher-window_FlorianIPad",
    "window_bucket_id": "aw-watcher-window_FlorianIPad",
    "afk_bucket_id": "aw-watcher-afk_FlorianIPad",
    "hostname": "FlorianIPad",
    "debug_mode": False,
}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppConfig:
    backup_base_dir: Path
    backup_password: str | None
    aw_api_url: str
    bucket_id: str
    hostname: str
    debug_mode: bool
    window_bucket_id: str = "aw-watcher-window"
    afk_bucket_id: str = "aw-watcher-afk"


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def config_path(base_dir: Path | None = None) -> Path:
    return (base_dir or project_root()) / "config.json"


def config_example_path(base_dir: Path | None = None) -> Path:
    return (base_dir or project_root()) / "config.example.json"


def ensure_config_example(base_dir: Path | None = None) -> Path:
    path = config_example_path(base_dir)
    if not path.exists():
        path.write_text(
            json.dumps(DEFAULT_CONFIG_EXAMPLE, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return path


def _default_bucket_name(prefix: str, hostname: str) -> str:
    hostname = hostname.strip()
    if not hostname:
        return prefix
    return f"{prefix}_{hostname}"


def load_config(base_dir: Path | None = None) -> AppConfig:
    root = base_dir or project_root()
    cfg_path = config_path(root)
    example_path = ensure_config_example(root)

    if not cfg_path.exists():
        raise ConfigError(
            f"config.json is missing. Fill out {example_path.name} and save it as config.json."
        )

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json is not valid JSON: {exc}") from exc

    for key in ("backup_base_dir", "aw_api_url", "hostname"):
        if key not in raw or not str(raw[key]).strip():
            raise ConfigError(f"config.json does not contain a valid value for '{key}'.")

    debug_mode_raw = raw.get("debug_mode", False)
    if not isinstance(debug_mode_raw, bool):
        raise ConfigError("config.json does not contain a valid value for 'debug_mode'.")

    backup_base_dir = Path(
        os.path.expandvars(os.path.expanduser(str(raw["backup_base_dir"])))
    )
    backup_password = str(raw.get("backup_password", "")).strip() or None
    hostname = str(raw["hostname"]).strip()
    bucket_id = str(
        raw.get(
            "bucket_id",
            raw.get("window_bucket_id", _default_bucket_name("aw-watcher-window", hostname)),
        )
    ).strip()
    if not bucket_id:
        bucket_id = _default_bucket_name("aw-watcher-window", hostname)
    window_bucket_id = str(
        raw.get("window_bucket_id", bucket_id or _default_bucket_name("aw-watcher-window", hostname))
    ).strip() or bucket_id or _default_bucket_name("aw-watcher-window", hostname)
    afk_bucket_id = str(
        raw.get("afk_bucket_id", _default_bucket_name("aw-watcher-afk", hostname))
    ).strip() or _default_bucket_name("aw-watcher-afk", hostname)
    return AppConfig(
        backup_base_dir=backup_base_dir,
        backup_password=backup_password,
        aw_api_url=str(raw["aw_api_url"]).rstrip("/"),
        bucket_id=bucket_id,
        window_bucket_id=window_bucket_id,
        afk_bucket_id=afk_bucket_id,
        hostname=hostname,
        debug_mode=debug_mode_raw,
    )
