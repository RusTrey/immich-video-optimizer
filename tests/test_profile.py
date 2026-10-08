import unittest
from unittest.mock import patch

from app.profile import (
    defaults, job_profile, process_command, snapshot, validate_profile,
)


class EncodingProfileTest(unittest.TestCase):
    @patch("app.profile.available_cpu_count", return_value=8)
    def test_profile_is_bounded(self, available):
        values = defaults()
        values.update({
            "encoder": "x264", "quality": 30, "resolution": "2160p",
            "audio_bitrate": 320, "cpu_count": 8, "priority": "normal",
        })
        self.assertEqual(validate_profile(values), values)
        values["quality"] = 31
        with self.assertRaises(ValueError):
            validate_profile(values)

    @patch("app.profile.available_cpu_count", return_value=8)
    @patch("app.profile.available_cpu_ids", return_value=[2, 4, 6, 8, 10, 12, 14, 16])
    def test_process_command_uses_affinity_and_priority(self, cpu_ids, cpu_count):
        settings = snapshot({
            "encoder": "x265", "quality": 20, "resolution": "1080p",
            "audio_bitrate": 192, "cpu_count": 3, "priority": "low",
        })
        command = process_command(["ffprobe", "input.mp4"], settings)
        self.assertEqual(command[:8], [
            "ionice", "-c", "3", "nice", "-n", "10", "taskset", "-c",
        ])
        self.assertEqual(command[8:11], ["2,4,6", "ffprobe", "input.mp4"])

    @patch("app.profile.available_cpu_count", return_value=8)
    def test_legacy_job_is_mapped_to_orientation_aware_profile(self, available):
        job = {
            "encoder": "x265",
            "encoder_settings": '{"quality":20,"max_width":1920,"max_height":1080,"x265_pools":6}',
        }
        result = job_profile(job)
        self.assertEqual(result["resolution"], "1080p")
        self.assertEqual((result["max_long_edge"], result["max_short_edge"]), (1920, 1080))
        self.assertEqual(result["cpu_count"], 6)


if __name__ == "__main__":
    unittest.main()
