from __future__ import annotations

import re
import csv
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import APPLE_SCREEN_TIME_EPOCH


class ScreenTimeError(RuntimeError):
    pass


_ZOBJECT_STREAM_TOKENS = ("app", "focus", "display", "web", "safari", "foreground")

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


def _audit_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
    confidence: float = 1.0
    source_rank: int = 50
    drop_reason: str | None = None
    candidate_source: str | None = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass
class SourceAudit:
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    dropped: dict[str, dict[str, int]] = field(default_factory=dict)

    def _file(self, path: Path) -> dict[str, Any]:
        key = str(path)
        if key not in self.files:
            self.files[key] = {"tables": {}, "streams": {}, "date_ranges": {}}
        return self.files[key]

    def record_table(
        self,
        path: Path,
        table_name: str,
        row_count: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        entry = self._file(path)
        entry["tables"][table_name] = row_count
        if start is not None or end is not None:
            entry["date_ranges"][table_name] = {
                "start": _audit_dt(start),
                "end": _audit_dt(end),
            }

    def record_stream(self, path: Path, stream_name: str, row_count: int) -> None:
        self._file(path)["streams"][stream_name] = row_count

    def record_source(self, source: str, *, candidates: int = 0, accepted: int = 0) -> None:
        counts = self.source_counts.setdefault(source, {"candidates": 0, "accepted": 0})
        counts["candidates"] += candidates
        counts["accepted"] += accepted

    def record_drop(self, source: str, reason: str, count: int = 1) -> None:
        reasons = self.dropped.setdefault(source, {})
        reasons[reason] = reasons.get(reason, 0) + count

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "source_counts": self.source_counts,
            "dropped": self.dropped,
        }


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
                    "confidence": event.confidence,
                    "source_rank": event.source_rank,
                    "drop_reason": event.drop_reason,
                    "candidate_source": event.candidate_source,
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
            current["confidence"] = max(float(current["confidence"]), float(event.confidence))
            current["source_rank"] = min(int(current["source_rank"]), int(event.source_rank))
            if current["candidate_source"] in (None, ""):
                current["candidate_source"] = event.candidate_source
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
                "confidence": event.confidence,
                "source_rank": event.source_rank,
                "drop_reason": event.drop_reason,
                "candidate_source": event.candidate_source,
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
                confidence=float(session["confidence"]),
                source_rank=int(session["source_rank"]),
                drop_reason=session["drop_reason"],
                candidate_source=session["candidate_source"],
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


def _event_source_metadata(source_table: str, source: str | None = None) -> tuple[int, float, str]:
    if source_table == "ZOBJECT":
        return 10, 1.0, "coreduet_zobject"
    if source_table == "ZINTERACTIONS":
        return 30, 0.8, "coreduet_interactions"
    if source_table == "history_visits":
        return 40, 0.6, "safari_history"
    if source_table == "Manifest.Files" or source == "app_manifest_mtime":
        return 90, 0.25, "manifest_fallback"
    return 50, 0.5, source or source_table


def _with_source_metadata(
    event: ScreenTimeEvent,
    *,
    source_rank: int,
    confidence: float,
    candidate_source: str,
    drop_reason: str | None = None,
) -> ScreenTimeEvent:
    data = dict(event.data or {})
    data["confidence"] = confidence
    data["source_rank"] = source_rank
    data["candidate_source"] = candidate_source
    if drop_reason:
        data["drop_reason"] = drop_reason
    return ScreenTimeEvent(
        start=event.start,
        end=event.end,
        app=event.app,
        data=data,
        count=event.count,
        source_table=event.source_table,
        source_pk=event.source_pk,
        raw_start=event.raw_start,
        raw_end=event.raw_end,
        inferred_duration=event.inferred_duration,
        confidence=confidence,
        source_rank=source_rank,
        drop_reason=drop_reason,
        candidate_source=candidate_source,
    )


def _source_name(event: ScreenTimeEvent) -> str:
    data = event.data or {}
    source = _normalize_text(data.get("source"))
    rank, _confidence, candidate = _event_source_metadata(event.source_table or "", source)
    if event.candidate_source:
        return event.candidate_source
    if candidate:
        return candidate
    return f"rank_{rank}"


def _event_rank(event: ScreenTimeEvent) -> int:
    data = event.data or {}
    if event.source_rank != 50:
        return event.source_rank
    try:
        return int(data.get("source_rank", 50))
    except (TypeError, ValueError):
        return _event_source_metadata(event.source_table or "", _normalize_text(data.get("source")))[0]


