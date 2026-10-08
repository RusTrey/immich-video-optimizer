"""Read-only endpoints added for the 0.9.5 UI: run plan, result preview, journal filters."""
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db, fileops
from app.scheduler import DEFAULTS, save_scheduler_settings

try:
    from app.web import create_app
except ModuleNotFoundError as error:
    if error.name != "flask":
        raise
    create_app = None

ORIGINAL = b"original-video-bytes" * 500
OPTIMIZED = b"optimized" * 300
CONTAINER = "mov,mp4,m4a,3gp,3g2,mj2"


@unittest.skipIf(create_app is None, "Flask is not installed in the host test environment")
class UiApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.library = base / "data" / "library"
        self.work = base / "optimizer-work"
        (self.library / "user").mkdir(parents=True)
        self.work.mkdir()
        self.env = patch.dict(os.environ, {"MEDIA_ROOTS": str(self.library), "WORK_ROOT": str(self.work)})
        self.env.start()
        self.replacement = patch.object(fileops, "REPLACEMENT_ENABLED", True)
        self.replacement.start()
        self.old_path = db.DB_PATH
        db.DB_PATH = base / "state" / "optimizer.sqlite3"
        with patch("app.web.start_runtime"):
            self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.replacement.stop()
        self.env.stop()
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def make_ready_job(self, name="VID_1.mp4", created_by="admin"):
        source = self.library / "user" / name
        source.write_bytes(ORIGINAL)
        stat = source.stat()
        with db.connect() as connection:
            db.upsert_video(connection, {
                "path": str(source), "root": str(self.library),
                "relative_path": f"user/{name}", "extension": ".mp4",
                "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "duration_seconds": 3, "bit_rate": 50_000_000, "video_codec": "h264",
                "audio_streams": 1, "subtitle_streams": 0, "data_streams": 0,
                "container": CONTAINER, "classification": "untracked", "probe_error": None,
                "present": 1, "last_scan_id": "test", "scanned_at": "2026-01-01T00:00:00+00:00",
                "first_seen_at": "2026-01-01T00:00:00+00:00",
            })
            video = connection.execute("SELECT * FROM videos WHERE path=?", (str(source),)).fetchone()
        job_id, _ = db.create_job(
            video, preset="Creator 1080p60", encoder="x265", encoder_settings="{}",
            minimum_saving_percent=20, created_at="2026-01-01T00:00:00+00:00",
            created_by=created_by,
        )
        output = self.work / "jobs" / str(job_id) / "result.mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(OPTIMIZED)
        db.update_job(
            job_id, status="ready", phase="ready", original_sha1=hashlib.sha1(ORIGINAL).hexdigest(),
            output_path=str(output), optimized_size=len(OPTIMIZED),
            optimized_sha1=hashlib.sha1(OPTIMIZED).hexdigest(),
            validation_json=json.dumps({"duration_seconds": 3, "video_codec": "hevc"}),
        )
        return job_id, source

    def test_watch_result_serves_output_before_and_library_file_after_replacement(self):
        job_id, _ = self.make_ready_job()
        response = self.client.get(f"/watch-result/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(), OPTIMIZED)
        response.close()

        fileops.request_replace(job_id, wait=True)
        response = self.client.get(f"/watch-result/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(), OPTIMIZED)
        response.close()
        response = self.client.get(f"/watch-original/{job_id}")
        self.assertEqual(response.get_data(), ORIGINAL)
        response.close()

    def test_watch_result_refuses_paths_outside_work_jobs_and_other_statuses(self):
        job_id, source = self.make_ready_job()
        db.update_job(job_id, output_path=str(source))
        self.assertEqual(self.client.get(f"/watch-result/{job_id}").status_code, 404)
        db.update_job(job_id, status="queued", output_path=None)
        self.assertEqual(self.client.get(f"/watch-result/{job_id}").status_code, 404)
        self.assertEqual(self.client.get("/watch-result/999").status_code, 404)

    def test_watch_result_refuses_library_file_that_is_not_the_result(self):
        job_id, source = self.make_ready_job()
        fileops.request_replace(job_id, wait=True)
        source.write_bytes(ORIGINAL)  # e.g. replaced again by someone else
        self.assertEqual(self.client.get(f"/watch-result/{job_id}").status_code, 404)

    def test_plan_lists_backups_due_on_next_run_only_when_cleanup_is_enabled(self):
        old_id, _ = self.make_ready_job("OLD.mp4")
        new_id, _ = self.make_ready_job("NEW.mp4")
        fileops.request_replace(old_id, wait=True)
        fileops.request_replace(new_id, wait=True)
        long_ago = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        db.update_job(old_id, replaced_at=long_ago)

        save_scheduler_settings(dict(DEFAULTS, enabled=True, timezone="UTC"))
        plan = self.client.get("/api/plan").get_json()
        self.assertIsNotNone(plan["next_run"])
        self.assertEqual({item["job_id"] for item in plan["backups"]}, {old_id, new_id})
        self.assertFalse(any(item["due"] for item in plan["backups"]))
        self.assertTrue(all(item["expires_at"] is None for item in plan["backups"]))

        save_scheduler_settings(dict(DEFAULTS, enabled=True, timezone="UTC",
                                     cleanup_backups_enabled=True, backup_retention_days=30))
        plan = self.client.get("/api/plan").get_json()
        due = {item["job_id"]: item["due"] for item in plan["backups"]}
        self.assertEqual(due, {old_id: True, new_id: False})
        self.assertIn("candidates", plan)
        self.assertIn("replacement_enabled", plan)

    def test_plan_counts_candidates_and_respects_run_limit(self):
        save_scheduler_settings(dict(
            DEFAULTS, enabled=True, timezone="UTC", minimum_size_enabled=False,
            minimum_bitrate_enabled=False, minimum_age_enabled=False, maximum_jobs_per_run=1,
        ))
        for name in ("A.mp4", "B.mp4"):
            path = self.library / "user" / name
            path.write_bytes(ORIGINAL)
            stat = path.stat()
            with db.connect() as connection:
                db.upsert_video(connection, {
                    "path": str(path), "root": str(self.library), "relative_path": f"user/{name}",
                    "extension": ".mp4", "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                    "duration_seconds": 3, "bit_rate": 50_000_000, "video_codec": "h264",
                    "audio_streams": 1, "subtitle_streams": 0, "data_streams": 0,
                    "container": CONTAINER, "classification": "untracked", "probe_error": None,
                    "present": 1, "last_scan_id": "test", "scanned_at": "2026-01-01T00:00:00+00:00",
                    "first_seen_at": "2026-01-01T00:00:00+00:00",
                })
        candidates = self.client.get("/api/plan").get_json()["candidates"]
        self.assertEqual(candidates["total"], 2)
        self.assertEqual(candidates["will_add"], 1)
        self.assertEqual(len(candidates["items"]), 2)

    def test_disabled_scheduler_plan_has_no_run_and_no_candidates(self):
        plan = self.client.get("/api/plan").get_json()
        self.assertFalse(plan["settings"]["enabled"])
        self.assertIsNone(plan["next_run"])
        self.assertEqual(plan["candidates"]["total"], 0)

    def test_journal_filters_by_backup_and_file_name(self):
        replaced_id, _ = self.make_ready_job("KEEP_backup.mp4")
        deleted_id, _ = self.make_ready_job("NO_backup.mp4")
        fileops.request_replace(replaced_id, wait=True)
        fileops.request_replace(deleted_id, wait=True)
        fileops.delete_job_backup(deleted_id)

        data = self.client.get("/api/jobs?view=journal&status=replaced&has_backup=1").get_json()
        self.assertEqual([item["id"] for item in data["items"]], [replaced_id])
        data = self.client.get("/api/jobs?view=journal&query=NO_back").get_json()
        self.assertEqual([item["id"] for item in data["items"]], [deleted_id])
        data = self.client.get("/api/jobs?view=journal&query=%25").get_json()
        self.assertEqual(data["items"], [])


if __name__ == "__main__":
    unittest.main()
