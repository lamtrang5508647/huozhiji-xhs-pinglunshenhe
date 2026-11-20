import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from huozhiji_audit import audit_workbook, convert_table, review
from huozhiji_audit._engine.build_audited_workbook import cell_value, set_cell, tag
from test_portable_api import fixture


class SpecifiedImageTests(unittest.TestCase):
    digest = "a"*64

    def check(self, task, comments):
        row = dict(case_id="one", platform="xiaohongshu", target_key="n", expected_amount="3.50",
                   partial_text_amount="1.50", comment_text="目标")
        row.update(task)
        observation = dict(platform="xiaohongshu", target_key="n", capture_status="ok", capture_complete=True,
                           image_detection_complete=True, comments=comments)
        result = review([row], [observation])["results"][0]
        return result["result_category"], result["approved_amount"], result.get("settlement_status")

    def test_text_is_visible_but_an_arbitrary_image_or_a_hash_on_another_comment_cannot_settle(self):
        on_body = dict(text="目标", has_image=True, comment_id="body")
        other = dict(text="其他文字", has_image=True, image_sha256=self.digest, comment_id="other")
        for field in ({"image_requirement":"必须使用指定图片"}, {"required_image_sha256":self.digest},
                      {"required_image_sha256":"not a digest"}):
            with self.subTest(field=field):
                self.assertEqual(self.check(field,[dict(on_body, image_sha256="b"*64), other]),
                                 ("文字成功",None,"pending_specified_image"))
        self.assertEqual(self.check({"required_image_sha256":self.digest}, [on_body,dict(on_body,comment_id="yes",image_sha256s=[self.digest.upper()])]),
                         ("图字成功","3.50",None))

    def test_image_only_and_ordinary_requirements(self):
        bare = dict(text="", has_image=True, comment_id="image")
        task = dict(comment_text="", required_image_sha256=self.digest)
        self.assertEqual(self.check(task,[bare]), ("待人审",None,"pending_specified_image"))
        self.assertEqual(self.check(task,[dict(text="图片状态未知",has_image=None)]), ("待人审",None,"pending_specified_image"))
        self.assertEqual(self.check(task,[dict(bare,image_sha256=self.digest)]), ("成功","3.50",None))
        for generic in ("配图","embedded-image.png","无需使用指定图片"):
            self.assertEqual(self.check(dict(image_requirement=generic,has_image=True), [dict(bare,text="目标")]), ("图字成功","3.50",None))

    def test_workbook_preserves_text_and_withholds_the_price_until_attachments_match(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder,"源表.xlsx"), Path(folder,"审核表.xlsx")
            evidence = fixture(source)
            with zipfile.ZipFile(source) as z:
                parts = {n:z.read(n) for n in z.namelist()}
            name = "xl/worksheets/sheet1.xml"
            s=ET.fromstring(parts[name]); data=s.find(tag("sheetData"))
            set_cell(data,"F7","必须使用指定图片")
            set_cell(data,"I1","指定图片SHA256")
            set_cell(data,"I7",self.digest)
            parts[name]=ET.tostring(s)
            with zipfile.ZipFile(source, "w") as z:
                for n, raw in parts.items(): z.writestr(n,raw)
            self.assertEqual(convert_table(source)["rows"][-1]["required_image_sha256"], self.digest)
            for proven in (False,True):
                if proven: evidence["observations"][-1]["comments"][0]["image_sha256"]=self.digest
                result=audit_workbook(source,evidence,output)["results"][-1]
                with zipfile.ZipFile(output) as z:
                    cells={c.get("r"):cell_value(c,[]) for c in ET.fromstring(z.read(name)).iter(tag("c"))}
                    totals={c.get("r"):cell_value(c,[]) for c in ET.fromstring(z.read("xl/worksheets/sheet2.xml")).iter(tag("c"))}
                self.assertEqual((cells["G7"],cells["H7"]),("图字成功","2.00") if proven else ("文字成功",""))
                if not proven:
                    self.assertIn("specified_image_unconfirmed",result["reason_codes"])
                    self.assertIn("指定图片", totals["Y17"])
