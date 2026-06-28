from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ios_activitywatch_importer.config import (
    DEFAULT_CONFIG_EXAMPLE,
    config_example_path,
    config_path,
    ensure_config_example,
    load_config,
)


class ConfigTests(unittest.TestCase):
    def test_ensure_example_writes_expected_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            example = ensure_config_example(root)
            self.assertEqual(example, config_example_path(root))
            data = json.loads(example.read_text(encoding="utf-8"))
            self.assertEqual(data, DEFAULT_CONFIG_EXAMPLE)

    def test_missing_config_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_config_example(root)
            with self.assertRaises(Exception) as ctx:
                load_config(root)
            self.assertIn("config.json is missing", str(ctx.exception))

    def test_loads_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file = config_path(root)
            config_file.write_text(
                json.dumps(
                    {
                        "backup_base_dir": "C:\\Users\\Test\\AppData\\Roaming\\Apple Computer\\MobileSync\\Backup\\",
                        "backup_password": "",
                        "aw_api_url": "http://localhost:5600/api/0",
                        "bucket_id": "aw-watcher-ios",
                        "hostname": "test-iphone",
                        "debug_mode": True,
                    }
                ),
                encoding="utf-8",
            )
            cfg = load_config(root)
            self.assertEqual(cfg.bucket_id, "aw-watcher-ios")
            self.assertEqual(cfg.aw_api_url, "http://localhost:5600/api/0")
            self.assertEqual(cfg.hostname, "test-iphone")
            self.assertIsNone(cfg.backup_password)
            self.assertTrue(cfg.debug_mode)

    def test_loads_backup_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file = config_path(root)
            config_file.write_text(
                json.dumps(
                    {
                        "backup_base_dir": "C:\\Users\\Test\\AppData\\Roaming\\Apple Computer\\MobileSync\\Backup\\",
                        "backup_password": "secret",
                        "aw_api_url": "http://localhost:5600/api/0",
                        "bucket_id": "aw-watcher-ios",
                        "hostname": "test-iphone",
                    }
                ),
                encoding="utf-8",
            )
            cfg = load_config(root)
            self.assertEqual(cfg.backup_password, "secret")
