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
    "bucket_id": "aw-watcher-ios",
    "hostname": "my-iphone",
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


def load_config(base_dir: Path | None = None) -> AppConfig:
    root = base_dir or project_root()
    cfg_path = config_path(root)
    example_path = ensure_config_example(root)

    if not cfg_path.exists():
        raise ConfigError(
            f"config.json fehlt. Bitte {example_path.name} ausfüllen und als config.json speichern."
        )

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json ist kein gültiges JSON: {exc}") from exc

    for key in ("backup_base_dir", "aw_api_url", "bucket_id", "hostname"):
        if key not in raw or not str(raw[key]).strip():
            raise ConfigError(f"config.json enthält keinen gültigen Wert für '{key}'.")

    backup_base_dir = Path(
        os.path.expandvars(os.path.expanduser(str(raw["backup_base_dir"])))
    )
    backup_password = str(raw.get("backup_password", "")).strip() or None
    return AppConfig(
        backup_base_dir=backup_base_dir,
        backup_password=backup_password,
        aw_api_url=str(raw["aw_api_url"]).rstrip("/"),
        bucket_id=str(raw["bucket_id"]).strip(),
        hostname=str(raw["hostname"]).strip(),
    )
