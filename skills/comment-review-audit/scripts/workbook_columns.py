"""Insert an actual worksheet column and relocate its native Excel references."""
import copy
import re
from xml.etree import ElementTree as ET

if __package__:
    from .build_audited_workbook import R, col_index, column, encode, relation_target, tag
    from .screenshot_drawing import XDR, relationships
else:
    from build_audited_workbook import R, col_index, column, encode, relation_target, tag
    from screenshot_drawing import XDR, relationships

REFERENCE = re.compile(
    r"(?<![\w.])(?:(?P<sheet>'(?:[^']|'')+'|(?:\[[^]]+\])?[^\W\d][\w.\\]*)!)?"
    r"(?P<first>\$?[A-Z]{1,3}\$?[1-9][0-9]*|\$?[A-Z]{1,3}(?=:))"
    r"(?P<last>:(?:\$?[A-Z]{1,3}\$?[1-9][0-9]*|\$?[A-Z]{1,3}))?(?![\w.(])"
)


def shift_reference(value, at):
    def cell(match):
        letters = match[2]
        number = col_index(letters)
        return match[1] + (column(number + 1) if number >= at else letters)
    return re.sub(r"(\$?)([A-Z]{1,3})(?=\$?\d|$|:)", cell, value)


def shift_formula(text, owner, changed, at):
    # String literals (including escaped quotes), structured references and external
    # workbook references retain their own meaning. Only A1 tokens are relocated.
    def expression(chunk):
        def replace(match):
            sheet = match['sheet']
            target = sheet.strip("'").replace("''", "'") if sheet else owner
            if target != changed:
                return match[0]
            prefix = sheet + "!" if sheet else ""
            return prefix + shift_reference(match['first'] + (match['last'] or ""), at)
        return REFERENCE.sub(replace, chunk)
    chunks = re.split(r'("(?:[^"]|"")*"|(?:\[[^\]]+\])[^!\s]+!\$?[A-Z]{1,3}\$?[0-9]+|\[[^\]]*\])', text)
    return ''.join(piece if piece.startswith(('"','[')) else expression(piece) for piece in chunks)


