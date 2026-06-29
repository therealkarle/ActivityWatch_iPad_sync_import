from __future__ import annotations

import re
import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import APPLE_SCREEN_TIME_EPOCH


class ScreenTimeError(RuntimeError):
    pass


_BUNDLE_ID_TO_APP_NAME = {
    "com.apple.DocumentsApp": "Files",
    "com.apple.ScreenshotServicesService": "Screenshot Services",
    "com.apple.mobilecal": "Calendar",
    "com.apple.mobilemail": "Mail",
    "com.apple.mobileslideshow": "Photos",
    "com.burbn.instagram": "Instagram",
    "com.garmin.connect.mobile": "Garmin Connect",
    "com.google.Gmail": "Gmail",
    "com.amazon.aiv.AIVApp": "Prime Video",
    "com.videobrowser.ios": "Video Lite",
    "com.zhiliaoapp.musically": "TikTok",
    "com.microsoft.skydrive": "OneDrive",
    "net.whatsapp.WhatsApp": "WhatsApp",
    "org.mozilla.ios.Firefox": "Firefox",
}


@dataclass(frozen=True)
class ScreenTimeEvent:
    start: datetime
    end: datetime
    app: str
    data: dict[str, Any] | None = None
    count: int = 1
    source_table: str | None = None
    source_pk: int | None = None
    raw_start: datetime | None = None
    raw_end: datetime | None = None
    inferred_duration: bool = False

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass(frozen=True)
class AfkEvent:
    start: datetime
    end: datetime
    status: str
    data: dict[str, Any] | None = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


def coredata_to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) + APPLE_SCREEN_TIME_EPOCH, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def coredata_to_unix(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) + APPLE_SCREEN_TIME_EPOCH
    except (TypeError, ValueError):
        return None


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 256:
        return None
    if text.isdigit():
        return None
    return text


def _is_meaningful_app_name(text: str) -> bool:
    lowered = text.lower()
    if lowered in {"unknown", "null", "none"}:
        return False
    if "/" in text and "app/infocus" in lowered:
        return False
    return any(char.isalpha() for char in text)


def _humanize_bundle_id(bundle_id: str) -> str:
    if bundle_id in _BUNDLE_ID_TO_APP_NAME:
        return _BUNDLE_ID_TO_APP_NAME[bundle_id]

    tail = bundle_id.rsplit(".", maxsplit=1)[-1]
    tail = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tail).strip()
    return tail or bundle_id


def _bundle_id_from_app_domain(domain: str) -> str | None:
    for prefix in ("AppDomainGroup-", "AppDomain-"):
        if domain.startswith(prefix):
            bundle_id = domain[len(prefix):]
            if bundle_id.startswith("group."):
                bundle_id = bundle_id[len("group."):]
            return bundle_id or None
    return None


def _extract_name_from_text(values: list[str]) -> str | None:
    priority_patterns = [
        re.compile(r'"appName"\s*[:=]\s*"([^"]+)"', re.IGNORECASE),
        re.compile(r"appName\s*[:=]\s*([A-Za-z0-9 ._+-]+)", re.IGNORECASE),
        re.compile(r'"bundleIdentifier"\s*[:=]\s*"([^"]+)"', re.IGNORECASE),
        re.compile(r"bundleIdentifier\s*[:=]\s*([A-Za-z0-9.\-]+)", re.IGNORECASE),
        re.compile(r"([A-Za-z0-9][A-Za-z0-9 ._+-]{1,80})"),
    ]
    for text in values:
        for pattern in priority_patterns:
            match = pattern.search(text)
            if match:
                candidate = match.group(1).strip()
                if _is_meaningful_app_name(candidate):
                    return candidate
    return None


def _metadata_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%METADATA%' OR name LIKE '%STRUCTURED%')"
    ).fetchall()
    return [row[0] for row in rows]


def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return [str(row[1]) for row in rows]


