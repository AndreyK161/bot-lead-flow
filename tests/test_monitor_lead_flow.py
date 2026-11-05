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

    def test_system_recipient_is_used_instead_of_super_admin_list(self):
        values = {
            "SYSTEM_NOTIFICATION_CHAT_ID": "1297686797",
            "SUPER_ADMIN_USERS": "399005637,8306778724",
            "MODE_SWITCH_USER_ID": "42",
        }
        with patch.dict("os.environ", {}, clear=True), patch.object(
            monitor, "dotenv_value", side_effect=lambda name: values.get(name, ""),
        ):
            self.assertEqual(monitor.telegram_chat_id(), "1297686797")

    def test_explicit_monitor_recipient_has_highest_priority(self):
        with patch.dict(
            "os.environ", {"LEADFLOW_MONITOR_CHAT_ID": "1297686797"}, clear=True,
        ), patch.object(monitor, "dotenv_value", return_value="399005637"):
            self.assertEqual(monitor.telegram_chat_id(), "1297686797")


if __name__ == "__main__":
    unittest.main()
