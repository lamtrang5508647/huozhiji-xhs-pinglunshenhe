#!/usr/bin/env python3
"""Convert CSV/TSV/JSON/XLSX table rows to the comment-review expected contract."""

from __future__ import annotations

import argparse
import csv
import json
import posixpath
import re
import sys
import unicodedata
import zipfile
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET


FIELD_ALIASES = {
    "case_id": ["case_id", "caseid", "订单号", "编号", "案例id", "id"],
    "platform": ["platform", "平台"],
    "target_key": ["target_key", "targetkey", "作品id", "笔记id", "视频id", "目标id"],
    "target_url": ["target_url", "targeturl", "文章链接", "作品链接", "笔记链接", "视频链接", "链接"],
    "comment_text": ["comment_text", "commenttext", "评论文字需求", "评论内容", "评论", "文字"],
    "comment_type": ["comment_type", "commenttype", "评论类型", "类型", "形式"],
    "expected_amount": ["expected_amount", "expectedamount", "金额", "返现金额", "核对金额", "amount"],
    "reply_text": ["reply_text", "replytext", "需要回复评论内容左边填写回第几条", "回复内容"],
    "image_requirement": ["image_requirement", "imagerequirement", "评论图片需求", "图片需求"],
    "product_name": ["product_name", "productname", "产品名", "产品"],
    "group_leader": ["group_leader", "groupleader", "团长", "团"],
    "leader_contact": ["leader_contact", "leadercontact", "团长联系方式", "联系方式"],
    "settlement_time": ["settlement_time", "settlementtime", "跟团长结算时间", "结算时间"],
    "settlement_voucher": ["settlement_voucher", "settlementvoucher", "结算凭证", "转账明细"],
    "qr_code": ["qr_code", "qrcode", "二维码", "收款码"],
}


def norm_header(value: Any) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).strip().casefold()
    return re.sub(r"[\s_\-（）()【】\[\]：:，,]+", "", text)


def read_json(path: Path) -> List[Dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict) and isinstance(value.get("rows"), list):
        rows = value["rows"]
    else:
        raise ValueError("JSON must be an array or an object containing 'rows'")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("Every source row must be an object")
    return rows


def read_delimited(path: Path) -> List[Dict[str, Any]]:
    delimiter = "\t" if path.suffix.casefold() == ".tsv" else ","
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter=delimiter)]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child(element: ET.Element, name: str) -> Optional[ET.Element]:
    return next((item for item in list(element) if local_name(item.tag) == name), None)


def xlsx_text(cell: ET.Element, shared_strings: List[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline = child(cell, "is")
        return "".join(item.text or "" for item in inline.iter() if local_name(item.tag) == "t") if inline is not None else ""
    value = child(cell, "v")
    raw = "" if value is None or value.text is None else value.text
    formula = child(cell, "f")
    if not raw and formula is not None and formula.text:
        return "=" + formula.text
    if cell_type == "s" and raw:
        return shared_strings[int(raw)]
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw


def col_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        raise ValueError(f"Invalid XLSX cell reference: {reference}")
    result = 0
    for letter in letters.group(0):
        result = result * 26 + ord(letter) - ord("A") + 1
    return result - 1


def cell_reference(reference: str) -> Tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", reference.upper())
    if not match:
        raise ValueError(f"Invalid XLSX cell reference: {reference}")
    return col_index(match.group(1)), int(match.group(2)) - 1


def list_xlsx_sheets(path: Path) -> List[str]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        return [
            sheet.attrib.get("name", "")
            for sheet in workbook.iter()
            if local_name(sheet.tag) == "sheet"
        ]


def resolve_xlsx_target(source_member: str, target: str) -> str:
    """Resolve an OOXML relationship target to a ZIP member path."""
    normalized = target.replace("\\", "/")
    if normalized.startswith("/"):
        return normalized.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_member), normalized))


