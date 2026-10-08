import unittest
from unittest.mock import patch

from app.runtime import auto_replace_scheduler_result


class RuntimeAutomationTest(unittest.TestCase):
    @patch("app.runtime.request_replace")
    @patch("app.runtime.get_scheduler_settings", return_value={"auto_replace_ready": True})
    @patch("app.runtime.get_job", return_value={"created_by": "scheduler"})
    def test_ready_scheduler_job_is_replaced(self, _job, _settings, replace):
        self.assertTrue(auto_replace_scheduler_result(42, "ready"))
        replace.assert_called_once_with(42, wait=True)

    @patch("app.runtime.request_replace")
    @patch("app.runtime.get_scheduler_settings", return_value={"auto_replace_ready": True})
    @patch("app.runtime.get_job", return_value={"created_by": "admin"})
    def test_admin_job_is_never_replaced_automatically(self, _job, _settings, replace):
        self.assertFalse(auto_replace_scheduler_result(42, "ready"))
        replace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
