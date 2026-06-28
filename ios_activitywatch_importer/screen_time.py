from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import APPLE_SCREEN_TIME_EPOCH


class ScreenTimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScreenTimeEvent:
    start: datetime
    end: datetime
    app: str

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
        "ZDISPLAYNAME",
        "ZNAME",
        "ZVALUESTRING",
    ]

    textual_values: list[str] = []
    for column in direct_priority:
        if column in row_dict:
            text = _normalize_text(row_dict[column])
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


def load_screen_time_events(db_path: Path, cutoff: datetime | None = None) -> list[ScreenTimeEvent]:
    if not db_path.exists():
        raise ScreenTimeError(f"SQLite-Datei nicht gefunden: {db_path}")

    try:
        conn = sqlite3.connect(db_path.resolve().as_uri(), uri=True)
    except sqlite3.Error as exc:
        raise ScreenTimeError(f"SQLite-Datenbank kann nicht geöffnet werden: {exc}") from exc

    conn.row_factory = sqlite3.Row
    try:
        schema = {
            row[0]: _table_columns(conn, row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "ZOBJECT" not in schema:
            raise ScreenTimeError("Tabelle ZOBJECT wurde in der SQLite-Datenbank nicht gefunden.")

        query = 'SELECT * FROM "ZOBJECT" WHERE ZSTREAMNAME = ? AND ZSTARTDATE IS NOT NULL AND ZENDDATE IS NOT NULL'
        params: list[Any] = ["/app/inFocus"]
        if cutoff is not None:
            cutoff_core = cutoff.timestamp() - APPLE_SCREEN_TIME_EPOCH
            query += " AND ZENDDATE > ?"
            params.append(cutoff_core)
        query += " ORDER BY ZSTARTDATE ASC"

        events: list[ScreenTimeEvent] = []
        rows = conn.execute(query, params).fetchall()
        for row in rows:
            start = coredata_to_datetime(row["ZSTARTDATE"])
            end = coredata_to_datetime(row["ZENDDATE"])
            if start is None or end is None or end <= start:
                continue
            app_name = _extract_app_name_from_row(conn, row, schema)
            events.append(ScreenTimeEvent(start=start, end=end, app=app_name))
        return events
    except sqlite3.DatabaseError as exc:
        raise ScreenTimeError(
            "SQLite-Datenbank konnte nicht gelesen werden. "
            "Sie kann gesperrt, beschädigt oder verschlüsselt sein."
        ) from exc
    finally:
        conn.close()
