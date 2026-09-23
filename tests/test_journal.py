import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import journal, store


class BitrixJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(database_path=self.tmp.name + "/db.sqlite", database_url="")
        self.patcher = patch("app.store.get_settings", return_value=self.settings)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def payload(self):
        return [
            ("event", "ONCRMLEADUPDATE"),
            ("data[FIELDS][ID]", "42"),
            ("ts", "1790160000"),
            ("auth[application_token]", "never-store-this"),
        ]

    def test_persists_sanitized_event_before_processing_and_deduplicates_retry(self):
        entry, duplicate = journal.begin("ONCRMLEADUPDATE", "lead", "42", self.payload())
        self.assertFalse(duplicate)
        self.assertEqual(entry["status"], "pending")
        saved = json.loads(entry["payload_json"])
        self.assertNotIn("auth[application_token]", saved)
        self.assertNotIn("never-store-this", entry["payload_json"])

        journal.mark_processed(entry["id"])
        second, duplicate = journal.begin("ONCRMLEADUPDATE", "lead", "42", self.payload())
        self.assertTrue(duplicate)
        self.assertEqual(second["id"], entry["id"])
        self.assertEqual(len(store.rows("SELECT * FROM bitrix_events")), 1)

    def test_failed_event_remains_retryable(self):
        entry, _ = journal.begin("ONCRMLEADUPDATE", "lead", "42", self.payload())
        journal.mark_failed(entry["id"], RuntimeError("temporary failure"))
        rows = journal.retryable()
        self.assertEqual([row["id"] for row in rows], [entry["id"]])
        self.assertIn("temporary failure", rows[0]["last_error"])


if __name__ == "__main__":
    unittest.main()