def _events_are_near(left: ScreenTimeEvent, right: ScreenTimeEvent, window_seconds: int) -> bool:
    if left.end >= right.start and right.end >= left.start:
        return True
    gap_seconds = min(
        abs((left.start - right.end).total_seconds()),
        abs((right.start - left.end).total_seconds()),
    )
    return gap_seconds <= window_seconds


def _event_overlap_seconds(left: ScreenTimeEvent, right: ScreenTimeEvent) -> float:
    start = max(left.start, right.start)
    end = min(left.end, right.end)
    return max(0.0, (end - start).total_seconds())


def _consolidate_events_by_session_key(
    events: list[ScreenTimeEvent],
    *,
    session_gap_seconds: int,
) -> list[ScreenTimeEvent]:
    grouped: dict[tuple[str, str], list[ScreenTimeEvent]] = {}
    for event in events:
        grouped.setdefault(_session_key(event), []).append(event)

    consolidated: list[ScreenTimeEvent] = []
    for group_events in grouped.values():
        consolidated.extend(
            consolidate_screen_time_events(group_events, session_gap_seconds=session_gap_seconds)
        )
    return sorted(consolidated, key=lambda item: (item.start, item.end, item.app))


def _consolidate_events_by_source(events: list[ScreenTimeEvent]) -> list[ScreenTimeEvent]:
    grouped: dict[tuple[int, str, str], list[ScreenTimeEvent]] = {}
    for event in events:
        key = (_event_rank(event), _source_name(event), event.source_table or "")
        grouped.setdefault(key, []).append(event)

    consolidated: list[ScreenTimeEvent] = []
    for key, group_events in grouped.items():
        _rank, source, _source_table = key
        if source == "manifest_fallback":
            consolidated.extend(
                _consolidate_events_by_session_key(
                    group_events,
                    session_gap_seconds=1800,
                )
            )
            continue
        consolidated.extend(consolidate_screen_time_events(group_events))
    return consolidated


def _is_shadowed_by_higher_rank_source(
    event: ScreenTimeEvent,
    accepted: list[ScreenTimeEvent],
    *,
    fallback_near_seconds: int,
    long_fallback_seconds: int,
    covered_fraction_threshold: float,
) -> bool:
    higher_rank_neighbors = [
        existing
        for existing in accepted
        if _event_rank(existing) < _event_rank(event)
        and _events_are_near(existing, event, fallback_near_seconds)
    ]
    if not higher_rank_neighbors:
        return False

    if event.duration_seconds < long_fallback_seconds:
        return True

    covered_seconds = sum(_event_overlap_seconds(existing, event) for existing in higher_rank_neighbors)
    covered_fraction = covered_seconds / max(event.duration_seconds, 1.0)
    return covered_fraction >= covered_fraction_threshold


def rank_and_consolidate_events(
    events: list[ScreenTimeEvent],
    *,
    audit: SourceAudit | None = None,
    fallback_near_seconds: int = 120,
    long_fallback_seconds: int = 300,
    covered_fraction_threshold: float = 0.5,
) -> list[ScreenTimeEvent]:
    ranked: list[ScreenTimeEvent] = []
    for event in events:
        source_rank, confidence, candidate_source = _event_source_metadata(
            event.source_table or "",
            _normalize_text((event.data or {}).get("source")),
        )
        ranked.append(
            _with_source_metadata(
                event,
                source_rank=event.source_rank if event.source_rank != 50 else source_rank,
                confidence=event.confidence if event.confidence != 1.0 else confidence,
                candidate_source=event.candidate_source or candidate_source,
            )
        )

    ranked = _consolidate_events_by_source(ranked)
    accepted: list[ScreenTimeEvent] = []
    dropped: list[ScreenTimeEvent] = []
    for event in sorted(ranked, key=lambda item: (_event_rank(item), item.start, -item.duration_seconds)):
        source = _source_name(event)
        if audit is not None:
            audit.record_source(f"final:{source}", candidates=1)
        suppress = _is_shadowed_by_higher_rank_source(
            event,
            accepted,
            fallback_near_seconds=fallback_near_seconds,
            long_fallback_seconds=long_fallback_seconds,
            covered_fraction_threshold=covered_fraction_threshold,
        )
        if suppress:
            dropped_event = _with_source_metadata(
                event,
                source_rank=_event_rank(event),
                confidence=event.confidence,
                candidate_source=source,
                drop_reason="shadowed_by_higher_rank_source",
            )
            dropped.append(dropped_event)
            if audit is not None:
                audit.record_drop(f"final:{source}", "shadowed_by_higher_rank_source")
            continue
        accepted.append(event)
        if audit is not None:
            audit.record_source(f"final:{source}", accepted=1)

    consolidated = consolidate_screen_time_events(accepted)
    return sorted(consolidated, key=lambda item: (item.start, item.end, item.app))


