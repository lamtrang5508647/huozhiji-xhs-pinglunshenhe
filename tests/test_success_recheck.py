"""Second checks must account for every source row while visiting only prior successes."""
import copy
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from huozhiji_audit._engine.audit_pass import compare_audit_rows
from huozhiji_audit._engine.audit_validation import validate_results
from huozhiji_audit._engine.build_audited_workbook import build_workbook, cell_value, set_cell, tag
from test_portable_api import fixture


def task(n, prior, *, amount=None, **fields):
    normalized = prior if prior in ("成功", "失败", "待人审") else ""
    return {"case_id": str(n), "source_sheet": "品牌A", "source_row": n, "platform": "xiaohongshu",
            "target_key": "note", "comment_type": "text", "comment_text": f"评论{n}", "expected_amount": "3.25",
            "prior_review": {"category": normalized, "raw_category": prior, "triage": normalized,
                             "amount": amount, "result_column": "J", "amount_column": "K"}, **fields}


def capture(*comments):
    return {"platform": "xiaohongshu", "target_key": "note", "capture_status": "ok", "capture_complete": True,
            "image_detection_complete": True, "comments": list(comments)}


class SuccessRecheckTests(unittest.TestCase):
    def test_same_note_and_separate_note_only_recheck_success(self):
        rows = [task(2, "成功"), task(3, "失败", amount="12"), task(4, "成功"), task(5, "", amount="11"),
                task(6, "待人审"), task(7, "未提交", target_key="other")]
        seen = [{"text": f"评论{n}", "comment_id": str(n), "parent_comment_id": "parent", "has_image": False}
                for n in (2, 3, 5, 6, 7)]
        report = compare_audit_rows(rows, [capture(*seen)])
        scope = report["summary"]["audit_pass"]
        self.assertEqual(scope["reviewed_triage_counts"], {"成功": 1, "失败": 1, "待人审": 0})
        self.assertEqual((scope["reviewed_rows"], scope["carried_rows"]), (2, 4))
        self.assertEqual(report["summary"]["triage_counts"], {"成功": 1, "失败": 2, "待人审": 1, "未复核": 2})
        self.assertEqual([r["source_row"] for r in report["results"]], list(range(2, 8)))
        self.assertEqual(report["results"][-1]["result_category"], "未提交")
        for result in report["results"]:
            if result["status"] == "carried_forward":
                self.assertNotIn("matched_comment", result)
                self.assertIsNone(result["approved_amount"])
        self.assertEqual(str(validate_results(rows, report["results"])), "3.25")
        forged = copy.deepcopy(report["results"])
        forged[1] = dict(forged[0], case_id="3", source_row=3)
        with self.assertRaisesRegex(RuntimeError, "missing_carried_forward_row"):
            validate_results(rows, forged)

    def test_zero_active_rows_uses_no_evidence(self):
        def unvisited():
            raise AssertionError("visited evidence although prior workbook has no successes")
            yield
        rows = [task(2, "失败", amount=0), task(3, "失败"), task(4, ""), task(5, "待人审")]
        report = compare_audit_rows(rows, unvisited())
        self.assertEqual(report["summary"]["audit_pass"]["reviewed_rows"], 0)
        self.assertEqual([r["approved_amount"] for r in report["results"]], ["0.00", None, None, None])

    def test_combined_json_keeps_rows_from_new_workbook_eligible(self):
        rows = [task(2, "成功"), task(3, "失败"), task(4, ""), task(5, "", source_file="new.xlsx")]
        del rows[3]["prior_review"]
        report = compare_audit_rows(rows, [capture({"text": "评论5", "has_image": False})])
        self.assertEqual([r["review_action"] for r in report["results"]],
                         ["rechecked", "carried_forward", "carried_forward", "first_pass"])
        self.assertEqual(report["summary"]["audit_pass"]["first_pass_rows"], 1)
        self.assertEqual(report["results"][-1]["triage"], "成功")
        self.assertEqual(str(validate_results(rows, report["results"])), "3.25")

    def test_frozen_parent_still_resolves_old_strict_mode(self):
        rows = [task(2, "失败"), task(3, "成功", reply_to="回复第1条")]
        evidence = capture({"comment_id": "p", "text": "评论2", "has_image": False},
                           {"comment_id": "c", "text": "评论3", "has_image": False, "parent_comment_id": "p"})
        report = compare_audit_rows(rows, [evidence], require_reply_parent=True)
        self.assertEqual([r["status"] for r in report["results"]], ["carried_forward", "verified"])
        self.assertEqual(report["results"][1]["matched_comment"]["comment_id"], "c")

    def test_unidentified_image_on_mixed_note_keeps_all_possible_source_owners(self):
        rows = [task(2, "失败", comment_type="text+image"), task(3, "成功", comment_type="text+image")]
        report = compare_audit_rows(rows, [capture({"comment_id": "image", "text": "", "has_image": True})])
        self.assertIn("ambiguous_image_only_candidate", report["results"][1]["reason_codes"])

    def test_workbook_keeps_unreviewed_cells_and_excludes_source_quotes(self):
        for amount_col in ("K", None):
            with self.subTest(amount_column=amount_col), tempfile.TemporaryDirectory() as temp:
                rows = [task(2, "成功"), task(3, "失败", amount="42"), task(4, ""), task(5, "待人审")]
                source, output = Path(temp, "source.xlsx"), Path(temp, "output.xlsx")
                fixture(source)
                with zipfile.ZipFile(source) as archive:
                    parts = {n: archive.read(n) for n in archive.namelist()}
                sheet = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
                data = sheet.find(tag("sheetData"))
                set_cell(data, "J1", "审核状态")
                for r in rows:
                    r["prior_review"]["amount_column"] = amount_col
                    n = r["source_row"]
                    set_cell(data, f"J{n}", r["prior_review"]["raw_category"] or None)
                    set_cell(data, f"G{n}", f"业务备注{n}")
                if amount_col:
                    set_cell(data, "K1", "核定金额")
                    set_cell(data, "K3", 42, formula="40+2")
                    set_cell(data, "K4", 99)
                parts["xl/worksheets/sheet1.xml"] = ET.tostring(sheet)
                with zipfile.ZipFile(source, "w") as archive:
                    for name, value in parts.items():
                        archive.writestr(name, value)
                results = compare_audit_rows(rows, [capture({"text": "评论2", "has_image": False})])["results"]
                build_workbook(source, rows, results, output)
                with zipfile.ZipFile(output) as archive:
                    def cells(part):
                        return {c.get("r"): c for c in ET.fromstring(archive.read(part)).iter(tag("c"))}
                    product = cells("xl/worksheets/sheet1.xml")
                    summary = cells("xl/worksheets/sheet2.xml")
                self.assertEqual(cell_value(product["J2"], []), "成功")
                self.assertEqual(cell_value(product["K2"], []), "3.25")
                self.assertEqual(cell_value(product["G2"], []), "业务备注2")
                self.assertEqual(cell_value(product["J4"], []), "")
                if amount_col:
                    self.assertEqual(product["K3"].findtext(tag("f")), "40+2")
                    self.assertEqual(cell_value(product["K4"], []), "99")
                else:
                    self.assertEqual(cell_value(product["K1"], []), "核定金额")
                    self.assertNotIn("K4", product)
                self.assertIsNone(summary["X13"].find(tag("f")))
                self.assertEqual(cell_value(summary["X13"], []), "")
                self.assertEqual(cell_value(summary["W14"], []), "未复核")
                self.assertEqual(cell_value(summary["Q7"], []), "1")
                self.assertEqual(cell_value(summary["I3"], []), "3.25")
                self.assertAlmostEqual(float(cell_value(summary["H3"], [])), 1 / 3)
                self.assertEqual(cell_value(summary["Z12"], []), "3.25")
                self.assertIn("!J2", summary["V12"].findtext(tag("f")))

    def test_amount_merge_is_split_without_losing_original_value_on_frozen_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp, "source.xlsx"), Path(temp, "output.xlsx")
            fixture(source)
            with zipfile.ZipFile(source) as archive:
                parts = {n: archive.read(n) for n in archive.namelist()}
            rows = [task(i, status, amount="9") for i, status in enumerate(("成功", "失败", "", "成功", "待人审"), 2)]
            sheet = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
            data = sheet.find(tag("sheetData"))
            for row in rows:
                set_cell(data, f"J{row['source_row']}", row["prior_review"]["raw_category"] or None)
            set_cell(data, "K2", 9, formula="1+8")
            ET.SubElement(sheet.find(tag("mergeCells")), tag("mergeCell"), {"ref": "K2:K6"})
            parts["xl/worksheets/sheet1.xml"] = ET.tostring(sheet)
            with zipfile.ZipFile(source, "w") as archive:
                for name, content in parts.items():
                    archive.writestr(name, content)
            results = compare_audit_rows(rows, [capture({"text": "评论2", "has_image": False})])["results"]
            report = build_workbook(source, rows, results, output)
            self.assertEqual(report["approved_amount"], "3.25")
            with zipfile.ZipFile(output) as archive:
                saved = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            cells = {c.get("r"): c for c in saved.iter(tag("c"))}
            self.assertEqual([cell_value(cells[f"K{i}"], []) for i in (2, 3, 5, 6)], ["3.25", "9", "0.00", "9"])
            self.assertIn("K3:K4", [m.get("ref") for m in saved.iter(tag("mergeCell"))])
