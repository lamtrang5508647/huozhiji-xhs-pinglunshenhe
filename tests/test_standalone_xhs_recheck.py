"""The standalone XLSX collector applies the same recheck rules as a full job."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from test_portable_api import fixture, tag

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/comment-review-audit/scripts"))
import xhs_batch_capture as xhs


def source_book(path, statuses=None):
    fixture(path)
    with ZipFile(path) as archive:
        parts = {key: archive.read(key) for key in archive.namelist()}
    sheet = ET.Element(tag("worksheet"))
    data = ET.SubElement(sheet, tag("sheetData"))
    rows = [["笔记链接"]] if statuses is None else [["笔记链接", "评论内容", "首审结果"]]
    for index, status in enumerate(statuses if statuses is not None else [""]):
        key = "legacy" if statuses is None else f"note{index // 2}"
        link = f"https://www.xiaohongshu.com/explore/{key}"
        rows.append([link] if statuses is None else [link, f"评论{index}", status])
    for number, cells in enumerate(rows, 1):
        row = ET.SubElement(data, tag("row"), r=str(number))
        for i, value in enumerate(cells):
            cell = ET.SubElement(row, tag("c"), t="inlineStr", r=f"{chr(65 + i)}{number}")
            ET.SubElement(ET.SubElement(cell, tag("is")), tag("t")).text = value
    parts["xl/worksheets/sheet1.xml"] = ET.tostring(sheet, encoding="utf-8")
    with ZipFile(path, "w") as archive:
        for key, value in parts.items():
            archive.writestr(key, value)


class StandaloneXhsRecheckTests(unittest.TestCase):
    def test_input_alone_and_stale_expected_both_use_physical_success_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed, legacy, stale = root / "reviewed.xlsx", root / "links.xlsx", root / "stale.json"
            source_book(reviewed, ["失败", "首审成功", "待人审", "", "失败"])
            source_book(legacy)
            stale.write_text(json.dumps({"rows": [{"source_file": "old-name.xlsx", "target_key": "note2"}]}))
            for index, options in enumerate(([], ["--expected", str(stale)])):
                out = root / f"capture{index}.json"
                with patch.object(xhs, "plan_targets", return_value=[]) as plan:
                    self.assertEqual(xhs.main(["--input", str(reviewed), "--input", str(legacy),
                                              "--output", str(out), *options]), 0)
                targets = plan.call_args.args[0]
                self.assertEqual([(t["note_id"], [r["row"] for r in t["sources"]]) for t in targets],
                                 [("note0", [3]), ("legacy", [2])])
                self.assertEqual(set(plan.call_args.args[2]), {"note0"})
                self.assertTrue(json.loads(out.read_text())["recheck_scope"])

    def test_zero_success_never_opens_browser_and_link_only_keeps_old_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, out = root / "source.xlsx", root / "capture.json"
            source_book(path, ["审核失败", ""])
            with patch.object(xhs, "load_xhs_modules", side_effect=AssertionError("unrequested browser")):
                self.assertEqual(xhs.main(["--input", str(path), "--output", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["observations"], [])
            source_book(path)
            with patch.object(xhs, "plan_targets", return_value=[]) as plan:
                self.assertEqual(xhs.main(["--input", str(path), "--output", str(root / "links.json")]), 0)
            self.assertEqual([t["note_id"] for t in plan.call_args.args[0]], ["legacy"])
            self.assertEqual(plan.call_args.args[2], {})
