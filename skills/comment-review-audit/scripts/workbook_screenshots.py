"""Write note screenshots and their task decisions together in a self-contained financial audit workbook."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import unicodedata
from xml.etree import ElementTree as ET
import zipfile

if __package__:
    from .build_audited_workbook import P, R, add_worksheet_part, column, encode, relation_target, sheet_cell_writer, tag
    from .note_links import checked_note_url, with_finance_note_urls
    from .screenshot_evidence import _safe_path, image_bytes
    from .screenshot_drawing import install_pictures, relationships, relfile, source_picture_ownership
    from .table_to_expected import (SCREENSHOT_INDEX_HEADERS, SCREENSHOT_INDEX_TITLE, SCREENSHOT_GALLERY_HEADERS,
                                   SCREENSHOT_GALLERY_TITLE, generated_screenshot_index, screenshot_index_name,
                                   xlsx_sheet_members, xlsx_shared_strings)
else:
    from build_audited_workbook import P, R, add_worksheet_part, column, encode, relation_target, sheet_cell_writer, tag
    from note_links import checked_note_url, with_finance_note_urls
    from screenshot_evidence import _safe_path, image_bytes
    from screenshot_drawing import install_pictures, relationships, relfile, source_picture_ownership
    from table_to_expected import (SCREENSHOT_INDEX_HEADERS, SCREENSHOT_INDEX_TITLE, SCREENSHOT_GALLERY_HEADERS,
                                  SCREENSHOT_GALLERY_TITLE, generated_screenshot_index, screenshot_index_name,
                                  xlsx_sheet_members, xlsx_shared_strings)

PLATFORMS = {"xiaohongshu":"小红书", "douyin":"抖音"}
KINDS = {"comment":"评论", "comments":"评论列表", "panel":"评论区", "end":"列表末尾", "empty":"空评论区", "unavailable":"笔记页面提示"}
MAX_ROWS = 1048576


def utc_time(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (AttributeError, ValueError):
        pass
    return ""


def collect_images(rows, results, base):
    by_id = {r["case_id"]:r for r in results}
    checked, pictures, tasks = {}, {}, []
    for n, row in enumerate(rows, 3):
        result = by_id[row["case_id"]]
        platform, key = row.get("platform",""), row.get("target_key","")
        carried = result.get("status")=="carried_forward" or result.get("review_action")=="carried_forward"
        files = [] if carried else result.get("comment_screenshots") or []
        nums, seen, invalid = [], set(), False
        for record in files:
            part = _safe_path(record.get("path")) if isinstance(record,dict) else None
            if part is None:
                invalid = True
                continue
            cache_key = platform,key,str(part),str(record.get("sha256","")),str(record.get("kind",""))
            if cache_key not in checked:
                directory = base/part.parts[0]
                checked[cache_key] = image_bytes(record,directory,platform,key) if directory.resolve().is_relative_to(base) else None
            binary = checked[cache_key]
            if binary is None:
                invalid = True
                continue
            # Two notes can display identical empty-state pixels. Keep their task contexts separate.
            identity = platform,key,record["path"]
            if identity not in pictures:
                w,h = struct.unpack(">II",binary[16:24])
                scale = min(1, 768 / w)
                pictures[identity] = {"data":binary,"sha":record["sha256"],"number":len(pictures)+1,
                                   "width":max(1,round(w*scale)),"height":max(1,round(h*scale)),
                                   "kind":KINDS[record["kind"]],"stamp":utc_time(record.get("captured_at")),
                                   "platform":PLATFORMS.get(platform,platform),"key":key,"tasks":[]}
            picture = pictures[identity]
            if picture["number"] not in seen:
                seen.add(picture["number"])
                picture["tasks"].append(n)
                nums.append(picture["number"])
        if carried:
            status = "本次未复核"
        elif not nums:
            status = "截图文件缺失或无效" if files else "未取得截图"
        else:
            status = "截图不完整" if invalid or result.get("screenshot_status")!="captured" else "已留图"
        source = row["source_sheet"]
        amount = result.get("approved_amount")
        url = checked_note_url(row.get("finance_note_url"), platform, key) or checked_note_url(row.get("target_url"), platform, key)
        url_label = "打开原视频" if platform=="douyin" else "打开原笔记"
        tasks.append(dict(number=n,platform=PLATFORMS.get(platform,platform),key=key,
                          location=f"'{source.replace(chr(39),chr(39)*2)}'!A{row['source_row']}",
                          source=f"{source} · {row['source_row']} 行",body=row.get("comment_text") or "图片任务",
                          category=result.get("result_category") or "",amount=None if amount in (None,"") else Decimal(str(amount)),
                          status=status,images=nums,carried=carried,url=url,url_label=url_label))
    return tasks,list(pictures.values())


def new_sheet(title, headers, widths, header_style, last_column):
    root = ET.Element(tag("worksheet"))
    view = ET.SubElement(ET.SubElement(root,tag("sheetViews")),tag("sheetView"),{"workbookViewId":"0"})
    ET.SubElement(view,tag("pane"),{"ySplit":"2","topLeftCell":"A3","activePane":"bottomLeft","state":"frozen"})
    ET.SubElement(root,tag("sheetFormatPr"),{"defaultRowHeight":"24"})
    cols = ET.SubElement(root,tag("cols"))
    for i,width in enumerate(widths,1):
        ET.SubElement(cols,tag("col"),{"min":str(i),"max":str(i),"width":str(width),"customWidth":"1"})
    data = ET.SubElement(root,tag("sheetData"))
    write = sheet_cell_writer(data)
    write("A1",title,style=header_style)
    for i,title in enumerate(headers,1):
        write(f"{column(i)}2",title,style=header_style)
    ET.SubElement(ET.SubElement(root,tag("mergeCells"),{"count":"1"}),tag("mergeCell"),{"ref":f"A1:{last_column}1"})
    return root,data,write


def internal(links, cell, location):
    ET.SubElement(links,tag("hyperlink"),{"ref":cell,"location":location})


def link_posts(parts, sheet_path, root, links_by_cell):
    links = root.find(tag("hyperlinks"))
    if links is None and links_by_cell:
        links = ET.SubElement(root,tag("hyperlinks"))
    rels = relationships(parts,sheet_path)
    for previous in list(rels):
        # This is an owned, regenerated sheet. artifact-tool renumbers its prior cell links,
        # including links from the older index which pointed at external PNG files.
        if previous.get("Type") == R+"/hyperlink":
            rels.remove(previous)
    ids = {}
    for cell,url in links_by_cell:
        if url not in ids:
            rid = f"auditPost{len(ids)+1}"
            ids[url]=rid
            ET.SubElement(rels,f"{{{P}}}Relationship",{"Id":rid,"Type":R+"/hyperlink","TargetMode":"External","Target":url})
        ET.SubElement(links,tag("hyperlink"),{"ref":cell,f"{{{R}}}id":ids[url]})
    if len(rels):
        parts[relfile(sheet_path)]=encode(rels,parts.get(relfile(sheet_path),b""))
    else:
        parts.pop(relfile(sheet_path),None)


def display_gallery(pictures,tasks,styles,post_links):
    width = max((p["width"] for p in pictures),default=390)
    root,data,write = new_sheet(SCREENSHOT_GALLERY_TITLE,SCREENSHOT_GALLERY_HEADERS,
        (max(36,math.ceil((width+22)/7)),22,43,14,13,16,8,14,31,24),styles[0],"J")
    links = ET.Element(tag("hyperlinks"))
    heights = {}
    next_row = 3
    def write_task(row, task):
        amount = "未核定" if task["amount"] is None else task["amount"]
        for number,value in enumerate((task["source"], task["body"], task["category"], amount,
                                       task["url_label"] if task["url"] else "链接未核实"),2):
            write(f"{column(number)}{row}",value,style=styles[2] if number==5 else styles[1])
        internal(links,f"B{row}",task["location"])
        if task["url"]:
            post_links.append((f"F{row}",task["url"]))
        # Allow wrapped CJK and multi-line comment text beside the picture.
        lines = sum(max(1,math.ceil(sum(2 if unicodedata.east_asian_width(c) in ("W","F") else 1 for c in line)/40))
                    for line in str(task["body"]).split("\n"))
        heights[row] = min(400,max(27,lines*17+8))
    for pic in pictures:
        first = next_row
        pic["row"] = first
        for n in pic["tasks"]:
            write_task(next_row,tasks[n-3])
            next_row += 1
        for number,value in enumerate((pic["number"],pic["kind"],f"{pic['platform']} · {pic['key']}",pic["stamp"]),7):
            write(f"{column(number)}{first}",value,style=styles[1])
        # Excel allows only 409 points per row. Span multiple physical rows for tall screenshots.
        remaining = max(0, math.ceil((pic["height"]+14)*0.75) - sum(heights[n] for n in range(first,next_row)))
        extra = min(remaining,400-heights[next_row-1])
        heights[next_row-1] += extra
        remaining -= extra
        if next_row + math.ceil(remaining/400) > MAX_ROWS+1:
            raise ValueError("screenshot_gallery_exceeds_excel_row_limit")
        while remaining:
            h = min(400,remaining)
            write(f"A{next_row}",None)
            heights[next_row] = h
            remaining -= h
            next_row += 1
    for task in tasks:
        if not task["images"] and not task["carried"]:
            write(f"A{next_row}",task["status"],style=styles[1])
            write_task(next_row,task)
            next_row += 1
    if next_row > MAX_ROWS+1:
        raise ValueError("screenshot_gallery_exceeds_excel_row_limit")
    for row in data:
        n = int(row.get("r"))
        if n in heights:
            row.attrib.update(ht=str(heights[n]),customHeight="1")
    if len(links):
        root.append(links)
    return root


def display_index(tasks, pictures, styles, gallery_name):
    root,_,write = new_sheet(SCREENSHOT_INDEX_TITLE,SCREENSHOT_INDEX_HEADERS,
        (12,28,24,54,14,13,19,31,3,10,12,28,16,18),styles[0],"H")
    merges = root.find(tag("mergeCells"))
    ET.SubElement(merges,tag("mergeCell"),{"ref":"J1:N1"})
    merges.set("count","2")
    links = ET.Element(tag("hyperlinks"))
    write("J1","本轮笔记截图",style=styles[0])
    for i,title in enumerate(("图号","平台","笔记或视频编号","画面","查看原图及结果"),10):
        write(f"{column(i)}2",title,style=styles[0])
    def target(pic):
        return f"'{gallery_name}'!A{pic['row']}"
    for pic in pictures:
        n = pic["number"]+2
        for i,value in enumerate((pic["number"],pic["platform"],pic["key"],pic["kind"],f"查看图 {pic['number']}"),10):
            write(f"{column(i)}{n}",value,style=styles[1])
        internal(links,f"N{n}",target(pic))
    for task in tasks:
        n=task["number"]
        for i,value in enumerate((task["platform"],task["key"],task["source"],task["body"],task["category"],task["amount"],task["status"]),1):
            write(f"{column(i)}{n}",value,style=styles[2] if i==6 else styles[1])
        internal(links,f"C{n}",task["location"])
        if task["images"]:
            write(f"H{n}","图 "+"、".join(str(k) for k in task["images"]),style=styles[1])
            internal(links,f"H{n}",target(pictures[task["images"][0]-1]))
    root.insert(list(root).index(merges),ET.Element(tag("autoFilter"),{"ref":f"A2:H{len(tasks)+2}"}))
    root.append(links)
    return root


def add_screenshot_index(parts, rows, results, base, removed_media=None, *, source_owned=None):
    core = ("xl/workbook.xml","xl/_rels/workbook.xml.rels","[Content_Types].xml")
    wb,rels,types = (ET.fromstring(parts[name]) for name in core)
    sheets = wb.find(tag("sheets"))
    targets = {r.get("Id"):relation_target(core[0],r.get("Target","")) for r in rels}
    paths = {s.get("name"):targets[s.get(f"{{{R}}}id")] for s in sheets}
    requested = any("screenshot_status" in r or "comment_screenshots" in r for r in results)
    candidates = [name for name in paths if screenshot_index_name(name)]
    if not requested and not candidates:
        return None
    strings = []
    if candidates and "xl/sharedStrings.xml" in parts:
        strings = ["".join(t.text or "" for t in s.iter(tag("t"))) for s in ET.fromstring(parts["xl/sharedStrings.xml"])]
    owned={}
    for name in candidates:
        root=ET.fromstring(parts[paths[name]])
        if generated_screenshot_index(name,root,strings):
            owned[name.split("（")[0]]=(name,paths[name],root)
    if not requested and not owned:
        return None
    def sheet_for(stem):
        if stem in owned:
            result=owned[stem]
            if result[2].find(tag("sheetProtection")) is not None:
                raise ValueError("protected_screenshot_sheet")
            return result
        name=stem
        suffix=2
        while name in paths:
            name=f"{stem}（{suffix}）"
            suffix+=1
        part=add_worksheet_part(parts,sheets,rels,types,name,"rIdCommentScreenshots")
        parts[part]=b""
        paths[name]=part
        return name,part,None
    index_name,idx,old_index=sheet_for("评论截图")
    gallery_name,gal,old_gallery=sheet_for("评论原图")
    tasks,pictures=collect_images(rows,results,Path(base).resolve())
    if max(len(tasks),len(pictures))>MAX_ROWS-2:
        raise ValueError("screenshot_index_exceeds_excel_row_limit")
    summary=ET.fromstring(parts[paths["审核汇总"]])
    previous={c.get("r"):c.get("s") for c in summary.iter(tag("c")) if c.get("r") in ("A1","A2","I2")}
    styles=[previous.get(k) for k in ("A1","A2","I2")]
    gallery_links=[]
    gallery=display_gallery(pictures,tasks,styles,gallery_links)
    index=display_index(tasks,pictures,styles,gallery_name)
    if old_index is not None:
        for element in old_index:
            if element.tag in (tag("drawing"),tag("legacyDrawing"),tag("picture")):
                index.append(element)
    link_posts(parts,idx,index,[(f"B{t['number']}",t["url"]) for t in tasks if t["url"]])
    link_posts(parts,gal,gallery,gallery_links)
    install_pictures(parts,gal,old_gallery,gallery,types,pictures,removed_media if removed_media is not None else set(),source_owned)
    if old_gallery is not None:
        for el in old_gallery:
            if el.tag in (tag("legacyDrawing"),tag("picture")):
                gallery.append(el)
    gallery_view=gallery.find(f"{tag('sheetViews')}/{tag('sheetView')}")
    gallery_view.set("tabSelected","1")
    views=wb.find(tag("bookViews"))
    if views is None:
        views=ET.Element(tag("bookViews"))
        wb.insert(list(wb).index(sheets),views)
    if not len(views):
        ET.SubElement(views,tag("workbookView"))
    active=next(i for i,s in enumerate(sheets) if s.get("name")==gallery_name)
    for v in views:
        v.set("activeTab",str(active))
    for name,part in paths.items():
        if part in (idx,gal) or b"tabSelected" not in parts[part]:
            continue
        source_sheet=ET.fromstring(parts[part])
        for v in source_sheet.findall(f"{tag('sheetViews')}/{tag('sheetView')}"):
            v.attrib.pop("tabSelected",None)
        parts[part]=encode(source_sheet,parts[part])
    parts[idx],parts[gal]=encode(index),encode(gallery)
    for part,node in zip(core,(wb,rels,types)):
        parts[part]=encode(node,parts[part])
    return {"sheet":index_name,"gallery":gallery_name,"tasks":len(tasks),"images":len(pictures)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ("workbook","output","expected","report"):
        parser.add_argument("--"+key,type=Path,required=True)
    parser.add_argument("--screenshot-base",type=Path)
    parser.add_argument("--source","--original",type=Path)
    args=parser.parse_args()
    expected=json.loads(args.expected.read_text(encoding="utf-8"))
    results=json.loads(args.report.read_text(encoding="utf-8"))["results"]
    with zipfile.ZipFile(args.workbook) as archive:
        parts={n:archive.read(n) for n in archive.namelist()}
    original_owned=None
    rows=expected["rows"] if isinstance(expected,dict) else expected
    if args.source:
        rows=with_finance_note_urls(args.source,rows)
        with zipfile.ZipFile(args.source) as source:
            for name,path in xlsx_sheet_members(source).items():
                if not name.startswith("评论原图") or not screenshot_index_name(name):
                    continue
                root=ET.fromstring(source.read(path))
                if generated_screenshot_index(name,root,xlsx_shared_strings(source)):
                    original_parts={n:source.read(n) for n in source.namelist()
                                    if n.startswith(("xl/drawings/","xl/media/")) or n==relfile(path)}
                    original_owned=source_picture_ownership(original_parts,path,root)
                    break
    add_screenshot_index(parts,rows,results,
                         args.screenshot_base or args.report.resolve().parent,source_owned=original_owned)
    with tempfile.NamedTemporaryFile(dir=args.output.parent,delete=False) as tmp:
        dest=Path(tmp.name)
    try:
        with zipfile.ZipFile(dest,"w",zipfile.ZIP_DEFLATED) as archive:
            for name,data in parts.items():
                archive.writestr(name,data)
        os.replace(dest,args.output)
    finally:
        dest.unlink(missing_ok=True)


if __name__=="__main__":
    main()
