import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db
from app.scanner import run_scan_sync


PROBE_RESULT = {
    "format": {"duration": "2.0", "bit_rate": "1000000", "format_name": "mov,mp4", "tags": {}},
    "streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080, "avg_frame_rate": "30/1"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}


class IncrementalScannerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "media"
        self.root.mkdir()
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"first")
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "optimizer.sqlite3"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_second_scan_skips_unchanged_file(self):
        with patch("app.scanner.media_roots", return_value=[self.root]), patch("app.scanner.probe", return_value=PROBE_RESULT) as probe:
            first = run_scan_sync()
            second = run_scan_sync()
            self.video.write_bytes(b"changed")
            third = run_scan_sync()
        self.assertEqual(first["probed"], 1)
        self.assertEqual(second["probed"], 0)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(third["probed"], 1)
        self.assertEqual(probe.call_count, 2)

    def test_missing_media_root_fails_without_changing_index(self):
        missing = self.root / "missing"
        with patch("app.scanner.media_roots", return_value=[missing]):
            with self.assertRaisesRegex(RuntimeError, "Недоступны каталоги"):
                run_scan_sync()
        with db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM videos WHERE present=1").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
