import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import db
from app.scheduler import (
    DEFAULTS, due_scheduled_slot, find_candidates, get_scheduler_settings,
    preview_candidates, run_scheduler, save_scheduler_settings, validate_settings,
)


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "media"
        self.root.mkdir()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "optimizer.sqlite3"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def add_video(self, name, *, size, bitrate, classification="untracked",
                  container="mov,mp4,m4a,3gp,3g2,mj2"):
        path = self.root / name
        path.write_bytes(b"x" * size)
        stat = path.stat()
        with db.connect() as connection:
            db.upsert_video(connection, {
                "path": str(path), "root": str(self.root), "relative_path": name,
                "extension": ".mp4", "size_bytes": size, "mtime_ns": stat.st_mtime_ns,
                "duration_seconds": 120, "bit_rate": bitrate, "video_codec": "hevc",
                "audio_streams": 1, "subtitle_streams": 0, "data_streams": 0,
                "container": container, "classification": classification, "probe_error": None, "present": 1,
                "last_scan_id": "test", "scanned_at": "2026-01-01T00:00:00+00:00",
                "first_seen_at": "2026-01-01T00:00:00+00:00",
            })
            return connection.execute("SELECT * FROM videos WHERE path=?", (str(path),)).fetchone()

    def test_defaults_are_disabled_and_round_trip(self):
        defaults = get_scheduler_settings()
        self.assertFalse(defaults["enabled"])
        self.assertFalse(defaults["auto_replace_ready"])
        self.assertFalse(defaults["cleanup_backups_enabled"])
        self.assertEqual(defaults["backup_retention_days"], 30)
        values = dict(DEFAULTS, enabled=True, minimum_size_gb=300 / 1024, minimum_size_unit="mb")
        saved = save_scheduler_settings(values)
        self.assertTrue(saved["enabled"])
        self.assertEqual(get_scheduler_settings()["minimum_size_gb"], 300 / 1024)
        self.assertEqual(get_scheduler_settings()["minimum_size_unit"], "mb")

    def test_backup_retention_is_validated(self):
        with self.assertRaisesRegex(ValueError, "срок хранения резервов"):
            validate_settings(dict(DEFAULTS, backup_retention_days=0))

    def test_conditions_are_combined_and_preview_does_not_create_jobs(self):
        self.add_video("small.mp4", size=100, bitrate=40_000_000)
        matching = self.add_video("large.mp4", size=300, bitrate=40_000_000)
        self.add_video("slow.mp4", size=300, bitrate=10_000_000)
        settings = dict(
            DEFAULTS, minimum_size_gb=200 / 1024 ** 3,
            minimum_bitrate_mbps=30, minimum_age_hours=0,
        )
        with patch("app.scheduler.media_roots", return_value=[self.root.resolve()]):
            rows, total = find_candidates(settings, enqueue_limit=20)
            preview = preview_candidates(settings)
        self.assertEqual(total, 1)
        self.assertEqual([row["id"] for row in rows], [matching["id"]])
        self.assertEqual(preview["total"], 1)
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM optimization_jobs").fetchone()[0], 0)

    def test_existing_job_and_handbrake_tag_are_excluded(self):
        processed = self.add_video("processed.mp4", size=300, bitrate=40_000_000)
        self.add_video("handbrake.mp4", size=300, bitrate=40_000_000, classification="historical_handbrake")
        with db.connect() as connection:
            connection.execute(
                """
                INSERT INTO optimization_jobs(video_id,source_path,source_size,source_mtime_ns,
                    preset,encoder,encoder_settings,minimum_saving_percent,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (processed["id"], processed["path"], processed["size_bytes"], processed["mtime_ns"],
                 "Creator", "x265", "{}", 20, "failed", "2026-08-01T00:00:00+00:00"),
            )
        settings = dict(DEFAULTS, minimum_size_gb=0, minimum_bitrate_mbps=0, minimum_age_hours=0)
        with patch("app.scheduler.media_roots", return_value=[self.root.resolve()]):
            rows, total = find_candidates(settings, enqueue_limit=20)
        self.assertEqual(total, 0)
        self.assertEqual(rows, [])

    def test_schedule_uses_local_time_and_grace(self):
        settings = validate_settings(dict(
            DEFAULTS, enabled=True, time="04:00", missed_run_grace_minutes=30,
            timezone="Asia/Krasnoyarsk",
        ))
        zone = ZoneInfo("Asia/Krasnoyarsk")
        due = datetime(2026, 8, 24, 4, 15, tzinfo=zone)
        late = datetime(2026, 8, 24, 5, 0, tzinfo=zone)
        self.assertIsNotNone(due_scheduled_slot(settings, due))
        self.assertIsNone(due_scheduled_slot(settings, late))

    @patch("app.fileops.delete_backups_older_than")
    @patch("app.scheduler.find_candidates", return_value=([], 0))
    def test_scheduler_runs_backup_cleanup_without_candidates(self, _candidates, cleanup):
        cleanup.return_value = {"deleted": [4, 5], "failed": []}
        save_scheduler_settings(dict(
            DEFAULTS, scan_before_run=False, cleanup_backups_enabled=True,
            backup_retention_days=30,
        ))
        result = run_scheduler(trigger="manual")
        cleanup.assert_called_once_with(30)
        self.assertEqual(result["backups_deleted"], 2)
        with db.connect() as connection:
            run = connection.execute("SELECT * FROM scheduler_runs").fetchone()
        self.assertEqual(run["status"], "completed")
        self.assertIsNone(run["error"])


if __name__ == "__main__":
    unittest.main()
