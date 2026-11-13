"""Regression tests for Sky Planner's desktop shutdown/reconnect protocol.

Run with: python -m unittest discover -s tests -v
No optional astronomy, Flask, or browser dependencies are required.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from desktop_session import record_activity, should_stop


class DesktopSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_language_change_with_ongoing_heartbeats_keeps_server(self):
        record_activity(self.folder, "ping", now=100)
        for t in (102, 104, 106, 110, 120):
            record_activity(self.folder, "ping", now=t)
            self.assertFalse(should_stop(self.folder, now=t + 1))

    def test_inflight_heartbeat_after_close_does_not_cancel_genuine_exit(self):
        record_activity(self.folder, "ping", now=100, page_id="tab-1")
        record_activity(self.folder, "close", now=101, page_id="tab-1")
        record_activity(self.folder, "ping", now=102, page_id="tab-1")
        self.assertEqual((self.folder / "closed").read_text(), "101")
        self.assertTrue(should_stop(self.folder, now=113))
        # BFCache restore/reconnect is treated as a fresh page identity.
        record_activity(self.folder, "ping", now=114, page_id="tab-2")
        self.assertFalse(should_stop(self.folder, now=200))

    def test_page_refresh_cancels_previous_close(self):
        record_activity(self.folder, "ping", now=100)
        record_activity(self.folder, "close", now=101)
        self.assertFalse(should_stop(self.folder, now=102))
        record_activity(self.folder, "ping", now=102.5)
        self.assertFalse((self.folder / "closed").exists())
        self.assertFalse(should_stop(self.folder, now=104))

    def test_real_tab_closure_stops_after_grace_period(self):
        record_activity(self.folder, "ping", now=100)
        record_activity(self.folder, "close", now=101)
        self.assertEqual(float((self.folder / "closed").read_text()), 101)
        self.assertFalse(should_stop(self.folder, now=102.99))
        self.assertFalse(should_stop(self.folder, now=103))
        self.assertTrue(should_stop(self.folder, now=113))

    def test_background_tab_and_sleep_never_cause_launcher_shutdown(self):
        record_activity(self.folder, "ping", now=100)
        for now in (190, 3600, 86400):
            self.assertFalse(should_stop(self.folder, now=now))
        self.assertTrue(should_stop(self.folder, now=190.01, heartbeat_timeout=90))

    def test_old_page_close_after_reload_cannot_stop_reconnected_page(self):
        record_activity(self.folder, "ping", now=100, page_id="old-tab")
        record_activity(self.folder, "ping", now=102, page_id="new-tab")
        record_activity(self.folder, "close", now=103, page_id="old-tab")
        self.assertFalse((self.folder / "closed").exists())
        self.assertFalse(should_stop(self.folder, now=500))
        record_activity(self.folder, "close", now=501, page_id="new-tab")
        self.assertTrue(should_stop(self.folder, now=513))

    def test_empty_and_malformed_legacy_close_files_are_ignored(self):
        record_activity(self.folder, "ping", now=100)
        for value in ("", "not-a-timestamp", "nan", "inf"):
            (self.folder / "closed").write_text(value)
            self.assertFalse(should_stop(self.folder, now=103))

    def test_new_heartbeat_after_close_makes_close_stale(self):
        record_activity(self.folder, "ping", now=100)
        record_activity(self.folder, "close", now=101)
        # A newer ping revokes a pending close, even if its file is restored
        # later by an older version of the server.
        record_activity(self.folder, "ping", now=102)
        (self.folder / "closed").write_text("101")
        self.assertFalse(should_stop(self.folder, now=105))

    def test_cli_exit_status_matches_shutdown_decision(self):
        import time
        moment = time.time()
        record_activity(self.folder, "ping", now=moment - 20)
        command = [sys.executable, "-m", "desktop_session", str(self.folder), "12"]
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 1)
        record_activity(self.folder, "close", now=moment - 15)
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