def _fetch_related_metadata(
    conn: sqlite3.Connection,
    row_dict: dict[str, Any],
    schema: dict[str, list[str]],
) -> list[dict[str, Any]]:
    related_rows: list[dict[str, Any]] = []
    metadata_tables = _metadata_tables(conn)
    if not metadata_tables:
        return related_rows

    for column, value in row_dict.items():
        upper_column = column.upper()
        if not any(token in upper_column for token in ("METADATA", "STRUCTURED", "SOURCE")):
            continue
        if not isinstance(value, int):
            continue
        for table_name in metadata_tables:
            columns = schema.get(table_name, [])
            if "Z_PK" not in (column.upper() for column in columns):
                continue
            query = f'SELECT * FROM "{table_name}" WHERE Z_PK = ?'
            for candidate in conn.execute(query, (value,)).fetchall():
                related_rows.append(dict(candidate))
    return related_rows


def _extract_app_name_from_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    schema: dict[str, list[str]],
) -> str:
    row_dict = {key: row[key] for key in row.keys()}
    direct_priority = [
        "ZAPPNAME",
        "ZAPPLICATIONNAME",
        "ZBUNDLEIDENTIFIER",
        "ZBUNDLEID",
        "ZTARGETBUNDLEID",
        "ZDISPLAYNAME",
        "ZNAME",
        "ZVALUESTRING",
    ]

    textual_values: list[str] = []
    for column in direct_priority:
        if column in row_dict:
            text = _normalize_text(row_dict[column])
            if text and column in {"ZBUNDLEIDENTIFIER", "ZBUNDLEID", "ZTARGETBUNDLEID"}:
                return _humanize_bundle_id(text)
            if text and _is_meaningful_app_name(text):
                return text
            if text:
                textual_values.append(text)

    for value in row_dict.values():
        text = _normalize_text(value)
        if text:
            textual_values.append(text)

    related_rows = _fetch_related_metadata(conn, row_dict, schema)
    for related_row in related_rows:
        for column in direct_priority:
            if column in related_row:
                text = _normalize_text(related_row[column])
                if text and _is_meaningful_app_name(text):
                    return text
                if text:
                    textual_values.append(text)
        for value in related_row.values():
            text = _normalize_text(value)
            if text:
                textual_values.append(text)

    parsed = _extract_name_from_text(textual_values)
    if parsed:
        return parsed

    return "unknown"


def _event_data_from_row(row_dict: dict[str, Any]) -> dict[str, Any] | None:
    data: dict[str, Any] = {}

    bundle_id = _normalize_text(row_dict.get("ZBUNDLEID"))
    if bundle_id:
        data["bundle_id"] = bundle_id

    target_bundle_id = _normalize_text(row_dict.get("ZTARGETBUNDLEID"))
    if target_bundle_id:
        data["target_bundle_id"] = target_bundle_id

    group_name = _normalize_text(row_dict.get("ZGROUPNAME"))
    if group_name:
        data["title"] = group_name

    domain_identifier = _normalize_text(row_dict.get("ZDOMAINIDENTIFIER"))
    if domain_identifier:
        data["domain_identifier"] = domain_identifier

    sender = _normalize_text(row_dict.get("ZSENDER"))
    if sender:
        data["sender"] = sender

    account = _normalize_text(row_dict.get("ZACCOUNT"))
    if account:
        data["account"] = account

    content_url = _normalize_text(row_dict.get("ZCONTENTURL"))
    if content_url:
        if content_url.startswith(("http://", "https://")):
            data["url"] = content_url
        else:
            data["content_url"] = content_url

    derived_intent_identifier = _normalize_text(row_dict.get("ZDERIVEDINTENTIDENTIFIER"))
    if derived_intent_identifier:
        data["derived_intent_identifier"] = derived_intent_identifier

    for numeric_column, data_key in (
        ("ZDIRECTION", "direction"),
        ("ZMECHANISM", "mechanism"),
        ("ZISRESPONSE", "is_response"),
        ("ZRECIPIENTCOUNT", "recipient_count"),
    ):
        value = row_dict.get(numeric_column)
        if value is not None:
            data[data_key] = value

    return data or None


