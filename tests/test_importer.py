from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from ios_activitywatch_importer.config import AppConfig
from ios_activitywatch_importer.filesystem import UsageDataFiles
from ios_activitywatch_importer.importer import _write_debug_copy
from ios_activitywatch_importer.importer import run_import
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
                    source_table="ZINTERACTIONS",
                    source_pk=1,
                    raw_start=datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc),
                    raw_end=datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc),
                    inferred_duration=True,
                )
            ]

            csv_path = _write_debug_csv(events, root)

            self.assertEqual(csv_path, root / "debugOut" / "aw-watcher-window.recognized-events.csv")
            content = csv_path.read_text(encoding="utf-8")
            self.assertIn("source_table,source_pk,raw_start_utc,raw_end_utc,inferred_duration,start_utc,end_utc,duration_seconds,count,app,bundle_id,target_bundle_id,title,domain_identifier,sender,account,url,content_url,derived_intent_identifier,source,domain,source_path,file_size,direction,mechanism,is_response,recipient_count", content)
            self.assertIn("ZINTERACTIONS,1", content)
            self.assertIn("true", content)
            self.assertIn("WhatsApp", content)
            self.assertIn("Family", content)

    def test_run_import_always_reports_bucket_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = AppConfig(
                backup_base_dir=root,
                backup_password=None,
                aw_api_url="http://localhost:5600/api/0",
                bucket_id="aw-watcher-ios",
                hostname="test-iphone",
                debug_mode=False,
            )
            fake_db = root / "knowledgeC.db"
            fake_db.write_bytes(b"sqlite-bytes")
            events = [
                ScreenTimeEvent(
                    start=datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc),
                    end=datetime(2026, 6, 28, 10, 5, tzinfo=timezone.utc),
                    app="WhatsApp",
                )
            ]

            fake_client = Mock()
            fake_client.ensure_bucket.return_value = None
            fake_client.get_events.return_value = []
            fake_client.get_last_event_end.return_value = None
            fake_client.post_events.return_value = len(events)
            fake_load = Mock(return_value=events)
            fake_usage_files = UsageDataFiles(primary_db=fake_db, interaction_db=fake_db)

            with (
                patch("ios_activitywatch_importer.importer.find_usage_data_files", return_value=fake_usage_files),
                patch("ios_activitywatch_importer.importer.ActivityWatchClient", return_value=fake_client),
                patch("ios_activitywatch_importer.importer.load_window_events_from_files", fake_load),
                patch("ios_activitywatch_importer.importer.derive_afk_events", return_value=[]),
                patch("ios_activitywatch_importer.importer.project_root", return_value=root),
            ):
                buffer = StringIO()
                with redirect_stdout(buffer):
                    imported = run_import(config, verbose=False)

            self.assertEqual(imported, 1)
            fake_load.assert_called_once()
            _, kwargs = fake_load.call_args
            self.assertIsNone(kwargs["cutoff"])
            self.assertFalse(kwargs["verbose"])
            output = buffer.getvalue()
            self.assertIn("Bucket aw-watcher-window: 1 events written.", output)
            self.assertIn("Bucket aw-watcher-afk: 0 events written.", output)

    def test_run_import_ignores_activitywatch_cutoff_for_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = AppConfig(
                backup_base_dir=root,
                backup_password=None,
                aw_api_url="http://localhost:5600/api/0",
                bucket_id="aw-watcher-ios",
                hostname="test-iphone",
                debug_mode=False,
            )
            fake_db = root / "knowledgeC.db"
            fake_db.write_bytes(b"sqlite-bytes")
            events = [
                ScreenTimeEvent(
                    start=datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc),
                    end=datetime(2026, 6, 28, 10, 5, tzinfo=timezone.utc),
                    app="WhatsApp",
                )
            ]
            last_end = datetime(2026, 6, 28, 9, 55, tzinfo=timezone.utc)

            fake_client = Mock()
            fake_client.ensure_bucket.return_value = None
            fake_client.get_events.return_value = []
            fake_client.get_last_event_end.return_value = last_end
            fake_client.post_events.return_value = len(events)
            fake_load = Mock(return_value=events)
            fake_usage_files = UsageDataFiles(primary_db=fake_db, interaction_db=fake_db)

            with (
                patch("ios_activitywatch_importer.importer.find_usage_data_files", return_value=fake_usage_files),
                patch("ios_activitywatch_importer.importer.ActivityWatchClient", return_value=fake_client),
                patch("ios_activitywatch_importer.importer.load_window_events_from_files", fake_load),
                patch("ios_activitywatch_importer.importer.derive_afk_events", return_value=[]),
                patch("ios_activitywatch_importer.importer.project_root", return_value=root),
            ):
                buffer = StringIO()
                with redirect_stdout(buffer):
                    imported = run_import(config, verbose=False)

            self.assertEqual(imported, 1)
            _, kwargs = fake_load.call_args
            self.assertIsNone(kwargs["cutoff"])
