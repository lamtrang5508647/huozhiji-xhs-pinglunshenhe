import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from huozhiji_audit._engine.build_audited_workbook import build_workbook, cell_value, col_index, tag
from test_portable_api import fixture


class WorkbookWriterTests(unittest.TestCase):
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
