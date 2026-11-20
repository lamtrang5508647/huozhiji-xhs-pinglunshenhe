"""Check first-pass layouts through the public workbook API and a second import."""
from pathlib import Path
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from huozhiji_audit import audit_workbook, convert_table
from huozhiji_audit._engine.build_audited_workbook import cell_value, set_cell, tag
from test_portable_api import fixture


class FirstPassColumnsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name) / "报价与审核.xlsx"
        self.output = Path(temp.name) / "首审.xlsx"
        self.evidence = fixture(self.source)

    def source_layout(self, values):
        with zipfile.ZipFile(self.source) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        path = "xl/worksheets/sheet1.xml"
        root = ET.fromstring(parts[path])
        root.remove(root.find(tag("mergeCells")))
        data = root.find(tag("sheetData"))
        for row in data.findall(tag("row")):
            for c in list(row):
                if c.get("r", "")[:1] in ("G", "H"):
                    row.remove(c)
        for ref, value in values.items():
            set_cell(data, ref, value)
        parts[path] = ET.tostring(root, encoding="utf-8")
        with zipfile.ZipFile(self.source, "w") as archive:
            for name, value in parts.items():
                archive.writestr(name, value)

    def export(self):
        result = audit_workbook(self.source, self.evidence, self.output)
        self.assertEqual(result["summary"]["audit_pass"]["mode"], "first_pass")
        with zipfile.ZipFile(self.output) as z:
            s = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        return {c.get("r"): cell_value(c, []) for c in s.iter(tag("c"))}

    def test_blank_existing_verdict_and_amount_columns_keep_quote_and_notes(self):
        self.source_layout({"G1":"单价", "G2":"3.75", "G3":"7.25", "H1":"备注",
                            "H2":"已联系", "H3":"保留", "J1":"初审结果", "K1":"核定金额"})
        cells = self.export()
        self.assertEqual([cells[c] for c in ("G1","G2","G3","H1","H2","H3")],
                         ["单价","3.75","7.25","备注","已联系","保留"])
        self.assertEqual([cells[c] for c in ("J1","J2","J3","K1","K2","K3")],
                         ["初审结果","成功","失败","核定金额","3.75","0.00"])
        rows = convert_table(self.output)["rows"]
        self.assertEqual((rows[0]["prior_review"]["result_column"], rows[0]["prior_review"]["amount_column"]), ("J","K"))
        self.assertEqual(rows[1]["expected_amount"], "7.25")

    def test_no_audit_headers_append_instead_of_overwriting_amount_and_other_data(self):
        self.source_layout({"G1":"备注", "G3":"保留", "H1":"金额", "H2":"4.50", "H3":"7.25"})
        cells = self.export()
        self.assertEqual([cells[c] for c in ("G1","G3","H1","H2","H3")], ["备注","保留","金额","4.50","7.25"])
        self.assertEqual([cells[c] for c in ("I1","I2","I3","J1","J2","J3")],
                         ["审核结果","成功","失败","核定金额","4.50","0.00"])
        rows = convert_table(self.output)["rows"]
        self.assertEqual(rows[1]["expected_amount"], "7.25")
