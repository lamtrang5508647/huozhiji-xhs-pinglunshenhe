"""Recover the assigned post's original share link only when writing the private finance workbook."""
import re
from collections import defaultdict
from urllib.parse import urlsplit
from zipfile import ZipFile

if __package__:
    from .table_to_expected import douyin_video_id, extract_url, read_worksheet, resolve_mapping, xlsx_shared_strings, xlsx_sheet_members
else:
    from table_to_expected import douyin_video_id, extract_url, read_worksheet, resolve_mapping, xlsx_shared_strings, xlsx_sheet_members


def checked_note_url(value, platform, key):
    link = extract_url(value)
    if not link or any(ch in link for ch in ('\\', '"', '<', '>', '\x00')):
        return ""
    try:
        origin = urlsplit(link)
        host = (origin.hostname or "").lower()
        if (origin.scheme not in ("https", "http") or origin.username is not None or origin.password is not None
                or origin.port not in (None, 443 if origin.scheme == "https" else 80)):
            return ""
        if platform == "xiaohongshu" and host in ("www.xiaohongshu.com", "xiaohongshu.com", "www.xhslink.com", "xhslink.com", "xhslink.cn", "www.xhslink.cn"):
            pattern = (r"/(?:explore|discovery/item|user/profile/[a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)/?"
                       if host.endswith("xiaohongshu.com") else r"/(?:[a-zA-Z0-9_-]+/)*([a-zA-Z0-9_-]+)/?")
            hit = re.fullmatch(pattern, origin.path)
            identity = hit[1] if hit else None
        elif platform == "douyin" and host in ("www.douyin.com", "douyin.com", "www.iesdouyin.com", "v.douyin.com"):
            if host == "v.douyin.com":
                hit = re.fullmatch(r"/([a-zA-Z0-9_-]+)/?", origin.path)
                identity = hit[1] if hit else None
            elif host == "www.iesdouyin.com":
                hit = re.fullmatch(r"/share/(?:video|note)/(\d+)/?", origin.path)
                identity = hit[1] if hit else None
            elif re.fullmatch(r"/(?:share/)?(?:video|note|slides)/\d+/?|/|/(?:user|search)/[^/]+/?|/(?:jingxuan|recommend|discover)/?", origin.path or "/"):
                identity = douyin_video_id(link)
            else:
                identity = None
        else:
            return ""
        return link if identity and identity == str(key) else ""
    except (ValueError, UnicodeError):
        return ""


def with_finance_note_urls(source, rows):
    enriched = [dict(r) for r in rows]
    sheets = defaultdict(dict)
    for row in enriched:
        sheets[row.get("source_sheet")][row.get("source_row")] = row
    with ZipFile(source) as archive:
        names = xlsx_sheet_members(archive)
        strings = xlsx_shared_strings(archive)
        for sheet, tasks in sheets.items():
            if sheet not in names:
                continue
            raw_rows = read_worksheet(archive, names[sheet], strings, all_link_columns=True)
            header = resolve_mapping(raw_rows[0] if raw_rows else (), {}).get("target_url")
            previous = {}
            last_column_values = {}
            for n, raw in enumerate(raw_rows, 2):
                for col, val in raw.items():
                    if "http" in str(val).lower():
                        last_column_values[col] = val
                if n not in tasks:
                    continue
                row = tasks[n]
                identity = row.get("platform"), row.get("target_key")
                candidates = [raw.get(header), *raw.values(), last_column_values.get(header), previous.get(identity),
                              *last_column_values.values(), row.get("target_url")]
                link = next((u for v in candidates if v and (u := checked_note_url(v, *identity))), "")
                if link:
                    previous[identity] = link
                    row["finance_note_url"] = link
    return enriched
