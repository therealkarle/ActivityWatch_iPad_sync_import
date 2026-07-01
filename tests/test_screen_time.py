from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ios_activitywatch_importer.config import APPLE_SCREEN_TIME_EPOCH
from ios_activitywatch_importer.screen_time import (
    SourceAudit,
    coredata_to_datetime,
    load_manifest_app_activity_events,
    load_screen_time_events,
    rank_and_consolidate_events,
    ScreenTimeEvent,
)


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

    def test_falls_back_to_interactions_when_zobject_has_no_usable_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "mixed.db"
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
                "INSERT INTO ZOBJECT (ZSTREAMNAME, ZSTARTDATE, ZENDDATE, ZVALUESTRING) VALUES (?, ?, ?, ?)",
                ("/app/inFocus", 10.0, 10.0, "Safari"),
            )
            conn.execute(
                "INSERT INTO ZINTERACTIONS (ZBUNDLEID, ZGROUPNAME, ZSTARTDATE, ZENDDATE) VALUES (?, ?, ?, ?)",
                ("net.whatsapp.WhatsApp", "Family", 30.0, 90.0),
            )
            conn.commit()
            conn.close()

            events = load_screen_time_events(db_path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].app, "WhatsApp")
            self.assertEqual(events[0].source_table, "ZINTERACTIONS")

    def test_combines_zobject_and_interactions_from_same_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "mixed.db"
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
                "INSERT INTO ZOBJECT (ZSTREAMNAME, ZSTARTDATE, ZENDDATE, ZVALUESTRING) VALUES (?, ?, ?, ?)",
                ("/app/inFocus", 10.0, 70.0, "Safari"),
            )
            conn.execute(
                "INSERT INTO ZINTERACTIONS (ZBUNDLEID, ZGROUPNAME, ZSTARTDATE, ZENDDATE) VALUES (?, ?, ?, ?)",
                ("net.whatsapp.WhatsApp", "Family", 100.0, 160.0),
            )
            conn.commit()
            conn.close()

            events = load_screen_time_events(db_path)
            self.assertEqual(len(events), 2)
            self.assertEqual([event.app for event in events], ["Safari", "WhatsApp"])
            self.assertEqual([event.source_table for event in events], ["ZOBJECT", "ZINTERACTIONS"])

    def test_zobject_loads_supported_streams_and_audits_unknown_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "knowledge.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE ZOBJECT (
                    ZSTREAMNAME TEXT,
                    ZSTARTDATE REAL,
                    ZENDDATE REAL,
                    ZBUNDLEID TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO ZOBJECT (ZSTREAMNAME, ZSTARTDATE, ZENDDATE, ZBUNDLEID) VALUES (?, ?, ?, ?)",
                [
                    ("/app/inFocus", 10.0, 40.0, "com.google.Gmail"),
                    ("/device/noise", 50.0, 80.0, "net.whatsapp.WhatsApp"),
                ],
            )
            conn.commit()
            conn.close()
            audit = SourceAudit()

            events = load_screen_time_events(db_path, audit=audit)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].app, "Gmail")
            self.assertEqual(events[0].data["stream_name"], "/app/inFocus")
            self.assertEqual(audit.files[str(db_path)]["streams"]["/device/noise"], 1)
            self.assertEqual(audit.dropped["coreduet_zobject"]["unsupported_stream:/device/noise"], 1)

    def test_higher_rank_events_shadow_nearby_manifest_fallback(self) -> None:
        real_event = ScreenTimeEvent(
            start=datetime(2026, 6, 28, 20, 0, tzinfo=timezone.utc),
            end=datetime(2026, 6, 28, 20, 5, tzinfo=timezone.utc),
            app="WhatsApp",
            data={"bundle_id": "net.whatsapp.WhatsApp", "source": "zinteractions"},
            source_table="ZINTERACTIONS",
            confidence=0.8,
            source_rank=30,
            candidate_source="coreduet_interactions",
        )
        fallback_event = ScreenTimeEvent(
            start=datetime(2026, 6, 28, 20, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 28, 20, 1, 30, tzinfo=timezone.utc),
            app="WhatsApp",
            data={"bundle_id": "net.whatsapp.WhatsApp", "source": "app_manifest_mtime"},
            source_table="Manifest.Files",
            confidence=0.25,
            source_rank=90,
            candidate_source="manifest_fallback",
        )
        audit = SourceAudit()

        events = rank_and_consolidate_events([fallback_event, real_event], audit=audit)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source_table, "ZINTERACTIONS")
        self.assertEqual(audit.dropped["final:manifest_fallback"]["shadowed_by_higher_rank_source"], 1)

    def test_manifest_fallback_remains_when_no_higher_rank_source_is_nearby(self) -> None:
        fallback_event = ScreenTimeEvent(
            start=datetime(2026, 6, 28, 20, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 28, 20, 1, 30, tzinfo=timezone.utc),
            app="Fitbit Mobile",
            data={"bundle_id": "com.fitbit.FitbitMobile", "source": "app_manifest_mtime"},
            source_table="Manifest.Files",
            confidence=0.25,
            source_rank=90,
            candidate_source="manifest_fallback",
        )

        events = rank_and_consolidate_events([fallback_event])

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].app, "Fitbit Mobile")
        self.assertEqual(events[0].confidence, 0.25)

    def test_long_manifest_session_is_not_fully_shadowed_by_short_interaction(self) -> None:
        real_event = ScreenTimeEvent(
            start=datetime(2026, 6, 28, 18, 31, tzinfo=timezone.utc),
            end=datetime(2026, 6, 28, 18, 43, tzinfo=timezone.utc),
            app="WhatsApp",
            data={"bundle_id": "net.whatsapp.WhatsApp", "source": "zinteractions"},
            source_table="ZINTERACTIONS",
            confidence=0.8,
            source_rank=30,
            candidate_source="coreduet_interactions",
        )
        fallback_events = [
            ScreenTimeEvent(
                start=datetime(2026, 6, 28, 18, 34, tzinfo=timezone.utc),
                end=datetime(2026, 6, 28, 18, 34, 30, tzinfo=timezone.utc),
                app="TikTok",
                data={"bundle_id": "com.zhiliaoapp.musically", "source": "app_manifest_mtime"},
                source_table="Manifest.Files",
                confidence=0.25,
                source_rank=90,
                candidate_source="manifest_fallback",
            ),
            ScreenTimeEvent(
                start=datetime(2026, 6, 28, 18, 58, tzinfo=timezone.utc),
                end=datetime(2026, 6, 28, 18, 58, 30, tzinfo=timezone.utc),
                app="TikTok",
                data={"bundle_id": "com.zhiliaoapp.musically", "source": "app_manifest_mtime"},
                source_table="Manifest.Files",
                confidence=0.25,
                source_rank=90,
                candidate_source="manifest_fallback",
            ),
        ]

        events = rank_and_consolidate_events([real_event, *fallback_events])

        self.assertEqual([event.app for event in events], ["WhatsApp", "TikTok"])
        self.assertEqual(events[0].end, datetime(2026, 6, 28, 18, 34, tzinfo=timezone.utc))
        self.assertEqual(events[1].start, datetime(2026, 6, 28, 18, 34, tzinfo=timezone.utc))
        self.assertEqual(events[1].end, datetime(2026, 6, 28, 18, 58, 30, tzinfo=timezone.utc))
        self.assertEqual(events[1].count, 2)

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

    def test_manifest_app_activity_events_are_sessionized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "app-activity.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "domain,relative_path,mtime,size",
                        "AppDomain-com.zhiliaoapp.musically,Library/tracker.sqlite,1782671702,1024",
                        "AppDomain-com.zhiliaoapp.musically,Library/cache.db,1782671762,2048",
                        "AppDomain-com.amazon.aiv.AIVApp,Library/cache.db,1782675362,2048",
                    ]
                ),
                encoding="utf-8",
            )

            events = load_manifest_app_activity_events(csv_path)

            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].app, "TikTok")
            self.assertEqual(events[0].duration_seconds, 90.0)
            self.assertEqual(events[0].count, 2)
            self.assertEqual(events[0].data["bundle_id"], "com.zhiliaoapp.musically")
            self.assertEqual(events[0].data["source"], "app_manifest_mtime")
            self.assertEqual(events[1].app, "Prime Video")
