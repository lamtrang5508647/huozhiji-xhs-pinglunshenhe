"""Integration tests use synthetic OOXML and supplied evidence, never live accounts."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree as ET

from huozhiji_audit import audit_workbook, convert_table, review
from huozhiji_audit._engine.build_audited_workbook import S, cell_value, tag
from huozhiji_audit._engine.audit_validation import validate_results


def fixture(path):
    root = ET.Element(tag("worksheet"))
    ET.SubElement(root, tag("dimension"), {"ref": "A1:H7"})
    data = ET.SubElement(root, tag("sheetData"))
    values = [["文章链接", "产品", "团长", "评论文字需求", "需要回复评论内容【左边填写回第几条】", "评论图片需求", "审核结果", "金额【文字1.5，文字加图2元，单图1.5】"]]
    for number, text, image in ((1, "已收到", ""), (2, "找不到", ""), (3, "待验证", ""), (4, "缺图片", "图片"), (5, "", "图片"), (6, "图文都有", "图片")):
        values.append([f"https://www.xiaohongshu.com/explore/n{number}", "产品A", "团长*完整名称", text, "", image, "旧结果", "99"])
    # Explicit blank amounts use header-derived prices, not a hardcoded policy.
    for row in values[1:]:
        row[7] = ""
    for number, values_row in enumerate(values, 1):
        row = ET.SubElement(data, tag("row"), {"r": str(number)})
        for index, value in enumerate(values_row):
            cell = ET.SubElement(row, tag("c"), {"r": f"{chr(65+index)}{number}", "t": "inlineStr"})
            ET.SubElement(ET.SubElement(cell, tag("is")), tag("t")).text = value
    merges = ET.SubElement(root, tag("mergeCells"), {"count": "2"})
    ET.SubElement(merges, tag("mergeCell"), {"ref": "G2:G3"})
    ET.SubElement(merges, tag("mergeCell"), {"ref": "H2:H3"})
    sheet = ET.tostring(root, encoding="utf-8").replace(b"<ns0:worksheet ", b'<ns0:worksheet xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac" mc:Ignorable="x14ac" ')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{S}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="品牌A" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>')
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/media/image1.png", b"synthetic-preservation-fixture")
        archive.writestr("xl/cellimages.xml", b"synthetic-cell-image-definitions")
    comments = {1: [{"text": "已收到", "has_image": False}], 2: [], 3: [],
                4: [{"text": "缺图片", "has_image": False}], 5: [{"text": "", "has_image": True}],
                6: [{"text": "图文都有", "has_image": True}]}
    return {"observations": [{"platform": "xiaohongshu", "target_key": f"n{number}", "capture_status": "ok",
                              "capture_complete": number != 3, "image_detection_complete": True, "comments": rows}
                             for number, rows in comments.items()]}


class PortableAuditTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name) / "中文 空格😀"
        self.root.mkdir()
        self.source = self.root / "评论 源表.xlsx"
        self.evidence = fixture(self.source)

    def tearDown(self):
        self.folder.cleanup()

    def test_offline_end_to_end_preserves_source_and_media(self):
        original = self.source.read_bytes()
        output = self.root / "audited.xlsx"
        result = audit_workbook(self.source, self.evidence, output)
        self.assertEqual(result["summary"]["triage_counts"], {"成功": 4, "失败": 1, "待人审": 1})
        self.assertEqual(result["summary"]["approved_amount"], "6.50")
        self.assertEqual(result["workbook"]["source_sha256"], sha256(original).hexdigest())
        self.assertEqual(self.source.read_bytes(), original)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read("xl/media/image1.png"), b"synthetic-preservation-fixture")
            self.assertEqual(archive.read("xl/cellimages.xml"), b"synthetic-cell-image-definitions")
            xml = archive.read("xl/worksheets/sheet1.xml")
            self.assertIn(b'xmlns:x14ac=', xml)
            sheet = ET.fromstring(xml)
            self.assertIsNone(sheet.find(tag("mergeCells")))
            cells = {c.get("r"): c for c in sheet.iter(tag("c"))}
            self.assertEqual(cell_value(cells["G3"], []), "失败")
            self.assertEqual(cell_value(cells["H3"], []), "0.00")
            self.assertEqual(cell_value(cells["H4"], []), "")
            self.assertEqual(cell_value(cells["G5"], []), "文字成功")
            summary = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
            summary_cells = {c.get("r"): c for c in summary.iter(tag("c"))}
            self.assertEqual(cell_value(summary_cells["A2"], []), "团长*完整名称")
            self.assertEqual(cell_value(summary_cells["I3"], []), "6.50")
            self.assertIn('"~*"', summary_cells["D2"].findtext(tag("f")))

    def test_manual_settlement_cells_survive_rebuild(self):
        first = self.root / "first.xlsx"
        audit_workbook(self.source, self.evidence, first)
        with zipfile.ZipFile(first) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        summary = ET.fromstring(parts["xl/worksheets/sheet2.xml"])
        from huozhiji_audit._engine.build_audited_workbook import set_cell
        set_cell(summary.find(tag("sheetData")), "K2", "原联系方式")
        parts["xl/worksheets/sheet2.xml"] = ET.tostring(summary)
        with zipfile.ZipFile(first, "w") as archive:
            for name, value in parts.items():
                archive.writestr(name, value)
        second = self.root / "second.xlsx"
        result = audit_workbook(first, self.evidence, second)
        self.assertEqual(result["summary"]["total"], 6)
        with zipfile.ZipFile(second) as archive:
            summary = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
            self.assertEqual(cell_value(next(c for c in summary.iter(tag("c")) if c.get("r") == "K2"), []), "原联系方式")

    def test_unknown_evidence_never_becomes_failure(self):
        expected = convert_table(self.source)
        report = review(expected, {"observations": []})
        self.assertEqual(report["summary"]["triage_counts"]["待人审"], 6)
        self.assertTrue(all(row["approved_amount"] is None for row in report["results"]))

    def test_invalid_ids_or_money_do_not_pass_validation(self):
        expected = convert_table(self.source)
        report = review(expected, self.evidence)
        for mutate in (lambda rows: rows.pop(), lambda rows: rows[0].update(approved_amount="NaN"),
                       lambda rows: rows[0].update(approved_amount="0.001"), lambda rows: rows[0].update(triage="失败")):
            invalid = copy.deepcopy(report["results"])
            mutate(invalid)
            with self.assertRaises(RuntimeError):
                validate_results(expected["rows"], invalid)
        with self.assertRaises(ValueError):
            audit_workbook(self.source, self.evidence, self.source)

    def test_cli_json_strict_and_doctor(self):
        evidence = self.root / "observations.json"
        evidence.write_text(json.dumps(self.evidence), encoding="utf-8")
        output = self.root / "cli.xlsx"
        result = subprocess.run([sys.executable, "-m", "huozhiji_audit", "audit", "--source", str(self.source), "--observations", str(evidence), "--output", str(output), "--strict"], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["summary"]["approved_amount"], "6.50")
        self.assertTrue(output.is_file())
        result = subprocess.run([sys.executable, "-m", "huozhiji_audit", "doctor"], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertFalse(status["feishu_required"])
        self.assertFalse(status["codex_required"])
        self.assertFalse(status["live_session_verified"])

    def test_failed_live_command_never_returns_old_complete_manifest(self):
        job = self.root / "job"
        job.mkdir()
        (job / "job-result.json").write_text('{"status":"complete","output":"old.xlsx"}')
        proc = subprocess.run([sys.executable, "-m", "huozhiji_audit", "run", "--source", str(self.root / "missing.xlsx"), f"--work-dir={job}"], capture_output=True, encoding="utf-8")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["status"], "error")

    def test_protected_source_rejected_without_touching_existing_output(self):
        with zipfile.ZipFile(self.source) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        root = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
        ET.SubElement(root, tag("sheetProtection"), {"sheet": "1"})
        parts["xl/worksheets/sheet1.xml"] = ET.tostring(root)
        with zipfile.ZipFile(self.source, "w") as archive:
            for name, value in parts.items():
                archive.writestr(name, value)
        output = self.root / "prior.xlsx"
        output.write_bytes(b"prior-output")
        with self.assertRaises(ValueError):
            audit_workbook(self.source, self.evidence, output)
        self.assertEqual(output.read_bytes(), b"prior-output")

    def test_summary_qr_drawing_is_preserved(self):
        first = self.root / "first.xlsx"
        audit_workbook(self.source, self.evidence, first)
        with zipfile.ZipFile(first) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        root = ET.fromstring(parts["xl/worksheets/sheet2.xml"])
        ET.SubElement(root, tag("drawing"), {"{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id": "qrDrawing"})
        parts["xl/worksheets/sheet2.xml"] = ET.tostring(root)
        parts["xl/worksheets/_rels/sheet2.xml.rels"] = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="qrDrawing" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/></Relationships>'
        parts["xl/drawings/drawing1.xml"] = b'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"><xdr:oneCellAnchor><xdr:from><xdr:col>13</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:ext cx="100" cy="100"/><xdr:clientData/></xdr:oneCellAnchor></xdr:wsDr>'
        with zipfile.ZipFile(first, "w") as archive:
            for name, value in parts.items():
                archive.writestr(name, value)
        second = self.root / "second.xlsx"
        audit_workbook(first, self.evidence, second)
        with zipfile.ZipFile(second) as archive:
            self.assertIsNotNone(ET.fromstring(archive.read("xl/worksheets/sheet2.xml")).find(tag("drawing")))
            drawing = ET.fromstring(archive.read("xl/drawings/drawing1.xml"))
            self.assertEqual(next(child for child in drawing.iter() if child.tag.endswith("}row")).text, "1")


if __name__ == "__main__":
    unittest.main()
