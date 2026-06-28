from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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