def _merge_event_data(
    base: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if base is None:
        return dict(incoming) if incoming else None
    if not incoming:
        return dict(base)

    merged = dict(base)
    for key, value in incoming.items():
        if key not in merged or merged[key] in (None, ""):
            merged[key] = value
        elif key == "source_pk" and merged[key] != value:
            existing = str(merged[key])
            merged[key] = f"{existing},{value}"
    return merged or None


def _session_key(event: ScreenTimeEvent) -> tuple[str, str]:
    data = event.data or {}
    bundle_id = _normalize_text(data.get("bundle_id")) or event.app
    return bundle_id, event.app


def consolidate_screen_time_events(
    events: list[ScreenTimeEvent],
    *,
    session_gap_seconds: int = 900,
    min_duration_seconds: int = 30,
) -> list[ScreenTimeEvent]:
    if not events:
        return []

    ordered = sorted(events, key=lambda event: (event.start, event.end, event.app))
    sessions: list[dict[str, Any]] = []

    for event in ordered:
        if not sessions:
            sessions.append(
                {
                    "start": event.start,
                    "end": event.end,
                    "app": event.app,
                    "data": event.data,
                    "count": event.count,
                    "key": _session_key(event),
                    "source_table": event.source_table,
                    "source_pk": event.source_pk,
                    "raw_start": event.raw_start or event.start,
                    "raw_end": event.raw_end or event.end,
                    "inferred_duration": event.inferred_duration,
                }
            )
            continue

        current = sessions[-1]
        same_key = current["key"] == _session_key(event)
        gap_seconds = (event.start - current["end"]).total_seconds()
        if same_key and gap_seconds <= session_gap_seconds:
            current["end"] = max(current["end"], event.end, event.start)
            current["count"] += event.count
            current["data"] = _merge_event_data(current["data"], event.data)
            current["raw_end"] = max(current["raw_end"], event.raw_end or event.end, event.end)
            current["inferred_duration"] = bool(current["inferred_duration"] or event.inferred_duration)
            continue

        sessions.append(
            {
                "start": event.start,
                "end": event.end,
                "app": event.app,
                "data": event.data,
                "count": event.count,
                "key": _session_key(event),
                "source_table": event.source_table,
                "source_pk": event.source_pk,
                "raw_start": event.raw_start or event.start,
                "raw_end": event.raw_end or event.end,
                "inferred_duration": event.inferred_duration,
            }
        )

    consolidated: list[ScreenTimeEvent] = []
    for index, session in enumerate(sessions):
        start = session["start"]
        end = session["end"]
        duration_seconds = max(0.0, (end - start).total_seconds())
        if duration_seconds < min_duration_seconds:
            end = start + timedelta(seconds=min_duration_seconds)
        if index + 1 < len(sessions):
            next_start = sessions[index + 1]["start"]
            if next_start > start and end > next_start:
                end = next_start
        consolidated.append(
            ScreenTimeEvent(
                start=start,
                end=end,
                app=session["app"],
                data=session["data"],
                count=int(session["count"]),
                source_table=session["source_table"],
                source_pk=session["source_pk"],
                raw_start=session["raw_start"],
                raw_end=session["raw_end"],
                inferred_duration=bool(session["inferred_duration"] or end != session["raw_end"]),
            )
        )
    return consolidated


def _debug_print(verbose: bool, message: str) -> None:
    if verbose:
        print(message)


def _table_row_count(conn: sqlite3.Connection, table_name: str) -> int:
    row = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _table_date_range(
    conn: sqlite3.Connection,
    table_name: str,
    start_column: str,
    end_column: str,
) -> tuple[datetime | None, datetime | None]:
    row = conn.execute(
        f'SELECT MIN({start_column}), MAX({end_column}) FROM "{table_name}"'
    ).fetchone()
    if not row:
        return None, None
    start_raw, end_raw = row
    return coredata_to_datetime(start_raw), coredata_to_datetime(end_raw)


def _is_plausible_event_end(
    end: datetime,
    reference_time: datetime | None,
    future_tolerance_days: int,
) -> bool:
    if reference_time is None:
        return True
    return end <= reference_time + timedelta(days=future_tolerance_days)


def _is_calendar_interaction(row_dict: dict[str, Any]) -> bool:
    return _normalize_text(row_dict.get("ZBUNDLEID")) == "com.apple.mobilecal"


def _load_events_from_zobject(
    conn: sqlite3.Connection,
    schema: dict[str, list[str]],
    cutoff: datetime | None,
    *,
    verbose: bool = False,
    reference_time: datetime | None = None,
    future_tolerance_days: int = 30,
) -> list[ScreenTimeEvent]:
    query = 'SELECT * FROM "ZOBJECT" WHERE ZSTREAMNAME = ? AND ZSTARTDATE IS NOT NULL AND ZENDDATE IS NOT NULL'
    params: list[Any] = ["/app/inFocus"]
    if cutoff is not None:
        cutoff_core = cutoff.timestamp() - APPLE_SCREEN_TIME_EPOCH
        query += " AND ZENDDATE > ?"
        params.append(cutoff_core)
    query += " ORDER BY ZSTARTDATE ASC"

    events: list[ScreenTimeEvent] = []
    rows = conn.execute(query, params).fetchall()
    _debug_print(verbose, f"ZOBJECT candidate rows after cutoff: {len(rows)}")
    for row in rows:
        start = coredata_to_datetime(row["ZSTARTDATE"])
        end = coredata_to_datetime(row["ZENDDATE"])
        if start is None or end is None or end <= start:
            continue
        if not _is_plausible_event_end(end, reference_time, future_tolerance_days):
            continue
        app_name = _extract_app_name_from_row(conn, row, schema)
        events.append(
            ScreenTimeEvent(
                start=start,
                end=end,
                app=app_name,
                source_table="ZOBJECT",
                source_pk=row["Z_PK"] if "Z_PK" in row.keys() else None,
                raw_start=start,
                raw_end=end,
            )
        )
    return events


def _load_events_from_zinteractions(
    conn: sqlite3.Connection,
    schema: dict[str, list[str]],
    cutoff: datetime | None,
    *,
    verbose: bool = False,
    reference_time: datetime | None = None,
    future_tolerance_days: int = 30,
) -> list[ScreenTimeEvent]:
    query = 'SELECT * FROM "ZINTERACTIONS" WHERE ZSTARTDATE IS NOT NULL AND ZENDDATE IS NOT NULL'
    params: list[Any] = []
    if cutoff is not None:
        cutoff_core = cutoff.timestamp() - APPLE_SCREEN_TIME_EPOCH
        query += " AND ZENDDATE > ?"
        params.append(cutoff_core)
    query += " ORDER BY ZSTARTDATE ASC"

    events: list[ScreenTimeEvent] = []
    rows = conn.execute(query, params).fetchall()
    _debug_print(verbose, f"ZINTERACTIONS candidate rows after cutoff: {len(rows)}")
    for row in rows:
        start = coredata_to_datetime(row["ZSTARTDATE"])
        end = coredata_to_datetime(row["ZENDDATE"])
        if start is None or end is None:
            continue
        if end < start:
            continue
        if not _is_plausible_event_end(end, reference_time, future_tolerance_days):
            continue
        row_dict = {key: row[key] for key in row.keys()}
        if _is_calendar_interaction(row_dict):
            continue
        app_name = _extract_app_name_from_row(conn, row, schema)
        if not _is_meaningful_app_name(app_name) and app_name != "unknown":
            continue
        data = _event_data_from_row(row_dict) or {}
        data["source"] = "zinteractions"
        if "Z_PK" in row.keys():
            data["source_pk"] = row["Z_PK"]
        events.append(
            ScreenTimeEvent(
                start=start,
                end=end,
                app=app_name,
                data=data,
                source_table="ZINTERACTIONS",
                source_pk=row["Z_PK"] if "Z_PK" in row.keys() else None,
                raw_start=start,
                raw_end=end,
                inferred_duration=end == start,
            )
        )
    return events


def load_screen_time_events(
    db_path: Path,
    cutoff: datetime | None = None,
    *,
    verbose: bool = False,
    reference_time: datetime | None = None,
    future_tolerance_days: int = 30,
) -> list[ScreenTimeEvent]:
    if not db_path.exists():
        raise ScreenTimeError(f"SQLite file not found: {db_path}")

    try:
        conn = sqlite3.connect(db_path.resolve().as_uri(), uri=True)
    except sqlite3.Error as exc:
        raise ScreenTimeError(f"SQLite database could not be opened: {exc}") from exc

    conn.row_factory = sqlite3.Row
    try:
        schema = {
            row[0]: _table_columns(conn, row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if verbose:
            table_names = ", ".join(sorted(schema)) or "(none)"
            print(f"SQLite tables: {table_names}")
            for table_name in sorted(schema):
                print(f"  {table_name}: { _table_row_count(conn, table_name) } rows")
            if "ZOBJECT" in schema:
                start_dt, end_dt = _table_date_range(conn, "ZOBJECT", "ZSTARTDATE", "ZENDDATE")
                if start_dt or end_dt:
                    print(
                        "  ZOBJECT date range: "
                        f"{start_dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if start_dt else 'unknown'} -> "
                        f"{end_dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if end_dt else 'unknown'}"
                    )
            if "ZINTERACTIONS" in schema:
                start_dt, end_dt = _table_date_range(conn, "ZINTERACTIONS", "ZSTARTDATE", "ZENDDATE")
                if start_dt or end_dt:
                    print(
                        "  ZINTERACTIONS date range: "
                        f"{start_dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if start_dt else 'unknown'} -> "
                        f"{end_dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if end_dt else 'unknown'}"
                    )
            if cutoff is not None:
                print(f"Cutoff event end: {cutoff.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}")
        if "ZOBJECT" in schema:
            events = _load_events_from_zobject(
                conn,
                schema,
                cutoff,
                verbose=verbose,
                reference_time=reference_time,
                future_tolerance_days=future_tolerance_days,
            )
            return consolidate_screen_time_events(events)
        if "ZINTERACTIONS" in schema:
            events = _load_events_from_zinteractions(
                conn,
                schema,
                cutoff,
                verbose=verbose,
                reference_time=reference_time,
                future_tolerance_days=future_tolerance_days,
            )
            return consolidate_screen_time_events(events)
        raise ScreenTimeError(
            "No supported tables were found in the SQLite database. "
            "Expected: ZOBJECT or ZINTERACTIONS."
        )
    except sqlite3.DatabaseError as exc:
        raise ScreenTimeError(
            "SQLite database could not be read. "
            "It may be locked, damaged, or encrypted."
        ) from exc
    finally:
        conn.close()


def _has_tables(conn: sqlite3.Connection, table_names: set[str]) -> bool:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    existing = {str(row[0]) for row in rows}
    return table_names.issubset(existing)


def load_safari_history_events(
    db_path: Path,
    cutoff: datetime | None = None,
    *,
    reference_time: datetime | None = None,
    future_tolerance_days: int = 30,
) -> list[ScreenTimeEvent]:
    if not db_path.exists():
        return []

    try:
        conn = sqlite3.connect(db_path.resolve().as_uri(), uri=True)
    except sqlite3.Error:
        return []

    conn.row_factory = sqlite3.Row
    try:
        if not _has_tables(conn, {"history_items", "history_visits"}):
            return []

        query = """
            SELECT
                hv.id AS visit_id,
                hv.visit_time,
                hv.title,
                hv.load_successful,
                hi.url
            FROM history_visits hv
            JOIN history_items hi ON hi.id = hv.history_item
            WHERE hv.visit_time IS NOT NULL
        """
        params: list[Any] = []
        if cutoff is not None:
            cutoff_core = cutoff.timestamp() - APPLE_SCREEN_TIME_EPOCH
            query += " AND hv.visit_time > ?"
            params.append(cutoff_core)
        query += " ORDER BY hv.visit_time ASC"

        events: list[ScreenTimeEvent] = []
        for row in conn.execute(query, params).fetchall():
            start = coredata_to_datetime(row["visit_time"])
            if start is None:
                continue
            end = start + timedelta(seconds=30)
            if not _is_plausible_event_end(end, reference_time, future_tolerance_days):
                continue
            title = _normalize_text(row["title"])
            url = _normalize_text(row["url"])
            data: dict[str, Any] = {
                "source": "safari_history",
                "source_pk": row["visit_id"],
            }
            if title:
                data["title"] = title
            if url:
                data["url"] = url
            if row["load_successful"] is not None:
                data["load_successful"] = row["load_successful"]
            events.append(
                ScreenTimeEvent(
                    start=start,
                    end=end,
                    app="Safari",
                    data=data,
                    source_table="history_visits",
                    source_pk=row["visit_id"],
                    raw_start=start,
                    raw_end=start,
                    inferred_duration=True,
                )
            )
        return consolidate_screen_time_events(events)
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def load_manifest_app_activity_events(
    csv_path: Path,
    cutoff: datetime | None = None,
    *,
    reference_time: datetime | None = None,
    future_tolerance_days: int = 30,
) -> list[ScreenTimeEvent]:
    if not csv_path.exists():
        return []

    events: list[ScreenTimeEvent] = []
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                bundle_id = _bundle_id_from_app_domain(row.get("domain", ""))
                if not bundle_id:
                    continue
                try:
                    modified = datetime.fromtimestamp(float(row.get("mtime", 0)), tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    continue
                if cutoff is not None and modified <= cutoff:
                    continue
                end = modified + timedelta(seconds=30)
                if not _is_plausible_event_end(end, reference_time, future_tolerance_days):
                    continue
                app_name = _humanize_bundle_id(bundle_id)
                data: dict[str, Any] = {
                    "bundle_id": bundle_id,
                    "source": "app_manifest_mtime",
                    "domain": row.get("domain", ""),
                    "source_path": row.get("relative_path", ""),
                }
                try:
                    data["file_size"] = int(row.get("size", 0))
                except (TypeError, ValueError):
                    pass
                events.append(
                    ScreenTimeEvent(
                        start=modified,
                        end=end,
                        app=app_name,
                        data=data,
                        source_table="Manifest.Files",
                        raw_start=modified,
                        raw_end=modified,
                        inferred_duration=True,
                    )
                )
    except OSError:
        return []
    return consolidate_screen_time_events(events)


def load_window_events_from_files(
    db_paths: list[Path],
    *,
    safari_history_db_path: Path | None = None,
    app_activity_manifest_csv_path: Path | None = None,
    cutoff: datetime | None = None,
    verbose: bool = False,
    reference_time: datetime | None = None,
    future_tolerance_days: int = 30,
) -> list[ScreenTimeEvent]:
    events: list[ScreenTimeEvent] = []
    seen_paths: set[Path] = set()
    for db_path in db_paths:
        resolved = db_path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        events.extend(
            load_screen_time_events(
                db_path,
                cutoff=cutoff,
                verbose=verbose,
                reference_time=reference_time,
                future_tolerance_days=future_tolerance_days,
            )
        )

    if safari_history_db_path is not None:
        events.extend(
            load_safari_history_events(
                safari_history_db_path,
                cutoff=cutoff,
                reference_time=reference_time,
                future_tolerance_days=future_tolerance_days,
            )
        )

    if app_activity_manifest_csv_path is not None:
        events.extend(
            load_manifest_app_activity_events(
                app_activity_manifest_csv_path,
                cutoff=cutoff,
                reference_time=reference_time,
                future_tolerance_days=future_tolerance_days,
            )
        )

    return consolidate_screen_time_events(events)


def derive_afk_events(
    window_events: list[ScreenTimeEvent],
    *,
    afk_threshold_seconds: int = 180,
) -> list[AfkEvent]:
    active_events = [
        event for event in sorted(window_events, key=lambda item: (item.start, item.end))
        if event.end > event.start
    ]
    if not active_events:
        return []

    sessions: list[tuple[datetime, datetime]] = []
    current_start = active_events[0].start
    current_end = active_events[0].end
    for event in active_events[1:]:
        gap_seconds = (event.start - current_end).total_seconds()
        if gap_seconds < afk_threshold_seconds:
            current_end = max(current_end, event.end)
            continue
        sessions.append((current_start, current_end))
        current_start = event.start
        current_end = event.end
    sessions.append((current_start, current_end))

    afk_events: list[AfkEvent] = []
    for index, (start, end) in enumerate(sessions):
        if end > start:
            afk_events.append(
                AfkEvent(
                    start=start,
                    end=end,
                    status="not-afk",
                    data={"source": "derived_from_window"},
                )
            )
        if index + 1 >= len(sessions):
            continue
        next_start = sessions[index + 1][0]
        if (next_start - end).total_seconds() >= afk_threshold_seconds:
            afk_events.append(
                AfkEvent(
                    start=end,
                    end=next_start,
                    status="afk",
                    data={
                        "source": "derived_from_window",
                        "threshold_seconds": afk_threshold_seconds,
                    },
                )
            )
    return afk_events
