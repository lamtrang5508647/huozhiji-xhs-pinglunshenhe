"""Both settlement and screenshot references must survive re-import and directory moves."""
import base64
import copy
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree as ET

from huozhiji_audit import convert_table, review
from huozhiji_audit._engine.build_audited_workbook import P, R, add_worksheet_part, build_workbook, encode, set_cell, tag
from huozhiji_audit._engine.screenshot_evidence import NoteScreenshots, attach_screenshots
from huozhiji_audit._engine.audit_validation import validate_results
from huozhiji_audit._engine.screenshot_drawing import XDR, relfile
from huozhiji_audit._engine.workbook_columns import shift_formula
from huozhiji_audit._engine.table_to_expected import xlsx_shared_strings, xlsx_sheet_members, xlsx_text
from test_portable_api import fixture


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aAL8AAAAASUVORK5CYII=")


def sheet_data(book, name):
    with zipfile.ZipFile(book) as archive:
        member = xlsx_sheet_members(archive)[name]
        sheet = ET.fromstring(archive.read(member))
        strings = xlsx_shared_strings(archive)
        values = {c.get("r"): xlsx_text(c, strings) for c in sheet.iter(tag("c"))}
        links = {c.get("ref"): c.attrib for c in sheet.iter(tag("hyperlink"))}
        relpath = member.replace("/worksheets/", "/worksheets/_rels/") + ".rels"
        rels = {r.get("Id"): r.attrib for r in ET.fromstring(archive.read(relpath))} if relpath in archive.namelist() else {}
        return values, links, rels


def link_target(links, relationships, cell):
    relation = relationships[links[cell][f"{{{R}}}id"]]
    if relation["Type"] != R + "/hyperlink" or relation["TargetMode"] != "External":
        raise AssertionError(f"{cell} is not an external hyperlink")
    return relation["Target"]


