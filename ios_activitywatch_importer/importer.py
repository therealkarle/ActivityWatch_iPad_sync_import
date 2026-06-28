from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .activitywatch_client import ActivityWatchClient
from .config import AppConfig
from .filesystem import find_knowledge_db
from .screen_time import load_screen_time_events


def _event_payload(event) -> dict:
    return {
        "timestamp": event.start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration": event.duration_seconds,
        "data": {"app": event.app},
    }


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run_import(config: AppConfig, *, verbose: bool = False) -> int:
    if verbose:
        print(f"Backup folder: {config.backup_base_dir}")
        print(f"ActivityWatch: {config.aw_api_url}")
        print(f"Bucket: {config.bucket_id}")

    db_path = find_knowledge_db(Path(config.backup_base_dir), config.backup_password)
    if verbose:
        source_label = "knowledgeC.db" if db_path.name.lower().startswith("knowledgec") else db_path.name
        print(f"Using file: {db_path} ({source_label})")

    client = ActivityWatchClient(config.aw_api_url)
    if verbose:
        print("Checking/creating ActivityWatch bucket ...")
    client.ensure_bucket(
        config.bucket_id,
        bucket_type="currentapp",
        hostname=config.hostname,
    )
    last_end = client.get_last_event_end(config.bucket_id)
    if verbose:
        print(f"Last event in bucket: {_format_dt(last_end)}")
        print("Loading Screen Time events from backup ...")

    events = load_screen_time_events(db_path, cutoff=last_end)
    if verbose:
        print(f"Found events: {len(events)}")
        if events:
            print(f"First event: {_format_dt(events[0].start)} -> {_format_dt(events[0].end)}")
            print(f"Last event: {_format_dt(events[-1].start)} -> {_format_dt(events[-1].end)}")
            print(f"First app: {events[0].app}")
            print(f"Last app: {events[-1].app}")
    if not events:
        if verbose:
            print("No new events to import.")
        return 0

    payloads = [_event_payload(event) for event in events]
    imported = client.post_events(config.bucket_id, payloads)
    if verbose:
        print(f"Posted to ActivityWatch: {imported}")
    return imported
