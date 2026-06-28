from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ActivityWatchError(RuntimeError):
    pass


def _isoformat_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso_datetime(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(text)


@dataclass(frozen=True)
class ActivityWatchClient:
    api_url: str

    def _request(self, method: str, path: str, payload: Any | None = None) -> tuple[int, Any]:
        url = f"{self.api_url.rstrip('/')}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
                if not body.strip():
                    return response.status, None
                return response.status, json.loads(body)
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
                if body.strip():
                    try:
                        parsed = json.loads(body)
                    except json.JSONDecodeError:
                        parsed = body
                else:
                    parsed = None
                return exc.code, parsed
            finally:
                exc.close()
        except URLError as exc:
            raise ActivityWatchError(f"ActivityWatch nicht erreichbar: {exc.reason}") from exc

    def ensure_bucket(
        self,
        bucket_id: str,
        bucket_type: str = "currentapp",
        hostname: str = "unknown",
    ) -> None:
        status, _ = self._request("GET", f"/buckets/{bucket_id}")
        if status == 200:
            return
        if status != 404:
            raise ActivityWatchError(f"Bucket-Prüfung fehlgeschlagen (HTTP {status}).")

        payload = {
            "client": "ios-screen-time-importer",
            "hostname": hostname,
            "type": bucket_type,
        }
        status, _ = self._request("POST", f"/buckets/{bucket_id}", payload)
        if status not in (200, 201, 204, 304):
            raise ActivityWatchError(f"Bucket-Erstellung fehlgeschlagen (HTTP {status}).")

    def get_last_event_end(self, bucket_id: str) -> datetime | None:
        status, body = self._request("GET", f"/buckets/{bucket_id}/events")
        if status == 404:
            return None
        if status != 200:
            raise ActivityWatchError(f"Events-Abfrage fehlgeschlagen (HTTP {status}).")
        if not isinstance(body, list) or not body:
            return None

        latest_end: datetime | None = None
        for event in body:
            if not isinstance(event, dict):
                continue
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            start = _parse_iso_datetime(timestamp)
            duration = event.get("duration", 0)
            try:
                duration_seconds = float(duration or 0)
            except (TypeError, ValueError):
                duration_seconds = 0.0
            end = start + timedelta(seconds=duration_seconds)
            if latest_end is None or end > latest_end:
                latest_end = end
        return latest_end

    def post_event(self, bucket_id: str, event: dict[str, Any]) -> None:
        status, body = self._request("POST", f"/buckets/{bucket_id}/events", event)
        if status not in (200, 201, 204):
            raise ActivityWatchError(
                f"Event-Import fehlgeschlagen (HTTP {status}): {body if body is not None else ''}".strip()
            )

    def post_events(self, bucket_id: str, events: list[dict[str, Any]]) -> int:
        count = 0
        for event in events:
            self.post_event(bucket_id, event)
            count += 1
        return count
