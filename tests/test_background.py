import io
import logging
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import background
from runtime import InstanceLock


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        root_patch = patch.object(background, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.fake_bot = ModuleType("bot")
        self.fake_bot.load_config = lambda: ("unused-test-token", 123, self.root / "data")

    def run_with(self, action):
        self.fake_bot.main = action
        with patch.dict(sys.modules, {"bot": self.fake_bot}):
            return background.run()

    def test_background_runs_without_console_and_writes_utf8_log(self):
        def action():
            self.assertEqual(sys.stdin.read(), "")
            print("FC DKP online. Проверка фонового запуска.", flush=True)
            print("Diagnostic message", file=sys.stderr)
            return 0

        with patch.object(sys, "stdin", None), patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            self.assertEqual(self.run_with(action), 0)
            self.assertIsNone(sys.stdin)
            self.assertIsNone(sys.stdout)
            self.assertIsNone(sys.stderr)
        log = self.root / "logs" / "background.log"
        content = log.read_text(encoding="utf-8")
        self.assertIn("Проверка фонового запуска", content)
        self.assertIn("Diagnostic message", content)
        # File handles must also close on Windows.
        log.unlink()
        self.assertEqual(logging.getLogger("fc_dkp.background").handlers, [])

    def test_failure_exit_code_is_preserved_for_scheduler_restart(self):
        self.assertEqual(self.run_with(lambda: 7), 7)
        self.assertIn("Exit code: 7", (self.root / "logs" / "background.log").read_text())

    def test_uncaught_failure_returns_error_without_logging_exception_body(self):
        def fail():
            raise RuntimeError("private-value-not-for-logs")

        self.assertEqual(self.run_with(fail), 1)
        log = (self.root / "logs" / "background.log").read_text()
        self.assertIn("RuntimeError", log)
        self.assertNotIn("private-value-not-for-logs", log)

    def test_preflight_rejects_running_process_and_releases_its_own_lock(self):
        data = self.root / "data"
        data.mkdir()
        with patch.dict(sys.modules, {"bot": self.fake_bot}), redirect_stdout(io.StringIO()):
            with InstanceLock(data / "bot.lock"):
                self.assertEqual(background.preflight(), 1)
            self.assertEqual(background.preflight(), 0)
        with InstanceLock(data / "bot.lock"):
            pass


if __name__ == "__main__":
    unittest.main()
