"""Exercise the installed module's JSON protocol through its CLI entry point."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from test_portable_api import fixture


class BriefCliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "任务.xlsx"
        self.observations = self.root / "证据.json"
        self.expected = self.root / "期望.json"
        self.review = self.root / "逐行审核.json"
        self.audited = self.root / "审核结果.xlsx"
        self.observations.write_text(json.dumps(fixture(self.source)), encoding="utf-8")

    def invoke(self, *args, code=0):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        proc = subprocess.run([sys.executable, "-m", "huozhiji_audit", *map(str, args)],
                              cwd=self.root, env=env, capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(proc.returncode, code, proc.stderr + proc.stdout)
        return json.loads(proc.stdout)

    def test_brief_exports_complete_files_and_keeps_strict_exit(self):
        convert_args = ["convert", "--source", self.source, "--output", self.expected]
        full = self.invoke(*convert_args)
        brief = self.invoke(*convert_args, "--brief")
        self.assertEqual(brief, {"status": "complete", "output": str(self.expected.resolve()),
                                 "summary": {"total": 6}})
        self.assertEqual(json.loads(self.expected.read_text(encoding="utf-8")), full)

        for command_args in (["review", "--expected", self.expected, "--output", self.review],
                             ["audit", "--source", self.source, "--output", self.audited, "--report", self.review]):
            with self.subTest(command=command_args[0]):
                command_args += ["--observations", self.observations, "--strict"]
                full = self.invoke(*command_args, code=2)
                brief = self.invoke(*command_args, "--brief", code=2)
                saved = json.loads(self.review.read_text(encoding="utf-8"))
                self.assertEqual(saved["summary"], full["summary"])
                self.assertEqual(saved["results"], full["results"])
                self.assertEqual(len(saved["results"]), 6)
                expected_brief = {"status": "complete", "report": str(self.review.resolve()),
                                  "summary": {"total": 6, "approved_amount": "6.50",
                                              "triage_counts": {"成功": 4, "失败": 1, "待人审": 1}}}
                if command_args[0] == "audit":
                    self.assertTrue(zipfile.is_zipfile(self.audited))
                    self.assertEqual(saved["workbook"]["output"], str(self.audited.resolve()))
                    expected_brief["output"] = str(self.audited.resolve())
                self.assertEqual(brief, expected_brief)

    def test_brief_requires_full_evidence_destination_before_processing(self):
        for args, flag in ((["convert", "--source", self.source], "--output"),
                           (["review", "--expected", self.expected, "--observations", self.observations], "--output"),
                           (["audit", "--source", self.source, "--observations", self.observations,
                             "--output", self.audited], "--report")):
            with self.subTest(command=args[0]):
                error = self.invoke(*args, "--brief", code=1)
                self.assertEqual(error["status"], "error")
                self.assertIn(flag, error["error"])
        self.assertFalse(self.audited.exists())

    def test_brief_rejects_colliding_files_before_modification(self):
        original = self.source.read_bytes()
        error = self.invoke("convert", "--source", self.source, "--output", self.source.name, "--brief", code=1)
        self.assertIn("distinct", error["error"])
        self.assertEqual(self.source.read_bytes(), original)
        error = self.invoke("audit", "--source", self.source, "--observations", self.observations,
                            "--output", self.audited, "--report", self.audited, "--brief", code=1)
        self.assertIn("distinct", error["error"])
        self.assertFalse(self.audited.exists())

    def test_report_hard_link_to_evidence_is_rejected_before_workbook_export(self):
        self.review.hardlink_to(self.observations)
        original = self.observations.read_bytes()
        error = self.invoke("audit", "--source", self.source, "--observations", self.observations,
                            "--output", self.audited, "--report", self.review, "--brief", code=1)
        self.assertIn("distinct", error["error"])
        self.assertEqual(self.observations.read_bytes(), original)
        self.assertTrue(self.review.samefile(self.observations))
        self.assertFalse(self.audited.exists())
