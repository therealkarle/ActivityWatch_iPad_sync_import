from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ios_activitywatch_importer.config import APPLE_SCREEN_TIME_EPOCH
from ios_activitywatch_importer.screen_time import coredata_to_datetime, load_screen_time_events


class ScreenTimeTests(unittest.TestCase):
    def test_coredata_timestamp_conversion(self) -> None:
        dt = coredata_to_datetime(0)
        self.assertEqual(dt, datetime.fromtimestamp(APPLE_SCREEN_TIME_EPOCH, tz=timezone.utc))

    def test_extraction_from_minimal_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "knowledge.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE ZOBJECT (
                    ZSTREAMNAME TEXT,
                    ZSTARTDATE REAL,
                    ZENDDATE REAL,
                    ZVALUESTRING TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO ZOBJECT (ZSTREAMNAME, ZSTARTDATE, ZENDDATE, ZVALUESTRING) VALUES (?, ?, ?, ?)",
                ("/app/inFocus", 10.0, 20.0, "Safari"),
            )
            conn.commit()
            conn.close()

            events = load_screen_time_events(db_path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].app, "Safari")
            self.assertEqual(events[0].duration_seconds, 10.0)

    def test_extraction_from_interaction_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "interaction.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE ZINTERACTIONS (
                    ZBUNDLEID TEXT,
                    ZSTARTDATE REAL,
                    ZENDDATE REAL
                )
                """
            )
            conn.execute(
                "INSERT INTO ZINTERACTIONS (ZBUNDLEID, ZSTARTDATE, ZENDDATE) VALUES (?, ?, ?)",
                ("com.apple.mobilecal", 30.0, 45.0),
            )
            conn.commit()
            conn.close()

            events = load_screen_time_events(db_path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].app, "com.apple.mobilecal")
            self.assertEqual(events[0].duration_seconds, 15.0)