class ScreenshotWorkbookTests(unittest.TestCase):
    def test_column_references_preserve_absolute_refs_and_literals(self):
        self.assertEqual(shift_formula('SUM($H$2:H9)+G2+"H2"+Other!H2', '品牌A', '品牌A', 8),
                         'SUM($I$2:I9)+G2+"H2"+Other!H2')
        self.assertEqual(shift_formula("'品牌A'!H2+'品牌A'!$H:$J", '汇总', '品牌A', 8),
                         "'品牌A'!I2+'品牌A'!$I:$K")
        self.assertEqual(shift_formula('[other.xlsx]品牌A!H2+"H2"+Table1[H2]', '品牌A', '品牌A', 8),
                         '[other.xlsx]品牌A!H2+"H2"+Table1[H2]')

    def test_inline_success_images_follow_rows_and_second_pass_replaces_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "源表.xlsx"
            obs = fixture(source)
            rows = convert_table(source)["rows"]
            report = review(rows, obs)
            writer = NoteScreenshots(base / "评论截图", "xiaohongshu", "n1")
            shot = writer.save(Mock(screenshot=Mock(return_value=PNG)), "comment", ["a"])
            result = report["results"][0]
            shot["comment_ids"] = [str(result["matched_comment"].get("comment_id"))]
            shot["comment_texts"] = [result["matched_comment"]["text"]]
            result.update(screenshot_status="captured", comment_screenshots=[shot])
            # Negative rows must not be given an unrelated success picture.
            report["results"][1].update(screenshot_status="captured", comment_screenshots=[shot])
            out = base / "导出.xlsx"
            details = build_workbook(source, rows, report["results"], out, screenshot_base=base)
            self.assertEqual(details["screenshot_index"]["images"], 1)
            values = sheet_data(out, "品牌A")[0]
            self.assertEqual((values["G2"],values["H1"],values["I2"]), ("成功","评论截图","1.50"))
            self.assertEqual((values["G3"],values["H3"]), ("失败",""))
            with zipfile.ZipFile(out) as book:
                self.assertEqual(set(xlsx_sheet_members(book)), {"品牌A","审核汇总"})
                drawing = ET.fromstring(book.read("xl/drawings/auditShots1.xml"))
                origin = drawing[0].find(f"{{{XDR}}}from")
                self.assertEqual((origin.findtext(f"{{{XDR}}}col"), origin.findtext(f"{{{XDR}}}row")), ("7","1"))
                self.assertEqual(book.read("xl/media/image1.png"), b"synthetic-preservation-fixture")
                sheet = ET.fromstring(book.read(xlsx_sheet_members(book)["审核汇总"]))
                formula = sheet.find(f".//{tag('c')}[@r='X12']/{tag('f')}")
                self.assertIn("I2", formula.text)
            later = convert_table(out)["rows"]
            self.assertEqual(later[0]["prior_review"]["amount_column"], "I")
            renewed = review(later,obs)
            second = base / "二审.xlsx"
            build_workbook(out,later,renewed["results"],second,screenshot_base=base)
            self.assertEqual(sheet_data(second,"品牌A")[0]["H2"],"未取得对应评论截图")
            self.assertEqual(sum(v=="评论截图" for v in sheet_data(second,"品牌A")[0].values()),1)
            with zipfile.ZipFile(second) as book:
                self.assertEqual(len(ET.fromstring(book.read("xl/drawings/auditShots1.xml"))),0)

    def test_explicit_unavailable_page_uses_its_visit_and_survives_reimport(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "源表.xlsx"
            observations = fixture(source)["observations"]
            rows = convert_table(source)["rows"]
            stamp = "2026-11-25T10:00:00+00:00"
            older = "2026-11-24T10:00:00+00:00"
            recorder = NoteScreenshots(base/"评论截图","xiaohongshu","n1")
            closed = recorder.save(Mock(screenshot=Mock(return_value=PNG)), "unavailable")
            stale = recorder.save(Mock(screenshot=Mock(return_value=PNG)), "comment", ["old"])
            confirmed = dict(platform="xiaohongshu",target_key="n1",resolved_target_key="n1",captured_at=stamp,
                             capture_status="inaccessible",target_page_state="post_unavailable",
                             screenshot_status="captured",comment_screenshots=[closed])
            generic = dict(confirmed, target_key="n2", resolved_target_key="n2", target_page_state=None,
                           warnings=["note_identity_unconfirmed"],screenshot_status="missing",comment_screenshots=[])
            wrong = dict(confirmed, target_key="n3", resolved_target_key="different")
            history = dict(confirmed,captured_at=older,target_page_state=None,comment_screenshots=[stale])
            observations[:3] = [history,confirmed,generic,wrong]
            report = review(rows,observations)
            attach_screenshots(report,observations,base,enabled=True)
            decision = report["results"][0]
            self.assertEqual((decision["status"],decision["result_category"],decision["triage"],decision["approved_amount"]),
                             ("blocked","原帖不可访问","待人审",None))
            self.assertEqual(decision["capture"]["captured_at"],stamp)
            self.assertEqual(decision["comment_screenshots"], [closed])
            self.assertEqual([r["result_category"] for r in report["results"][1:3]],["待人审","待人审"])
            self.assertEqual(report["summary"]["result_category_counts"]["原帖不可访问"], 1)
            # An old short-link observation has no same-visit proof tying the short code to the resolved note.
            short = dict(rows[0],target_url="https://xhslink.com/a/shortCode",target_key="shortCode")
            for unverified_id in ("a"*24, None, "shortCode"):
                uncertain = dict(confirmed,target_key="shortCode",resolved_target_key=unverified_id)
                short_result = review([short],[uncertain])["results"][0]
                self.assertEqual((short_result["result_category"],short_result["approved_amount"]),("待人审",None))
            bad = copy.deepcopy(report["results"])
            bad[0]["capture"]["target_page_state"] = None
            with self.assertRaisesRegex(RuntimeError,"unconfirmed_post_unavailable"):
                validate_results(rows,bad)

            output = base/"一审.xlsx"
            build_workbook(source, rows, report["results"], output)
            self.assertEqual(sheet_data(output,"品牌A")[0]["G2"],"原帖不可访问")
            self.assertEqual(sheet_data(output,"品牌A")[0]["H1"],"评论截图")
            self.assertEqual(sheet_data(output,"品牌A")[0]["I2"],"")
            summary = sheet_data(output,"审核汇总")[0]
            self.assertEqual((summary["V12"],summary["W12"],summary["T1"]),("原帖不可访问","待人审","1"))
            rerows = convert_table(output)["rows"]
            prior = rerows[0]["prior_review"]
            self.assertEqual((prior["category"],prior["triage"],prior["amount"]),("原帖不可访问","待人审",None))
            second = review(rerows,observations)
            self.assertEqual(second["results"][0]["review_action"],"carried_forward")
            self.assertEqual(second["summary"]["result_category_counts"]["原帖不可访问"],1)
            rerun = base/"二审.xlsx"
            build_workbook(output,rerows,second["results"],rerun)
            self.assertEqual(sheet_data(rerun,"品牌A")[0]["G2"],"原帖不可访问")
            self.assertEqual(sheet_data(rerun,"品牌A")[0]["H1"],"评论截图")
