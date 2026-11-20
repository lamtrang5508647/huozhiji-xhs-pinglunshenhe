import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from huozhiji_audit import audit_workbook, convert_table
from huozhiji_audit._engine.audit_validation import validate_results
from huozhiji_audit._engine.build_audited_workbook import cell_value, set_cell, tag
from test_portable_api import fixture


class InputCoverageTests(unittest.TestCase):
    def test_douyin_homepage_modal_links_keep_distinct_official_note_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "任务.csv"
            def convert_links(*links):
                source.write_text("文章链接,评论内容\n" + "".join(f"{link},目标\n" for link in links), encoding="utf-8")
                return convert_table(source)["rows"]
            rows = convert_links("https://www.douyin.com/user/example?modal_id=101&private=trim",
                                 "https://www.douyin.com/user/example?modal_id=202",
                                 "https://www.douyin.com.example.org/user/example?modal_id=101",
                                 "https://www.douyin.com/share/slides/303/")
            self.assertEqual([r["target_key"] for r in rows], ["101", "202", "example", "303"])
            self.assertEqual([r["target_url"] for r in rows[:2]], ["https://www.douyin.com/video/101", "https://www.douyin.com/video/202"])
            self.assertEqual(rows[2]["platform"], "")
            for path in ("video/101?modal_id=202", "user/example?modal_id=101&modal_id=202", "user/example?modal_id=abc"):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "ambiguous_or_invalid_douyin_video_id"):
                    convert_links("https://www.douyin.com/" + path)

    def test_only_post_column_uses_hyperlink_destinations_including_merged_rows(self):
        from huozhiji_audit._engine.table_to_expected import read_xlsx
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "task.xlsx"
            fixture(source)
            with zipfile.ZipFile(source) as book:
                parts = {name: book.read(name) for name in book.namelist()}
            member = "xl/worksheets/sheet1.xml"
            root = ET.fromstring(parts[member])
            data = root.find(tag("sheetData"))
            set_cell(data, "A1", "打开帖子")
            set_cell(data, "A2", "点击打开")
            set_cell(data, "A3", None)
            ET.SubElement(root.find(tag("mergeCells")), tag("mergeCell"), {"ref": "A2:A3"})
            link = "https://www.xiaohongshu.com/explore/n1?xsec_token=fixture"
            rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            links = ET.SubElement(root, tag("hyperlinks"))
            for ref in ("A2", "D2"):
                ET.SubElement(links, tag("hyperlink"), {"ref": ref, f"{{{rel}}}id": "post"})
            parts[member] = ET.tostring(root)
            parts["xl/worksheets/_rels/sheet1.xml.rels"] = (
                f'<Relationships><Relationship Id="post" Type="{rel}/hyperlink" TargetMode="External" Target="{link}"/></Relationships>'
            )
            with zipfile.ZipFile(source, "w") as book:
                for name, contents in parts.items():
                    book.writestr(name, contents)
            self.assertEqual(read_xlsx(source, target_url_header="打开帖子")[1]["打开帖子"], link)
            for selection in ({}, {"sheet": "品牌A"}):
                rows = convert_table(source, mapping={"target_url": "打开帖子"}, **selection)["rows"]
                self.assertEqual([r["target_key"] for r in rows[:2]], ["n1", "n1"])
                self.assertEqual(rows[0]["comment_text"], "已收到")
                self.assertNotIn("xsec_token", str(rows))

    def test_short_reply_headers_and_cached_wps_images_survive_export(self):
        for header in ("需要回复评论内容", "回复评论内容"):
            with self.subTest(header=header), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "task.xlsx"
                output = Path(tmp) / "audited.xlsx"
                observations = fixture(source)
                with zipfile.ZipFile(source) as book:
                    parts = {name: book.read(name) for name in book.namelist()}
                member = "xl/worksheets/sheet1.xml"
                root = ET.fromstring(parts[member])
                data = root.find(tag("sheetData"))
                set_cell(data, "E1", header)
                set_cell(data, "D2", None)
                set_cell(data, "E2", "已收到")
                set_cell(data, "F6", "#VALUE!", formula='_xlfn.DISPIMG("fixture",1)').set("t", "e")
                parts[member] = ET.tostring(root)
                with zipfile.ZipFile(source, "w") as book:
                    for name, contents in parts.items():
                        book.writestr(name, contents)
                report = audit_workbook(source, observations, output)
                by_row = {result["source_row"]: result for result in report["results"]}
                self.assertEqual(sorted(by_row), list(range(2, 8)))
                self.assertEqual(by_row[2]["result_category"], "成功")
                self.assertEqual(by_row[6]["result_category"], "成功")
                with zipfile.ZipFile(output) as book:
                    cells = {c.get("r"): cell_value(c, []) for c in ET.fromstring(book.read(member)).iter(tag("c"))}
                self.assertEqual([cells[f"G{number}"] for number in sorted(by_row)],
                                 [result["result_category"] for number, result in sorted(by_row.items())])

    def test_json_rows_with_different_optional_keys_are_all_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.json"
            records = [{"笔记ID": "n", "评论": "root"}, {"回复内容": "reply"}, {"评论图片需求": "图片"}]
            path.write_text(json.dumps(records), encoding="utf-8")
            rows = convert_table(path)["rows"]
        self.assertEqual([(r["source_row"], r["comment_type"]) for r in rows],
                         [(2, "text"), (3, "text"), (4, "image")])
        self.assertEqual([r["target_key"] for r in rows], ["n", "n", "n"])

    def test_explicit_image_only_type_is_a_task_without_a_reference_image(self):
        from huozhiji_audit._engine.table_to_expected import convert
        rows = convert([{"作品ID": "n", "类型": "图片"},
                        {"作品ID": "m", "类型": ""}], {}, "fixture.csv", amount_rules={"image": "1.00"})
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source_row"], rows[0]["has_image"], rows[0]["expected_amount"]), (2, True, "1.00"))

    def test_unknown_requirement_schema_does_not_silently_discard_a_sheet(self):
        from huozhiji_audit._engine.table_to_expected import convert
        rows = [{"作品ID": "n", "正文列": "fixture"}]
        with self.assertRaisesRegex(ValueError, "Missing required column mapping.*comment_text"):
            convert(rows, {}, "task.xlsx", "Sheet2")
        self.assertEqual(convert(rows, {"comment_text": "正文列"}, "task.xlsx", "Sheet2")[0]["comment_text"], "fixture")
        with self.assertRaisesRegex(ValueError, "Mapped column not found"):
            convert(rows, {"comment_text": "wrong"}, "task.xlsx", "Sheet2")

    def test_report_with_correct_ids_but_swapped_row_provenance_is_rejected(self):
        rows = [{"case_id": "a", "source_sheet": "sheet", "source_row": 2},
                {"case_id": "b", "source_sheet": "sheet", "source_row": 3}]
        results = [{**row, "triage": "成功", "result_category": "成功", "approved_amount": "2"} for row in rows]
        validate_results(rows, list(reversed(results)))
        for key, wrong in (("source_row", 3), ("source_sheet", "other")):
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, "report_source_location_mismatch"):
                validate_results(rows, [{**results[0], key: wrong}, results[1]])
