"""Replacement, restore, backups and crash recovery on real files (no video tools needed)."""
import hashlib
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import db, fileops, statuses
from app.scheduler import DEFAULTS, find_candidates

ORIGINAL = b"original-video-bytes" * 500
OPTIMIZED = b"optimized" * 300
CONTAINER = "mov,mp4,m4a,3gp,3g2,mj2"
RULES = dict(DEFAULTS, minimum_size_enabled=False, minimum_bitrate_enabled=False,
             minimum_age_enabled=False)


def sha1(data):
    return hashlib.sha1(data).hexdigest()


class FileOperationsTest(unittest.TestCase):
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
        db.init_db()

    def tearDown(self):
        self.replacement.stop()
        self.env.stop()
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def add_video(self, path, data):
        path.write_bytes(data)
        stat = path.stat()
        with db.connect() as connection:
            db.upsert_video(connection, {
                "path": str(path), "root": str(self.library),
                "relative_path": str(path.relative_to(self.library)), "extension": ".mp4",
                "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "duration_seconds": 3, "bit_rate": 50_000_000, "video_codec": "h264",
                "audio_streams": 1, "subtitle_streams": 0, "data_streams": 0,
                "container": CONTAINER, "classification": "untracked", "probe_error": None,
                "present": 1, "last_scan_id": "test", "scanned_at": "2026-01-01T00:00:00+00:00",
                "first_seen_at": "2026-01-01T00:00:00+00:00",
            })
            return connection.execute("SELECT * FROM videos WHERE path=?", (str(path),)).fetchone()

    def make_ready_job(self, created_by="scheduler"):
        source = self.library / "user" / "VID_1.mp4"
        video = self.add_video(source, ORIGINAL)
        job_id, _ = db.create_job(
            video, preset="Creator 1080p60", encoder="x265", encoder_settings="{}",
            minimum_saving_percent=20, created_at="2026-01-01T00:00:00+00:00",
            created_by=created_by,
        )
        output = self.work / "jobs" / str(job_id) / "VID_1_HB.mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(OPTIMIZED)
        db.update_job(
            job_id, status="ready", phase="ready", original_sha1=sha1(ORIGINAL),
            output_path=str(output), optimized_size=len(OPTIMIZED), optimized_sha1=sha1(OPTIMIZED),
            validation_json=json.dumps({"duration_seconds": 3, "video_codec": "hevc"}),
        )
        return job_id, source, output

    def backup_of(self, job_id, source):
        return self.work / "backups" / str(job_id) / f"{source.name}.original"

    def test_replace_and_restore_round_trip_keeps_history(self):
        job_id, source, output = self.make_ready_job()
        backup = fileops.request_replace(job_id, wait=True)
        self.assertEqual(source.read_bytes(), OPTIMIZED)
        self.assertEqual(backup.read_bytes(), ORIGINAL)
        self.assertFalse(output.exists())
        self.assertEqual(db.get_job(job_id)["status"], statuses.REPLACED)

        fileops.request_restore(job_id, wait=True)
        self.assertEqual(source.read_bytes(), ORIGINAL)
        self.assertFalse(backup.exists())
        job = db.get_job(job_id)
        self.assertEqual(job["status"], statuses.RESTORED)
        self.assertIsNotNone(job["restored_at"])
        # The restored video must not be picked and auto-replaced again.
        self.assertEqual(find_candidates(RULES)[1], 0)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs root to change owners")
    def test_replacement_keeps_owner_and_mode_of_media_file(self):
        job_id, source, _ = self.make_ready_job()
        os.chown(source, 1234, 2345)
        os.chmod(source, 0o640)  # chown/chmod change ctime only; size and mtime stay
        fileops.request_replace(job_id, wait=True)
        stat = source.stat()
        self.assertEqual((stat.st_uid, stat.st_gid, stat.st_mode & 0o7777), (1234, 2345, 0o640))
        self.assertEqual(source.read_bytes(), OPTIMIZED)

    def test_concurrent_replace_runs_once(self):
        job_id, source, _ = self.make_ready_job()
        results = []

        def worker():
            try:
                fileops.request_replace(job_id, wait=True)
                results.append("ok")
            except ValueError:
                results.append("rejected")
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["ok", "rejected"])
        self.assertEqual(source.read_bytes(), OPTIMIZED)
        self.assertEqual(self.backup_of(job_id, source).read_bytes(), ORIGINAL)
        self.assertEqual(db.get_job(job_id)["status"], statuses.REPLACED)

    def test_replaced_job_cannot_be_claimed_again(self):
        job_id, source, _ = self.make_ready_job()
        backup = fileops.request_replace(job_id, wait=True)
        with self.assertRaises(ValueError):
            fileops.request_replace(job_id, wait=True)
        job = db.get_job(job_id)
        self.assertEqual(job["status"], statuses.REPLACED)
        self.assertEqual(Path(job["backup_path"]), backup)

    def test_failed_check_returns_job_to_ready_with_reason(self):
        job_id, source, _ = self.make_ready_job()
        source.write_bytes(ORIGINAL + b"changed")
        with self.assertRaises(RuntimeError):
            fileops.request_replace(job_id, wait=True)
        job = db.get_job(job_id)
        self.assertEqual(job["status"], statuses.READY)
        self.assertIn("изменился", job["error"])
        self.assertIsNone(job["backup_path"])

    def test_crash_before_database_update_is_recovered_and_backup_survives_delete_all(self):
        job_id, source, _ = self.make_ready_job()
        with patch.object(fileops, "update_video_after_replacement", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                fileops.request_replace(job_id, wait=True)
        job = db.get_job(job_id)
        self.assertEqual(job["status"], statuses.REPLACEMENT_INTERRUPTED)
        self.assertEqual(Path(job["backup_path"]), self.backup_of(job_id, source))

        result = fileops.delete_all_backups()
        self.assertEqual(result["deleted"], [])
        self.assertEqual(self.backup_of(job_id, source).read_bytes(), ORIGINAL)

        self.assertEqual(fileops.reconcile_job(job_id), statuses.REPLACED)
        self.assertEqual(db.get_job(job_id)["status"], statuses.REPLACED)

    def test_recovery_after_exchange_moves_original_to_backup(self):
        job_id, source, output = self.make_ready_job()
        db.update_job(job_id, status=statuses.REPLACEMENT_INTERRUPTED,
                      backup_path=str(self.backup_of(job_id, source)))
        fileops.exchange_paths(source, output)
        self.assertEqual(fileops.reconcile_job(job_id), statuses.REPLACED)
        self.assertEqual(source.read_bytes(), OPTIMIZED)
        self.assertEqual(self.backup_of(job_id, source).read_bytes(), ORIGINAL)

    def test_recovery_before_exchange_returns_to_ready(self):
        job_id, source, _ = self.make_ready_job()
        db.init_db()
        db.update_job(job_id, status=statuses.REPLACING)
        db.init_db()  # container restart
        self.assertEqual(db.get_job(job_id)["status"], statuses.REPLACEMENT_INTERRUPTED)
        self.assertEqual(fileops.reconcile_all(), {job_id: statuses.READY})
        self.assertEqual(source.read_bytes(), ORIGINAL)

    def test_unknown_state_is_left_for_manual_check(self):
        job_id, source, output = self.make_ready_job()
        db.update_job(job_id, status=statuses.REPLACEMENT_INTERRUPTED)
        source.write_bytes(b"something else")
        with self.assertRaises(RuntimeError):
            fileops.reconcile_job(job_id)
        job = db.get_job(job_id)
        self.assertEqual(job["status"], statuses.REPLACEMENT_INTERRUPTED)
        self.assertIn("чужой файл", job["error"])
        self.assertEqual(output.read_bytes(), OPTIMIZED)

    def test_restore_interrupted_after_exchange_is_completed(self):
        job_id, source, _ = self.make_ready_job()
        backup = fileops.request_replace(job_id, wait=True)
        db.update_job(job_id, status=statuses.RESTORE_INTERRUPTED)
        fileops.exchange_paths(source, backup)
        self.assertEqual(fileops.reconcile_job(job_id), statuses.RESTORED)
        self.assertEqual(source.read_bytes(), ORIGINAL)
        self.assertFalse(backup.exists())

    def test_delete_all_keeps_unattached_files(self):
        job_id, source, _ = self.make_ready_job()
        backup = fileops.request_replace(job_id, wait=True)
        stray = self.work / "backups" / "999" / "unknown.original"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"keep me")
        self.assertEqual(fileops.backup_summary()["unattached"], 1)
        result = fileops.delete_all_backups()
        self.assertEqual(result["deleted"], [job_id])
        self.assertFalse(backup.exists())
        self.assertTrue(stray.exists())
        self.assertIsNone(db.get_job(job_id)["backup_path"])

    def test_cleanup_keeps_backup_when_library_file_is_missing(self):
        job_id, source, _ = self.make_ready_job()
        backup = fileops.request_replace(job_id, wait=True)
        source.unlink()
        result = fileops.delete_backups_older_than(1, now=datetime(2100, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(result["deleted"], [])
        self.assertTrue(backup.exists())
        self.assertTrue(any(event["source"] == "cleanup" for event in db.query_events()))

    def test_restore_follows_file_moved_by_immich(self):
        job_id, source, _ = self.make_ready_job()
        fileops.request_replace(job_id, wait=True)
        moved = self.library / "user" / "2026-07-02" / source.name
        moved.parent.mkdir()
        os.replace(source, moved)
        with db.connect() as connection:
            connection.execute("UPDATE videos SET present=0 WHERE path=?", (str(source),))
        self.add_video(moved, OPTIMIZED)
        fileops.request_restore(job_id, wait=True)
        self.assertEqual(moved.read_bytes(), ORIGINAL)
        self.assertEqual(db.get_job(job_id)["source_path"], str(moved))
        self.assertEqual(db.get_job(job_id)["status"], statuses.RESTORED)

    def test_cancelled_and_discarded_jobs_are_not_selected_again(self):
        from app.encoder import cancel_queued_job, discard_job
        job_id, _, _ = self.make_ready_job()
        discard_job(job_id)
        self.assertEqual(db.get_job(job_id)["status"], statuses.DISCARDED)
        self.assertEqual(find_candidates(RULES)[1], 0)
        other = self.add_video(self.library / "user" / "VID_2.mp4", ORIGINAL)
        queued, _ = db.create_job(other, preset="p", encoder="x265", encoder_settings="{}",
                                  minimum_saving_percent=20, created_at="x", created_by="scheduler")
        cancel_queued_job(queued)
        self.assertEqual(db.get_job(queued)["status"], statuses.CANCELLED)
        self.assertEqual(find_candidates(RULES)[1], 0)


if __name__ == "__main__":
    unittest.main()