def worksheet_image_cells(archive: zipfile.ZipFile, worksheet_member: str) -> List[Tuple[int, int]]:
    """Return zero-based worksheet cells containing floating drawing pictures."""
    rels_member = posixpath.join(
        posixpath.dirname(worksheet_member),
        "_rels",
        posixpath.basename(worksheet_member) + ".rels",
    )
    if rels_member not in archive.namelist():
        return []

    relationships = ET.fromstring(archive.read(rels_member))
    drawing_members = []
    for relationship in relationships:
        if local_name(relationship.tag) != "Relationship":
            continue
        if not relationship.attrib.get("Type", "").endswith("/drawing"):
            continue
        target = relationship.attrib.get("Target", "")
        if target:
            drawing_members.append(resolve_xlsx_target(worksheet_member, target))

    cells: List[Tuple[int, int]] = []
    for drawing_member in drawing_members:
        if drawing_member not in archive.namelist():
            continue
        drawing = ET.fromstring(archive.read(drawing_member))
        for anchor in drawing.iter():
            if local_name(anchor.tag) not in {"oneCellAnchor", "twoCellAnchor"}:
                continue
            if not any(local_name(item.tag) == "pic" for item in anchor.iter()):
                continue
            origin = child(anchor, "from")
            if origin is None:
                continue
            col_node = child(origin, "col")
            row_node = child(origin, "row")
            if col_node is None or row_node is None or col_node.text is None or row_node.text is None:
                continue
            try:
                cells.append((int(row_node.text), int(col_node.text)))
            except ValueError:
                continue
    return cells


def read_xlsx(path: Path, sheet_name: Optional[str] = None) -> List[Dict[str, Any]]:
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    with zipfile.ZipFile(path) as archive:
        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for si in root:
                shared_strings.append("".join(item.text or "" for item in si.iter() if local_name(item.tag) == "t"))

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in relationships
            if local_name(rel.tag) == "Relationship"
        }
        sheets = []
        for sheet in workbook.iter():
            if local_name(sheet.tag) != "sheet":
                continue
            relation_id = sheet.attrib.get("{%s}id" % ns["rel"])
            target = rel_targets.get(relation_id, "")
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            sheets.append((sheet.attrib.get("name", ""), target))
        if not sheets:
            raise ValueError("XLSX contains no worksheets")
        selected = next((item for item in sheets if item[0] == sheet_name), None) if sheet_name else sheets[0]
        if selected is None:
            raise ValueError(f"Worksheet not found: {sheet_name}")
        root = ET.fromstring(archive.read(selected[1]))
        image_cells = worksheet_image_cells(archive, selected[1])
        merged_ranges = [
            item.attrib.get("ref", "")
            for item in root.iter()
            if local_name(item.tag) == "mergeCell"
        ]
        matrix: List[Dict[int, str]] = []
        for row in root.iter():
            if local_name(row.tag) != "row":
                continue
            try:
                row_number = int(row.attrib.get("r", str(len(matrix) + 1)))
            except ValueError:
                row_number = len(matrix) + 1
            while len(matrix) < row_number:
                matrix.append({})
            values: Dict[int, str] = {}
            for cell in row:
                if local_name(cell.tag) == "c":
                    values[col_index(cell.attrib.get("r", "A1"))] = xlsx_text(cell, shared_strings)
            matrix[row_number - 1].update(values)
        for row_index, column_index in image_cells:
            while len(matrix) <= row_index:
                matrix.append({})
            if not matrix[row_index].get(column_index, ""):
                matrix[row_index][column_index] = "embedded-image.png"
    for merged in merged_ranges:
        if ":" not in merged:
            continue
        start, end = merged.split(":", 1)
        start_col, start_row = cell_reference(start)
        end_col, end_row = cell_reference(end)
        if start_col == end_col:
            top_value = matrix[start_row].get(start_col, "") if start_row < len(matrix) else ""
            for row_index in range(start_row, min(end_row + 1, len(matrix))):
                if not matrix[row_index].get(start_col, ""):
                    matrix[row_index][start_col] = top_value
        elif start_row == end_row and start_row < len(matrix):
            top_value = matrix[start_row].get(start_col, "")
            for col_index_value in range(start_col, end_col + 1):
                if not matrix[start_row].get(col_index_value, ""):
                    matrix[start_row][col_index_value] = top_value
    if not matrix:
        return []
    max_col = max((max(row.keys()) for row in matrix if row), default=-1)
    headers = [matrix[0].get(index, "") for index in range(max_col + 1)]
    return [{headers[index]: row.get(index, "") for index in range(max_col + 1) if headers[index]} for row in matrix[1:]]


