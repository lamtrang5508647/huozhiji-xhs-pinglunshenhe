import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from test_portable_api import fixture

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/comment-review-audit/scripts"
sys.path.insert(0, str(SCRIPTS))
from table_to_expected import convert_xlsx_sheets
from xhs_batch_capture import targets_from_workbooks
from build_audited_workbook import tag, set_cell


class TaskCapturePlanTests(unittest.TestCase):
    def test_raw_share_link_follows_mapped_column_and_only_assigned_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.xlsx"
            fixture(source)
            member = "xl/worksheets/sheet1.xml"
            with zipfile.ZipFile(source) as archive:
                parts = {name: archive.read(name) for name in archive.namelist()}
            doc = ET.fromstring(parts[member])
            data = doc.find(tag("sheetData"))
            set_cell(data, "A1", "参考链接")
            set_cell(data, "I1", "计划入口")
            for i in range(2, 8):
                set_cell(data, f"I{i}", f"https://www.xiaohongshu.com/explore/actual{i}?xsec_token=fixture")
            set_cell(data, "I3", "")  # Same note as row 2.
            set_cell(data, "I8", "https://www.xiaohongshu.com/explore/unassigned")
            parts[member] = ET.tostring(doc, encoding="utf-8")
            with zipfile.ZipFile(source, "w") as archive:
                for name, blob in parts.items():
                    archive.writestr(name, blob)
            mapping = {"target_url": "计划入口"}
            rows = convert_xlsx_sheets(source, mapping, None)
            targets = targets_from_workbooks([str(source)], task_rows=rows, mapping=mapping)
            self.assertEqual([t["note_id"] for t in targets], ["actual2", "actual4", "actual5", "actual6", "actual7"])
            self.assertEqual([s["row"] for s in targets[0]["sources"]], [2, 3])
            self.assertTrue(all(t["token"] == "fixture" for t in targets))
            self.assertTrue(all("xsec_token" not in r["target_url"] for r in rows))
            with self.assertRaisesRegex(ValueError, "capture_target_identity_mismatch"):
                targets_from_workbooks([str(source)], task_rows=[{**rows[0], "target_key":"other"}], mapping=mapping)
