from __future__ import annotations

import csv
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .activitywatch_client import ActivityWatchClient
from .config import AppConfig, project_root
from .filesystem import UsageDataFiles, find_usage_data_files
from .screen_time import AfkEvent, ScreenTimeEvent, derive_afk_events, load_window_events_from_files


def _parse_aw_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_fingerprint(event: dict) -> tuple[int, int, str, str, str] | None:
    start = _parse_aw_timestamp(event.get("timestamp"))
    if start is None:
        return None
    try:
        duration_ms = int(round(float(event.get("duration", 0) or 0) * 1000))
    except (TypeError, ValueError):
        duration_ms = 0
    start_ms = int(start.timestamp() * 1000)
    data = event.get("data")
    if not isinstance(data, dict):
        data = {}
    return (
        start_ms,
        duration_ms,
        str(data.get("app", "")),
        str(data.get("bundle_id", "")),
        str(data.get("source", "")),
    )


def _existing_event_fingerprints(events: list[dict]) -> set[tuple[int, int, str, str, str]]:
    fingerprints: set[tuple[int, int, str, str, str]] = set()
    for event in events:
        fingerprint = _event_fingerprint(event)
        if fingerprint is not None:
            fingerprints.add(fingerprint)
    return fingerprints


def _filter_new_payloads(
    payloads: list[dict],
    existing_fingerprints: set[tuple[int, int, str, str, str]],
) -> list[dict]:
    new_payloads: list[dict] = []
    seen = set(existing_fingerprints)
    for payload in payloads:
        fingerprint = _event_fingerprint(payload)
        if fingerprint is not None and fingerprint in seen:
            continue
        if fingerprint is not None:
            seen.add(fingerprint)
        new_payloads.append(payload)
    return new_payloads


def _window_event_payload(event: ScreenTimeEvent) -> dict:
    data = {"app": event.app, "title": event.app}
    if getattr(event, "data", None):
        data.update(event.data)
    data.setdefault("title", event.app)
    return {
        "timestamp": event.start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration": event.duration_seconds,
        "data": data,
    }


def _afk_event_payload(event: AfkEvent) -> dict:
    data = {"status": event.status}
    if event.data:
        data.update(event.data)
    return {
        "timestamp": event.start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration": event.duration_seconds,
        "data": data,
    }


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_debug_copy(db_path: Path, root_dir: Path) -> Path:
    debug_dir = root_dir / "debugOut"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_copy_path = debug_dir / "knowledgeC.decrypted.db"
    shutil.copy2(db_path, debug_copy_path)
    return debug_copy_path


def _write_usage_debug_copies(files: UsageDataFiles, root_dir: Path) -> list[Path]:
    debug_dir = root_dir / "debugOut"
    debug_dir.mkdir(parents=True, exist_ok=True)
    targets = [
        (files.knowledge_db, "knowledgeC.decrypted.db"),
        (files.interaction_db, "interactionC.decrypted.db"),
        (files.safari_history_db, "Safari-History.decrypted.db"),
        (files.app_activity_manifest_csv, "app-domain-activity-manifest.csv"),
        (files.screen_time_agent_plist, "ScreenTimeAgent.plist"),
        (files.screen_time_settings_plist, "ScreenTimeSettingsAgent.plist"),
    ]
    copied: list[Path] = []
    for source_path, target_name in targets:
        if source_path is None or not source_path.exists():
            continue
        target_path = debug_dir / target_name
        shutil.copy2(source_path, target_path)
        copied.append(target_path)
    if not copied and files.primary_db.exists():
        copied.append(_write_debug_copy(files.primary_db, root_dir))
    return copied


def _debug_csv_path(root_dir: Path) -> Path:
    return root_dir / "debugOut" / "aw-watcher-window.recognized-events.csv"


