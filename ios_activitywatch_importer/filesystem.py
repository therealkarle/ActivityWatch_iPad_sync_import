from __future__ import annotations

import glob
import os
import plistlib
import tempfile
from pathlib import Path

from iphone_backup_decrypt import EncryptedBackup
from iphone_backup_decrypt import google_iphone_dataprotection, utils

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


def _screen_time_related_files(backup: EncryptedBackup) -> list[tuple[str, str | None]]:
    with backup.manifest_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT relativePath, domain
            FROM Files
            WHERE lower(relativePath) LIKE lower(?)
               OR lower(relativePath) LIKE lower(?)
               OR lower(relativePath) LIKE lower(?)
            ORDER BY relativePath
            """,
            ("%knowledgec.db%", "%screentime%", "%interactionc.db%"),
        )
        rows = cursor.fetchall()
    return [(str(row[0]), str(row[1]) if row[1] is not None else None) for row in rows]


def _format_related_files(rows: list[tuple[str, str | None]]) -> str:
    return ", ".join(
        f"{relative_path}" + (f" ({domain})" if domain else "")
        for relative_path, domain in rows
    )


def _decrypt_file_without_size_check(backup: EncryptedBackup, relative_path: str, domain: str | None) -> bytes:
    file_id, file_bplist = backup._file_metadata_from_manifest(relative_path, domain)
    backup._read_and_unlock_keybag()
    file_plist = utils.FilePlist(file_bplist)
    if file_plist.encryption_key is None:
        raise ValueError("Path is not an encrypted file.")

    inner_key = backup._keybag.unwrapKeyForClass(file_plist.protection_class, file_plist.encryption_key)
    filename_in_backup = os.path.join(backup._backup_directory, file_id[:2], file_id)
    with open(filename_in_backup, "rb") as encrypted_file_filehandle:
        encrypted_data = encrypted_file_filehandle.read()

    decrypted_data = google_iphone_dataprotection.AESdecryptCBC(encrypted_data, inner_key)
    return google_iphone_dataprotection.removePadding(decrypted_data)


def _decrypt_knowledge_db(backup_dir: Path, backup_password: str) -> Path:
    backup = EncryptedBackup(backup_directory=str(backup_dir), passphrase=backup_password)
    try:
        with backup.manifest_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT fileID, relativePath, domain
                FROM Files
                WHERE lower(relativePath) LIKE lower(?)
                   OR lower(relativePath) LIKE lower(?)
                ORDER BY
                    CASE
                        WHEN lower(relativePath) LIKE lower(?) THEN 0
                        ELSE 1
                    END,
                    flags DESC,
                    domain,
                    relativePath
                LIMIT 1
                """,
                ("%knowledgeC.db", "%interactionC.db", "%knowledgeC.db"),
            )
            row = cursor.fetchone()
            related_rows = _screen_time_related_files(backup)

        if not row:
            related_suffix = ""
            if related_rows:
                related_suffix = (
                    f" Gefundene Screen-Time-Dateien: {_format_related_files(related_rows)}."
                )
            raise BackupNotFoundError(
                f"knowledgeC.db nicht im entschlüsselten Backup-Manifest gefunden: {backup_dir}."
                f"{related_suffix}"
            )

        _file_id = str(row[0])
        relative_path = str(row[1])
        domain = str(row[2]) if row[2] is not None else None
        try:
            file_bytes = backup.extract_file_as_bytes(relative_path=relative_path, domain_like=domain)
        except AssertionError:
            file_bytes = _decrypt_file_without_size_check(backup, relative_path, domain)

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
