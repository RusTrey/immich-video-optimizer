import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db

try:
    from app.web import create_app
except ModuleNotFoundError as error:
    if error.name != "flask":
        raise
    create_app = None


@unittest.skipIf(create_app is None, "Flask is not installed in the host test environment")
class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "optimizer.sqlite3"
        with patch("app.web.start_runtime"):
            self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_mutation_without_interface_header_is_rejected(self):
        with patch("app.api.delete_all_backups") as delete_all:
            response = self.client.post("/api/backups/delete-all")
        self.assertEqual(response.status_code, 403)
        delete_all.assert_not_called()

    def test_mutation_from_foreign_origin_is_rejected(self):
        with patch("app.api.request_replace") as replace:
            response = self.client.post(
                "/api/jobs/1/replace",
                headers={"X-Optimizer-Request": "1", "Origin": "http://evil.example"},
            )
        self.assertEqual(response.status_code, 403)
        replace.assert_not_called()

    def test_interface_request_is_accepted(self):
        with patch("app.api.request_replace") as replace:
            response = self.client.post(
                "/api/jobs/1/replace",
                headers={"X-Optimizer-Request": "1", "Origin": "http://localhost"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["accepted"])
        replace.assert_called_once_with(1)

    def test_overview_reports_facts_and_attention(self):
        response = self.client.get("/api/overview")
        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        for key in ("facts", "events", "disk", "next_run", "backups"):
            self.assertIn(key, data)
        self.assertIn("unattached", data["backups"])


if __name__ == "__main__":
    unittest.main()
