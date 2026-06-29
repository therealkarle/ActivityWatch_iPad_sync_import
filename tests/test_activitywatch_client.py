from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ios_activitywatch_importer.activitywatch_client import ActivityWatchClient


class _Handler(BaseHTTPRequestHandler):
    bucket_exists = False
    received_events = []

    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path == "/api/0/buckets/aw-watcher-ios":
            if type(self).bucket_exists:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id":"aw-watcher-ios","type":"currentapp"}')
            else:
                self.send_response(404)
                self.end_headers()
            return
        if self.path == "/api/0/buckets/aw-watcher-ios/events":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "duration": 5,
                    "data": {"app": "Safari"},
                }
            ]
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()

    def do_PUT(self):
        if self.path == "/api/0/buckets/aw-watcher-ios":
            type(self).bucket_exists = True
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/0/buckets/aw-watcher-ios/events":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            type(self).received_events.append(json.loads(body.decode("utf-8")))
            self.send_response(201)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


class ActivityWatchClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/api/0"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def test_bucket_and_events(self) -> None:
        client = ActivityWatchClient(self.base_url)
        client.ensure_bucket("aw-watcher-ios", hostname="test-iphone")
        last = client.get_last_event_end("aw-watcher-ios")
        self.assertIsNotNone(last)
        count = client.post_events(
            "aw-watcher-ios",
            [
                {
                    "timestamp": "2026-01-01T00:00:10Z",
                    "duration": 1,
                    "data": {"app": "Notes"},
                }
            ],
        )
        self.assertEqual(count, 1)
        self.assertEqual(_Handler.received_events[-1]["data"]["app"], "Notes")
