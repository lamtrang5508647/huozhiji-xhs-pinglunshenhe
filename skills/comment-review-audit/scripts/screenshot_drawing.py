"""Embed PNGs at native aspect ratio and remove only this reader's obsolete drawing anchors."""
import hashlib
from collections import Counter
import posixpath
import re
from xml.etree import ElementTree as ET

if __package__:
    from .build_audited_workbook import CT, P, R, encode, relation_target, tag
else:
    from build_audited_workbook import CT, P, R, encode, relation_target, tag

XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
OWNER = "comment-review-audit:"


def relfile(part):
    return posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")


def relationships(parts, part):
    path = relfile(part)
    return ET.fromstring(parts[path]) if path in parts else ET.Element(f"{{{P}}}Relationships")


def next_id(used, prefix):
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


def picture_identity(anchor, drawing_path, drels, parts):
    marker = anchor.find(f"{{{XDR}}}from")
    blip = anchor.find(f"{{{XDR}}}pic/{{{XDR}}}blipFill/{{{A}}}blip")
    if marker is None or blip is None:
        return None
    rel = next((r for r in drels if r.get("Id")==blip.get(f"{{{R}}}embed") and r.get("Type")==R+"/image"),None)
    if rel is None:
        return None
    media = parts.get(relation_target(drawing_path,rel.get("Target","")))
    if media is None:
        return None
    return (marker.findtext(f"{{{XDR}}}col"), marker.findtext(f"{{{XDR}}}row"), hashlib.sha256(media).hexdigest())


def source_picture_ownership(parts, sheet_path, gallery):
    counts=Counter()
    rels=relationships(parts,sheet_path)
    for d in gallery.findall(tag("drawing")):
        rel=next(r for r in rels if r.get("Id")==d.get(f"{{{R}}}id"))
        path=relation_target(sheet_path,rel.get("Target",""))
        drels=relationships(parts,path)
        for anchor in ET.fromstring(parts[path]):
            props=anchor.find(f"{{{XDR}}}pic/{{{XDR}}}nvPicPr/{{{XDR}}}cNvPr")
            if props is not None and re.fullmatch(OWNER+r"[0-9a-f]{64}",props.get("descr","")):
                key=picture_identity(anchor,path,drels,parts)
                if key and props.get("descr")==OWNER+key[2]:
                    counts[key]+=1
    return counts


