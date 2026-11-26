"""Embed successful tasks' original screenshots beside results on the source sheet."""
import argparse
from collections import defaultdict
import copy
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from xml.etree import ElementTree as ET
import zipfile

if __package__:
    from .build_audited_workbook import R, col_index, column, encode, relation_target, sheet_cell_writer, tag, cell_value
    from .screenshot_evidence import _safe_path, image_bytes
    from .screenshot_drawing import install_pictures, source_picture_ownership
    from .table_to_expected import xlsx_sheet_members
    from .workbook_columns import insert_column, set_column_width
else:
    from build_audited_workbook import R, col_index, column, encode, relation_target, sheet_cell_writer, tag, cell_value
    from screenshot_evidence import _safe_path, image_bytes
    from screenshot_drawing import install_pictures, source_picture_ownership
    from table_to_expected import xlsx_sheet_members
    from workbook_columns import insert_column, set_column_width

SCREENSHOT_HEADER = "评论截图"


def row_images(row, result, base):
    """Accept the matched comment's same-visit original evidence."""
    matched = result.get("matched_comment") or {}
    ident = str(matched.get("comment_id") or "")
    text = matched.get("text")
    evidence = (result.get("capture") or {}).get("evidence_path")
    images, seen = [], set()
    for record in result.get("comment_screenshots") or ():
        if not isinstance(record, dict):
            continue
        exact = (ident and ident in record.get("comment_ids", ())) or (
            text and text in record.get("comment_texts", ())) or record.get("path") == evidence
        if matched and not exact:
            continue
        part = _safe_path(record.get("path"))
        if part is None:
            continue
        directory = base / part.parts[0]
        binary = image_bytes(record, directory, row["platform"], row["target_key"]) if directory.resolve().is_relative_to(base) else None
        if binary is None or record["sha256"] in seen:
            continue
        seen.add(record["sha256"])
        width, height = struct.unpack(">II", binary[16:24])
        images.append(dict(data=binary, sha=record["sha256"], width=width, height=height))
    return images


