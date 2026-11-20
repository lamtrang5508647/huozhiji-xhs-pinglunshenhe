#!/usr/bin/env python3
"""Portable OOXML audit writer; no Excel, Node, Codex, Feishu or third-party library.

Patch reviewed rows' result and amount cells and the audit summary. Preserve all source media,
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
from functools import partial
from hashlib import sha256
from itertools import groupby
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    from .audit_validation import validate_results
    from .review_comments import parse_amount
    from .table_to_expected import first_pass_columns, read_xlsx, resolve_mapping
except ImportError:
    from audit_validation import validate_results
    from review_comments import parse_amount
    from table_to_expected import first_pass_columns, read_xlsx, resolve_mapping

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
HEADERS = ["团长", "产品名", "涉及工作表数", "总条数", "成功数", "失败数", "需人审数", "成功率", "报销金额", "涉及工作表", "团长联系方式", "跟团长结算时间", "结算凭证", "二维码"]
SETTLEMENT_NOTES = {
    "pending_specified_image": "指定图片尚未核实，金额暂缓核定",
    "pending_duplicate_claim": "多行共用评论证据，金额暂缓核定",
    "pending_source_amount": "原表缺少可用报价，金额暂缓核定",
}


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
    # Serialize XML into a string buffer, then encode each complete part once.
    data = ET.tostring(root, encoding="unicode", xml_declaration=True).encode("utf-8")
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


def sheet_cell_writer(data):
    rows = {}
    for row in data:
        rows.setdefault(row.get("r"), row)
    return partial(set_cell, data, row_index=rows)


def cell_signature(element):
    return element.tag, sorted(element.attrib.items()), element.text, tuple(cell_signature(child) for child in element)


def set_cell(data, address, value, *, style=None, formula=None, row_index=None):
    row_number = int(re.search(r"\d+$", address)[0])
    row_key = str(row_number)
    if row_index is None:
        row = next((row for row in data if row.get("r") == row_key), None)
    else:
        row = row_index.get(row_key)
    if row is None:
        row = ET.Element(tag("row"), {"r": row_key})
        if not len(data) or int(data[-1].get("r", "0")) < row_number:
            data.append(row)
        else:
            index = next((i for i, item in enumerate(data) if int(item.get("r", "0")) > row_number), len(data))
            data.insert(index, row)
        if row_index is not None:
            row_index[row_key] = row
    cell_tag = tag("c")
    existing = next((item for item in row if item.tag == cell_tag and item.get("r") == address), None)
    if style is None and existing is not None:
        style = existing.get("s") or None
    cell = make_cell(address, value, style=style, formula=formula)
    if existing is not None:
        row.remove(existing)
    target_column = col_index(address)
    index = next((i for i, item in enumerate(row) if item.tag == cell_tag and col_index(item.get("r")) > target_column), len(row))
    row.insert(index, cell)
    return cell


def make_cell(address, value, *, style=None, formula=None):
    attrs = {"r": address}
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
    rows_by_sheet = {}
    locations = set()
    layouts = {}
    preserved_cells = {}
    for item in rows:
        name, number = item.get("source_sheet"), item.get("source_row")
        if name not in sheet_paths or name == "审核汇总" or not isinstance(number, int) or number < 2:
            raise ValueError("invalid_source_row_location")
        if (name, number) in locations:
            raise ValueError("duplicate_source_row_location")
        locations.add((name, number))
        rows_by_sheet.setdefault(name, []).append(item)
    for name, selected in rows_by_sheet.items():
        path = sheet_paths[name]
        root = ET.fromstring(parts[path])
        data = root.find(tag("sheetData"))
        active_rows = {row["source_row"] for row in selected if by_id[row["case_id"]].get("status") != "carried_forward"}
        layout = selected[0].get("prior_review") or selected[0].get("audit_columns")
        if layout is None:
            # Standalone writer callers can supply contracts created before column metadata existed.
            table = read_xlsx(source, name)
            columns = getattr(table, "columns", {})
            layout = first_pass_columns(columns, getattr(table, "occupied", set()),
                                        getattr(table, "data_columns", set()), resolve_mapping(columns, {}))
        category_column = layout["result_column"]
        amount_column = layout["amount_column"]
        if active_rows and not amount_column:
            highest = max((col_index(c.get("r")) for c in data.iter(tag("c"))), default=0)
            if highest >= 16384:
                raise ValueError("no_room_for_settlement_column")
            amount_column = column(highest + 1)
        layouts[name] = category_column, amount_column
        frozen = {row["source_row"] for row in selected} - active_rows
        if frozen:
            protected = {f"{col}{n}" for n in frozen for col in (category_column, amount_column) if col}
            preserved_cells[name] = {c.get("r"): cell_signature(c) for c in data.iter(tag("c")) if c.get("r") in protected}
        if not active_rows:
            continue
        if not category_column or category_column == amount_column:
            raise ValueError("invalid_audit_output_columns")
        if root.find(tag("sheetProtection")) is not None:
            raise ValueError("protected_source_sheet")
        write_cell = sheet_cell_writer(data)
        maximum = max(item["source_row"] for item in selected)
        audit_columns = {col_index(category_column), col_index(amount_column)}
        merges = root.find(tag("mergeCells"))
        if merges is not None:
            for merge in list(merges):
                begin, end = (merge.get("ref", "") + ":" + merge.get("ref", "")).split(":")[:2]
                left, right = col_index(begin), col_index(end)
                top, bottom = int(re.search(r"\d+$", begin)[0]), int(re.search(r"\d+$", end)[0])
                if any(left <= col <= right for col in audit_columns) and any(top <= n <= bottom for n in active_rows):
                    if any(col not in audit_columns for col in range(left, right + 1)) or top < 2 or bottom > maximum:
                        raise ValueError("audit_merge_extends_outside_authorized_area")
                    merges.remove(merge)
                    if frozen and any(top <= n <= bottom for n in frozen):
                        anchor = data.find(f".//{tag('c')}[@r='{begin}']")
                        for active, numbers in groupby(range(top, bottom + 1), key=active_rows.__contains__):
                            if active:
                                continue
                            segment = list(numbers)
                            head, tail = segment[0], segment[-1]
                            head_ref = f"{column(left)}{head}"
                            if head_ref != begin and anchor is not None and (cell_value(anchor, strings) != "" or anchor.find(tag("f")) is not None):
                                # A newly exposed cell inherits the original merged value. Its quote
                                # stays on the source sheet only, even when it looks like a payout.
                                exposed = data.find(f".//{tag('c')}[@r='{head_ref}']")
                                if cell_value(exposed, strings) == "" and (exposed is None or exposed.find(tag("f")) is None):
                                    exposed = write_cell(head_ref, None)
                                    exposed.attrib.update({k: v for k, v in anchor.attrib.items() if k != "r"})
                                    keep_formula = cell_value(anchor, strings) == ""
                                    exposed.extend(copy.deepcopy(child) for child in anchor if child.tag != tag("f") or keep_formula)
                                    if head in frozen:
                                        preserved_cells[name][head_ref] = cell_signature(exposed)
                            if head != tail or left != right:
                                ET.SubElement(merges, tag("mergeCell"), {"ref": f"{head_ref}:{column(right)}{tail}"})
            merges.set("count", str(len(merges)))
            if not len(merges):
                root.remove(merges)
        if layout.get("new_result"):
            write_cell(f"{category_column}1", "审核结果")
        if layout.get("new_amount") or layout.get("amount_column") is None:
            write_cell(f"{amount_column}1", "核定金额")
        for item in selected:
            result = by_id[item["case_id"]]
            if result.get("status") == "carried_forward":
                continue
            write_cell(f'{category_column}{item["source_row"]}', result["result_category"])
            amount = result.get("approved_amount")
            write_cell(f'{amount_column}{item["source_row"]}', None if amount in (None, "") else Decimal(str(amount)), style=styles["money"])
        dimension = root.find(tag("dimension"))
        if dimension is not None:
            dimension.set("ref", f"A1:{column(max(8, max((col_index(c.get('r')) for c in data.iter(tag('c'))), default=8)))}{max(maximum, max((int(r.get('r', '1')) for r in data), default=1))}")
        parts[path] = encode(root, original_parts[path])
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
    has_pending = any(item.get("settlement_status") in SETTLEMENT_NOTES for item in results)
    ET.SubElement(summary, tag("dimension"), {"ref": f"A1:Z{max(end, total_row)}"})
    view = ET.SubElement(ET.SubElement(summary, tag("sheetViews")), tag("sheetView"), {"workbookViewId": "0", "showGridLines": "0"})
    ET.SubElement(view, tag("pane"), {"ySplit": "1", "topLeftCell": "A2", "activePane": "bottomLeft", "state": "frozen"})
    ET.SubElement(summary, tag("sheetFormatPr"), {"defaultRowHeight": "22"})
    cols = ET.SubElement(summary, tag("cols"))
    for index, width in enumerate((13, 15, 14, 9, 9, 9, 11, 10, 12, 38, 16, 18, 16, 18), 1):
        ET.SubElement(cols, tag("col"), {"min": str(index), "max": str(index), "width": str(width), "customWidth": "1"})
    if has_pending:
        ET.SubElement(cols, tag("col"), {"min": "25", "max": "25", "width": "42", "customWidth": "1"})
    data = ET.SubElement(summary, tag("sheetData"))
    write_cell = sheet_cell_writer(data)
    for index, header in enumerate(HEADERS, 1):
        write_cell(f"{column(index)}1", header, style=styles["header" if index <= 10 else "manual"])
    write_cell("P1", "图文组合结果", style=styles["header"])
    write_cell("Q1", "数量", style=styles["header"])
    for number, category in enumerate(("图字成功", "文字成功", "图片成功", "失败", "待人审"), 2):
        count = sum(item.get("comment_type") == "text+image" and by_id[item["case_id"]]["result_category"] == category for item in rows)
        write_cell(f"P{number}", category, style=styles["body"])
        write_cell(f"Q{number}", count, formula=f'COUNTIFS($U$12:$U${end},"text+image",$V$12:$V${end},P{number})', style=styles["body"])
    for index, header in enumerate(("案例ID", "团长", "产品名", "工作表", "原行", "要求类型", "结果分类", "分流", "核定金额"), 16):
        write_cell(f"{column(index)}11", header, style=styles["detail"])
    if has_pending:
        write_cell("Y11", "结算说明", style=styles["detail"])
    counts = Counter(item["triage"] for item in results)
    if counts["未复核"]:
        write_cell("P7", "未复核（原结果空白或未识别）", style=styles["body"])
        write_cell("Q7", counts["未复核"], formula=f'COUNTIF($W$12:$W${end},"未复核")', style=styles["body"])
        write_cell("H1", "成功率（有审核结果）", style=styles["header"])
    write_cell("Z11", "原任务报价", style=styles["header"])
    # Detail rows follow the fixed headers and their cells arrive in P:X order.
    for number, item in enumerate(rows, 12):
        result = by_id[item["case_id"]]
        key = (str(item.get("group_leader") or "未填写").strip(), str(item.get("product_name") or item["source_sheet"]).strip())
        detail_row = ET.SubElement(data, tag("row"), {"r": str(number)})
        for index, value in enumerate((item["case_id"], *key, item["source_sheet"], item["source_row"], item["comment_type"]), 16):
            detail_row.append(make_cell(f"{column(index)}{number}", value, style=styles["body"]))
        quoted = "'" + item["source_sheet"].replace("'", "''") + "'"
        cat_col, amount_col = layouts[item["source_sheet"]]
        carried = result.get("status") == "carried_forward"
        detail_row.append(make_cell(f"V{number}", result["result_category"], formula=None if carried else f"{quoted}!{cat_col}{item['source_row']}", style=styles["body"]))
        settlement_note = SETTLEMENT_NOTES.get(result.get("settlement_status"))
        amount_ref = f'{quoted}!{amount_col}{item["source_row"]}'
        partial_approved = f'ISNUMBER({amount_ref})'
        if settlement_note:
            partial_approved = f'OR({partial_approved},Y{number}<>"")'
        detail_row.append(make_cell(f"W{number}", result["triage"], formula=None if carried else f'IF(OR(V{number}="成功",V{number}="图字成功",AND(OR(V{number}="文字成功",V{number}="图片成功"),{partial_approved})),"成功",IF(V{number}="失败","失败","待人审"))', style=styles["body"]))
        amount = result.get("approved_amount")
        detail_row.append(make_cell(f"X{number}", None if amount in (None, "") else Decimal(str(amount)), formula=None if carried else f'IF(ISNUMBER({amount_ref}),{amount_ref},"")', style=styles["money"]))
        if settlement_note:
            detail_row.append(make_cell(f"Y{number}", settlement_note, style=styles["body"]))
        quote = parse_amount(item.get("expected_amount"))
        if quote is not None and quote.is_finite() and quote >= 0:
            detail_row.append(make_cell(f"Z{number}", quote, style=styles["money"]))
    # Large group tables may reach the detail rows from the left.
    write_cell = sheet_cell_writer(data)
    new_row_numbers = {}
    for number, (key, members) in enumerate(groups.items(), 2):
        new_row_numbers[key] = number - 1
        selected = [by_id[item["case_id"]] for item in members]
        group_counts = Counter(item["triage"] for item in selected)
        group_payout = sum((Decimal(str(item["approved_amount"])) for item in selected if item.get("approved_amount") not in (None, "")), Decimal("0"))
        source_sheets = list(dict.fromkeys(item["source_sheet"] for item in members))
        reviewed = len(members) - group_counts["未复核"]
        values = [*key, len(source_sheets), len(members), group_counts["成功"], group_counts["失败"], group_counts["待人审"], group_counts["成功"] / reviewed if reviewed else 0, group_payout, "、".join(source_sheets)]
        leader = f'"="&SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(A{number},"~","~~"),"*","~*"),"?","~?")'
        product = leader.replace(f"A{number}", f"B{number}")
        criteria = f'$Q$12:$Q${end},{leader},$R$12:$R${end},{product}'
        formulas = {4: f"COUNTIFS({criteria})", 5: f'COUNTIFS({criteria},$W$12:$W${end},"成功")', 6: f'COUNTIFS({criteria},$W$12:$W${end},"失败")', 7: f'COUNTIFS({criteria},$W$12:$W${end},"待人审")', 8: f"IFERROR(E{number}/SUM(E{number}:G{number}),0)", 9: f"SUMIFS($X$12:$X${end},{criteria})"}
        for index, value in enumerate(values, 1):
            write_cell(f"{column(index)}{number}", value, formula=formulas.get(index), style=styles["money" if index == 9 else "percent" if index == 8 else "body"])
        for index, field in enumerate(("leader_contact", "settlement_time", "settlement_voucher", "qr_code"), 11):
            prior = manual.get(key, {}).get(index)
            value = next((item.get(field) for item in members if item.get(field)), None)
            if value == "embedded-image.png":
                value = "见原工作表"
            cell = write_cell(f"{column(index)}{number}", value, style=styles["body"])
            if prior is not None:
                old_number = int(re.search(r"\d+$", prior.get("r"))[0])
                if prior.find(tag("f")) is not None and old_number != number:
                    raise ValueError("manual_summary_formula_row_moved")
                if cell_value(prior, strings) or prior.find(tag("f")) is not None:
                    attrs = {**prior.attrib, "r": cell.get("r")}
                    cell.clear()
                    cell.attrib.update(attrs)
                    cell.extend(copy.deepcopy(list(prior)))
    reviewed = len(results) - counts["未复核"]
    totals = ["合计", None, sum(len({item["source_sheet"] for item in members}) for members in groups.values()), len(results), counts["成功"], counts["失败"], counts["待人审"], counts["成功"] / reviewed if reviewed else 0, payout]
    for index, value in enumerate(totals, 1):
        formula = (f"IFERROR(E{total_row}/SUM(E{total_row}:G{total_row}),0)" if index == 8 else f"SUM({column(index)}2:{column(index)}{total_row-1})") if 3 <= index <= 9 else None
        write_cell(f"{column(index)}{total_row}", value, formula=formula, style=styles["total_money" if index == 9 else "total_percent" if index == 8 else "total"])
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
            for name, selected in rows_by_sheet.items():
                root = ET.fromstring(archive.read(sheet_paths[name]))
                cells = {c.get("r"): c for c in root.iter(tag("c"))}
                category_column, amount_column = layouts[name]
                for item in selected:
                    result = by_id[item["case_id"]]
                    if result.get("status") == "carried_forward":
                        for col in (category_column, amount_column):
                            if not col:
                                continue
                            ref = f'{col}{item["source_row"]}'
                            original = preserved_cells[name].get(ref)
                            saved = cell_signature(cells[ref]) if ref in cells else None
                            if original != saved:
                                raise RuntimeError("export_retained_row_changed")
                        continue
                    if cell_value(cells[f'{category_column}{item["source_row"]}'], strings) != result["result_category"]:
                        raise RuntimeError("export_row_category_mismatch")
                    actual = cell_value(cells[f'{amount_column}{item["source_row"]}'], strings)
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