def _load_events_from_zobject(
    conn: sqlite3.Connection,
    schema: dict[str, list[str]],
    cutoff: datetime | None,
    *,
    db_path: Path | None = None,
    audit: SourceAudit | None = None,
    verbose: bool = False,
    reference_time: datetime | None = None,
    future_tolerance_days: int = 30,
) -> list[ScreenTimeEvent]:
    query = 'SELECT * FROM "ZOBJECT" WHERE ZSTARTDATE IS NOT NULL AND ZENDDATE IS NOT NULL'
    params: list[Any] = []
    if cutoff is not None:
        cutoff_core = cutoff.timestamp() - APPLE_SCREEN_TIME_EPOCH
        query += " AND ZENDDATE > ?"
        params.append(cutoff_core)
    query += " ORDER BY ZSTARTDATE ASC"

    events: list[ScreenTimeEvent] = []
    rows = conn.execute(query, params).fetchall()
    _debug_print(verbose, f"ZOBJECT candidate rows after cutoff: {len(rows)}")
    if audit is not None:
        audit.record_source("coreduet_zobject", candidates=len(rows))
    for row in rows:
        row_dict = {key: row[key] for key in row.keys()}
        stream_name = _normalize_text(row_dict.get("ZSTREAMNAME")) or ""
        if stream_name and not any(token in stream_name.lower() for token in _ZOBJECT_STREAM_TOKENS):
            if audit is not None:
                audit.record_drop("coreduet_zobject", f"unsupported_stream:{stream_name}")
            continue
        start = coredata_to_datetime(row["ZSTARTDATE"])
        end = coredata_to_datetime(row["ZENDDATE"])
        if start is None or end is None or end <= start:
            if audit is not None:
                audit.record_drop("coreduet_zobject", "invalid_time_range")
            continue
        if not _is_plausible_event_end(end, reference_time, future_tolerance_days):
            if audit is not None:
                audit.record_drop("coreduet_zobject", "future_outlier")
            continue
        app_name = _extract_app_name_from_row(conn, row, schema)
        if not _is_meaningful_app_name(app_name):
            if audit is not None:
                audit.record_drop("coreduet_zobject", "unknown_app")
            continue
        data = _event_data_from_row(row_dict) or {}
        data["source"] = "zobject"
        if stream_name:
            data["stream_name"] = stream_name
        if "Z_PK" in row.keys():
            data["source_pk"] = row["Z_PK"]
        events.append(
            ScreenTimeEvent(
                start=start,
                end=end,
                app=app_name,
                data=data,
                source_table="ZOBJECT",
                source_pk=row["Z_PK"] if "Z_PK" in row.keys() else None,
                raw_start=start,
                raw_end=end,
                confidence=1.0,
                source_rank=10,
                candidate_source="coreduet_zobject",
            )
        )
    if audit is not None:
        audit.record_source("coreduet_zobject", accepted=len(events))
    return events


def _load_events_from_zinteractions(
    conn: sqlite3.Connection,
    schema: dict[str, list[str]],
    cutoff: datetime | None,
    *,
    audit: SourceAudit | None = None,
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
    if audit is not None:
        audit.record_source("coreduet_interactions", candidates=len(rows))
    for row in rows:
        start = coredata_to_datetime(row["ZSTARTDATE"])
        end = coredata_to_datetime(row["ZENDDATE"])
        if start is None or end is None:
            if audit is not None:
                audit.record_drop("coreduet_interactions", "invalid_time_range")
            continue
        if end < start:
            if audit is not None:
                audit.record_drop("coreduet_interactions", "negative_duration")
            continue
        if not _is_plausible_event_end(end, reference_time, future_tolerance_days):
            if audit is not None:
                audit.record_drop("coreduet_interactions", "future_outlier")
            continue
        row_dict = {key: row[key] for key in row.keys()}
        if _is_calendar_interaction(row_dict):
            if audit is not None:
                audit.record_drop("coreduet_interactions", "calendar_interaction")
            continue
        app_name = _extract_app_name_from_row(conn, row, schema)
        if not _is_meaningful_app_name(app_name) and app_name != "unknown":
            if audit is not None:
                audit.record_drop("coreduet_interactions", "unknown_app")
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
                confidence=0.8,
                source_rank=30,
                candidate_source="coreduet_interactions",
            )
        )
    if audit is not None:
        audit.record_source("coreduet_interactions", accepted=len(events))
    return events


