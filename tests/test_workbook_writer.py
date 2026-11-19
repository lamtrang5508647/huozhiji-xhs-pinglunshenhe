import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from huozhiji_audit._engine.build_audited_workbook import build_workbook, cell_value, col_index, tag
from test_portable_api import fixture


class WorkbookWriterTests(unittest.TestCase):
    def test_visible_rows_with_unsettled_payments_show_success_and_empty_amounts(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.xlsx"
            output = Path(folder) / "result.xlsx"
            fixture(source)
            rows = [{"case_id": str(i), "source_row": i, "source_sheet": "品牌A",
                     "comment_type": "text" if i != 4 else "text+image"} for i in range(2, 6)]
            results = []
            for row in rows:
                results.append({"case_id": row["case_id"], "triage": "成功", "result_category": "成功",
                                "approved_amount": None, "settlement_status": "pending_duplicate_claim",
                                "reason_codes": ["evidence_claimed_by_multiple_rows"]})
            results[2].update(result_category="文字成功", settlement_status="pending_source_amount",
                              reason_codes=["missing_component_amount"])
            results[3] = {"case_id": "5", "triage": "成功", "result_category": "成功", "approved_amount": "1.50"}
            summary = build_workbook(source, rows, results, output)
            self.assertEqual(summary["approved_amount"], "1.50")
            with zipfile.ZipFile(output) as archive:
                def sheet(name):
                    root = ET.fromstring(archive.read(f"xl/worksheets/{name}.xml"))
                    return {c.get("r"): c for c in root.iter(tag("c"))}
                product, totals = sheet("sheet1"), sheet("sheet2")
                for n in (2, 3, 4):
                    self.assertEqual(cell_value(product[f"G{n}"], []), results[n - 2]["result_category"])
                    self.assertEqual(cell_value(product[f"H{n}"], []), "")
                    self.assertIn("暂缓核定", cell_value(totals[f"Y{n + 10}"], []))
                self.assertIn('Y14<>""', totals["W14"].findtext(tag("f")))
                self.assertEqual(cell_value(totals["E3"], []), "4")
                self.assertEqual(cell_value(totals["I3"], []), "1.50")
            results[0]["approved_amount"] = "1.50"
            with self.assertRaisesRegex(RuntimeError, "invalid_pending_settlement"):
                build_workbook(source, rows, results, output)
            results[0].update(approved_amount=None, reason_codes=[])
            with self.assertRaisesRegex(RuntimeError, "invalid_pending_settlement"):
                build_workbook(source, rows, results, output)

    def test_out_of_order_rows_and_overlapping_summary_sections(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.xlsx"
            output = Path(folder) / "output.xlsx"
            fixture(source)
            numbers = [12, 5, 37, 9, 3, 4, 18, 8, 2, 16, 7, 6, 20, 10]
            rows = [{"case_id": str(n), "source_row": n, "source_sheet": "品牌A",
                     "group_leader": f"团长{n}", "comment_type": "text"} for n in numbers]
            results = [{"case_id": str(n), "result_category": "成功", "triage": "成功",
                        "approved_amount": str(n)} for n in reversed(numbers)]
            build_workbook(source, rows, results, output)
            with zipfile.ZipFile(output) as archive:
                source_sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                summary = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
            for sheet in (source_sheet, summary):
                row_numbers = [int(row.get("r")) for row in sheet.find(tag("sheetData"))]
                self.assertEqual(row_numbers, sorted(set(row_numbers)))
                for row in sheet.find(tag("sheetData")):
                    columns = [col_index(cell.get("r")) for cell in row.findall(tag("c"))]
                    self.assertEqual(columns, sorted(set(columns)))

            def values(sheet):
                return {cell.get("r"): cell_value(cell, []) for cell in sheet.iter(tag("c"))}

            source_values, summary_values = values(source_sheet), values(summary)
            for offset, number in enumerate(numbers):
                self.assertEqual(source_values[f"H{number}"], str(number))
                self.assertEqual(summary_values[f"A{2 + offset}"], f"团长{number}")
                self.assertEqual(summary_values[f"P{12 + offset}"], str(number))

    def test_late_row_corruption_is_rejected_before_replacing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.xlsx"
            output = Path(folder) / "output.xlsx"
            fixture(source)
            output.write_bytes(b"previous output")
            rows = [{"case_id": str(n), "source_row": n, "source_sheet": "品牌A",
                     "comment_type": "text"} for n in (2, 3, 4)]
            results = [{"case_id": str(n), "result_category": "成功", "triage": "成功",
                        "approved_amount": "1.5"} for n in (2, 3, 4)]
            original_read = zipfile.ZipFile.read

            def wrong_final_amount(archive, name, *args, **kwargs):
                raw = original_read(archive, name, *args, **kwargs)
                if name == "xl/worksheets/sheet1.xml" and not source.samefile(archive.filename):
                    root = ET.fromstring(raw)
                    root.find(f".//{tag('c')}[@r='H4']/{tag('v')}").text = "0.01"
                    return ET.tostring(root)
                return raw

            with patch.object(zipfile.ZipFile, "read", wrong_final_amount):
                with self.assertRaisesRegex(RuntimeError, "export_row_amount_mismatch"):
                    build_workbook(source, rows, results, output)
            self.assertEqual(output.read_bytes(), b"previous output")

    def test_full_sheet_cell_scans_do_not_grow_with_row_count(self):
        scans = []
        original_fromstring = ET.fromstring

        class CountSheetScans(ET.Element):
            def iter(self, wanted_tag=None):
                if self.tag == tag("worksheet") and wanted_tag == tag("c"):
                    scans.append(self.tag)
                return super().iter(wanted_tag)

        def parse(xml):
            parser = ET.XMLParser(target=ET.TreeBuilder(element_factory=CountSheetScans))
            return original_fromstring(xml, parser=parser)

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.xlsx"
            output = Path(folder) / "output.xlsx"
            fixture(source)
            scan_counts = []
            for batch_size in (6, 60):
                rows = [{"case_id": str(n), "source_row": n + 2, "source_sheet": "品牌A",
                         "comment_type": "text"} for n in range(batch_size)]
                results = [{"case_id": str(n), "result_category": "成功", "triage": "成功",
                            "approved_amount": "1.5"} for n in range(batch_size)]
                scans.clear()
                with patch.object(ET, "fromstring", parse):
                    build_workbook(source, rows, results, output)
                scan_counts.append(len(scans))
            self.assertLessEqual(scan_counts[1], scan_counts[0] + 1)
