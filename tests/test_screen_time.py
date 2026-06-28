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
            self.assertEqual(events[0].duration_seconds, 30.0)

    def test_extraction_from_interaction_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "interaction.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE ZINTERACTIONS (
                    ZBUNDLEID TEXT,
                    ZGROUPNAME TEXT,
                    ZSTARTDATE REAL,
                    ZENDDATE REAL
                )
                """
            )
            conn.execute(
                "INSERT INTO ZINTERACTIONS (ZBUNDLEID, ZGROUPNAME, ZSTARTDATE, ZENDDATE) VALUES (?, ?, ?, ?)",
                ("net.whatsapp.WhatsApp", "Family", 30.0, 30.0),
            )
            conn.execute(
                "INSERT INTO ZINTERACTIONS (ZBUNDLEID, ZGROUPNAME, ZSTARTDATE, ZENDDATE) VALUES (?, ?, ?, ?)",
                ("net.whatsapp.WhatsApp", "Family", 90.0, 90.0),
            )
            conn.commit()
            conn.close()

            events = load_screen_time_events(db_path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].app, "WhatsApp")
            self.assertEqual(events[0].duration_seconds, 60.0)
            self.assertEqual(events[0].data["bundle_id"], "net.whatsapp.WhatsApp")
            self.assertEqual(events[0].data["title"], "Family")
            self.assertEqual(events[0].count, 2)
            self.assertTrue(events[0].inferred_duration)
            self.assertEqual(events[0].source_table, "ZINTERACTIONS")

    def test_zero_duration_interactions_are_sessionized_by_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "interaction.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE ZINTERACTIONS (
                    Z_PK INTEGER,
                    ZBUNDLEID TEXT,
                    ZGROUPNAME TEXT,
                    ZSTARTDATE REAL,
                    ZENDDATE REAL
                )
                """
            )
            conn.executemany(
                "INSERT INTO ZINTERACTIONS (Z_PK, ZBUNDLEID, ZGROUPNAME, ZSTARTDATE, ZENDDATE) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "net.whatsapp.WhatsApp", "Family", 10.0, 10.0),
                    (2, "net.whatsapp.WhatsApp", "Work", 70.0, 70.0),
                ],
            )
            conn.commit()
            conn.close()

            events = load_screen_time_events(db_path)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].app, "WhatsApp")
            self.assertEqual(events[0].duration_seconds, 60.0)
            self.assertEqual(events[0].count, 2)
            self.assertEqual(events[0].data["source"], "zinteractions")

    def test_interaction_sessions_are_clamped_at_next_app(self) -> None:
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
            conn.executemany(
                "INSERT INTO ZINTERACTIONS (ZBUNDLEID, ZSTARTDATE, ZENDDATE) VALUES (?, ?, ?)",
                [
                    ("net.whatsapp.WhatsApp", 10.0, 10.0),
                    ("com.google.Gmail", 20.0, 20.0),
                ],
            )
            conn.commit()
            conn.close()

            events = load_screen_time_events(db_path)

            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].app, "WhatsApp")
            self.assertEqual(events[0].duration_seconds, 10.0)
            self.assertEqual(events[1].app, "Gmail")
            self.assertEqual(events[1].duration_seconds, 30.0)

    def test_positive_interaction_interval_is_preserved(self) -> None:
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
                ("com.google.Gmail", 10.0, 130.0),
            )
            conn.commit()
            conn.close()

            events = load_screen_time_events(db_path)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].app, "Gmail")
            self.assertEqual(events[0].duration_seconds, 120.0)
            self.assertFalse(events[0].inferred_duration)

    def test_calendar_interactions_are_filtered_from_app_usage(self) -> None:
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
            conn.executemany(
                "INSERT INTO ZINTERACTIONS (ZBUNDLEID, ZSTARTDATE, ZENDDATE) VALUES (?, ?, ?)",
                [
                    ("com.apple.mobilecal", 10.0, 86410.0),
                    ("net.whatsapp.WhatsApp", 20.0, 20.0),
                ],
            )
            conn.commit()
            conn.close()

            events = load_screen_time_events(db_path)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].app, "WhatsApp")

    def test_future_outlier_is_filtered(self) -> None:
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
            conn.executemany(
                "INSERT INTO ZINTERACTIONS (ZBUNDLEID, ZSTARTDATE, ZENDDATE) VALUES (?, ?, ?)",
                [
                    ("net.whatsapp.WhatsApp", 100.0, 100.0),
                    ("com.apple.mobilecal", 923003999.0, 923003999.0),
                ],
            )
            conn.commit()
            conn.close()

            reference_time = datetime(2026, 6, 28, tzinfo=timezone.utc)
            events = load_screen_time_events(
                db_path,
                reference_time=reference_time,
                future_tolerance_days=30,
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].app, "WhatsApp")
