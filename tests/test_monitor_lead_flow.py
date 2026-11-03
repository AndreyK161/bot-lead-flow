import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import monitor_lead_flow as monitor


class MonitorLeadFlowTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.state = Path(directory) / "state.json"
        self.state_patch = patch.object(monitor, "STATE_FILE", self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def test_first_healthy_run_is_quiet(self):
        with patch.object(monitor, "run_checks", return_value={}), patch.object(monitor, "send_message") as send:
            self.assertEqual(monitor.main(), 0)
        send.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text())["failures"], {})

    def test_failure_and_recovery_are_notified_once(self):
        failure = {"bot health": "HTTP 503"}
        with patch.object(monitor, "dotenv_token", return_value="token"), patch.object(
            monitor, "send_message",
        ) as send, patch.object(monitor, "run_checks", return_value=failure):
            self.assertEqual(monitor.main(), 1)
            self.assertEqual(monitor.main(), 1)
            self.assertEqual(send.call_count, 1)
            with patch.object(monitor, "run_checks", return_value={}):
                self.assertEqual(monitor.main(), 0)
        self.assertEqual(send.call_count, 2)
        self.assertIn("восстановлен", send.call_args.args[1])

    def test_backup_freshness_rejects_stale_file(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        backup_dir = Path(directory) / "backups" / "automatic"
        backup_dir.mkdir(parents=True)
        backup = backup_dir / "bot-lead-flow-test.dump"
        backup.write_bytes(b"valid-enough-for-age-check")
        old = time.time() - 200
        backup.touch()
        with patch.object(monitor, "APP_DIR", Path(directory)), patch.object(monitor, "BACKUP_MAX_AGE", 100):
            import os
            os.utime(backup, (old, old))
            with self.assertRaisesRegex(RuntimeError, "backup is"):
                monitor.newest_backup_age()


if __name__ == "__main__":
    unittest.main()
