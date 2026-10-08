#!/usr/bin/env python3
"""Portable OOXML audit writer; no Excel, Node, Codex, Feishu or third-party library.

Patch only authorized G/H cells and audit summary. Preserve all source media,
cell-image definitions, relationships and unrelated ZIP entries verbatim.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import posixpath
import re
import tempfile
import zipfile
from collections import Counter
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    from .audit_validation import validate_results
except ImportError:
    from audit_validation import validate_results

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
HEADERS = ["团长", "产品名", "涉及工作表数", "总条数", "成功数", "失败数", "需人审数", "成功率", "报销金额", "涉及工作表", "团长联系方式", "跟团长结算时间", "结算凭证", "二维码"]


def tag(name):
    return f"{{{S}}}{name}"


def encode(root, original=b""):
    # Preserve original namespace declarations, including prefixes mentioned
    # only by mc:Ignorable. Dropping those can make Excel repair a valid file.
    namespaces = {}
    if original:
        namespaces = dict(value for _, value in ET.iterparse(io.BytesIO(original), events=("start-ns",)))
    namespaces.setdefault("", root.tag.split("}")[0][1:])
    for prefix, uri in namespaces.items():
        if not re.fullmatch(r"ns\d+", prefix):
            ET.register_namespace(prefix, uri)
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    first_end = data.index(b">", data.index(b"?>") + 2)
    first = data[:first_end]
    missing = []
    for prefix, uri in namespaces.items():
        key = ("xmlns:" + prefix if prefix else "xmlns").encode()
        if not re.search(rb"\s" + re.escape(key) + rb"=", first):
            missing.append(b" " + key + b'="' + uri.encode() + b'"')
    return data[:first_end] + b"".join(missing) + data[first_end:]


def relation_target(part, target):
    return posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(part), target))


def col_index(address):
    value = 0
    for letter in re.match(r"[A-Z]+", address)[0]:
        value = value * 26 + ord(letter) - 64
    return value


def column(number):
    out = ""
    while number:
        number, rest = divmod(number - 1, 26)
        out = chr(65 + rest) + out
    return out


def cell_value(cell, strings):
    if cell is None:
        return ""
    if cell.get("t") == "inlineStr":
        return "".join(item.text or "" for item in cell.iter(tag("t")))
    value = cell.findtext(tag("v"), "")
    if cell.get("t") == "s":
        return strings[int(value)] if value else ""
    return value


def set_cell(data, address, value, *, style=None, formula=None):
    row_number = int(re.search(r"\d+$", address)[0])
    row = next((row for row in data if row.get("r") == str(row_number)), None)
    if row is None:
        row = ET.Element(tag("row"), {"r": str(row_number)})
        index = next((i for i, item in enumerate(data) if int(item.get("r", "0")) > row_number), len(data))
        data.insert(index, row)
    existing = row.find(f"{tag('c')}[@r='{address}']")
    attrs = {"r": address}
    if existing is not None and existing.get("s"):
        attrs["s"] = existing.get("s")
    if style is not None:
        attrs["s"] = str(style)
    cell = ET.Element(tag("c"), attrs)
    if formula is not None:
        ET.SubElement(cell, tag("f")).text = formula.lstrip("=")
        if isinstance(value, str) or value is None:
            cell.set("t", "str")
        ET.SubElement(cell, tag("v")).text = "" if value is None else str(value)
    elif value is not None:
        if isinstance(value, (Decimal, int, float)):
            ET.SubElement(cell, tag("v")).text = str(value)
        else:
            cell.set("t", "inlineStr")
            node = ET.SubElement(ET.SubElement(cell, tag("is")), tag("t"))
            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            node.text = str(value)
    if existing is not None:
        row.remove(existing)
    index = next((i for i, item in enumerate(row) if item.tag == tag("c") and col_index(item.get("r")) > col_index(address)), len(row))
    row.insert(index, cell)
    return cell


def add_styles(parts):
    original = parts.get("xl/styles.xml", b"")
    root = ET.fromstring(original) if original else ET.Element(tag("styleSheet"))
    def collection(name, defaults):
        node = root.find(tag(name))
        if node is None:
            node = ET.SubElement(root, tag(name))
            for default in defaults:
                node.append(ET.fromstring(default))
        return node
    fonts = collection("fonts", [f'<font xmlns="{S}"><sz val="11"/><name val="Calibri"/></font>'])
    fills = collection("fills", [f'<fill xmlns="{S}"><patternFill patternType="none"/></fill>', f'<fill xmlns="{S}"><patternFill patternType="gray125"/></fill>'])
    borders = collection("borders", [f'<border xmlns="{S}"/>'])
    collection("cellStyleXfs", [f'<xf xmlns="{S}" numFmtId="0" fontId="0" fillId="0" borderId="0"/>'])
    xfs = collection("cellXfs", [f'<xf xmlns="{S}" numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'])
    font_ids = []
    for color in ("FFFFFFFF", "FF111827"):
        font_ids.append(len(fonts))
        font = ET.SubElement(fonts, tag("font"))
        ET.SubElement(font, tag("b"))
        ET.SubElement(font, tag("sz"), {"val": "11"})
        ET.SubElement(font, tag("name"), {"val": "Calibri"})
        ET.SubElement(font, tag("color"), {"rgb": color})
    fill_ids = []
    for color in ("FF1F4E78", "FFF4CCCC", "FFDDEBF7", "FF5B9BD5"):
        fill_ids.append(len(fills))
        pattern = ET.SubElement(ET.SubElement(fills, tag("fill")), tag("patternFill"), {"patternType": "solid"})
        ET.SubElement(pattern, tag("fgColor"), {"rgb": color})
        ET.SubElement(pattern, tag("bgColor"), {"indexed": "64"})
    border_id = len(borders)
    border = ET.SubElement(borders, tag("border"))
    for side in ("left", "right", "top", "bottom"):
        ET.SubElement(ET.SubElement(border, tag(side), {"style": "thin"}), tag("color"), {"rgb": "FF111827"})
    styles = {}
    for name, font, fill, number in (("body", 0, 0, 0), ("money", 0, 0, 2), ("percent", 0, 0, 10),
                                    ("header", font_ids[0], fill_ids[0], 0), ("manual", font_ids[1], fill_ids[1], 0),
                                    ("total", font_ids[1], fill_ids[2], 0), ("total_money", font_ids[1], fill_ids[2], 2),
                                    ("total_percent", font_ids[1], fill_ids[2], 10), ("detail", font_ids[0], fill_ids[3], 0)):
        styles[name] = len(xfs)
        xf = ET.SubElement(xfs, tag("xf"), {"numFmtId": str(number), "fontId": str(font), "fillId": str(fill), "borderId": str(border_id), "xfId": "0", "applyAlignment": "1", "applyNumberFormat": "1", "applyFill": "1", "applyFont": "1", "applyBorder": "1"})
        ET.SubElement(xf, tag("alignment"), {"horizontal": "center", "vertical": "center", "wrapText": "1"})
    for node in (fonts, fills, borders, xfs, root.find(tag("cellStyleXfs"))):
        node.set("count", str(len(node)))
    parts["xl/styles.xml"] = encode(root, original)
    return styles


def build_workbook(source: Path, rows: list[dict], results: list[dict], output: Path) -> dict:
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source.suffix.lower() != ".xlsx" or output.suffix.lower() != ".xlsx":
        raise ValueError("use_distinct_source_and_output_xlsx")
    payout = validate_results(rows, results)
    by_id = {item["case_id"]: item for item in results}
    with zipfile.ZipFile(source) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("source_zip_invalid")
        if any(name.startswith("_xmlsignatures/") for name in archive.namelist()):
            raise ValueError("signed_workbook_not_supported")
        parts = {name: archive.read(name) for name in archive.namelist()}
    original_parts = parts.copy()
    workbook = ET.fromstring(parts["xl/workbook.xml"])
    rels = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
    types = ET.fromstring(parts["[Content_Types].xml"])
    sheets = workbook.find(tag("sheets"))
    if sheets is None:
        raise ValueError("source_sheets_missing")
    targets = {item.get("Id"): relation_target("xl/workbook.xml", item.get("Target", "")) for item in rels}
    sheet_paths = {item.get("name"): targets[item.get(f"{{{R}}}id")] for item in sheets}
    strings = []
    if "xl/sharedStrings.xml" in parts:
        strings = ["".join(t.text or "" for t in item.iter(tag("t"))) for item in ET.fromstring(parts["xl/sharedStrings.xml"])]
    styles = add_styles(parts)
    modified = set()
    locations = set()
    for item in rows:
        name, number = item.get("source_sheet"), item.get("source_row")
        if name not in sheet_paths or name == "审核汇总" or not isinstance(number, int) or number < 2:
            raise ValueError("invalid_source_row_location")
        if (name, number) in locations:
            raise ValueError("duplicate_source_row_location")
        locations.add((name, number))
    for name in dict.fromkeys(item["source_sheet"] for item in rows):
        path = sheet_paths[name]
        root = ET.fromstring(parts[path])
        if root.find(tag("sheetProtection")) is not None:
            raise ValueError("protected_source_sheet")
        data = root.find(tag("sheetData"))
        selected = [item for item in rows if item["source_sheet"] == name]
        maximum = max(item["source_row"] for item in selected)
        merges = root.find(tag("mergeCells"))
        if merges is not None:
            for merge in list(merges):
                begin, end = (merge.get("ref", "") + ":" + merge.get("ref", "")).split(":")[:2]
                left, right = col_index(begin), col_index(end)
                top, bottom = int(re.search(r"\d+$", begin)[0]), int(re.search(r"\d+$", end)[0])
                if left <= 8 and right >= 7 and top <= maximum and bottom >= 2:
                    if left < 7 or right > 8 or top < 2 or bottom > maximum:
                        raise ValueError("audit_merge_extends_outside_authorized_area")
                    merges.remove(merge)
            merges.set("count", str(len(merges)))
            if not len(merges):
                root.remove(merges)
        set_cell(data, "G1", "审核结果")
        for item in selected:
            result = by_id[item["case_id"]]
            set_cell(data, f'G{item["source_row"]}', result["result_category"])
            amount = result.get("approved_amount")
            set_cell(data, f'H{item["source_row"]}', None if amount in (None, "") else Decimal(str(amount)), style=styles["money"])
        dimension = root.find(tag("dimension"))
        if dimension is not None:
            dimension.set("ref", f"A1:{column(max(8, max((col_index(c.get('r')) for c in data.iter(tag('c'))), default=8)))}{max(maximum, max((int(r.get('r', '1')) for r in data), default=1))}")
        parts[path] = encode(root, original_parts[path])
        modified.add(path)
    groups = {}
    for item in rows:
        key = (str(item.get("group_leader") or "未填写").strip(), str(item.get("product_name") or item["source_sheet"]).strip())
        groups.setdefault(key, []).append(item)
    manual, old_row_keys = {}, {}
    summary_path = sheet_paths.get("审核汇总")
    old_summary = ET.fromstring(parts[summary_path]) if summary_path else None
    if old_summary is not None:
        if old_summary.find(tag("sheetProtection")) is not None:
            raise ValueError("protected_summary_sheet")
        for row in old_summary.find(tag("sheetData")):
            cells = {col_index(c.get("r")): c for c in row if c.tag == tag("c")}
            key = (cell_value(cells.get(1), strings).strip(), cell_value(cells.get(2), strings).strip())
            if key in groups:
                if key in manual:
                    raise ValueError("ambiguous_existing_summary_group")
                manual[key] = {n: copy.deepcopy(cells[n]) for n in range(11, 15) if n in cells}
                old_row_keys[int(row.get("r")) - 1] = key
    if summary_path is None:
        number = 1
        while f"xl/worksheets/sheet{number}.xml" in parts:
            number += 1
        summary_path = f"xl/worksheets/sheet{number}.xml"
        rid = "rIdAuditSummary"
        while rid in targets:
            rid += "x"
        ET.SubElement(rels, f"{{{P}}}Relationship", {"Id": rid, "Type": R + "/worksheet", "Target": summary_path[3:]})
        ET.SubElement(sheets, tag("sheet"), {"name": "审核汇总", "sheetId": str(max(int(item.get("sheetId")) for item in sheets) + 1), f"{{{R}}}id": rid})
        ET.SubElement(types, f"{{{CT}}}Override", {"PartName": "/" + summary_path, "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"})
    summary = ET.Element(tag("worksheet"))
    end = 11 + len(rows)
    total_row = len(groups) + 2
    ET.SubElement(summary, tag("dimension"), {"ref": f"A1:X{max(end, total_row)}"})
    view = ET.SubElement(ET.SubElement(summary, tag("sheetViews")), tag("sheetView"), {"workbookViewId": "0", "showGridLines": "0"})
    ET.SubElement(view, tag("pane"), {"ySplit": "1", "topLeftCell": "A2", "activePane": "bottomLeft", "state": "frozen"})
    ET.SubElement(summary, tag("sheetFormatPr"), {"defaultRowHeight": "22"})
    cols = ET.SubElement(summary, tag("cols"))
    for index, width in enumerate((13, 15, 14, 9, 9, 9, 11, 10, 12, 38, 16, 18, 16, 18), 1):
        ET.SubElement(cols, tag("col"), {"min": str(index), "max": str(index), "width": str(width), "customWidth": "1"})
    data = ET.SubElement(summary, tag("sheetData"))
    for index, header in enumerate(HEADERS, 1):
        set_cell(data, f"{column(index)}1", header, style=styles["header" if index <= 10 else "manual"])
    set_cell(data, "P1", "图文组合结果", style=styles["header"])
    set_cell(data, "Q1", "数量", style=styles["header"])
    for number, category in enumerate(("图字成功", "文字成功", "图片成功", "失败", "待人审"), 2):
        count = sum(item.get("comment_type") == "text+image" and by_id[item["case_id"]]["result_category"] == category for item in rows)
        set_cell(data, f"P{number}", category, style=styles["body"])
        set_cell(data, f"Q{number}", count, formula=f'COUNTIFS($U$12:$U${end},"text+image",$V$12:$V${end},P{number})', style=styles["body"])
    for index, header in enumerate(("案例ID", "团长", "产品名", "工作表", "原行", "要求类型", "结果分类", "分流", "核定金额"), 16):
        set_cell(data, f"{column(index)}11", header, style=styles["detail"])
    counts = Counter(item["triage"] for item in results)
    for number, item in enumerate(rows, 12):
        result = by_id[item["case_id"]]
        key = (str(item.get("group_leader") or "未填写").strip(), str(item.get("product_name") or item["source_sheet"]).strip())
        for index, value in enumerate((item["case_id"], *key, item["source_sheet"], item["source_row"], item["comment_type"]), 16):
            set_cell(data, f"{column(index)}{number}", value, style=styles["body"])
        quoted = "'" + item["source_sheet"].replace("'", "''") + "'"
        set_cell(data, f"V{number}", result["result_category"], formula=f"{quoted}!G{item['source_row']}", style=styles["body"])
        set_cell(data, f"W{number}", result["triage"], formula=f'IF(OR(V{number}="成功",V{number}="图字成功",AND(OR(V{number}="文字成功",V{number}="图片成功"),ISNUMBER({quoted}!H{item["source_row"]}))),"成功",IF(V{number}="失败","失败","待人审"))', style=styles["body"])
        amount = result.get("approved_amount")
        set_cell(data, f"X{number}", None if amount in (None, "") else Decimal(str(amount)), formula=f'IF(ISNUMBER({quoted}!H{item["source_row"]}),{quoted}!H{item["source_row"]},"")', style=styles["money"])
    new_row_numbers = {}
    for number, (key, members) in enumerate(groups.items(), 2):
        new_row_numbers[key] = number - 1
        selected = [by_id[item["case_id"]] for item in members]
        group_counts = Counter(item["triage"] for item in selected)
        group_payout = sum((Decimal(str(item["approved_amount"])) for item in selected if item.get("approved_amount") not in (None, "")), Decimal("0"))
        source_sheets = list(dict.fromkeys(item["source_sheet"] for item in members))
        values = [*key, len(source_sheets), len(members), group_counts["成功"], group_counts["失败"], group_counts["待人审"], group_counts["成功"] / len(members), group_payout, "、".join(source_sheets)]
        leader = f'"="&SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(A{number},"~","~~"),"*","~*"),"?","~?")'
        product = leader.replace(f"A{number}", f"B{number}")
        criteria = f'$Q$12:$Q${end},{leader},$R$12:$R${end},{product}'
        formulas = {4: f"COUNTIFS({criteria})", 5: f'COUNTIFS({criteria},$W$12:$W${end},"成功")', 6: f'COUNTIFS({criteria},$W$12:$W${end},"失败")', 7: f'COUNTIFS({criteria},$W$12:$W${end},"待人审")', 8: f"IFERROR(E{number}/D{number},0)", 9: f"SUMIFS($X$12:$X${end},{criteria})"}
        for index, value in enumerate(values, 1):
            set_cell(data, f"{column(index)}{number}", value, formula=formulas.get(index), style=styles["money" if index == 9 else "percent" if index == 8 else "body"])
        for index, field in enumerate(("leader_contact", "settlement_time", "settlement_voucher", "qr_code"), 11):
            prior = manual.get(key, {}).get(index)
            value = next((item.get(field) for item in members if item.get(field)), None)
            if value == "embedded-image.png":
                value = "见原工作表"
            cell = set_cell(data, f"{column(index)}{number}", value, style=styles["body"])
            if prior is not None:
                old_number = int(re.search(r"\d+$", prior.get("r"))[0])
                if prior.find(tag("f")) is not None and old_number != number:
                    raise ValueError("manual_summary_formula_row_moved")
                if cell_value(prior, strings) or prior.find(tag("f")) is not None:
                    attrs = {**prior.attrib, "r": cell.get("r")}
                    cell.clear()
                    cell.attrib.update(attrs)
                    cell.extend(copy.deepcopy(list(prior)))
    totals = ["合计", None, sum(len({item["source_sheet"] for item in members}) for members in groups.values()), len(results), counts["成功"], counts["失败"], counts["待人审"], counts["成功"] / len(results), payout]
    for index, value in enumerate(totals, 1):
        formula = (f"IFERROR(E{total_row}/D{total_row},0)" if index == 8 else f"SUM({column(index)}2:{column(index)}{total_row-1})") if 3 <= index <= 9 else None
        set_cell(data, f"{column(index)}{total_row}", value, formula=formula, style=styles["total_money" if index == 9 else "total_percent" if index == 8 else "total"])
    for row in data:
        if int(row.get("r")) <= total_row:
            row.set("ht", "30" if row.get("r") == "1" else "44")
            row.set("customHeight", "1")
    ET.SubElement(summary, tag("autoFilter"), {"ref": f"A1:J{total_row - 1}"})
    if old_summary is not None:
        hyperlinks = old_summary.find(tag("hyperlinks"))
        if hyperlinks is not None:
            retained = ET.Element(tag("hyperlinks"))
            for link in hyperlinks:
                ref = link.get("ref", "")
                if re.fullmatch(r"[K-N]\d+", ref):
                    old_row = int(re.search(r"\d+$", ref)[0]) - 1
                    if old_row in old_row_keys:
                        node = copy.deepcopy(link)
                        node.set("ref", ref[0] + str(new_row_numbers[old_row_keys[old_row]] + 1))
                        retained.append(node)
            if len(retained):
                summary.append(retained)
    # Keep QR/payment images rather than silently deleting summary drawings.
    if old_summary is not None:
        summary_rels_path = posixpath.join(posixpath.dirname(summary_path), "_rels", posixpath.basename(summary_path) + ".rels")
        drawing_rels = ET.fromstring(parts[summary_rels_path]) if summary_rels_path in parts else []
        for drawing in old_summary.findall(tag("drawing")):
            relation = next((item for item in drawing_rels if item.get("Id") == drawing.get(f"{{{R}}}id")), None)
            if relation is None:
                raise ValueError("summary_drawing_relationship_missing")
            drawing_path = relation_target(summary_path, relation.get("Target"))
            drawing_root = ET.fromstring(parts[drawing_path])
            for anchor in drawing_root:
                markers = [item for item in anchor if item.tag.rsplit("}", 1)[-1] in ("from", "to")]
                origin = next((item for item in markers if item.tag.rsplit("}", 1)[-1] == "from"), None)
                if origin is None:
                    raise ValueError("summary_absolute_drawing_requires_review")
                values = {item.tag.rsplit("}", 1)[-1]: item for item in origin}
                old_row = int(values["row"].text)
                if old_row not in old_row_keys or not 10 <= int(values["col"].text) <= 13:
                    raise ValueError("summary_drawing_outside_manual_group")
                shift = new_row_numbers[old_row_keys[old_row]] - old_row
                for marker in markers:
                    for child in marker:
                        if child.tag.rsplit("}", 1)[-1] == "row":
                            child.text = str(int(child.text) + shift)
            parts[drawing_path] = encode(drawing_root, original_parts[drawing_path])
            summary.append(copy.deepcopy(drawing))
        if old_summary.find(tag("legacyDrawing")) is not None:
            raise ValueError("summary_legacy_drawing_requires_review")
    parts[summary_path] = encode(summary, original_parts.get(summary_path, b""))
    if not any(item.get("Type") == R + "/styles" for item in rels):
        ET.SubElement(rels, f"{{{P}}}Relationship", {"Id": "rIdAuditStyles", "Type": R + "/styles", "Target": "styles.xml"})
    if not any(item.get("PartName") == "/xl/styles.xml" for item in types):
        ET.SubElement(types, f"{{{CT}}}Override", {"PartName": "/xl/styles.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"})
    calc = workbook.find(tag("calcPr"))
    if calc is None:
        calc = ET.SubElement(workbook, tag("calcPr"))
    calc.attrib.update({"fullCalcOnLoad": "1", "forceFullCalc": "1"})
    for item in list(rels):
        if item.get("Type", "").endswith("/calcChain"):
            parts.pop(relation_target("xl/workbook.xml", item.get("Target")), None)
            rels.remove(item)
    for item in list(types):
        if item.get("ContentType", "").endswith(".calcChain+xml"):
            types.remove(item)
    parts["xl/workbook.xml"] = encode(workbook, original_parts["xl/workbook.xml"])
    parts["xl/_rels/workbook.xml.rels"] = encode(rels, original_parts["xl/_rels/workbook.xml.rels"])
    parts["[Content_Types].xml"] = encode(types, original_parts["[Content_Types].xml"])
    output.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=output.parent, prefix=".audit-", suffix=".xlsx", delete=False)
    temporary = Path(handle.name)
    handle.close()
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data_bytes in parts.items():
                archive.writestr(name, data_bytes)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("export_zip_validation_failed")
            for name in original_parts:
                if name.startswith("xl/media/") and archive.read(name) != original_parts[name]:
                    raise RuntimeError("source_media_changed")
            for path in modified:
                root = ET.fromstring(archive.read(path))
                for item in (item for item in rows if sheet_paths[item["source_sheet"]] == path):
                    result = by_id[item["case_id"]]
                    cells = {c.get("r"): c for c in root.iter(tag("c"))}
                    if cell_value(cells[f'G{item["source_row"]}'], strings) != result["result_category"]:
                        raise RuntimeError("export_row_category_mismatch")
                    actual = cell_value(cells[f'H{item["source_row"]}'], strings)
                    approved = result.get("approved_amount")
                    if (approved in (None, "") and actual != "") or (approved not in (None, "") and Decimal(actual) != Decimal(str(approved))):
                        raise RuntimeError("export_row_amount_mismatch")
            summary_cells = {c.get("r"): c for c in ET.fromstring(archive.read(summary_path)).iter(tag("c"))}
            for letter, wanted in (("D", len(results)), ("E", counts["成功"]), ("F", counts["失败"]), ("G", counts["待人审"]), ("I", payout)):
                if Decimal(cell_value(summary_cells[f"{letter}{total_row}"], strings)) != Decimal(str(wanted)):
                    raise RuntimeError("export_summary_mismatch")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"output": str(output), "groups": len(groups), "total": len(results), "approved_amount": format(payout, ".2f"), "source_sha256": sha256(source.read_bytes()).hexdigest(), "output_sha256": sha256(output.read_bytes()).hexdigest(), "engine": "portable-ooxml", "formula_check": "cached_values_reconciled; Excel_recalculates_on_open"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "expected", "report", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    print(json.dumps(build_workbook(Path(args.source), expected.get("rows", []) if isinstance(expected, dict) else expected, report["results"], Path(args.output)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
