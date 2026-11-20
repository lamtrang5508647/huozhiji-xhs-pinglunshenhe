"""Real offline child processes, including a grandchild, exercise capture supervision."""
import json
from contextlib import redirect_stdout
from io import StringIO
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from huozhiji_audit import cli

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/comment-review-audit/scripts"
sys.path.insert(0, str(SCRIPTS))
from capture_watchdog import IDLE_EXIT, PROGRESS_ENV, owner_confirmation_input, supervise


class CaptureWatchdogTests(unittest.TestCase):
    def test_interrupted_owner_input_leaves_no_wait_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory, "progress")
            with patch.dict("os.environ", {PROGRESS_ENV: str(receipt)}):
                for exc in (KeyboardInterrupt, EOFError):
                    with patch("builtins.input", side_effect=exc), self.assertRaises(exc):
                        owner_confirmation_input()
                    self.assertFalse(receipt.with_name("waiting-for-owner").exists())
                self.assertEqual(receipt.read_bytes(), b"..")

    def test_owner_input_can_wait_but_capture_after_confirmation_still_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "collector.py"
            adapter.write_text(
                "import sys,time,types\nfrom pathlib import Path\n"
                "from types import SimpleNamespace\n"
                "from unittest.mock import patch\n"
                "sys.path.insert(0,sys.argv[sys.argv.index('--scripts')+1])\n"
                "from xhs_batch_capture import hold_verification\n"
                "auth=types.ModuleType('xhs_cli.auth');auth.REQUIRED_COOKIES=set();auth.save_cookies=lambda _: None\n"
                "out=Path(sys.argv[sys.argv.index('--output')+1])\n"
                "def wait(client, required, save, confirm=input):\n"
                "    out.write_text('waiting');confirm()\n"
                "page=SimpleNamespace(bring_to_front=lambda:None)\n"
                "with patch.dict(sys.modules,{'xhs_cli.auth':auth}), patch('xhs_batch_capture.wait_for_owner_confirmation',wait):\n"
                "    if not hold_verification(SimpleNamespace(_page=page)): sys.exit(3)\n"
                "out.write_text('confirmed');time.sleep(4)\n", encoding="utf-8")
            out = root / "evidence"
            cmd = [sys.executable, "-c", "import sys; from pathlib import Path; "
                   "from huozhiji_audit.cli import capture_adapter; "
                   "sys.exit(capture_adapter('capture-xhs',Path(sys.argv[1]),sys.argv[2:]))",
                   str(adapter), "--scripts", str(SCRIPTS), "--output", str(out),
                   "--wait-for-confirmation", "--capture-idle-timeout", "1.5"]
            with subprocess.Popen(cmd, stdin=subprocess.PIPE, cwd=SCRIPTS.parents[2]) as process:
                deadline = time.monotonic() + 12
                while not out.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(out.exists(), "owner prompt never opened")
                time.sleep(2.3)
                self.assertIsNone(process.poll(), "watchdog ended an active owner prompt")
                process.communicate(b"\n", timeout=10)
                self.assertEqual(out.read_text(), "confirmed")
                self.assertEqual(process.returncode, IDLE_EXIT)

    def test_idle_stops_only_owned_processes_and_keeps_last_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / "observations.json"
            heartbeat = root / "child-activity"
            child = root / "child.py"
            child.write_text(
                "import sys, time\n"
                "with open(sys.argv[1], 'ab', buffering=0) as out:\n"
                "    while True:\n"
                "        out.write(b'.')\n"
                "        time.sleep(0.04)\n", encoding="utf-8")
            parent = root / "capture.py"
            parent.write_text(
                "import json, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
                "while not Path(sys.argv[2]).exists(): time.sleep(0.02)\n"
                "Path(sys.argv[3]).write_text(json.dumps({'observations': [{}]}))\n"
                "time.sleep(30)\n", encoding="utf-8")
            outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                code = supervise([sys.executable, str(parent), str(child), str(heartbeat), str(check)], check, 2)
                self.assertEqual(code, IDLE_EXIT)
                self.assertEqual(json.loads(check.read_bytes()), {"observations": [{}]})
                stopped_size = heartbeat.stat().st_size
                time.sleep(0.2)
                self.assertGreater(stopped_size, 0)
                self.assertEqual(heartbeat.stat().st_size, stopped_size)
                self.assertIsNone(outsider.poll())
            finally:
                outsider.terminate()
                outsider.wait(timeout=5)

    def test_new_visible_evidence_and_checkpoint_renew_the_idle_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            check = Path(directory, "observations.json")
            script = Path(directory, "slow.py")
            script.write_text(
                "import sys, time\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from capture_watchdog import visible_progress\n"
                "time.sleep(1.1)\n"
                "visible_progress()\n"
                "time.sleep(1.1)\n"
                "Path(sys.argv[2]).write_text('checkpoint')\n"
                "time.sleep(1.1)\n", encoding="utf-8")
            self.assertEqual(supervise([sys.executable, str(script), str(SCRIPTS), str(check)], check, 2), 0)
            self.assertEqual(check.read_text(), "checkpoint")

    def test_scheduled_rest_longer_than_idle_budget_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "new-output", "observations.json")
            command = [sys.executable, "-c",
                       "import sys;sys.path.insert(0,sys.argv[1]);from capture_watchdog import paced_sleep;paced_sleep(2.4)",
                       str(SCRIPTS)]
            self.assertEqual(supervise(command, path, 1.5), 0)

    def test_direct_capture_commands_report_stall_and_preserve_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            standin = root / "adapter.py"
            standin.write_text(
                "import sys, time\n"
                "from pathlib import Path\n"
                "Path(sys.argv[sys.argv.index('--output')+1]).write_text('last complete note')\n"
                "time.sleep(60)\n", encoding="utf-8")
            for name in ("capture-xhs", "capture-douyin"):
                with self.subTest(name=name), patch.object(cli, "script_path", return_value=standin):
                    out = root / (name + ".json")
                    buf = StringIO()
                    with redirect_stdout(buf):
                        code = cli.main([name, "--capture-idle-timeout=1", "--output", str(out)])
                    self.assertEqual(code, 3)
                    self.assertEqual(json.loads(buf.getvalue())["reason"], "capture_idle_timeout")
                    self.assertEqual(out.read_text(), "last complete note")