def _write_debug_csv(events, root_dir: Path) -> Path:
    debug_dir = root_dir / "debugOut"
    debug_dir.mkdir(parents=True, exist_ok=True)
    csv_path = _debug_csv_path(root_dir)
    fieldnames = [
        "source_table",
        "source_pk",
        "raw_start_utc",
        "raw_end_utc",
        "inferred_duration",
        "start_utc",
        "end_utc",
        "duration_seconds",
        "count",
        "app",
        "bundle_id",
        "target_bundle_id",
        "title",
        "domain_identifier",
        "sender",
        "account",
        "url",
        "content_url",
        "derived_intent_identifier",
        "source",
        "domain",
        "source_path",
        "file_size",
        "direction",
        "mechanism",
        "is_response",
        "recipient_count",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for event in events:
            data = event.data or {}
            source_pk = data.get("source_pk", getattr(event, "source_pk", ""))
            writer.writerow(
                {
                    "start_utc": _format_dt(event.start),
                    "end_utc": _format_dt(event.end),
                    "source_table": getattr(event, "source_table", "") or "",
                    "source_pk": source_pk or "",
                    "raw_start_utc": _format_dt(getattr(event, "raw_start", None)),
                    "raw_end_utc": _format_dt(getattr(event, "raw_end", None)),
                    "inferred_duration": str(bool(getattr(event, "inferred_duration", False))).lower(),
                    "duration_seconds": f"{event.duration_seconds:.6f}",
                    "count": getattr(event, "count", 1),
                    "app": event.app,
                    "bundle_id": data.get("bundle_id", ""),
                    "target_bundle_id": data.get("target_bundle_id", ""),
                    "title": data.get("title", ""),
                    "domain_identifier": data.get("domain_identifier", ""),
                    "sender": data.get("sender", ""),
                    "account": data.get("account", ""),
                    "url": data.get("url", ""),
                    "content_url": data.get("content_url", ""),
                    "derived_intent_identifier": data.get("derived_intent_identifier", ""),
                    "source": data.get("source", ""),
                    "domain": data.get("domain", ""),
                    "source_path": data.get("source_path", ""),
                    "file_size": data.get("file_size", ""),
                    "direction": data.get("direction", ""),
                    "mechanism": data.get("mechanism", ""),
                    "is_response": data.get("is_response", ""),
                    "recipient_count": data.get("recipient_count", ""),
                }
            )
    return csv_path


def _write_afk_debug_csv(events: list[AfkEvent], root_dir: Path) -> Path:
    debug_dir = root_dir / "debugOut"
    debug_dir.mkdir(parents=True, exist_ok=True)
    csv_path = debug_dir / "aw-watcher-afk.recognized-events.csv"
    fieldnames = ["start_utc", "end_utc", "duration_seconds", "status", "source", "threshold_seconds"]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for event in events:
            data = event.data or {}
            writer.writerow(
                {
                    "start_utc": _format_dt(event.start),
                    "end_utc": _format_dt(event.end),
                    "duration_seconds": f"{event.duration_seconds:.6f}",
                    "status": event.status,
                    "source": data.get("source", ""),
                    "threshold_seconds": data.get("threshold_seconds", ""),
                }
            )
    return csv_path


def _usage_db_paths(files: UsageDataFiles) -> list[Path]:
    paths = [path for path in (files.knowledge_db, files.interaction_db) if path is not None]
    if not paths:
        paths = [files.primary_db]
    return paths


def run_import(config: AppConfig, *, verbose: bool = False) -> int:
    if verbose:
        print(f"Backup folder: {config.backup_base_dir}")
        print(f"ActivityWatch: {config.aw_api_url}")
        print(f"Window bucket: {config.window_bucket_id}")
        print(f"AFK bucket: {config.afk_bucket_id}")
        print(f"Debug mode: {'on' if config.debug_mode else 'off'}")

    usage_files = find_usage_data_files(Path(config.backup_base_dir), config.backup_password)
    if verbose:
        print(f"Primary usage DB: {usage_files.primary_db}")
        if usage_files.knowledge_db:
            print(f"Knowledge DB: {usage_files.knowledge_db}")
        if usage_files.interaction_db:
            print(f"Interaction DB: {usage_files.interaction_db}")
        if usage_files.safari_history_db:
            print(f"Safari History DB: {usage_files.safari_history_db}")
        if usage_files.app_activity_manifest_csv:
            print(f"App activity manifest CSV: {usage_files.app_activity_manifest_csv}")

    if config.debug_mode:
        debug_copy_paths = _write_usage_debug_copies(usage_files, project_root())
        if verbose:
            for debug_copy_path in debug_copy_paths:
                print(f"Debug copy written: {debug_copy_path}")

    reference_time = datetime.fromtimestamp(usage_files.primary_db.stat().st_mtime, tz=timezone.utc)
    future_tolerance_days = 30

    client = ActivityWatchClient(config.aw_api_url)
    if verbose:
        print("Checking/creating ActivityWatch buckets ...")
    client.ensure_bucket(
        config.window_bucket_id,
        bucket_type="currentwindow",
        hostname=config.hostname,
    )
    client.ensure_bucket(
        config.afk_bucket_id,
        bucket_type="afkstatus",
        hostname=config.hostname,
    )
    existing_window_events = client.get_events(config.window_bucket_id)
    existing_afk_events = client.get_events(config.afk_bucket_id)
    last_end = client.get_last_event_end(config.window_bucket_id)
    afk_last_end = client.get_last_event_end(config.afk_bucket_id)
    effective_cutoff = last_end
    if last_end is not None and last_end > reference_time + timedelta(days=future_tolerance_days):
        effective_cutoff = None
        if verbose:
            print(
                "Ignoring ActivityWatch cutoff because it is far in the future "
                f"relative to the backup ({_format_dt(last_end)} > {_format_dt(reference_time + timedelta(days=future_tolerance_days))})."
            )
    if verbose:
        print(f"Last window event in bucket: {_format_dt(last_end)}")
        print(f"Last AFK event in bucket: {_format_dt(afk_last_end)}")
        print("Loading usage events from backup ...")

    source_events = load_window_events_from_files(
        _usage_db_paths(usage_files),
        safari_history_db_path=usage_files.safari_history_db,
        app_activity_manifest_csv_path=usage_files.app_activity_manifest_csv,
        cutoff=None,
        verbose=config.debug_mode,
        reference_time=reference_time,
        future_tolerance_days=future_tolerance_days,
    )
    all_events = source_events
    all_afk_events = derive_afk_events(source_events)
    if config.debug_mode:
        csv_path = _write_debug_csv(all_events, project_root())
        afk_csv_path = _write_afk_debug_csv(all_afk_events, project_root())
        if verbose:
            print(f"Window debug CSV written: {csv_path}")
            print(f"AFK debug CSV written: {afk_csv_path}")

    events = all_events
    if verbose:
        print(f"Found window events: {len(events)}")
        print(f"Found AFK events: {len(all_afk_events)}")
        if events:
            print(f"First event: {_format_dt(events[0].start)} -> {_format_dt(events[0].end)}")
            print(f"Last event: {_format_dt(events[-1].start)} -> {_format_dt(events[-1].end)}")
            print(f"First duration: {events[0].duration_seconds:.1f}s")
            print(f"Last duration: {events[-1].duration_seconds:.1f}s")
            print(f"First app: {events[0].app}")
            print(f"Last app: {events[-1].app}")
    if not events and not all_afk_events:
        if verbose:
            print("No new events to import.")
            if last_end is not None:
                print(
                    "The ActivityWatch cutoff is newer than the data in the backup, "
                    "so every matching row was filtered out."
                )
        return 0

    window_payloads = _filter_new_payloads(
        [_window_event_payload(event) for event in events],
        _existing_event_fingerprints(existing_window_events),
    )
    imported = client.post_events(config.window_bucket_id, window_payloads) if window_payloads else 0
    afk_payloads = _filter_new_payloads(
        [_afk_event_payload(event) for event in all_afk_events],
        _existing_event_fingerprints(existing_afk_events),
    )
    afk_imported = client.post_events(config.afk_bucket_id, afk_payloads) if afk_payloads else 0
    if verbose:
        print(f"Posted window events to ActivityWatch: {imported}")
        print(f"Posted AFK events to ActivityWatch: {afk_imported}")
    print(f"Bucket {config.window_bucket_id}: {imported} events written.")
    print(f"Bucket {config.afk_bucket_id}: {afk_imported} events written.")
    return imported + afk_imported
