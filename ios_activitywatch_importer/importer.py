from __future__ import annotations

import csv
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .activitywatch_client import ActivityWatchClient
from .config import AppConfig, project_root
from .filesystem import find_knowledge_db
from .screen_time import load_screen_time_events


def _event_payload(event) -> dict:
    data = {"app": event.app}
    if getattr(event, "data", None):
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


def _debug_csv_path(root_dir: Path) -> Path:
    return root_dir / "debugOut" / "knowledgeC.recognized-events.csv"


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
                }
            )
    return csv_path


def run_import(config: AppConfig, *, verbose: bool = False) -> int:
    if verbose:
        print(f"Backup folder: {config.backup_base_dir}")
        print(f"ActivityWatch: {config.aw_api_url}")
        print(f"Bucket: {config.bucket_id}")
        print(f"Debug mode: {'on' if config.debug_mode else 'off'}")

    db_path = find_knowledge_db(Path(config.backup_base_dir), config.backup_password)
    if verbose:
        source_label = "knowledgeC.db" if db_path.name.lower().startswith("knowledgec") else db_path.name
        print(f"Using file: {db_path} ({source_label})")

    if config.debug_mode:
        debug_copy_path = _write_debug_copy(db_path, project_root())
        if verbose:
            print(f"Debug copy written: {debug_copy_path}")

    reference_time = datetime.fromtimestamp(db_path.stat().st_mtime, tz=timezone.utc)
    future_tolerance_days = 30

    client = ActivityWatchClient(config.aw_api_url)
    if verbose:
        print("Checking/creating ActivityWatch bucket ...")
    client.ensure_bucket(
        config.bucket_id,
        bucket_type="currentwindow",
        hostname=config.hostname,
    )
    last_end = client.get_last_event_end(config.bucket_id)
    effective_cutoff = last_end
    if last_end is not None and last_end > reference_time + timedelta(days=future_tolerance_days):
        effective_cutoff = None
        if verbose:
            print(
                "Ignoring ActivityWatch cutoff because it is far in the future "
                f"relative to the backup ({_format_dt(last_end)} > {_format_dt(reference_time + timedelta(days=future_tolerance_days))})."
            )
    if verbose:
        print(f"Last event in bucket: {_format_dt(last_end)}")
        print("Loading Screen Time events from backup ...")

    all_events = load_screen_time_events(
        db_path,
        cutoff=effective_cutoff,
        verbose=config.debug_mode,
        reference_time=reference_time,
        future_tolerance_days=future_tolerance_days,
    )
    if config.debug_mode:
        csv_path = _write_debug_csv(all_events, project_root())
        if verbose:
            print(f"Debug CSV written: {csv_path}")

    events = all_events
    if verbose:
        print(f"Found events: {len(events)}")
        if events:
            print(f"First event: {_format_dt(events[0].start)} -> {_format_dt(events[0].end)}")
            print(f"Last event: {_format_dt(events[-1].start)} -> {_format_dt(events[-1].end)}")
            print(f"First duration: {events[0].duration_seconds:.1f}s")
            print(f"Last duration: {events[-1].duration_seconds:.1f}s")
            print(f"First app: {events[0].app}")
            print(f"Last app: {events[-1].app}")
    if not events:
        if verbose:
            print("No new events to import.")
            if last_end is not None:
                print(
                    "The ActivityWatch cutoff is newer than the data in the backup, "
                    "so every matching row was filtered out."
                )
        print(f"Bucket {config.bucket_id}: 0 events written.")
        return 0

    payloads = [_event_payload(event) for event in events]
    imported = client.post_events(config.bucket_id, payloads)
    if verbose:
        print(f"Posted to ActivityWatch: {imported}")
    print(f"Bucket {config.bucket_id}: {imported} events written.")
    return imported
