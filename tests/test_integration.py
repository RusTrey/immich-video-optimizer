"""End-to-end encoding with HandBrakeCLI, ffmpeg and ExifTool on tiny generated videos.

Runs inside the image (Docker build and CI image job); skipped where the tools are missing.
"""
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db, encoder, fileops, scanner, statuses

TOOLS = all(shutil.which(tool) for tool in ("HandBrakeCLI", "ffmpeg", "ffprobe", "exiftool"))
LOSSLESS_X264 = ["-c:v", "libx264", "-qp", "0", "-preset", "ultrafast"]


@unittest.skipUnless(TOOLS, "HandBrakeCLI, ffmpeg, ffprobe and exiftool are required")
class EncodingIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.library = base / "data" / "library"
        self.work = base / "optimizer-work"
        (self.library / "u").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"MEDIA_ROOTS": str(self.library), "WORK_ROOT": str(self.work)})
        self.env.start()
        self.old_path = db.DB_PATH
        db.DB_PATH = base / "state" / "optimizer.sqlite3"
        db.init_db()

    def tearDown(self):
        self.env.stop()
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def ffmpeg(self, name, video_args, *, rate=30, seconds=3, size="640x360", extra=(), container_args=()):
        path = self.library / "u" / name
        command = [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
            "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds),
            *video_args, "-c:a", "aac", "-shortest",
            "-metadata", "creation_time=2026-07-01T10:00:00Z", *extra, *container_args, str(path),
        ]
        subprocess.run(command, check=True)
        return path

    def encode(self, path):
        scanner.run_scan_sync()
        with db.connect() as connection:
            video = connection.execute("SELECT * FROM videos WHERE path=?", (str(path),)).fetchone()
        created, skipped = encoder.enqueue_videos([video["id"]], created_by="admin", wake=False)
        if skipped:
            return None, video
        encoder.encode_job(created[0])
        return db.get_job(created[0]), video

    def probe(self, path):
        import json
        output = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout
        return next(stream for stream in json.loads(output)["streams"] if stream["codec_type"] == "video")

    def assertReady(self, job):
        self.assertEqual(job["status"], statuses.READY, job["error"])

    def test_android_video_round_trip_keeps_capture_tags(self):
        source = self.ffmpeg(
            "VID_android.mp4", LOSSLESS_X264,
            extra=["-metadata", "com.android.version=13", "-metadata", "com.android.capture.fps=30"],
            container_args=["-movflags", "use_metadata_tags"],
        )
        job, _ = self.encode(source)
        self.assertReady(job)
        output = Path(job["output_path"])
        tags = encoder.read_metadata(output)
        self.assertEqual(str(tags.get("AndroidVersion")), "13")
        log = output.with_name(output.name + ".log").read_text()
        self.assertIn("options: pools=", log)
        self.assertNotIn("+ crop", log)
        with patch.object(fileops, "REPLACEMENT_ENABLED", True):
            fileops.request_replace(job["id"], wait=True)
            self.assertEqual(self.probe(source)["codec_name"], "hevc")
            fileops.request_restore(job["id"], wait=True)
        self.assertEqual(self.probe(source)["codec_name"], "h264")
        self.assertEqual(db.get_job(job["id"])["status"], statuses.RESTORED)

    def test_high_frame_rate_is_limited_to_sixty(self):
        source = self.ffmpeg("slowmo.mp4", LOSSLESS_X264, rate=120, seconds=2)
        job, _ = self.encode(source)
        self.assertReady(job)
        fps = scanner.rational(self.probe(job["output_path"])["avg_frame_rate"])
        self.assertLessEqual(fps, 60.5)
        self.assertGreater(fps, 55)

    def test_hlg_ten_bit_source_keeps_hdr_characteristics(self):
        source = self.ffmpeg(
            "hlg.mp4",
            ["-c:v", "libx265", "-x265-params", "lossless=1:log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc", "-pix_fmt", "yuv420p10le",
             "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
             "-tag:v", "hvc1"],
            seconds=2,
        )
        job, video = self.encode(source)
        self.assertEqual(video["color_transfer"], "arib-std-b67")
        self.assertReady(job)
        stream = self.probe(job["output_path"])
        self.assertEqual(stream["color_transfer"], "arib-std-b67")
        self.assertEqual(stream["color_primaries"], "bt2020")
        self.assertIn("10", stream["pix_fmt"])

    def test_iphone_mov_keeps_quicktime_keys(self):
        source = self.ffmpeg(
            "IMG_0001.MOV", LOSSLESS_X264,
            extra=[
                "-metadata", "com.apple.quicktime.creationdate=2026-07-01T17:00:00+0700",
                "-metadata", "com.apple.quicktime.make=Apple",
                "-metadata", "com.apple.quicktime.model=iPhone 15",
                "-metadata", "com.apple.quicktime.content.identifier=4E1F-TEST",
            ],
            container_args=["-movflags", "use_metadata_tags", "-f", "mov"],
        )
        expected = encoder.read_metadata(source)
        self.assertEqual(expected.get("ContentIdentifier"), "4E1F-TEST")
        job, _ = self.encode(source)
        self.assertReady(job)
        actual = encoder.read_metadata(job["output_path"])
        for tag in ("CreationDate", "Make", "Model", "ContentIdentifier"):
            self.assertEqual(str(actual.get(tag)), str(expected.get(tag)), tag)

    def test_matroska_source_is_not_enqueued(self):
        source = self.ffmpeg("clip.mkv", LOSSLESS_X264)
        job, video = self.encode(source)
        self.assertIsNone(job)
        self.assertIn("не поддерживается", encoder.unsupported_reason(video))

    def test_running_encode_can_be_stopped(self):
        source = self.ffmpeg("long.mp4", LOSSLESS_X264, seconds=40, size="1280x720")
        scanner.run_scan_sync()
        with db.connect() as connection:
            video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
        job_id = encoder.enqueue_videos([video_id], wake=False)[0][0]
        result = {}
        worker = threading.Thread(target=lambda: result.update(status=encoder.encode_job(job_id)))
        worker.start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and encoder.STATE.snapshot()["phase"] != "encoding":
            time.sleep(0.1)
        time.sleep(1)
        encoder.stop_running_job(job_id)
        worker.join(60)
        self.assertEqual(result.get("status"), statuses.INTERRUPTED)
        self.assertIn("Остановлено", db.get_job(job_id)["error"])
        self.assertEqual(source.stat().st_size, db.get_job(job_id)["source_size"])


if __name__ == "__main__":
    unittest.main()
