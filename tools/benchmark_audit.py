"""Time the offline audit CLI on a fixed synthetic workbook and compare content signatures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from xml.etree import ElementTree as ET


REPO = Path(__file__).resolve().parents[1]
SHEETS = 12
ROWS_PER_SHEET = 100
ROWS_PER_TARGET = 50
COMMENTS_PER_TARGET = 300
SPREADSHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006/"
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml."
HEADERS = ["文章链接", "产品", "团长", "评论文字需求", "需要回复评论内容", "评论图片需求",
           "审核结果", "金额【文字1.5，文字加图2元，单图1.5】"]


def add_row(data, number, values, strings):
    row = ET.SubElement(data, "row", r=str(number))
    for letter, value in zip("ABCDEFGH", values, strict=True):
        if value:
            cell = ET.SubElement(row, "c", r=f"{letter}{number}", t="s")
            ET.SubElement(cell, "v").text = str(strings.setdefault(value, len(strings)))


def make_sheet(sheet_index, strings, observations):
    product = f"产品{sheet_index:02}"
    sheet = ET.Element("worksheet", xmlns=SPREADSHEET)
    data = ET.SubElement(sheet, "sheetData")
    add_row(data, 1, HEADERS, strings)
    for first in range(0, ROWS_PER_SHEET, ROWS_PER_TARGET):
        target = f"n{sheet_index:02}-{first // ROWS_PER_TARGET}"
        comments = [{"text": f"普通评论 {sheet_index:02} {k}", "has_image": False}
                    for k in range(COMMENTS_PER_TARGET)]
        observations.append({"target_key": target, "platform": "xiaohongshu", "capture_status": "ok",
                             "capture_complete": sheet_index % 3 != 1, "image_detection_complete": True,
                             "comments": comments})
        for i in range(first, first + ROWS_PER_TARGET):
            text = f"样例 {sheet_index:02} 第 {i:03} 条评论"
            image = i % 5 == 0
            add_row(data, i + 2, [f"https://www.xiaohongshu.com/explore/{target}", product, f"团长{i % 4}",
                                  text, "", "图片" if image else "", "", ""], strings)
            if i % 9:
                comments[i - first] = {"text": text, "has_image": image and i % 10 != 0}
    return sheet


def generate(source, evidence):
    strings = {}
    observations = []
    workbook = ET.Element("workbook", {"xmlns": SPREADSHEET, "xmlns:r": OFFICE_RELS})
    sheets = ET.SubElement(workbook, "sheets")
    rels = ET.Element("Relationships", xmlns=PACKAGE + "relationships")
    types = ET.Element("Types", xmlns=PACKAGE + "content-types")
    ET.SubElement(types, "Default", Extension="xml", ContentType="application/xml")
    ET.SubElement(types, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    for name, kind in (("workbook.xml", "sheet.main"), ("sharedStrings.xml", "sharedStrings")):
        ET.SubElement(types, "Override", PartName="/xl/" + name, ContentType=MIME + kind + "+xml")
    ET.SubElement(rels, "Relationship", Id="strings", Target="sharedStrings.xml", Type=OFFICE_RELS + "/sharedStrings")

    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        for i in range(SHEETS):
            part = f"worksheets/sheet{i + 1}.xml"
            relation = f"rId{i + 1}"
            ET.SubElement(sheets, "sheet", {"name": f"产品{i:02}", "sheetId": str(i + 1), "r:id": relation})
            ET.SubElement(rels, "Relationship", Id=relation, Target=part, Type=OFFICE_RELS + "/worksheet")
            ET.SubElement(types, "Override", PartName="/xl/" + part, ContentType=MIME + "worksheet+xml")
            sheet = make_sheet(i, strings, observations)
            archive.writestr("xl/" + part, ET.tostring(sheet, encoding="utf-8"))
        shared_strings = ET.Element("sst", xmlns=SPREADSHEET)
        for value in strings:
            ET.SubElement(ET.SubElement(shared_strings, "si"), "t").text = value
        for part, tree in (("xl/workbook.xml", workbook), ("xl/_rels/workbook.xml.rels", rels),
                           ("[Content_Types].xml", types), ("xl/sharedStrings.xml", shared_strings)):
            archive.writestr(part, ET.tostring(tree, encoding="utf-8"))
    evidence.write_text(json.dumps({"observations": observations}, ensure_ascii=False), encoding="utf-8")


def signature(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def benchmark(checkout, runs, folder):
    sys.path.insert(0, str(checkout))
    from huozhiji_audit import convert_table

    source = folder / "batch.xlsx"
    evidence = folder / "batch-observations.json"
    output = folder / "audited.xlsx"
    generate(source, evidence)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(checkout)
    command = [sys.executable, "-X", "utf8", "-m", "huozhiji_audit", "audit", "--source", str(source),
               "--output", str(output), "--observations", str(evidence)]
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        proc = subprocess.run(command, cwd=checkout, env=env, capture_output=True)
        if proc.returncode:
            raise RuntimeError((proc.stdout + proc.stderr).decode("utf-8", errors="replace"))
        times.append(time.perf_counter() - start)
        report = json.loads(proc.stdout)

    conversions = []
    for _ in range(runs):
        start = time.perf_counter()
        converted = convert_table(source)
        conversions.append(time.perf_counter() - start)
    with zipfile.ZipFile(output) as archive:
        members = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()}
    # Paths and raw ZIP hashes vary between runs (ZIP timestamps). Hash the audit
    # report separately from workbook bytes and compare workbook members by name.
    business_report = {key: value for key, value in report.items() if key != "workbook"}
    return {"checkout": str(checkout), "runs": runs, "times": times, "median": statistics.median(times),
            "convert_median": statistics.median(conversions), "summary": report["summary"],
            "rows": signature(converted), "results": signature(report["results"]), "report": signature(business_report),
            "members": signature(members)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=REPO, help="Source checkout to measure (default: this repo)")
    parser.add_argument("--runs", type=int, default=5, help="Number of timed audit and conversion runs (default: 5)")
    parser.add_argument("--work-dir", type=Path, help="Keep generated files under a new subdirectory here")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    checkout = args.checkout.resolve()
    if not (checkout / "huozhiji_audit" / "__main__.py").is_file():
        parser.error("--checkout must contain the huozhiji_audit package")
    if args.work_dir:
        parent = args.work_dir.resolve()
        parent.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(dir=parent, prefix="audit-benchmark-"))
        result = benchmark(checkout, args.runs, folder)
        result["work_dir"] = str(folder)
    else:
        with tempfile.TemporaryDirectory(prefix="audit-benchmark-") as folder:
            result = benchmark(checkout, args.runs, Path(folder))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
