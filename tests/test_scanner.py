import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.encoder import (
    discard_job, display_dimensions, expected_output_dimensions, handbrake_command,
    metadata_values_equal, unsupported_reason, video_encoder,
)
from app.fileops import delete_backups_older_than
from app.profile import defaults as profile_defaults, snapshot as profile_snapshot
from app.scanner import rational, rotation_of


class ScannerHelpersTest(unittest.TestCase):
    def test_rational(self):
        self.assertAlmostEqual(rational("60000/1001"), 59.94005994)
        self.assertIsNone(rational("0/0"))

    def test_rotation(self):
        stream = {"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}
        self.assertEqual(rotation_of(stream), -90)

    def test_handbrake_profile_is_explicit(self):
        settings = profile_snapshot(profile_defaults())
        command = handbrake_command(
            Path("/input.mp4"), Path("/output.mp4"), settings, 1920, 1080, 0
        )
        expected = {
            "--encoder": "x265",
            "--encoder-preset": "fast",
            "--quality": "20",
            "--maxWidth": "1920",
            "--maxHeight": "1080",
            "--aencoder": "copy",
            "--audio-copy-mask": "aac",
            "--audio-fallback": "av_aac",
            "--ab": "192",
            "--crop-mode": "none",
            "--encopts": f"pools={settings['cpu_count']}",
        }
        for option, value in expected.items():
            self.assertEqual(command[command.index(option) + 1], value)

    def test_x264_options_replace_editing_preset_extras(self):
        settings = profile_snapshot(dict(profile_defaults(), encoder="x264"))
        command = handbrake_command(Path("/i.mp4"), Path("/o.mp4"), settings, 1920, 1080, 0)
        self.assertEqual(command[command.index("--encopts") + 1], f"threads={settings['cpu_count']}")

    def test_hdr_and_ten_bit_sources_use_ten_bit_x265(self):
        settings = profile_snapshot(dict(profile_defaults(), encoder="x264"))
        sdr = {"color_transfer": "bt709", "pixel_format": "yuv420p"}
        self.assertEqual(video_encoder(sdr, settings), "x264")
        self.assertEqual(video_encoder({"color_transfer": "arib-std-b67", "pixel_format": "yuv420p"}, settings), "x265_10bit")
        self.assertEqual(video_encoder({"color_transfer": "bt709", "pixel_format": "yuv420p10le"}, settings), "x265_10bit")

    def test_only_iso_media_containers_are_supported(self):
        row = {"probe_error": None, "container": "mov,mp4,m4a,3gp,3g2,mj2",
               "subtitle_streams": 0, "data_streams": 0, "audio_streams": 1}
        self.assertIsNone(unsupported_reason(row))
        self.assertIn("matroska", unsupported_reason(dict(row, container="matroska,webm")))
        self.assertIsNotNone(unsupported_reason(dict(row, data_streams=1)))

    def test_vertical_rotation_uses_portrait_limits(self):
        settings = profile_snapshot(profile_defaults())
        command = handbrake_command(
            Path("/input.mp4"), Path("/output.mp4"), settings, 1920, 1080, -90
        )
        self.assertEqual(command[command.index("--maxWidth") + 1], "1080")
        self.assertEqual(command[command.index("--maxHeight") + 1], "1920")
        self.assertEqual(display_dimensions(1920, 1080, -90), (1080, 1920))
        self.assertEqual(expected_output_dimensions(1920, 1080, -90), (1080, 1920))

    def test_physically_vertical_video_uses_portrait_limits(self):
        settings = profile_snapshot(profile_defaults())
        command = handbrake_command(
            Path("/input.mp4"), Path("/output.mp4"), settings, 1080, 1920, None
        )
        self.assertEqual(command[command.index("--maxWidth") + 1], "1080")
        self.assertEqual(command[command.index("--maxHeight") + 1], "1920")

    def test_4k_is_scaled_without_changing_orientation(self):
        self.assertEqual(expected_output_dimensions(3840, 2160, 0), (1920, 1080))
        self.assertEqual(expected_output_dimensions(3840, 2160, -90), (1080, 1920))

    @patch("app.encoder.transition_job", return_value=True)
    @patch("app.encoder._remove_job_work")
    @patch("app.encoder.get_job", return_value={"status": "failed", "finished_at": "x"})
    def test_failed_job_is_discarded_but_kept_in_history(self, get_job, remove_work, transition):
        discard_job(5)
        remove_work.assert_called_once_with(5)
        self.assertEqual(transition.call_args.kwargs["status"], "discarded")

    @patch("app.encoder.transition_job", return_value=False)
    @patch("app.encoder.get_job", return_value={"status": "running", "finished_at": None})
    def test_running_job_cannot_be_discarded(self, get_job, transition):
        with self.assertRaises(ValueError):
            discard_job(5)

    def test_gps_comparison_respects_source_precision(self):
        self.assertTrue(metadata_values_equal("GPSLongitude", 92.8787, 92.87869))
        self.assertTrue(metadata_values_equal("GPSLatitude", 56.0387, 56.03872))
        self.assertFalse(metadata_values_equal("GPSLongitude", 92.8787, 92.8788))
        self.assertFalse(metadata_values_equal("AndroidCaptureFPS", 60, 60.00001))

    @patch("app.fileops.add_event")
    @patch("app.fileops.delete_job_backup")
    @patch("app.fileops.jobs_with_backups_older_than")
    def test_expired_backup_cleanup_is_bounded_and_continues_after_error(self, jobs, delete, _event):
        jobs.return_value = [{"id": 7}, {"id": 8}]
        delete.side_effect = [None, RuntimeError("checksum mismatch")]
        result = delete_backups_older_than(
            30, now=datetime(2026, 8, 27, tzinfo=timezone.utc)
        )
        jobs.assert_called_once_with("2026-07-28T00:00:00+00:00")
        self.assertEqual(result["deleted"], [7])
        delete.assert_any_call(7, require_library_file=True)
        self.assertEqual(result["failed"][0]["id"], 8)


if __name__ == "__main__":
    unittest.main()
