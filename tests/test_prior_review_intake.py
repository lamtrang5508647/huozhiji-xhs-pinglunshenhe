"""Recognize prior task results and recover actual source rates from Excel."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from huozhiji_audit._engine.table_to_expected import column_letters, convert_xlsx_sheets


def workbook(path, sheets):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path, "w") as z:
        book = ET.Element("workbook")
        listing = ET.SubElement(book, "sheets")
        links = ET.Element("Relationships")
        for idx, (name, rows) in enumerate(sheets.items(), 1):
            ET.SubElement(listing, "sheet", name=name, attrib={f"{{{rel}}}id":str(idx)})
            ET.SubElement(links, "Relationship", Id=str(idx), Target=f"worksheets/s{idx}.xml")
            xml = ET.Element("worksheet", xmlns=ns)
            data = ET.SubElement(xml, "sheetData")
            for number, values in enumerate(rows, 1):
                row = ET.SubElement(data, "row", r=str(number))
                for index, value in enumerate(values):
                    if value is not None:
                        cell = ET.SubElement(row, "c", r=f"{column_letters(index)}{number}", t="inlineStr")
                        ET.SubElement(ET.SubElement(cell, "is"), "t").text = str(value)
            z.writestr(f"xl/worksheets/s{idx}.xml", ET.tostring(xml, encoding="utf-8"))
        z.writestr("xl/workbook.xml", ET.tostring(book, encoding="utf-8"))
        z.writestr("xl/_rels/workbook.xml.rels", ET.tostring(links))


class PriorReviewIntakeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "随意文件名.xlsx"

    def load(self, sheets, **options):
        workbook(self.path, sheets)
        return convert_xlsx_sheets(self.path, {}, None, skip_summary=True, **options)

    def test_known_status_controls_whole_book_but_placeholder_alone_is_first_pass(self):
        header = ["笔记链接", "评论内容", "", "审核结果", "金额"]
        other = [header, ["https://xhslink.com/z", "空白任务"]]
        first = [header, ["https://xhslink.com/a", "已知", "", "审核通过", "1.5"],
                 ["", "未知", "", "需外部确认", "4"], ["", "失败项", "", "失败", "0"]]
        sheet = self.load({"A": first, "B":other})
        self.assertEqual([r["prior_review"]["triage"] for r in sheet], ["成功", "", "失败", ""])
        self.assertEqual(sheet[0]["prior_review"], {"category":"成功", "raw_category":"审核通过", "triage":"成功",
                         "amount":"1.5", "result_column":"D", "amount_column":"E"})
        self.assertEqual(sheet[1]["prior_review"]["raw_category"], "需外部确认")
        self.assertEqual(self.load({"A":first, "B":other}, sheet_name="B"), sheet[-1:])
        template = [header, ["https://xhslink.com/a", "文字", "", "旧结果", "4"]]
        self.assertNotIn("prior_review", self.load({"A":template, "B":other})[0])

    def test_explicit_manual_success_and_failure_labels_trigger_recheck(self):
        header = ["笔记链接", "评论内容", "首审结果"]
        labels = {"审核成功":"成功", "初审成功":"成功", "首审成功":"成功", "初审失败":"失败",
                  "首审失败":"失败", "审核未通过":"失败", "审核不通过":"失败"}
        for label, triage in labels.items():
            with self.subTest(label=label):
                rows = [header, ["https://xhslink.com/a", "已填写", label], ["", "空白任务", None]]
                done, blank = self.load({"A": rows})
                self.assertEqual(done["prior_review"]["triage"], triage)
                self.assertEqual(blank["prior_review"]["triage"], "")

    def test_settled_component_and_failed_zero_do_not_replace_full_quote(self):
        header = ["笔记链接","评论内容","图片需求","审核结果","金额【文字1.5，文字加图3，单图2】"]
        rows = [header, ["https://xhslink.com/a", "双项", "配图", "文字成功", "1.5"],
                ["", "缺失", "", "失败", "0"]]
        partial, failed = self.load({"A":rows})
        self.assertEqual((partial["expected_amount"], partial["partial_text_amount"], partial["prior_review"]["amount"]),
                         ("3.00", "1.50", "1.5"))
        self.assertEqual((failed["expected_amount"], failed["prior_review"]["amount"]), ("1.50","0"))
        header[-1] = "金额"
        partial, failed = self.load({"A":rows})
        self.assertNotIn("expected_amount", partial)
        self.assertEqual(partial["partial_text_amount"], "1.5")
        self.assertNotIn("expected_amount", failed)
        header.extend(["单价","核定金额"])
        rows[1].extend(["8","1.25"])
        partial = self.load({"A":rows})[0]
        self.assertEqual((partial["expected_amount"], partial["prior_review"]["amount"],
                          partial["prior_review"]["amount_column"]), ("8","1.25","G"))

    def test_saved_original_price_survives_partial_reaudit_and_file_rename(self):
        head = ["笔记链接","评论内容","图片需求",None,None,None,"审核结果","金额"]
        tasks = [head, ["https://xhslink.com/a", "正文", "配图",None,None,None,"文字成功","1.25"]]
        summary = [[] for _ in range(10)]
        summary.append([None]*15 + ["案例ID","团长","产品名","工作表","原行","要求类型","结果分类","分流","核定金额","结算说明","原任务报价"])
        summary.append([None]*15 + ["已改名的旧文件:任务:2","团长","产品","任务","2","text+image","文字成功","成功","1.25",None,"7.75"])
        result = self.load({"任务":tasks, "审核汇总":summary})[0]
        self.assertEqual((result["expected_amount"],result["prior_review"]["amount"]), ("7.75","1.25"))