def install_pictures(parts, sheet_path, old_sheet, sheet, types, pictures, removed, source_owned=None):
    sheet_rels = relationships(parts, sheet_path)
    existing = old_sheet.find(tag("drawing")) if old_sheet is not None else None
    if existing is None and not pictures:
        return
    if existing is None:
        name = next_id({posixpath.basename(p).removesuffix(".xml") for p in parts}, "auditShots")
        drawing_path = f"xl/drawings/{name}.xml"
        rid = next_id({r.get("Id") for r in sheet_rels}, "auditDrawing")
        ET.SubElement(sheet_rels, f"{{{P}}}Relationship", {"Id":rid, "Type":R+"/drawing",
                      "Target":posixpath.relpath(drawing_path, posixpath.dirname(sheet_path))})
        ET.SubElement(types, f"{{{CT}}}Override", {"PartName":"/"+drawing_path,
                      "ContentType":"application/vnd.openxmlformats-officedocument.drawing+xml"})
        existing = ET.Element(tag("drawing"), {f"{{{R}}}id":rid})
    else:
        relation = next(r for r in sheet_rels if r.get("Id") == existing.get(f"{{{R}}}id"))
        drawing_path = relation_target(sheet_path, relation.get("Target"))
    original = parts.get(drawing_path, b"")
    drawing = ET.fromstring(original) if original else ET.Element(f"{{{XDR}}}wsDr")
    drels = relationships(parts, drawing_path)
    old_refs = set()
    old_media = set()
    for anchor in list(drawing):
        props = anchor.find(f"{{{XDR}}}pic/{{{XDR}}}nvPicPr/{{{XDR}}}cNvPr")
        if props is None:
            continue
        signed = re.fullmatch(OWNER+r"[0-9a-f]{64}", props.get("descr", ""))
        if source_owned:
            # Some Excel engines discard alt text during re-export. Match only anchors
            # explicitly owned in the input file, by original position and actual bytes.
            identity = picture_identity(anchor,drawing_path,drels,parts)
            if source_owned[identity]:
                source_owned[identity]-=1
                signed=True
        if not signed:
            continue
        old_refs.update(b.get(f"{{{R}}}embed") for b in anchor.iter(f"{{{A}}}blip"))
        drawing.remove(anchor)
    in_use = {b.get(f"{{{R}}}embed") for b in drawing.iter(f"{{{A}}}blip")}
    for rel in list(drels):
        if rel.get("Id") in old_refs - in_use and rel.get("Type") == R+"/image":
            old_media.add(relation_target(drawing_path,rel.get("Target")))
            drels.remove(rel)
    if pictures and not any(t.get("Extension", "").lower()=="png" for t in types):
        types.insert(0, ET.Element(f"{{{CT}}}Default", {"Extension":"png","ContentType":"image/png"}))
    pic_id = max((int(p.get("id", 0)) for p in drawing.iter(f"{{{XDR}}}cNvPr")),default=0)
    ids = {r.get("Id") for r in drels}
    media = {}
    for image in pictures:
        sha, binary = image["sha"], image["data"]
        if sha not in media:
            media_path = f"xl/media/audit-shot-{sha}.png"
            if media_path in parts and parts[media_path] != binary:
                raise ValueError("excel_image_media_collision")
            parts[media_path] = binary
            old_media.discard(media_path)
            rid = next_id(ids, "shot")
            ids.add(rid)
            media[sha] = rid
            ET.SubElement(drels, f"{{{P}}}Relationship", {"Id":rid,"Type":R+"/image",
                          "Target":posixpath.relpath(media_path,posixpath.dirname(drawing_path))})
        pic_id += 1
        anchor = ET.SubElement(drawing,f"{{{XDR}}}oneCellAnchor")
        start = ET.SubElement(anchor,f"{{{XDR}}}from")
        for name, value in (("col",0),("colOff",6*9525),("row",image["row"]-1),("rowOff",4*9525)):
            ET.SubElement(start,f"{{{XDR}}}{name}").text = str(value)
        extent = {"cx":str(image["width"]*9525),"cy":str(image["height"]*9525)}
        ET.SubElement(anchor,f"{{{XDR}}}ext",extent)
        pic = ET.SubElement(anchor,f"{{{XDR}}}pic")
        nv = ET.SubElement(pic,f"{{{XDR}}}nvPicPr")
        ET.SubElement(nv,f"{{{XDR}}}cNvPr",{"id":str(pic_id),"name":f"评论审核截图 {image['number']}","descr":OWNER+sha})
        props = ET.SubElement(nv,f"{{{XDR}}}cNvPicPr")
        ET.SubElement(props,f"{{{A}}}picLocks",{"noChangeAspect":"1"})
        fill = ET.SubElement(pic,f"{{{XDR}}}blipFill")
        ET.SubElement(fill,f"{{{A}}}blip",{f"{{{R}}}embed":media[sha]})
        ET.SubElement(ET.SubElement(fill,f"{{{A}}}stretch"),f"{{{A}}}fillRect")
        shape = ET.SubElement(pic,f"{{{XDR}}}spPr")
        xfrm = ET.SubElement(shape,f"{{{A}}}xfrm")
        ET.SubElement(xfrm,f"{{{A}}}off",{"x":"0","y":"0"})
        ET.SubElement(xfrm,f"{{{A}}}ext",extent)
        ET.SubElement(ET.SubElement(shape,f"{{{A}}}prstGeom",{"prst":"rect"}), f"{{{A}}}avLst")
        ET.SubElement(anchor,f"{{{XDR}}}clientData")
    sheet.append(existing)
    parts[drawing_path] = encode(drawing,original)
    parts[relfile(sheet_path)] = encode(sheet_rels, parts.get(relfile(sheet_path),b""))
    parts[relfile(drawing_path)] = encode(drels, parts.get(relfile(drawing_path),b""))
    if old_media:
        # A source sheet may also reference an image originally produced by our gallery.
        for path, data in parts.items():
            if not old_media or not path.endswith(".rels"):
                continue
            owner = posixpath.join(posixpath.dirname(posixpath.dirname(path)),posixpath.basename(path)[:-5])
            for rel in ET.fromstring(data):
                if rel.get("TargetMode")!="External":
                    old_media.discard(relation_target(owner,rel.get("Target","")))
        for stale in old_media:
            if stale.startswith("xl/media/") and stale in parts:
                parts.pop(stale)
                removed.add(stale)
                for override in list(types):
                    if override.get("PartName") == "/"+stale:
                        types.remove(override)
