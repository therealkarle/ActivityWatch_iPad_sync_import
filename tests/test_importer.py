from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ios_activitywatch_importer.importer import _write_debug_copy
from ios_activitywatch_importer.screen_time import ScreenTimeEvent
from ios_activitywatch_importer.importer import _write_debug_csv
from datetime import datetime, timezone


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

    def test_write_debug_csv_exports_recognized_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = [
                ScreenTimeEvent(
                    start=datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc),
                    end=datetime(2026, 6, 28, 10, 5, tzinfo=timezone.utc),
                    app="WhatsApp",
                    data={
                        "bundle_id": "net.whatsapp.WhatsApp",
                        "title": "Family",
                    },
                )
            ]

            csv_path = _write_debug_csv(events, root)

            self.assertEqual(csv_path, root / "debugOut" / "knowledgeC.recognized-events.csv")
            content = csv_path.read_text(encoding="utf-8")
            self.assertIn("start_utc,end_utc,duration_seconds,app,bundle_id,target_bundle_id,title,domain_identifier,sender,account", content)
            self.assertIn("WhatsApp", content)
            self.assertIn("Family", content)
