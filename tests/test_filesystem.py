from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ios_activitywatch_importer.config import KNOWLEDGE_DB_SHA1
from ios_activitywatch_importer.filesystem import find_knowledge_db


class FilesystemTests(unittest.TestCase):
    def test_finds_knowledge_db_under_udid_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            udid_dir = root / "00008030-001C195E3E42002E"
            udid_dir.mkdir(parents=True)
            target = udid_dir / KNOWLEDGE_DB_SHA1
            target.write_text("test", encoding="utf-8")
            found = find_knowledge_db(root)
            self.assertEqual(found, target)

    def test_encrypted_backup_without_password_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "00008030-001C195E3E42002E"
            backup_dir.mkdir(parents=True)
            (backup_dir / "Manifest.db").write_text("stub", encoding="utf-8")
            with (backup_dir / "Manifest.plist").open("wb") as manifest_file:
                plistlib.dump({"IsEncrypted": True}, manifest_file)

            with self.assertRaises(Exception) as ctx:
                find_knowledge_db(root)
            self.assertIn("verschlüsselt", str(ctx.exception))
            self.assertIn("backup_password", str(ctx.exception))

    def test_encrypted_backup_uses_password_and_writes_temp_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "00008030-001C195E3E42002E"
            backup_dir.mkdir(parents=True)
            (backup_dir / "Manifest.db").write_text("stub", encoding="utf-8")
            with (backup_dir / "Manifest.plist").open("wb") as manifest_file:
                plistlib.dump({"IsEncrypted": True}, manifest_file)

            class DummyCursor:
                def execute(self, *_args, **_kwargs):
                    return self

                def fetchone(self):
                    return ("dummy-file-id", "Documents/knowledgeC.db", "HomeDomain")

                def fetchall(self):
                    return []

            class DummyBackup:
                def __init__(self, *, backup_directory, passphrase):
                    self.backup_directory = backup_directory
                    self.passphrase = passphrase

                def manifest_db_cursor(self):
                    class _Context:
                        def __enter__(self_inner):
                            return DummyCursor()

                        def __exit__(self_inner, exc_type, exc, tb):
                            return False

                    return _Context()

                def extract_file_as_bytes(self, *, relative_path, domain_like=None):
                    self.relative_path = relative_path
                    self.domain_like = domain_like
                    return b"sqlite-bytes"

                def _cleanup(self):
                    self.cleaned_up = True

            with patch("ios_activitywatch_importer.filesystem.EncryptedBackup", DummyBackup):
                found = find_knowledge_db(root, "secret")

            self.assertTrue(found.exists())
            self.assertEqual(found.read_bytes(), b"sqlite-bytes")

    def test_encrypted_backup_without_knowledge_db_reports_related_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "00008030-001C195E3E42002E"
            backup_dir.mkdir(parents=True)
            (backup_dir / "Manifest.db").write_text("stub", encoding="utf-8")
            with (backup_dir / "Manifest.plist").open("wb") as manifest_file:
                plistlib.dump({"IsEncrypted": True}, manifest_file)

            class DummyCursor:
                def __init__(self) -> None:
                    self.params: tuple[str, ...] | None = None

                def execute(self, _query, params=None):
                    self.params = tuple(params) if params is not None else None
                    return self

                def fetchone(self):
                    if self.params == ("%knowledgeC.db%",):
                        return None
                    return None

                def fetchall(self):
                    return [
                        ("Library/Preferences/com.apple.ScreenTimeAgent.plist", "HomeDomain"),
                        ("Library/CoreDuet/People/interactionC.db", "HomeDomain"),
                    ]

            class DummyBackup:
                def __init__(self, *, backup_directory, passphrase):
                    self.backup_directory = backup_directory
                    self.passphrase = passphrase

                def manifest_db_cursor(self):
                    class _Context:
                        def __enter__(self_inner):
                            return DummyCursor()

                        def __exit__(self_inner, exc_type, exc, tb):
                            return False

                    return _Context()

                def extract_file_as_bytes(self, *, relative_path, domain_like=None):
                    raise AssertionError("extract_file_as_bytes should not be called")

            with patch("ios_activitywatch_importer.filesystem.EncryptedBackup", DummyBackup):
                with self.assertRaises(Exception) as ctx:
                    find_knowledge_db(root, "secret")

            message = str(ctx.exception)
            self.assertIn("knowledgeC.db nicht im entschlüsselten Backup-Manifest gefunden", message)
            self.assertIn("ScreenTimeAgent.plist", message)
            self.assertIn("interactionC.db", message)

    def test_encrypted_backup_uses_interaction_db_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "00008030-001C195E3E42002E"
            backup_dir.mkdir(parents=True)
            (backup_dir / "Manifest.db").write_text("stub", encoding="utf-8")
            with (backup_dir / "Manifest.plist").open("wb") as manifest_file:
                plistlib.dump({"IsEncrypted": True}, manifest_file)

            class DummyCursor:
                def __init__(self) -> None:
                    self.calls = 0

                def execute(self, *_args, **_kwargs):
                    self.calls += 1
                    return self

                def fetchone(self):
                    return ("dummy-file-id", "Library/CoreDuet/People/interactionC.db", "HomeDomain")

                def fetchall(self):
                    return []

            class DummyBackup:
                def __init__(self, *, backup_directory, passphrase):
                    self.backup_directory = backup_directory
                    self.passphrase = passphrase

                def manifest_db_cursor(self):
                    class _Context:
                        def __enter__(self_inner):
                            return DummyCursor()

                        def __exit__(self_inner, exc_type, exc, tb):
                            return False

                    return _Context()

                def extract_file_as_bytes(self, *, relative_path, domain_like=None):
                    self.relative_path = relative_path
                    self.domain_like = domain_like
                    return b"interaction-sqlite-bytes"

            with patch("ios_activitywatch_importer.filesystem.EncryptedBackup", DummyBackup):
                found = find_knowledge_db(root, "secret")

            self.assertTrue(found.exists())
            self.assertEqual(found.read_bytes(), b"interaction-sqlite-bytes")