def insert_column(parts, paths, changed, at):
    """Shift cells, merges, formulas, links, drawings, tables and scoped names together."""
    path = paths[changed]
    root = ET.fromstring(parts[path])
    old_filter = root.find(tag('autoFilter'))
    filter_start = col_index(old_filter.get('ref').split(':')[0]) if old_filter is not None else 1
    for cell in root.iter(tag('c')):
        cell.set('r', shift_reference(cell.get('r'), at))
    for element in root.iter():
        for attr in ('ref', 'sqref', 'activeCell', 'topLeftCell'):
            if attr in element.attrib:
                element.set(attr, shift_reference(element.get(attr), at))
        if element.tag == tag('pane') and element.get('state') in ('frozen','frozenSplit'):
            split = int(float(element.get('xSplit','0')))
            if split >= at:
                element.set('xSplit', str(split + 1))
        if element.tag == tag('filterColumn') and int(element.get('colId')) + filter_start >= at:
            element.set('colId', str(int(element.get('colId')) + 1))
        if element.tag == tag('row') and element.get('spans'):
            start, end = map(int, element.get('spans').split(':'))
            element.set('spans', f"{start + (start >= at)}:{end + (end >= at)}")
    cols = root.find(tag('cols'))
    if cols is not None:
        for col in list(cols):
            low, high = int(col.get('min')), int(col.get('max'))
            if low >= at:
                col.set('min',str(low+1))
                col.set('max',str(high+1))
            elif high >= at:
                right = copy.deepcopy(col)
                col.set('max',str(at-1))
                right.set('min',str(at+1))
                right.set('max',str(high+1))
                cols.insert(list(cols).index(col)+1,right)
    parts[path] = encode(root, parts[path])
    related_owners = {}
    for rel in relationships(parts,path):
        target = relation_target(path,rel.get('Target',''))
        if rel.get('TargetMode') == 'External' or target not in parts:
            continue
        kind = rel.get('Type','').rsplit('/',1)[-1]
        if kind == 'table':
            related_owners[target] = changed
        if kind not in ('drawing','comments','vmlDrawing','table'):
            continue
        document = ET.fromstring(parts[target])
        if kind == 'drawing':
            for marker in document.iter():
                if marker.tag in (f'{{{XDR}}}from',f'{{{XDR}}}to'):
                    col = marker.find(f'{{{XDR}}}col')
                    if col is not None and int(col.text) >= at-1:
                        col.text = str(int(col.text)+1)
        elif kind == 'vmlDrawing':
            for node in document.iter():
                if node.tag.endswith('}Column') and int(node.text) >= at-1:
                    node.text = str(int(node.text)+1)
                if node.tag.endswith('}Anchor'):
                    coords = [int(n.strip()) for n in node.text.split(',')]
                    for index in (0,4):
                        if coords[index] >= at-1:
                            coords[index] += 1
                    node.text = ', '.join(map(str,coords))
        else:
            old_range = document.get('ref','')
            for node in document.iter():
                if node.get('ref'):
                    node.set('ref',shift_reference(node.get('ref'),at))
            if kind == 'table' and old_range:
                begin,end = old_range.split(':')
                if col_index(begin) < at <= col_index(end):
                    columns = document.find(tag('tableColumns'))
                    index = at-col_index(begin)
                    new_id = max(int(c.get('id')) for c in columns)+1
                    columns.insert(index,ET.Element(tag('tableColumn'),{'id':str(new_id),'name':'评论截图'}))
                    columns.set('count',str(len(columns)))
                    for criterion in document.iter(tag('filterColumn')):
                        if int(criterion.get('colId')) >= index:
                            criterion.set('colId',str(int(criterion.get('colId'))+1))
        parts[target] = encode(document,parts[target])
    owners = {p:name for name,p in paths.items()}
    owners.update(related_owners)
    workbook = ET.fromstring(parts['xl/workbook.xml'])
    names = [s.get('name') for s in workbook.find(tag('sheets'))]
    for member, binary in list(parts.items()):
        if not member.endswith('.xml') or member.startswith(('xl/media/','docProps/')):
            continue
        if not any(marker in binary for marker in (b'<f',b':f',b'formula',b'definedName',b'location=')):
            continue
        document = ET.fromstring(binary)
        owner = owners.get(member)
        touched = False
        for node in document.iter():
            local = node.tag.rsplit('}',1)[-1]
            if local in ('f','formula','formula1','formula2','definedName','calculatedColumnFormula','totalsRowFormula') and node.text:
                context = owner
                if local == 'definedName' and node.get('localSheetId') is not None:
                    context = names[int(node.get('localSheetId'))]
                new = shift_formula(node.text,context,changed,at)
                touched |= new != node.text
                node.text = new
            if local == 'hyperlink' and node.get('location'):
                old = node.get('location')
                node.set('location',shift_formula(old,owner,changed,at))
                touched |= old != node.get('location')
        if touched:
            parts[member] = encode(document,binary)


def set_column_width(root, number, width):
    cols = root.find(tag('cols'))
    if cols is None:
        cols = ET.Element(tag('cols'))
        root.insert(list(root).index(root.find(tag('sheetData'))),cols)
    for col in list(cols):
        low,high = int(col.get('min')),int(col.get('max'))
        if low <= number <= high:
            cols.remove(col)
            for start,end in ((low,number-1),(number+1,high)):
                if start <= end:
                    segment = copy.deepcopy(col)
                    segment.set('min',str(start))
                    segment.set('max',str(end))
                    cols.append(segment)
    ET.SubElement(cols,tag('col'),{'min':str(number),'max':str(number),'width':str(width),'customWidth':'1'})
    cols[:] = sorted(cols,key=lambda c:int(c.get('min')))