def add_screenshot_index(parts, rows, results, base, removed_media=None, *, source_owned=None):
    # Entry name retained for existing portable and JavaScript exporters.
    by_id = {r["case_id"]: r for r in results}
    requested = any("screenshot_status" in r or "comment_screenshots" in r for r in results)
    strings = []
    if "xl/sharedStrings.xml" in parts:
        strings = ["".join(t.text or "" for t in s.iter(tag("t"))) for s in ET.fromstring(parts["xl/sharedStrings.xml"])]
    wb = ET.fromstring(parts["xl/workbook.xml"])
    rels = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
    types = ET.fromstring(parts["[Content_Types].xml"])
    sheets = wb.find(tag("sheets"))
    targets = {r.get("Id"): relation_target("xl/workbook.xml", r.get("Target","")) for r in rels}
    paths = {s.get("name"): targets[s.get(f"{{{R}}}id")] for s in sheets}
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["source_sheet"]].append(row)
    layouts, total_images, pictured_rows = {}, 0, 0
    for name, tasks in grouped.items():
        path = paths[name]
        root = ET.fromstring(parts[path])
        data = root.find(tag("sheetData"))
        header = data.find(f"{tag('row')}[@r='1']")
        headers = {col_index(c.get("r")): cell_value(c,strings) for c in header}
        existing = next((c for c,v in headers.items() if v == SCREENSHOT_HEADER), None)
        if not requested and existing is None:
            continue
        layout = tasks[0].get("prior_review") or tasks[0].get("audit_columns") or {}
        result_col = layout.get("result_column")
        result_col = col_index(result_col) if isinstance(result_col,str) else result_col
        if not result_col:
            result_col = next(c for c,v in headers.items() if v in ("审核结果","核验结果"))
        at = existing or result_col + 1
        if root.find(tag("sheetProtection")) is not None:
            raise ValueError("protected_screenshot_column")
        inserted = existing is None
        if inserted:
            insert_column(parts, paths, name, at)
            root = ET.fromstring(parts[path])
        old_root = copy.deepcopy(root)
        data = root.find(tag("sheetData"))
        write = sheet_cell_writer(data)
        result_header = data.find(f"{tag('row')}[@r='1']/{tag('c')}[@r='{column(result_col)}1']")
        write(f"{column(at)}1", SCREENSHOT_HEADER, style=result_header.get("s") if result_header is not None else None)
        filters = root.find(tag("autoFilter"))
        if filters is not None:
            for condition in list(filters.findall(tag("filterColumn"))):
                if int(condition.get("colId")) == result_col - 1:
                    filters.remove(condition)
            properties = root.find(tag("sheetPr"))
            if properties is not None and not filters.findall(tag("filterColumn")):
                properties.attrib.pop("filterMode", None)
        pictures, carried = [], set()
        for row in tasks:
            result = by_id[row["case_id"]]
            number = row["source_row"]
            if result.get("review_action") == "carried_forward" or result.get("status") == "carried_forward":
                carried.add(number)
                continue
            address = f"{column(at)}{number}"
            write(address, None)
            if result.get("triage") != "成功":
                continue
            images = row_images(row, result, Path(base).resolve())
            if not images:
                write(address, "未取得对应评论截图")
                continue
            # Fit originals inside Excel's 409-point row height without covering
            # the next task. Embedded bytes retain the full original resolution.
            scales = [min(1, 480 / img["width"]) for img in images]
            height = sum(img["height"] * scale for img, scale in zip(images,scales))
            fit = min(1, max(1,528 - 6 * len(images)) / height)
            offset = 4
            for img, scale in zip(images,scales):
                scale *= fit
                img.update(width=max(1,round(img["width"]*scale)), height=max(1,round(img["height"]*scale)),
                           col=at-1, row=number, number=len(pictures)+1, offset_y=offset)
                offset += img["height"] + 6
                pictures.append(img)
            sheetrow = data.find(f"{tag('row')}[@r='{number}']")
            sheetrow.attrib.pop("hidden", None)
            sheetrow.set("ht", str(min(409,max(float(sheetrow.get("ht","0")),math.ceil(offset*.75)))))
            sheetrow.set("customHeight","1")
            pictured_rows += 1
        set_column_width(root, at, 70)
        dimension = root.find(tag("dimension"))
        if dimension is not None:
            end_row = max(int(r.get("r")) for r in data)
            end_col = max(col_index(c.get("r")) for c in data.iter(tag("c")))
            dimension.set("ref", f"A1:{column(end_col)}{end_row}")
        install_pictures(parts,path,old_root,root,types,pictures,
                         removed_media if removed_media is not None else set(),
                         (source_owned or {}).get(name),keep_rows=carried)
        parts[path] = encode(root,parts[path])
        layouts[name] = dict(column=column(at), inserted=inserted)
        total_images += len(pictures)
    if not layouts:
        return None
    # Insertion also updates workbook names and cross-sheet formulas.
    wb = ET.fromstring(parts["xl/workbook.xml"])
    sheets = wb.find(tag("sheets"))
    views = wb.find(tag("bookViews"))
    if views is None:
        views = ET.Element(tag("bookViews"))
        wb.insert(list(wb).index(sheets),views)
    if not len(views):
        ET.SubElement(views,tag("workbookView"))
    active = next(i for i,s in enumerate(sheets) if s.get("name") in layouts and s.get("state","visible")=="visible")
    for view in views:
        view.set("activeTab",str(active))
    for index, sheet in enumerate(sheets):
        path = paths[sheet.get("name")]
        root = ET.fromstring(parts[path])
        for view in root.findall(f"{tag('sheetViews')}/{tag('sheetView')}"):
            view.attrib.pop("tabSelected",None)
            if index == active:
                view.set("tabSelected","1")
        parts[path] = encode(root,parts[path])
    parts["xl/workbook.xml"] = encode(wb,parts["xl/workbook.xml"])
    parts["[Content_Types].xml"] = encode(types,parts["[Content_Types].xml"])
    return dict(layout="source_rows", sheets=layouts, tasks=len(rows), rows_with_images=pictured_rows, images=total_images)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("workbook","output","expected","report"):
        parser.add_argument("--"+key,type=Path,required=True)
    parser.add_argument("--screenshot-base",type=Path)
    parser.add_argument("--source","--original",type=Path)
    args = parser.parse_args()
    expected = json.loads(args.expected.read_text(encoding="utf-8"))
    results = json.loads(args.report.read_text(encoding="utf-8"))["results"]
    with zipfile.ZipFile(args.workbook) as archive:
        parts = {n:archive.read(n) for n in archive.namelist()}
    original_owned = {}
    if args.source:
        with zipfile.ZipFile(args.source) as source:
            original = {n:source.read(n) for n in source.namelist()}
            for name,path in xlsx_sheet_members(source).items():
                original_owned[name] = source_picture_ownership(original,path,ET.fromstring(original[path]))
    rows = expected["rows"] if isinstance(expected,dict) else expected
    add_screenshot_index(parts,rows,results,args.screenshot_base or args.report.resolve().parent,
                         source_owned=original_owned)
    with tempfile.NamedTemporaryFile(dir=args.output.parent,delete=False) as tmp:
        dest = Path(tmp.name)
    try:
        with zipfile.ZipFile(dest,"w",zipfile.ZIP_DEFLATED) as archive:
            for name,data in parts.items():
                archive.writestr(name,data)
        os.replace(dest,args.output)
    finally:
        dest.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
