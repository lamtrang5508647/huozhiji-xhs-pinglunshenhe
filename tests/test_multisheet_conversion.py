"""Exercise shared strings through the SDK's multi-sheet XLSX entry point."""
import tempfile
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from huozhiji_audit import convert_table


class MultiSheetConversionTests(unittest.TestCase):
    def test_shared_parts_read_once_and_summary_skipped(self):
        namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        names = ["产品乙", "审核汇总", "产品甲"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "多工作表.xlsx"
            with zipfile.ZipFile(path, "w") as book:
                entries = "".join(f'<sheet name="{name}" r:id="s{i}"/>' for i, name in enumerate(names))
                book.writestr("xl/workbook.xml", f'<workbook xmlns:r="{rel}"><sheets>{entries}</sheets></workbook>')
                links = "".join(f'<Relationship Id="s{i}" Target="worksheets/s{i}.xml"/>' for i in range(3))
                book.writestr("xl/_rels/workbook.xml.rels", f"<Relationships>{links}</Relationships>")
                book.writestr("xl/sharedStrings.xml", '<sst><si><t>target_key</t></si><si><r><t>评论</t></r><r><t>内容</t></r></si><si><t>已收到</t></si></sst>')
                for i in (0, 2):
                    book.writestr(f"xl/worksheets/s{i}.xml", f'<worksheet xmlns="{namespace}"><sheetData>'
                                  '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                                  f'<row r="2"><c r="A2" t="inlineStr"><is><t>笔记{i}</t></is></c>'
                                  '<c r="B2" t="s"><v>2</v></c></row></sheetData></worksheet>')
                book.writestr("xl/worksheets/s1.xml", "unused summary")
            reads = Counter()
            read = zipfile.ZipFile.read

            def count_read(book, part):
                reads[part] += 1
                return read(book, part)

            with patch.object(zipfile.ZipFile, "read", count_read):
                rows = convert_table(path)["rows"]
            self.assertEqual([row["source_sheet"] for row in rows], ["产品乙", "产品甲"])
            self.assertEqual([row["target_key"] for row in rows], ["笔记0", "笔记2"])
            self.assertTrue(all(row["comment_text"] == "已收到" for row in rows))
            self.assertEqual(convert_table(path, sheet="产品甲")["rows"], [rows[1]])
            with self.assertRaisesRegex(ValueError, "Worksheet not found"):
                convert_table(path, sheet="不存在")
            with self.assertRaisesRegex(ValueError, "expected_rows_empty"):
                convert_table(path, sheet="审核汇总")
            for part in ("xl/sharedStrings.xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels"):
                self.assertEqual(reads[part], 1, part)
            self.assertNotIn("xl/worksheets/s1.xml", reads)
