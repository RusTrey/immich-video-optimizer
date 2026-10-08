import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db
from app.i18n import EN, RU, SUPPORTED_LANGUAGES

try:
    from app.web import create_app
except ModuleNotFoundError as error:
    if error.name != "flask":
        raise
    create_app = None


class I18nTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "optimizer.sqlite3"

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_catalogs_have_matching_keys_and_english_has_no_cyrillic(self):
        self.assertEqual(SUPPORTED_LANGUAGES, ("ru", "en"))
        self.assertEqual(set(RU), set(EN))
        for key, value in EN.items():
            values = value if isinstance(value, list) else [value]
            self.assertFalse(
                any(re.search(r"[А-Яа-яЁё]", str(item)) for item in values), key
            )

    @unittest.skipIf(create_app is None, "Flask is not installed in the host test environment")
    @patch("app.web.start_runtime")
    def test_language_cookie_switches_complete_page(self, _runtime):
        app = create_app()
        app.testing = True
        with app.test_client() as client:
            response = client.get("/settings")
            russian = response.get_data(as_text=True)
            response.close()
            self.assertIn('<html lang="ru">', russian)
            self.assertIn("Автоматические действия с файлами", russian)

            response = client.get("/language/en?next=/settings")
            self.assertEqual(response.status_code, 303)
            response.close()
            response = client.get("/settings")
            english = response.get_data(as_text=True)
            response.close()
            self.assertIn('<html lang="en">', english)
            self.assertIn("Automatic file actions", english)
            self.assertNotRegex(english, r"[А-Яа-яЁё]")


if __name__ == "__main__":
    unittest.main()