def load_screen_time_events(
    db_path: Path,
    cutoff: datetime | None = None,
    *,
    audit: SourceAudit | None = None,
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
        if audit is not None:
            for table_name in sorted(schema):
                start_dt = end_dt = None
                columns_upper = {column.upper() for column in schema[table_name]}
                if {"ZSTARTDATE", "ZENDDATE"}.issubset(columns_upper):
                    start_dt, end_dt = _table_date_range(conn, table_name, "ZSTARTDATE", "ZENDDATE")
                audit.record_table(db_path, table_name, _table_row_count(conn, table_name), start_dt, end_dt)
            if "ZOBJECT" in schema and "ZSTREAMNAME" in {column.upper() for column in schema["ZOBJECT"]}:
                stream_rows = conn.execute(
                    'SELECT ZSTREAMNAME, COUNT(*) FROM "ZOBJECT" GROUP BY ZSTREAMNAME ORDER BY COUNT(*) DESC'
                ).fetchall()
                for stream_name, row_count in stream_rows:
                    audit.record_stream(db_path, str(stream_name or ""), int(row_count or 0))
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
        events: list[ScreenTimeEvent] = []
        if "ZOBJECT" in schema:
            events.extend(
                _load_events_from_zobject(
                    conn,
                    schema,
                    cutoff,
                    db_path=db_path,
                    audit=audit,
                    verbose=verbose,
                    reference_time=reference_time,
                    future_tolerance_days=future_tolerance_days,
                )
            )
        if "ZINTERACTIONS" in schema:
            events.extend(
                _load_events_from_zinteractions(
                    conn,
                    schema,
                    cutoff,
                    audit=audit,
                    verbose=verbose,
                    reference_time=reference_time,
                    future_tolerance_days=future_tolerance_days,
                )
            )
        if events or "ZOBJECT" in schema or "ZINTERACTIONS" in schema:
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
    audit: SourceAudit | None = None,
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
            if audit is not None:
                audit.record_drop("safari_history", "missing_history_tables")
            return []
        if audit is not None:
            audit.record_table(db_path, "history_items", _table_row_count(conn, "history_items"))
            audit.record_table(db_path, "history_visits", _table_row_count(conn, "history_visits"))

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
        rows = conn.execute(query, params).fetchall()
        if audit is not None:
            audit.record_source("safari_history", candidates=len(rows))
        for row in rows:
            start = coredata_to_datetime(row["visit_time"])
            if start is None:
                if audit is not None:
                    audit.record_drop("safari_history", "invalid_time")
                continue
            end = start + timedelta(seconds=30)
            if not _is_plausible_event_end(end, reference_time, future_tolerance_days):
                if audit is not None:
                    audit.record_drop("safari_history", "future_outlier")
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
                    confidence=0.6,
                    source_rank=40,
                    candidate_source="safari_history",
                )
            )
        if audit is not None:
            audit.record_source("safari_history", accepted=len(events))
        return consolidate_screen_time_events(events)
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def load_manifest_app_activity_events(
    csv_path: Path,
    cutoff: datetime | None = None,
    *,
    audit: SourceAudit | None = None,
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
                if audit is not None:
                    audit.record_source("manifest_fallback", candidates=1)
                bundle_id = _bundle_id_from_app_domain(row.get("domain", ""))
                if not bundle_id:
                    if audit is not None:
                        audit.record_drop("manifest_fallback", "unsupported_domain")
                    continue
                try:
                    modified = datetime.fromtimestamp(float(row.get("mtime", 0)), tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    if audit is not None:
                        audit.record_drop("manifest_fallback", "invalid_mtime")
                    continue
                if cutoff is not None and modified <= cutoff:
                    if audit is not None:
                        audit.record_drop("manifest_fallback", "before_cutoff")
                    continue
                end = modified + timedelta(seconds=30)
                if not _is_plausible_event_end(end, reference_time, future_tolerance_days):
                    if audit is not None:
                        audit.record_drop("manifest_fallback", "future_outlier")
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
                        confidence=0.25,
                        source_rank=90,
                        candidate_source="manifest_fallback",
                    )
                )
    except OSError:
        return []
    if audit is not None:
        audit.record_source("manifest_fallback", accepted=len(events))
    return _consolidate_events_by_session_key(events, session_gap_seconds=1800)


def load_window_events_from_files(
    db_paths: list[Path],
    *,
    safari_history_db_path: Path | None = None,
    app_activity_manifest_csv_path: Path | None = None,
    cutoff: datetime | None = None,
    audit: SourceAudit | None = None,
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
                audit=audit,
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
                audit=audit,
                reference_time=reference_time,
                future_tolerance_days=future_tolerance_days,
            )
        )

    if app_activity_manifest_csv_path is not None:
        events.extend(
            load_manifest_app_activity_events(
                app_activity_manifest_csv_path,
                cutoff=cutoff,
                audit=audit,
                reference_time=reference_time,
                future_tolerance_days=future_tolerance_days,
            )
        )

    return rank_and_consolidate_events(events, audit=audit)


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
