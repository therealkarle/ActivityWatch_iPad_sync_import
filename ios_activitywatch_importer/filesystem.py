from __future__ import annotations

import glob
import csv
import os
import plistlib
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    from iphone_backup_decrypt import EncryptedBackup
    from iphone_backup_decrypt import google_iphone_dataprotection, utils
except ModuleNotFoundError as exc:
    EncryptedBackup = object  # type: ignore[assignment]
    google_iphone_dataprotection = None  # type: ignore[assignment]
    utils = None  # type: ignore[assignment]
    _IPHONE_BACKUP_DECRYPT_IMPORT_ERROR = exc
else:
    _IPHONE_BACKUP_DECRYPT_IMPORT_ERROR = None

from .config import KNOWLEDGE_DB_SHA1


class BackupNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class UsageDataFiles:
    primary_db: Path
    knowledge_db: Path | None = None
    interaction_db: Path | None = None
    safari_history_db: Path | None = None
    app_activity_manifest_csv: Path | None = None
    screen_time_agent_plist: Path | None = None
    screen_time_settings_plist: Path | None = None


def _require_iphone_backup_decrypt() -> None:
    if _IPHONE_BACKUP_DECRYPT_IMPORT_ERROR is not None:
        raise BackupNotFoundError(
            "Missing dependency: iphone-backup-decrypt. "
            "Install project dependencies or run `pip install iphone-backup-decrypt`."
        ) from _IPHONE_BACKUP_DECRYPT_IMPORT_ERROR


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
    _require_iphone_backup_decrypt()
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
    _require_iphone_backup_decrypt()
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
    _require_iphone_backup_decrypt()
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
                    f" Found Screen Time files: {_format_related_files(related_rows)}."
                )
            raise BackupNotFoundError(
                f"knowledgeC.db not found in decrypted backup manifest: {backup_dir}."
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
            "Encrypted backup could not be decrypted. "
            "Check the backup_password in config.json."
        ) from exc


def find_knowledge_db(backup_base_dir: Path, backup_password: str | None = None) -> Path:
    if not backup_base_dir.exists():
        raise BackupNotFoundError(f"backup_base_dir does not exist: {backup_base_dir}")

    files = sorted(_direct_knowledge_db_matches(backup_base_dir), key=lambda path: path.stat().st_mtime, reverse=True)
    if files:
        return files[0]

    backup_dirs = _backup_directories(backup_base_dir)
    if not backup_dirs:
        raise BackupNotFoundError(
            f"knowledgeC.db not found under: {backup_base_dir}"
        )

    encrypted_backups = [backup_dir for backup_dir in backup_dirs if _is_encrypted_backup(backup_dir)]
    if encrypted_backups:
        if not backup_password:
            raise BackupNotFoundError(
                "The found iTunes backup is encrypted. "
                "Set the backup password in config.json."
            )
        return _decrypt_knowledge_db(encrypted_backups[0], backup_password)

    raise BackupNotFoundError(
        f"knowledgeC.db not found under: {backup_base_dir}"
    )


def _extract_backup_file(
    backup: EncryptedBackup,
    *,
    relative_path: str,
    domain: str | None,
    suffix: str,
) -> Path | None:
    try:
        try:
            file_bytes = backup.extract_file_as_bytes(relative_path=relative_path, domain_like=domain)
        except AssertionError:
            file_bytes = _decrypt_file_without_size_check(backup, relative_path, domain)
    except Exception:
        return None

    temp_file = tempfile.NamedTemporaryFile(prefix="ios-aw-", suffix=suffix, delete=False)
    try:
        temp_file.write(file_bytes)
    finally:
        temp_file.close()
    return Path(temp_file.name)


