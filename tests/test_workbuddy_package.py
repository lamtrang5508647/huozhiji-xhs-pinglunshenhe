"""Check an unpacked WorkBuddy skill with no installed SDK or checkout on sys.path."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from test_portable_api import fixture

ROOT = Path(__file__).resolve().parents[1]


class WorkBuddyPackageTests(unittest.TestCase):
    def test_portable_skill_audits_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            archive = job / "comment-review-audit.zip"
            skill = job / "技能目录 有空格" / "comment-review-audit"
            subprocess.run([sys.executable, str(ROOT / "tools/package_workbuddy.py"),
                            "--output", str(archive)], cwd=job, check=True, capture_output=True)
            with zipfile.ZipFile(archive) as packed:
                self.assertIsNone(packed.testzip())
                self.assertIn("SKILL.md", packed.namelist())
                packed.extractall(skill)
            source = job / "原表.xlsx"
            evidence = job / "证据.json"
            evidence.write_text(json.dumps(fixture(source)), encoding="utf-8")
            output = job / "成品.xlsx"
            report = job / "报告.json"
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
            command = [sys.executable, "-S", "-X", "utf8", str(skill / "scripts/audit.py")]
            result = subprocess.run([*command, "audit", "--source", str(source), "--observations", str(evidence),
                                     "--output", str(output), "--report", str(report), "--brief", "--strict"],
                                    env=env, cwd=job, capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["summary"]["approved_amount"], "6.50")
            self.assertEqual(len(json.loads(report.read_text(encoding="utf-8"))["results"]), 6)
            self.assertTrue(zipfile.is_zipfile(output))
            for name in ("run", "capture-xhs", "capture-douyin"):
                subprocess.run([*command, name, "--help"], env=env, cwd=job, check=True, capture_output=True)