def read_rows(path: Path, sheet_name: Optional[str]) -> List[Dict[str, Any]]:
    suffix = path.suffix.casefold()
    if suffix == ".json":
        return read_json(path)
    if suffix in {".csv", ".tsv"}:
        return read_delimited(path)
    if suffix == ".xlsx":
        return read_xlsx(path, sheet_name)
    raise ValueError("Supported table formats: .csv, .tsv, .json, .xlsx")


def parse_maps(values: Iterable[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Mapping must use destination=source-header: {value}")
        destination, source = value.split("=", 1)
        if destination not in {
            "case_id", "platform", "target_key", "target_url", "comment_text", "comment_type",
            "expected_amount", "reply_text", "image_requirement", "product_name", "group_leader",
            "leader_contact", "settlement_time", "settlement_voucher", "qr_code",
        }:
            raise ValueError(f"Unknown destination field: {destination}")
        result[destination] = source
    return result


def extract_url(value: Any) -> str:
    text = "" if value is None else unicodedata.normalize("NFKC", str(value))
    match = re.search(r"https?://[^\s>】\]）)]+", text, flags=re.IGNORECASE)
    return match.group(0).rstrip(".,，。!?！？") if match else ""


def sanitize_url(value: Any) -> str:
    url = extract_url(value)
    if not url:
        return ""
    parsed = urlsplit(url)
    # Query strings may contain access tokens; they are not needed as a stable target identity.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def target_key_from_url(value: Any) -> str:
    url = sanitize_url(value)
    if not url:
        return ""
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    for pattern in (r"/discovery/item/([^/]+)$", r"/explore/([^/]+)$", r"/video/([^/]+)$"):
        match = re.search(pattern, path, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return path.rsplit("/", 1)[-1] or url


def infer_platform(value: Any) -> str:
    text = str(value or "").casefold()
    if "xiaohongshu.com" in text or "xhslink.com" in text or "xhslink.cn" in text:
        return "xiaohongshu"
    if "douyin.com" in text:
        return "douyin"
    return ""


def looks_like_image_requirement(value: Any) -> bool:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).strip().casefold()
    if not text:
        return False
    if text.startswith("=dispimg("):
        return True
    markers = ("图片", "配图", "晒图", "发图", "截图", "上图", "图一", "图二", "image", ".png", ".jpg", ".jpeg")
    return any(marker in text for marker in markers)


def parse_amount_rules(value: Optional[str]) -> Dict[str, str]:
    if not value:
        return {}
    result: Dict[str, str] = {}
    for item in value.split(","):
        if "=" not in item:
            raise ValueError(f"Amount rule must use type=value: {item}")
        key, raw_amount = item.split("=", 1)
        key = key.strip()
        if key not in {"text", "image", "text+image"}:
            raise ValueError(f"Unknown amount-rule type: {key}")
        try:
            result[key] = format(Decimal(raw_amount.strip()).quantize(Decimal("0.01")), "f")
        except InvalidOperation as exc:
            raise ValueError(f"Invalid amount rule: {item}") from exc
    return result


def amount_rules_from_header(header: Optional[str]) -> Dict[str, str]:
    text = unicodedata.normalize("NFKC", header or "")
    patterns = {
        "text+image": r"文字\s*加\s*图\s*([0-9]+(?:\.[0-9]+)?)",
        "image": r"单\s*图\s*([0-9]+(?:\.[0-9]+)?)",
        "text": r"(?<!加)文字(?!\s*加\s*图)\s*([0-9]+(?:\.[0-9]+)?)",
    }
    result = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            result[key] = format(Decimal(match.group(1)).quantize(Decimal("0.01")), "f")
    return result


def resolve_mapping(headers: Iterable[str], explicit: Dict[str, str]) -> Dict[str, str]:
    by_norm = {norm_header(header): header for header in headers}
    result = dict(explicit)
    for destination, aliases in FIELD_ALIASES.items():
        if destination in result:
            continue
        for alias in aliases:
            if norm_header(alias) in by_norm:
                result[destination] = by_norm[norm_header(alias)]
                break
        if destination not in result and destination in {"target_url", "expected_amount", "reply_text"}:
            for header_norm, header in by_norm.items():
                if any(header_norm.startswith(norm_header(alias)) for alias in aliases if norm_header(alias)):
                    result[destination] = header
                    break
    return result


def convert(
    rows: List[Dict[str, Any]],
    explicit: Dict[str, str],
    source_name: str,
    source_sheet: Optional[str] = None,
    amount_rules: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    if not rows:
        return []
    mapping = resolve_mapping(rows[0].keys(), explicit)
    if "group_leader" in mapping and norm_header(mapping["group_leader"]) == norm_header("负责人"):
        raise ValueError("group_leader must use the 团长/团 field; 负责人 abbreviations are not allowed")
    if "expected_amount" in mapping:
        inferred_rules = amount_rules_from_header(mapping["expected_amount"])
    else:
        inferred_rules = {}
    resolved_amount_rules = {**inferred_rules, **(amount_rules or {})}
    missing = []
    if "target_key" not in mapping and "target_url" not in mapping:
        missing.append("target_key or target_url")
    if missing:
        raise ValueError("Missing required column mapping: " + ", ".join(missing))
    output = []
    previous_target_url = ""
    previous_target_key = ""
    previous_group_leader = ""
    previous_product_name = ""
    for row_number, row in enumerate(rows, start=2):
        relevant_sources = [
            mapping[field]
            for field in ("target_key", "target_url", "comment_text", "comment_type", "expected_amount", "reply_text", "image_requirement")
            if field in mapping
        ]
        if not any(str(row.get(source, "")).strip() for source in relevant_sources):
            continue
        item: Dict[str, Any] = {
            "source_file": source_name,
            "source_sheet": source_sheet or "",
            "source_row": row_number,
            "case_id": str(row.get(mapping["case_id"], "")).strip() if "case_id" in mapping else f"{source_name}:{source_sheet or 'sheet'}:{row_number}",
        }
        for field, source in mapping.items():
            value = row.get(source, "")
            if value != "":
                item[field] = value
        # Many operational sheets use an unmerged visual block: the link is
        # written once and the following comment rows leave the link cell
        # blank. Carry only target identity within the current worksheet.
        if "target_url" in mapping:
            current_url = str(item.get("target_url", "")).strip()
            if current_url:
                previous_target_url = current_url
            elif previous_target_url:
                item["target_url"] = previous_target_url
        elif "target_key" in mapping:
            current_key = str(item.get("target_key", "")).strip()
            if current_key:
                previous_target_key = current_key
            elif previous_target_key:
                item["target_key"] = previous_target_key
        for field, previous_name in (("group_leader", "previous_group_leader"), ("product_name", "previous_product_name")):
            if field not in mapping:
                continue
            current_value = str(item.get(field, "")).strip()
            if current_value:
                if field == "group_leader":
                    previous_group_leader = current_value
                else:
                    previous_product_name = current_value
            else:
                previous_value = previous_group_leader if field == "group_leader" else previous_product_name
                if previous_value:
                    item[field] = previous_value
        if "target_url" in item:
            item["target_url"] = sanitize_url(item["target_url"])
        if "target_key" not in item or not str(item["target_key"]).strip():
            item["target_key"] = target_key_from_url(item.get("target_url", ""))
        if "platform" not in item or not str(item["platform"]).strip():
            item["platform"] = infer_platform(item.get("target_url", ""))
        item["platform"] = str(item.get("platform", "")).strip().casefold()
        item["comment_text"] = str(item.get("comment_text", "")).strip()
        image_in_reply_cell = False
        if "reply_text" in mapping:
            reply_text = str(row.get(mapping["reply_text"], "")).strip()
            # A populated reply-content cell is authoritative. Source sheets
            # use several relationship directives in the left column, such as
            # "第一条回复第二条", which do not necessarily start with "回复".
            reply_directive = bool(re.fullmatch(
                r"(?:第[一二三四五六七八九十\d]+条)?回复(?:第[一二三四五六七八九十\d]+条|图片)?",
                item["comment_text"],
            ))
            if reply_text.casefold() == "embedded-image.png" and item["comment_text"] and not reply_directive:
                # Pictures sometimes occupy the reply column alongside the
                # actual comment body in the text column. Do not erase it.
                image_in_reply_cell = True
            elif reply_text == "回复图片" and item["comment_text"].casefold() == "embedded-image.png":
                shifted_body = str(row.get(mapping.get("image_requirement", ""), "")).strip()
                item["reply_to"] = item["comment_text"]
                item["comment_text"] = shifted_body if shifted_body and not looks_like_image_requirement(shifted_body) else reply_text
            elif reply_text:
                item["reply_to"] = item["comment_text"]
                item["comment_text"] = reply_text
        # Separate an explicit trailing authoring note from the requested body.
        # This is source-data interpretation only: never perform the instruction.
        editorial = re.search(r"\s*[（(](辛苦\s*@[^（）()\s]+\s*要手打艾特出来[！!。]*)[）)]\s*$", item["comment_text"])
        if editorial and item["comment_text"][:editorial.start()].strip():
            item["source_comment_text"] = item["comment_text"]
            item["editorial_note"] = editorial.group(1)
            item["comment_text"] = item["comment_text"][:editorial.start()].strip()
        image_value = row.get(mapping["image_requirement"], "") if "image_requirement" in mapping else ""
        if image_in_reply_cell:
            image_value = "embedded-image.png"
        if item["comment_text"].casefold() == "embedded-image.png":
            image_value = item["comment_text"]
            item["comment_text"] = ""
        if str(image_value).strip():
            item["image_requirement"] = str(image_value).strip()
        item["has_image"] = looks_like_image_requirement(image_value)
        if "comment_type" not in item or not str(item["comment_type"]).strip():
            if item["comment_text"] and item["has_image"]:
                item["comment_type"] = "text+image"
            elif item["comment_text"]:
                item["comment_type"] = "text"
            elif item["has_image"]:
                item["comment_type"] = "image"
        # A URL-only placeholder row is not an audit task. Excluding it avoids
        # inflating human-review counts with rows that contain no requirement.
        if not item["comment_text"] and not item["has_image"]:
            continue
        if "expected_amount" not in item or not str(item["expected_amount"]).strip():
            rule = resolved_amount_rules.get(str(item.get("comment_type", "")).strip())
            if rule is not None:
                item["expected_amount"] = rule
        if item.get("comment_type") == "text+image":
            if resolved_amount_rules.get("text") is not None:
                item["partial_text_amount"] = resolved_amount_rules["text"]
            if resolved_amount_rules.get("image") is not None:
                item["partial_image_amount"] = resolved_amount_rules["image"]
        output.append(item)
    return output


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="CSV/TSV/JSON/XLSX source")
    parser.add_argument("--output", required=True, help="Expected-row JSON output")
    parser.add_argument("--sheet", help="XLSX worksheet name; defaults to the first sheet")
    parser.add_argument("--all-sheets", action="store_true", help="Convert every worksheet in an XLSX")
    parser.add_argument("--amount-rules", help="Fallback rules such as text=1.5,text+image=2,image=1.5")
    parser.add_argument("--map", action="append", default=[], help="Explicit mapping destination=source-header; repeatable")
    args = parser.parse_args(argv)
    try:
        source = Path(args.input)
        explicit = parse_maps(args.map)
        rules = parse_amount_rules(args.amount_rules)
        if source.suffix.casefold() == ".xlsx" and args.all_sheets:
            sheet_names = list_xlsx_sheets(source)
            converted = []
            for sheet_name in sheet_names:
                converted.extend(convert(read_xlsx(source, sheet_name), explicit, source.name, sheet_name, rules))
        else:
            sheet_name = args.sheet if source.suffix.casefold() == ".xlsx" else None
            converted = convert(read_rows(source, sheet_name), explicit, source.name, sheet_name, rules)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"rows": converted}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"converted {len(converted)} rows -> {output}")
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"input_error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
