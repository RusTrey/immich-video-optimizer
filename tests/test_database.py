import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import db


LEGACY_SCHEMA = """
CREATE TABLE videos (
 id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, root TEXT NOT NULL,
 relative_path TEXT NOT NULL, extension TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 mtime_ns INTEGER NOT NULL, duration_seconds REAL, bit_rate INTEGER, container TEXT,
 video_codec TEXT, audio_codec TEXT, width INTEGER, height INTEGER, fps REAL,
 pixel_format TEXT, color_space TEXT, color_transfer TEXT, color_primaries TEXT,
 encoder_tag TEXT, capture_time TEXT, has_location INTEGER NOT NULL DEFAULT 0,
 rotation INTEGER, audio_streams INTEGER NOT NULL DEFAULT 0,
 subtitle_streams INTEGER NOT NULL DEFAULT 0, data_streams INTEGER NOT NULL DEFAULT 0,
 classification TEXT NOT NULL DEFAULT 'untracked', probe_error TEXT,
 present INTEGER NOT NULL DEFAULT 1, last_scan_id TEXT NOT NULL, scanned_at TEXT NOT NULL
);
CREATE TABLE scan_runs (
 id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 discovered INTEGER NOT NULL DEFAULT 0, probed INTEGER NOT NULL DEFAULT 0,
 errors INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, error TEXT
);
CREATE TABLE optimization_jobs (
 id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL REFERENCES videos(id),
 source_path TEXT NOT NULL, source_size INTEGER NOT NULL, source_mtime_ns INTEGER NOT NULL,
 original_sha1 TEXT, output_path TEXT, optimized_size INTEGER, optimized_sha1 TEXT,
 original_video_codec TEXT, optimized_video_codec TEXT, original_bit_rate INTEGER,
 optimized_bit_rate INTEGER, preset TEXT NOT NULL, encoder TEXT NOT NULL,
 encoder_settings TEXT NOT NULL, minimum_saving_percent REAL NOT NULL,
 saving_percent REAL, status TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'queued',
 progress REAL NOT NULL DEFAULT 0, error TEXT, validation_json TEXT,
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, replaced_at TEXT,
 backup_path TEXT
);
"""


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "optimizer.sqlite3"

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def create_legacy(self):
        connection = sqlite3.connect(db.DB_PATH)
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            """
            INSERT INTO videos(
              id,path,root,relative_path,extension,size_bytes,mtime_ns,
              video_codec,audio_streams,subtitle_streams,data_streams,
              classification,present,last_scan_id,scanned_at
            ) VALUES (1,'/media/a.mp4','/media','a.mp4','.mp4',1000,10,
                      'hevc',1,0,0,'untracked',1,'scan-1','2026-08-01T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO optimization_jobs(
              id,video_id,source_path,source_size,source_mtime_ns,preset,encoder,
              encoder_settings,minimum_saving_percent,status,created_at
            ) VALUES (1,1,'/media/a.mp4',1000,10,'Creator','x265','{}',20,'replaced',
                      '2026-08-02T00:00:00+00:00')
            """
        )
        connection.commit()
        connection.close()

    def test_migrates_legacy_database_and_creates_one_backup(self):
        self.create_legacy()
        db.init_db()
        with db.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertIn("restored_at", db.columns_of(connection, "optimization_jobs"))
            self.assertTrue(db.table_exists(connection, "events"))
            video = connection.execute("SELECT * FROM videos WHERE id=1").fetchone()
            job = connection.execute("SELECT * FROM optimization_jobs WHERE id=1").fetchone()
            self.assertEqual(video["first_seen_at"], video["scanned_at"])
            self.assertEqual(job["created_by"], "admin")
            self.assertEqual(job["attempt_count"], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0], 0)
        backups = list((db.DB_PATH.parent / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        db.init_db()
        self.assertEqual(len(list((db.DB_PATH.parent / "backups").glob("*.sqlite3"))), 1)

    def test_version_one_database_is_migrated_with_backup(self):
        db.init_db()
        with db.connect() as connection:
            connection.execute("DROP TABLE events")
            connection.execute("ALTER TABLE optimization_jobs DROP COLUMN restored_at")
            connection.execute("PRAGMA user_version=1")
        db.init_db()
        with db.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIn("restored_at", db.columns_of(connection, "optimization_jobs"))
        backups = list((db.DB_PATH.parent / "backups").glob("optimizer-before-schema-1-*.sqlite3"))
        self.assertEqual(len(backups), 1)

    def test_restart_marks_interrupted_operations(self):
        self.create_legacy()
        db.init_db()
        with db.connect() as connection:
            connection.execute("UPDATE optimization_jobs SET status='restoring' WHERE id=1")
            connection.execute(
                "INSERT INTO scheduler_runs(trigger, status, rules_json, started_at) "
                "VALUES ('schedule', 'running', '{}', '2026-08-01T00:00:00+00:00')"
            )
        db.init_db()
        self.assertEqual(db.get_job(1)["status"], "restore_interrupted")
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT status FROM scheduler_runs").fetchone()[0], "interrupted")

    def test_transition_is_compare_and_set(self):
        self.create_legacy()
        db.init_db()
        self.assertTrue(db.transition_job(1, "replaced", status="restoring"))
        self.assertFalse(db.transition_job(1, "replaced", status="restoring"))
        self.assertEqual(db.get_job(1)["status"], "restoring")

    def test_paginated_query_uses_server_sorting(self):
        db.init_db()
        with db.connect() as connection:
            for index, size in enumerate((100, 300, 200), start=1):
                db.upsert_video(connection, {
                    "path": f"/media/{index}.mp4", "root": "/media",
                    "relative_path": f"{index}.mp4", "extension": ".mp4",
                    "size_bytes": size, "mtime_ns": index, "audio_streams": 1,
                    "subtitle_streams": 0, "data_streams": 0,
                    "classification": "untracked", "probe_error": None,
                    "present": 1, "last_scan_id": "x",
                    "scanned_at": "2026-08-01T00:00:00+00:00",
                })
        rows, total = db.query_videos(page=1, page_size=2, sort="size", order="desc")
        self.assertEqual(total, 3)
        self.assertEqual([row["size_bytes"] for row in rows], [300, 200])


if __name__ == "__main__":
    unittest.main()
