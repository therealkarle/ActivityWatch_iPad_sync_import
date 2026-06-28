from __future__ import annotations

import glob
from pathlib import Path

from .config import KNOWLEDGE_DB_SHA1


class BackupNotFoundError(RuntimeError):
    pass


def find_knowledge_db(backup_base_dir: Path) -> Path:
    if not backup_base_dir.exists():
        raise BackupNotFoundError(f"backup_base_dir existiert nicht: {backup_base_dir}")

    direct_pattern = str(backup_base_dir / "*" / KNOWLEDGE_DB_SHA1)
    recursive_pattern = str(backup_base_dir / "**" / KNOWLEDGE_DB_SHA1)

    matches = glob.glob(direct_pattern)
    if not matches:
        matches = glob.glob(recursive_pattern, recursive=True)

    files = [Path(match) for match in matches if Path(match).is_file()]
    if not files:
        raise BackupNotFoundError(
            f"knowledgeC.db nicht gefunden unter: {backup_base_dir}"
        )

    return max(files, key=lambda path: path.stat().st_mtime)
