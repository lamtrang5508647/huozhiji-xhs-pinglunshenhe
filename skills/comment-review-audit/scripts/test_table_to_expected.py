#!/usr/bin/env python3
"""Offline tests for the table converter, including a minimal XLSX workbook."""

import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from table_to_expected import convert, read_xlsx, infer_platform  # noqa: E402


def run():
    annotated = '@点点 大豆异黄酮对女性有什么好处？ （辛苦@点点要手打艾特出来！）'
    note_rows = convert([{'文章链接':'https://www.xiaohongshu.com/explore/fixture', '评论文字需求':annotated}], {}, 'fixture.xlsx')
    assert note_rows[0]['comment_text'] == '@点点 大豆异黄酮对女性有什么好处？'
    assert note_rows[0]['source_comment_text'] == annotated
    assert note_rows[0]['editorial_note'] == '辛苦@点点要手打艾特出来！'
    ordinary = '体验不错（朋友推荐给我的）'
    assert convert([{'文章链接':'https://www.xiaohongshu.com/explore/fixture', '评论文字需求':ordinary}], {}, 'fixture.xlsx')[0]['comment_text'] == ordinary
    assert infer_platform("https://xhslink.cn/o/example") == "xiaohongshu"
    image_reply_rows = [
        {'文章链接':'https://www.xiaohongshu.com/explore/fixture', '评论文字需求':'查了一下，里面的营养可以加快信息传递', '需要回复评论内容【左边填写回第几条】':'embedded-image.png', '金额【文字1.5，文字加图2元，单图1.5】':''},
        {'文章链接':'https://www.xiaohongshu.com/explore/fixture', '评论文字需求':'回复第一条', '需要回复评论内容【左边填写回第几条】':'embedded-image.png', '金额【文字1.5，文字加图2元，单图1.5】':''},
        {'文章链接':'https://www.xiaohongshu.com/explore/fixture', '评论文字需求':'embedded-image.png', '需要回复评论内容【左边填写回第几条】':'回复图片', '评论图片需求':'我看这个笔记的配方就不错', '金额【文字1.5，文字加图2元，单图1.5】':''},
    ]
    for image_row in image_reply_rows:
        image_row.setdefault('评论图片需求', '')
    converted_images = convert(image_reply_rows, {}, 'fixture.xlsx', '示例')
    assert converted_images[0]['comment_text'] == image_reply_rows[0]['评论文字需求']
    assert converted_images[0]['comment_type'] == 'text+image'
    assert converted_images[0]['expected_amount'] == '2.00'
    assert converted_images[1]['comment_type'] == 'image'
    assert converted_images[2]['comment_text'] == '我看这个笔记的配方就不错'
    assert converted_images[2]['comment_type'] == 'text'
    rows = [{"订单号": "a1", "平台": "抖音", "作品ID": "v1", "评论内容": "已收到", "评论类型": "文字", "金额": "9.90"}]
    converted = convert(rows, {}, "input.csv")
    assert converted[0]["case_id"] == "a1"
    assert converted[0]["target_key"] == "v1"
    assert converted[0]["expected_amount"] == "9.90"

    grouped = [
        {"文章链接": "标题 https://www.xiaohongshu.com/discovery/item/n1?xsec_token=secret", "产品": "巢动力", "负责人": "bx", "团长": "不想早起（完整名称）", "评论文字需求": "回复第一条", "需要回复评论内容【左边填写回第几条】": "已收到", "评论图片需求": "", "金额【文字1.5，文字加图2元，单图1.5】": ""},
        # Source workbooks often put the URL only on the first row of a visual
        # block without actually merging the following URL cells.
        {"文章链接": "", "评论文字需求": "晒单", "需要回复评论内容【左边填写回第几条】": "", "评论图片需求": "=DISPIMG(\"id\",1)", "金额【文字1.5，文字加图2元，单图1.5】": ""},
        {"文章链接": "标题 https://www.xiaohongshu.com/discovery/item/n1?xsec_token=secret", "评论文字需求": "第一条回复第二条", "需要回复评论内容【左边填写回第几条】": "实际回复正文", "评论图片需求": "", "金额【文字1.5，文字加图2元，单图1.5】": ""},
        {"文章链接": "标题 https://www.xiaohongshu.com/discovery/item/n1?xsec_token=secret", "评论文字需求": "embedded-image.png", "需要回复评论内容【左边填写回第几条】": "", "评论图片需求": "", "金额【文字1.5，文字加图2元，单图1.5】": ""},
        {"文章链接": "https://www.xiaohongshu.com/discovery/item/placeholder", "评论文字需求": "", "需要回复评论内容【左边填写回第几条】": "", "评论图片需求": "", "金额【文字1.5，文字加图2元，单图1.5】": ""},
        {"文章链接": "", "评论文字需求": "", "需要回复评论内容【左边填写回第几条】": "", "评论图片需求": "", "金额【文字1.5，文字加图2元，单图1.5】": ""},
    ]
    grouped_converted = convert(
        grouped,
        {},
        "8.21评论.xlsx",
        "月月顺",
    )
    assert grouped_converted[0]["platform"] == "xiaohongshu"
    assert grouped_converted[0]["target_key"] == "n1"
    assert grouped_converted[0]["target_url"] == "https://www.xiaohongshu.com/discovery/item/n1"
    assert grouped_converted[0]["comment_text"] == "已收到"
    assert grouped_converted[0]["expected_amount"] == "1.50"
    assert grouped_converted[0]["product_name"] == "巢动力"
    assert grouped_converted[0]["group_leader"] == "不想早起（完整名称）"
    assert grouped_converted[1]["group_leader"] == "不想早起（完整名称）"
    try:
        convert(grouped, {"group_leader": "负责人"}, "bad.xlsx", "月月顺")
    except ValueError as exc:
        assert "负责人 abbreviations are not allowed" in str(exc)
    else:
        raise AssertionError("负责人 must never be accepted as group_leader")
    assert grouped_converted[1]["target_key"] == "n1"
    assert grouped_converted[1]["comment_type"] == "text+image"
    assert grouped_converted[1]["expected_amount"] == "2.00"
    assert grouped_converted[1]["partial_text_amount"] == "1.50"
    assert grouped_converted[1]["partial_image_amount"] == "1.50"
    assert grouped_converted[2]["reply_to"] == "第一条回复第二条"
    assert grouped_converted[2]["comment_text"] == "实际回复正文"
    assert grouped_converted[3]["comment_text"] == ""
    assert grouped_converted[3]["has_image"] is True
    assert grouped_converted[3]["comment_type"] == "image"
    assert len(grouped_converted) == 4

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "input.xlsx"
        workbook = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>"""
        rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/></Relationships>"""
        sheet = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>case_id</t></is></c><c r="B1" t="inlineStr"><is><t>platform</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>a2</t></is></c><c r="B2" t="inlineStr"><is><t>xhs</t></is></c></row></sheetData></worksheet>"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", rels)
            archive.writestr("xl/worksheets/sheet1.xml", sheet)
        assert read_xlsx(path)[0]["case_id"] == "a2"

        image_path = Path(directory) / "floating-image.xlsx"
        image_sheet = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>文章链接</t></is></c><c r="B1" t="inlineStr"><is><t>评论文字需求</t></is></c><c r="C1" t="inlineStr"><is><t>需要回复评论内容【左边填写回第几条】</t></is></c><c r="F1" t="inlineStr"><is><t>评论图片需求</t></is></c><c r="G1" t="inlineStr"><is><t>金额【文字1.5，文字加图2元，单图1.5】</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>https://www.xiaohongshu.com/discovery/item/n2?token=private</t></is></c><c r="B2" t="inlineStr"><is><t>图文评论</t></is></c></row></sheetData><drawing r:id="rIdDrawing1"/></worksheet>"""
        image_sheet_rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdDrawing1" Target="../drawings/drawing1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"/></Relationships>"""
        drawing = """<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"><xdr:twoCellAnchor><xdr:from><xdr:col>5</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>6</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>2</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to><xdr:pic/></xdr:twoCellAnchor></xdr:wsDr>"""
        with zipfile.ZipFile(image_path, "w") as archive:
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", rels)
            archive.writestr("xl/worksheets/sheet1.xml", image_sheet)
            archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", image_sheet_rels)
            archive.writestr("xl/drawings/drawing1.xml", drawing)
        image_rows = read_xlsx(image_path)
        assert image_rows[0]["评论图片需求"] == "embedded-image.png"
        image_converted = convert(image_rows, {}, "floating-image.xlsx", "Sheet1")
        assert image_converted[0]["comment_type"] == "text+image"
        assert image_converted[0]["has_image"] is True
        assert image_converted[0]["expected_amount"] == "2.00"

    print("ok: table converter regression tests")


if __name__ == "__main__":
    run()
