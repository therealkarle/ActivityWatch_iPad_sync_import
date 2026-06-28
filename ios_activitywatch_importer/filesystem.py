from __future__ import annotations

import glob
import plistlib
import tempfile
from pathlib import Path

from iphone_backup_decrypt import EncryptedBackup

from .config import KNOWLEDGE_DB_SHA1


class BackupNotFoundError(RuntimeError):
    pass


def _backup_directories(backup_base_dir: Path) -> list[Path]:
    backup_dirs: list[Path] = []

    if (backup_base_dir / "Manifest.db").is_file() and (backup_base_dir / "Manifest.plist").is_file():
        backup_dirs.append(backup_base_dir)

    for candidate in backup_base_dir.iterdir():
        if candidate.is_dir() and (candidate / "Manifest.db").is_file() and (candidate / "Manifest.plist").is_file():
            backup_dirs.append(candidate)

    return sorted(set(backup_dirs), key=lambda path: path.stat().st_mtime, reverse=True)


def _is_encrypted_backup(backup_dir: Path) -> bool:
    manifest_path = backup_dir / "Manifest.plist"
    with manifest_path.open("rb") as manifest_file:
        manifest = plistlib.load(manifest_file)
    return bool(manifest.get("IsEncrypted"))


def _direct_knowledge_db_matches(backup_base_dir: Path) -> list[Path]:
    direct_pattern = str(backup_base_dir / "*" / KNOWLEDGE_DB_SHA1)
    recursive_pattern = str(backup_base_dir / "**" / KNOWLEDGE_DB_SHA1)

    matches = glob.glob(direct_pattern)
    if not matches:
        matches = glob.glob(recursive_pattern, recursive=True)

    return [Path(match) for match in matches if Path(match).is_file()]


def _decrypt_knowledge_db(backup_dir: Path, backup_password: str) -> Path:
    backup = EncryptedBackup(backup_directory=str(backup_dir), passphrase=backup_password)
    try:
        with backup.manifest_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT relativePath, domain
                FROM Files
                WHERE fileID = ?
                ORDER BY flags DESC, domain, relativePath
                LIMIT 1
                """,
                (KNOWLEDGE_DB_SHA1,),
            )
            row = cursor.fetchone()

        if not row:
            raise BackupNotFoundError(
                f"knowledgeC.db nicht im entschlüsselten Backup-Manifest gefunden: {backup_dir}"
            )

        relative_path = str(row[0])
        domain = str(row[1]) if row[1] is not None else None
        file_bytes = backup.extract_file_as_bytes(relative_path=relative_path, domain_like=domain)

        temp_file = tempfile.NamedTemporaryFile(prefix="knowledgeC-", suffix=".db", delete=False)
        try:
            temp_file.write(file_bytes)
        finally:
            temp_file.close()

        return Path(temp_file.name)
    except ValueError as exc:
        raise BackupNotFoundError(
            "Verschlüsseltes Backup konnte nicht entschlüsselt werden. "
            "Prüfe das backup_password in config.json."
        ) from exc
    finally:
        cleanup = getattr(backup, "_cleanup", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception:
                pass


def find_knowledge_db(backup_base_dir: Path, backup_password: str | None = None) -> Path:
    if not backup_base_dir.exists():
        raise BackupNotFoundError(f"backup_base_dir existiert nicht: {backup_base_dir}")

    files = sorted(_direct_knowledge_db_matches(backup_base_dir), key=lambda path: path.stat().st_mtime, reverse=True)
    if files:
        return files[0]

    backup_dirs = _backup_directories(backup_base_dir)
    if not backup_dirs:
        raise BackupNotFoundError(
            f"knowledgeC.db nicht gefunden unter: {backup_base_dir}"
        )

    encrypted_backups = [backup_dir for backup_dir in backup_dirs if _is_encrypted_backup(backup_dir)]
    if encrypted_backups:
        if not backup_password:
            raise BackupNotFoundError(
                "Das gefundene iTunes-Backup ist verschlüsselt. "
                "Trage den Backup-Schlüssel als backup_password in config.json ein."
            )
        return _decrypt_knowledge_db(encrypted_backups[0], backup_password)

    raise BackupNotFoundError(
        f"knowledgeC.db nicht gefunden unter: {backup_base_dir}"
    )
