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
from huozhiji_audit._engine.workbook_screenshots import display_gallery
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
            self.assertEqual(sheet_data(output,"评论原图")[0]["D3"],"原帖不可访问")
            self.assertEqual(sheet_data(output,"评论原图")[0]["E3"],"未核定")
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
            self.assertEqual(sheet_data(rerun,"评论截图")[0]["G3"],"本次未复核")

    def test_tall_images_and_many_related_rows_have_space_and_never_overlap(self):
        tasks=[dict(number=i+3,source="表1",location="'表1'!A2",body="核对任务",category="成功",amount=1,
                    status="已留图",images=[1],carried=False,url="",url_label="打开原笔记") for i in range(3)]
        pics=[dict(number=1,width=400,height=2200,tasks=[3,4],kind="评论",key="n",platform="小红书",stamp="2026-11-25"),
              dict(number=2,width=400,height=130,tasks=[5],kind="评论",key="n",platform="小红书",stamp="2026-11-25")]
        root=display_gallery(pics,tasks,[None,None,None],[])
        heights={int(row.get("r")):float(row.get("ht")) for row in root.iter(tag("row")) if row.get("ht")}
        start,following=pics[0]["row"],pics[1]["row"]
        self.assertGreaterEqual(sum(heights[n] for n in range(start,following))*4/3,2200+14)
        self.assertLessEqual(max(heights.values()),409)

    def test_replace_owned_anchors_but_retain_shared_media_and_custom_gallery_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            src=base/"input.xlsx"
            obs=fixture(src)
            rows=convert_table(src)["rows"]
            results=review(rows,obs)["results"]
            recorder=NoteScreenshots(base/"评论截图","xiaohongshu","n1")
            pic=recorder.save(Mock(screenshot=Mock(return_value=PNG)),"comment",["c1"])
            results[0].update(screenshot_status="captured",comment_screenshots=[pic])
            first=base/"first.xlsx"
            build_workbook(src,rows,results,first)
            with zipfile.ZipFile(first) as archive:
                parts={n:archive.read(n) for n in archive.namelist()}
                idx=xlsx_sheet_members(archive)["评论截图"]
            drawing_path="xl/drawings/auditShots1.xml"
            dr=ET.fromstring(parts[drawing_path])
            user=copy.deepcopy(dr[0])
            c=user.find(f".//{{{XDR}}}cNvPr")
            c.attrib.update(id="77",name="财务自己插的图片",descr="收款凭证")
            dr.append(user)
            parts[drawing_path]=encode(dr)
            # Simulate the older released index which linked to loose PNGs in H/N.
            index=ET.fromstring(parts[idx])
            rels=ET.Element(f"{{{P}}}Relationships")
            for link in index.iter(tag("hyperlink")):
                if link.get("ref","")[:1] in ("H","N"):
                    link.attrib.pop("location",None)
                    link.set(f"{{{R}}}id","oldExternal")
            ET.SubElement(rels,f"{{{P}}}Relationship",{"Id":"oldExternal","Type":R+"/hyperlink",
                          "Target":"old-folder/comment.png","TargetMode":"External"})
            parts[idx]=encode(index)
            parts[relfile(idx)]=encode(rels)
            with zipfile.ZipFile(first,"w") as archive:
                for name,blob in parts.items():archive.writestr(name,blob)
            latest=convert_table(first)["rows"]
            out=base/"next.xlsx"
            build_workbook(first,latest,review(latest,obs)["results"],out)
            with zipfile.ZipFile(out) as archive:
                dr=ET.fromstring(archive.read(drawing_path))
                self.assertEqual(len(dr),1)
                self.assertEqual(dr.find(f".//{{{XDR}}}cNvPr").get("name"),"财务自己插的图片")
                self.assertEqual(archive.read(f"xl/media/audit-shot-{pic['sha256']}.png"),PNG)
                current = ET.fromstring(archive.read(relfile(idx)))
                self.assertTrue(all(r.get("Target","").startswith("https://www.xiaohongshu.com/explore/") for r in current))
                self.assertNotIn("oldExternal", {r.get("Id") for r in current})
                self.assertEqual(archive.read("xl/media/image1.png"),b"synthetic-preservation-fixture")

    def test_shared_images_user_sheet_and_second_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "交付"
            folder.mkdir()
            source = folder / "输入.xlsx"
            observations = fixture(source)
            with zipfile.ZipFile(source) as archive:
                parts = {n: archive.read(n) for n in archive.namelist()}
            first_url = "https://www.xiaohongshu.com/explore/n1?xsec_token=test-a&xsec_source=pc_share"
            third_url = "https://www.xiaohongshu.com/explore/n3?xsec_token=test-b"
            user_sheet = parts["xl/worksheets/sheet1.xml"].replace(b"/explore/n2", b"/explore/n1")
            for key, url in (("n1", first_url), ("n3", third_url)):
                user_sheet = user_sheet.replace(
                    ("https://www.xiaohongshu.com/explore/" + key).encode(),
                    url.replace("&", "&amp;").encode(),
                )
            parts["xl/worksheets/sheet1.xml"] = user_sheet
            core = ("xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml")
            wb, rels, types = [ET.fromstring(parts[p]) for p in core]
            member = add_worksheet_part(parts, wb.find(tag("sheets")), rels, types, "评论截图", "rIdUser")
            ledger = ET.Element(tag("worksheet"))
            set_cell(ET.SubElement(ledger, tag("sheetData")), "A1", "财务自建台账")
            parts[member] = encode(ledger)
            for p, node in zip(core, (wb, rels, types)):
                parts[p] = encode(node, parts[p])
            with zipfile.ZipFile(source, "w") as archive:
                for p, b in parts.items():
                    archive.writestr(p, b)
            rows = convert_table(source)["rows"]
            results = review(rows, observations)["results"]
            recorder = NoteScreenshots(folder / "截图 #1", "xiaohongshu", "n1")
            pic = recorder.save(Mock(screenshot=Mock(return_value=PNG)), "comment", ["c1"])
            end = recorder.save(Mock(screenshot=Mock(return_value=PNG)), "end", [])
            for result in results[:2]:
                result.update(screenshot_status="captured", comment_screenshots=[pic, end])
            results[2].update(screenshot_status="captured", comment_screenshots=[dict(pic, path="../secret.png")])
            out = folder / "审核.xlsx"
            details = build_workbook(source, rows, results, out)
            index = details["screenshot_index"]["sheet"]
            self.assertEqual(details["screenshot_index"], {"sheet": "评论截图（2）", "gallery":"评论原图", "tasks": 6, "images": 2})
            self.assertEqual(sheet_data(out, "评论截图")[0]["A1"], "财务自建台账")
            moved = Path(tmp) / "另一台电脑"
            moved.mkdir()
            shutil.copyfile(out, moved/out.name)
            first = moved / out.name
            values, links, rels = sheet_data(first, index)
            self.assertEqual(values["H3"], "图 1、2")
            self.assertEqual(values["H4"], "图 1、2")
            self.assertEqual(values["G5"], "截图文件缺失或无效")
            self.assertEqual(links["C3"]["location"], "'品牌A'!A2")
            self.assertEqual(links["H3"]["location"], links["N3"]["location"])
            self.assertNotIn("H5", links)
            self.assertEqual(link_target(links,rels,"B3"), first_url)
            self.assertEqual(link_target(links,rels,"B4"), first_url)
            self.assertEqual(link_target(links,rels,"B5"), third_url)
            gallery, glinks, grels = sheet_data(first,"评论原图")
            self.assertEqual(links["H3"]["location"],"'评论原图'!A3")
            self.assertEqual((gallery["B3"],gallery["C3"],gallery["D3"],gallery["E3"]),("品牌A · 2 行","已收到","成功","1.50"))
            self.assertEqual((gallery["B4"],gallery["C4"],gallery["D4"],gallery["E4"]),("品牌A · 3 行","找不到","失败","0.00"))
            self.assertTrue(gallery["J3"])
            self.assertEqual(sum(r["Type"] == R+"/drawing" for r in grels.values()),1)
            for address, body in gallery.items():
                if not address.startswith("C") or not address[1:].isdigit() or int(address[1:]) < 3:
                    continue
                row = address[1:]
                self.assertEqual(gallery["F" + row], "打开原笔记")
                target = link_target(glinks, grels, "F" + row)
                if body in ("已收到","找不到"):
                    self.assertEqual(target, first_url)
                elif body == "待验证":
                    self.assertEqual(target, third_url)
            self.assertTrue(all(r["Target"].startswith("https://www.xiaohongshu.com/explore/")
                                for r in (*rels.values(), *grels.values()) if r.get("TargetMode") == "External"))
            with zipfile.ZipFile(first) as archive:
                added = [p for p in archive.namelist() if p.startswith("xl/media/audit-shot-")]
                self.assertEqual(len(added),1)
                self.assertEqual(archive.read(added[0]),PNG)
                drawing=ET.fromstring(archive.read("xl/drawings/auditShots1.xml"))
                self.assertEqual(len(drawing.findall(f"{{{XDR}}}oneCellAnchor")),2)
                self.assertEqual(len(set(pic.find(f"{{{XDR}}}from/{{{XDR}}}row").text for pic in drawing)),2)
                workbook=ET.fromstring(archive.read("xl/workbook.xml"))
                active=int(workbook.find(f".//{tag('workbookView')}").get("activeTab"))
                self.assertEqual(workbook.find(tag("sheets"))[active].get("name"),"评论原图")

            later = convert_table(first)["rows"]
            self.assertEqual(len(later), 6)
            self.assertTrue(all(r["source_sheet"] == "品牌A" for r in later))
            rerun = moved / "二审.xlsx"
            renewed = build_workbook(first, later, review(later, observations)["results"], rerun)
            self.assertEqual(renewed["screenshot_index"]["images"], 0)
            values, links, rels = sheet_data(rerun, index)
            self.assertEqual(values["G3"], "未取得截图")
            self.assertEqual(values["G4"], "本次未复核")
            self.assertEqual(link_target(links,rels,"B3"),first_url)
            self.assertEqual(link_target(links,rels,"B4"),first_url)
            self.assertEqual(link_target(links,rels,"B5"),third_url)
            second_gallery, second_links, second_rels = sheet_data(rerun, "评论原图")
            self.assertEqual(second_gallery["B3"], "品牌A · 2 行")
            self.assertEqual(link_target(second_links,second_rels,"F3"),first_url)
            self.assertNotIn("找不到",second_gallery.values())
            self.assertNotIn("H3", links)
            self.assertEqual(sheet_data(rerun, "评论截图")[0]["A1"], "财务自建台账")
            with zipfile.ZipFile(rerun) as archive:
                self.assertFalse(any(p.startswith("xl/media/audit-shot-") for p in archive.namelist()))
                self.assertTrue(archive.read("xl/media/image1.png"))
