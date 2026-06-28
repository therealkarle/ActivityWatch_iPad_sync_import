from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ios_activitywatch_importer.importer import _write_debug_copy


class ImporterTests(unittest.TestCase):
    def test_write_debug_copy_creates_debug_out_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "knowledgeC.db"
            source.write_bytes(b"sqlite-bytes")

            copied = _write_debug_copy(source, root)

            self.assertEqual(copied, root / "debugOut" / "knowledgeC.decrypted.db")
            self.assertTrue(copied.exists())
            self.assertEqual(copied.read_bytes(), b"sqlite-bytes")
