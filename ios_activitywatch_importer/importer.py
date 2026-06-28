from __future__ import annotations

from datetime import timezone
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


def run_import(config: AppConfig) -> int:
    db_path = find_knowledge_db(Path(config.backup_base_dir), config.backup_password)
    client = ActivityWatchClient(config.aw_api_url)
    client.ensure_bucket(
        config.bucket_id,
        bucket_type="currentapp",
        hostname=config.hostname,
    )
    last_end = client.get_last_event_end(config.bucket_id)
    events = load_screen_time_events(db_path, cutoff=last_end)
    if not events:
        return 0

    payloads = [_event_payload(event) for event in events]
    return client.post_events(config.bucket_id, payloads)