def _write_app_activity_manifest_csv(backup: EncryptedBackup) -> Path | None:
    _require_iphone_backup_decrypt()
    rows: list[tuple[str, str, int, int]] = []
    with backup.manifest_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT relativePath, domain, file
            FROM Files
            WHERE file IS NOT NULL
              AND (
                domain LIKE 'AppDomain-%'
                OR domain LIKE 'AppDomainGroup-group.%'
              )
              AND domain NOT LIKE 'AppDomainPlugin-%'
            """
        )
        manifest_rows = cursor.fetchall()

    for relative_path, domain, file_blob in manifest_rows:
        if file_blob is None or domain is None:
            continue
        try:
            file_plist = utils.FilePlist(file_blob)
        except Exception:
            continue
        mtime = getattr(file_plist, "mtime", None)
        filesize = getattr(file_plist, "filesize", 0) or 0
        if not isinstance(mtime, int) or mtime <= 0 or filesize <= 0:
            continue
        rows.append((str(domain), str(relative_path or ""), mtime, int(filesize)))

    if not rows:
        return None

    temp_file = tempfile.NamedTemporaryFile(prefix="ios-aw-app-activity-", suffix=".csv", delete=False, mode="w", encoding="utf-8", newline="")
    try:
        writer = csv.DictWriter(temp_file, fieldnames=["domain", "relative_path", "mtime", "size"])
        writer.writeheader()
        for domain, relative_path, mtime, size in rows:
            writer.writerow(
                {
                    "domain": domain,
                    "relative_path": relative_path,
                    "mtime": mtime,
                    "size": size,
                }
            )
    finally:
        temp_file.close()
    return Path(temp_file.name)


def _manifest_match(
    backup: EncryptedBackup,
    *,
    relative_path: str,
    domain: str | None = None,
) -> tuple[str, str | None] | None:
    _require_iphone_backup_decrypt()
    query = """
        SELECT relativePath, domain
        FROM Files
        WHERE lower(relativePath) = lower(?)
    """
    params: list[str] = [relative_path]
    if domain is not None:
        query += " AND domain = ?"
        params.append(domain)
    query += " ORDER BY flags DESC, domain, relativePath LIMIT 1"

    with backup.manifest_db_cursor() as cursor:
        cursor.execute(query, tuple(params))
        row = cursor.fetchone()
    if not row:
        return None
    return str(row[0]), str(row[1]) if row[1] is not None else None


def _manifest_db_match(
    manifest_db_path: Path,
    *,
    relative_path: str,
    domain: str | None = None,
) -> tuple[str, str, str | None] | None:
    conn = sqlite3.connect(manifest_db_path)
    try:
        query = """
            SELECT fileID, relativePath, domain
            FROM Files
            WHERE lower(relativePath) = lower(?)
        """
        params: list[str] = [relative_path]
        if domain is not None:
            query += " AND domain = ?"
            params.append(domain)
        query += " ORDER BY flags DESC, domain, relativePath LIMIT 1"
        row = conn.execute(query, tuple(params)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return str(row[0]), str(row[1]), str(row[2]) if row[2] is not None else None


def _extract_unencrypted_backup_file(
    backup_dir: Path,
    *,
    file_id: str,
    suffix: str,
) -> Path | None:
    candidates = [
        backup_dir / file_id[:2] / file_id,
        backup_dir / file_id,
    ]
    source_path = next((path for path in candidates if path.exists()), None)
    if source_path is None:
        return None
    temp_file = tempfile.NamedTemporaryFile(prefix="ios-aw-", suffix=suffix, delete=False)
    try:
        temp_file.write(source_path.read_bytes())
    finally:
        temp_file.close()
    return Path(temp_file.name)


def _write_unencrypted_app_activity_manifest_csv(backup_dir: Path) -> Path | None:
    manifest_db_path = backup_dir / "Manifest.db"
    if not manifest_db_path.exists():
        return None
    conn = sqlite3.connect(manifest_db_path)
    try:
        rows = conn.execute(
            """
            SELECT relativePath, domain, file
            FROM Files
            WHERE file IS NOT NULL
              AND (
                domain LIKE 'AppDomain-%'
                OR domain LIKE 'AppDomainGroup-group.%'
              )
              AND domain NOT LIKE 'AppDomainPlugin-%'
            """
        ).fetchall()
    finally:
        conn.close()

    manifest_rows: list[tuple[str, str, int, int]] = []
    for relative_path, domain, file_blob in rows:
        if file_blob is None or domain is None:
            continue
        try:
            file_plist = utils.FilePlist(file_blob)
        except Exception:
            continue
        mtime = getattr(file_plist, "mtime", None)
        filesize = getattr(file_plist, "filesize", 0) or 0
        if not isinstance(mtime, int) or mtime <= 0 or filesize <= 0:
            continue
        manifest_rows.append((str(domain), str(relative_path or ""), mtime, int(filesize)))

    if not manifest_rows:
        return None

    temp_file = tempfile.NamedTemporaryFile(
        prefix="ios-aw-app-activity-",
        suffix=".csv",
        delete=False,
        mode="w",
        encoding="utf-8",
        newline="",
    )
    try:
        writer = csv.DictWriter(temp_file, fieldnames=["domain", "relative_path", "mtime", "size"])
        writer.writeheader()
        for domain, relative_path, mtime, size in manifest_rows:
            writer.writerow(
                {
                    "domain": domain,
                    "relative_path": relative_path,
                    "mtime": mtime,
                    "size": size,
                }
            )
    finally:
        temp_file.close()
    return Path(temp_file.name)


def _usage_data_files_from_unencrypted_backup(backup_dir: Path) -> UsageDataFiles:
    targets = {
        "knowledge_db": ("Library/CoreDuet/Knowledge/knowledgeC.db", None, ".knowledgeC.db"),
        "interaction_db": ("Library/CoreDuet/People/interactionC.db", "HomeDomain", ".interactionC.db"),
        "safari_history_db": ("Library/Safari/History.db", "HomeDomain", ".safari-history.db"),
        "screen_time_agent_plist": ("Library/Preferences/com.apple.ScreenTimeAgent.plist", "HomeDomain", ".ScreenTimeAgent.plist"),
        "screen_time_settings_plist": (
            "Library/Preferences/com.apple.ScreenTimeSettingsAgent.plist",
            "HomeDomain",
            ".ScreenTimeSettingsAgent.plist",
        ),
    }
    manifest_db_path = backup_dir / "Manifest.db"
    found: dict[str, Path] = {}
    for key, (relative_path, domain, suffix) in targets.items():
        match = _manifest_db_match(manifest_db_path, relative_path=relative_path, domain=domain)
        if match is None and key == "knowledge_db":
            match = _manifest_db_match(manifest_db_path, relative_path="knowledgeC.db")
        if match is None:
            continue
        file_id, _matched_relative_path, _matched_domain = match
        extracted = _extract_unencrypted_backup_file(backup_dir, file_id=file_id, suffix=suffix)
        if extracted is not None:
            found[key] = extracted

    primary = found.get("knowledge_db") or found.get("interaction_db")
    if primary is None:
        raise BackupNotFoundError(f"No supported usage database found in backup manifest: {backup_dir}.")

    return UsageDataFiles(
        primary_db=primary,
        knowledge_db=found.get("knowledge_db"),
        interaction_db=found.get("interaction_db"),
        safari_history_db=found.get("safari_history_db"),
        app_activity_manifest_csv=_write_unencrypted_app_activity_manifest_csv(backup_dir),
        screen_time_agent_plist=found.get("screen_time_agent_plist"),
        screen_time_settings_plist=found.get("screen_time_settings_plist"),
    )


def _decrypt_usage_data_files(backup_dir: Path, backup_password: str) -> UsageDataFiles:
    _require_iphone_backup_decrypt()
    backup = EncryptedBackup(backup_directory=str(backup_dir), passphrase=backup_password)
    targets = {
        "knowledge_db": ("Library/CoreDuet/Knowledge/knowledgeC.db", None, ".knowledgeC.db"),
        "interaction_db": ("Library/CoreDuet/People/interactionC.db", "HomeDomain", ".interactionC.db"),
        "safari_history_db": ("Library/Safari/History.db", "HomeDomain", ".safari-history.db"),
        "screen_time_agent_plist": ("Library/Preferences/com.apple.ScreenTimeAgent.plist", "HomeDomain", ".ScreenTimeAgent.plist"),
        "screen_time_settings_plist": (
            "Library/Preferences/com.apple.ScreenTimeSettingsAgent.plist",
            "HomeDomain",
            ".ScreenTimeSettingsAgent.plist",
        ),
    }

    found: dict[str, Path] = {}
    for key, (relative_path, domain, suffix) in targets.items():
        match = _manifest_match(backup, relative_path=relative_path, domain=domain)
        if match is None and key == "knowledge_db":
            match = _manifest_match(backup, relative_path="knowledgeC.db")
        if match is None:
            continue
        matched_relative_path, matched_domain = match
        extracted = _extract_backup_file(
            backup,
            relative_path=matched_relative_path,
            domain=matched_domain,
            suffix=suffix,
        )
        if extracted is not None:
            found[key] = extracted

    primary = found.get("knowledge_db") or found.get("interaction_db")
    if primary is None:
        related_rows = _screen_time_related_files(backup)
        related_suffix = ""
        if related_rows:
            related_suffix = f" Found related files: {_format_related_files(related_rows)}."
        raise BackupNotFoundError(
            f"No supported usage database found in backup manifest: {backup_dir}.{related_suffix}"
        )

    return UsageDataFiles(
        primary_db=primary,
        knowledge_db=found.get("knowledge_db"),
        interaction_db=found.get("interaction_db"),
        safari_history_db=found.get("safari_history_db"),
        app_activity_manifest_csv=_write_app_activity_manifest_csv(backup),
        screen_time_agent_plist=found.get("screen_time_agent_plist"),
        screen_time_settings_plist=found.get("screen_time_settings_plist"),
    )


def find_usage_data_files(backup_base_dir: Path, backup_password: str | None = None) -> UsageDataFiles:
    if not backup_base_dir.exists():
        raise BackupNotFoundError(f"backup_base_dir does not exist: {backup_base_dir}")

    backup_dirs = _backup_directories(backup_base_dir)
    if backup_dirs:
        encrypted_backups = [backup_dir for backup_dir in backup_dirs if _is_encrypted_backup(backup_dir)]
        if encrypted_backups:
            if not backup_password:
                raise BackupNotFoundError(
                    "The found iTunes backup is encrypted. "
                    "Set the backup password in config.json."
                )
            return _decrypt_usage_data_files(encrypted_backups[0], backup_password)

        return _usage_data_files_from_unencrypted_backup(backup_dirs[0])

    direct_knowledge_files = sorted(
        _direct_knowledge_db_matches(backup_base_dir),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if direct_knowledge_files:
        return UsageDataFiles(primary_db=direct_knowledge_files[0], knowledge_db=direct_knowledge_files[0])

    raise BackupNotFoundError(f"usage data files not found under: {backup_base_dir}")
