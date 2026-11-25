"""Save original browser PNGs and verify portable, note-scoped finance evidence."""
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

DEFAULT_SCREENSHOT_DIR = "评论截图"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
KINDS = {"comment", "comments", "empty", "end", "unavailable", "panel"}


def _note_prefix(platform, key):
    digest = hashlib.sha256(f"{platform}\0{key}".encode()).hexdigest()
    return f"{platform}-{digest[:16]}-"


def _safe_path(value):
    if not isinstance(value, str) or "\\" in value or ":" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) != 3 or ".." in path.parts or path.suffix != ".png":
        return None
    return path


def _is_png(data):
    return (len(data) >= 57 and data.startswith(PNG_SIGNATURE + b"\x00\x00\x00\rIHDR")
            and int.from_bytes(data[16:20], "big") > 0 and int.from_bytes(data[20:24], "big") > 0
            and data.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82"))


def image_bytes(record, directory, platform, key):
    """Read only the expected note's PNG beneath the configured evidence root."""
    if not isinstance(record, dict) or platform not in ("xiaohongshu", "douyin"):
        return None
    path = _safe_path(record.get("path"))
    directory = Path(directory)
    if (not path or path.parts[0] != directory.name or record.get("kind") not in KINDS
            or not path.parts[1].startswith(_note_prefix(platform, key))
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))):
        return None
    try:
        root = directory.resolve()
        candidate = (root / path.parts[1] / path.name).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        data = candidate.read_bytes()
        if _is_png(data) and hashlib.sha256(data).hexdigest() == record["sha256"]:
            return data
    except (OSError, RuntimeError):
        pass
    return None


def screenshots_valid(observation, directory):
    if not observation or observation.get("screenshot_status") != "captured":
        return False
    records = observation.get("comment_screenshots")
    return bool(isinstance(records, list) and records and all(
        image_bytes(r, directory, observation.get("platform"), observation.get("target_key")) is not None for r in records))


class NoteScreenshots:
    def __init__(self, directory, platform, target_key):
        if platform not in ("xiaohongshu", "douyin"):
            raise ValueError("unknown screenshot platform")
        self.directory = Path(directory).resolve()
        self.folder = self.directory / (_note_prefix(platform, target_key) + uuid4().hex[:16])
        self.records = []
        self.failed = False
        self._owned = {}
        self._sequence = 0

    def save(self, locator, kind, comment_ids=()):
        if kind not in KINDS:
            raise ValueError("unknown screenshot kind")
        if isinstance(comment_ids, (str, int)):
            comment_ids = (comment_ids,)
        self._sequence += 1
        file = self.folder / f"{self._sequence:04}-{kind}.png"
        temp = file.with_suffix(".tmp")
        try:
            data = locator.screenshot(type="png", timeout=4500, animations="disabled")
            if not _is_png(data):
                raise ValueError("browser did not return a PNG")
            self.folder.mkdir(parents=True, exist_ok=True)
            with temp.open("xb") as stream:
                stream.write(data)
            os.replace(temp, file)
        except Exception:
            self.failed = True
            return None
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                self.failed = True
        record = {"path": file.relative_to(self.directory.parent).as_posix(),
                  "kind": kind, "sha256": hashlib.sha256(data).hexdigest(),
                  "captured_at": datetime.now(timezone.utc).isoformat(),
                  "comment_ids": [str(cid) for cid in (comment_ids or ()) if cid not in (None, "")]}
        self._owned[id(record)] = file
        self.records.append(record)
        return record

    def discard(self, record):
        file = self._owned.pop(id(record), None)
        if file is not None:
            file.unlink(missing_ok=True)
            self.records.remove(record)

    def discard_all(self):
        for record in self.records.copy():
            self.discard(record)


def materialize_screenshot(record, source_base, output_base, platform, key):
    path = _safe_path(record.get("path")) if isinstance(record, dict) else None
    if not path:
        return None
    src = Path(source_base) / path.parts[0]
    if not src.resolve().is_relative_to(Path(source_base).resolve()):
        return None
    data = image_bytes(record, src, platform, key)
    if data is None:
        return None
    root = Path(output_base).resolve()
    dest = (root / path).resolve()
    if not dest.is_relative_to(root):
        return None
    if dest != (src / path.parts[1] / path.name).resolve():
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if dest.read_bytes() != data:
                raise ValueError("screenshot destination contains different data")
        else:
            temp = dest.parent / (uuid4().hex + ".tmp")
            try:
                temp.write_bytes(data)
                os.replace(temp, dest)
            finally:
                temp.unlink(missing_ok=True)
    return dict(record)


def attach_screenshots(report, observations, source_base, *, enabled=False):
    """Map this visit's finance evidence to reviewed rows. Historic carried rows keep their prior result."""
    if not enabled and not any("screenshot_status" in o or "comment_screenshots" in o for o in observations):
        return
    grouped = {}
    for obs in observations:
        key = (obs.get("platform"), obs.get("target_key"))
        grouped.setdefault(key, []).append(obs)
    cache = {}
    rows_with_images = 0
    for row in report["results"]:
        if row.get("review_action") == "carried_forward":
            continue
        key = row["platform"], row["target_key"]
        visit = (row.get("capture") or {}).get("captured_at")
        unavailable = row.get("result_category") == "原帖不可访问"
        cache_key = (*key, visit, unavailable)
        if cache_key not in cache:
            shots = []
            statuses = []
            seen = set()
            for obs in grouped.get(key, []):
                if visit and obs.get("captured_at") != visit:
                    continue
                if unavailable and (obs.get("capture_status") != "inaccessible" or obs.get("target_page_state") != "post_unavailable"
                                    or obs.get("resolved_target_key") not in (None, "", key[1])
                                    or (not visit and len(grouped[key]) != 1)):
                    continue
                statuses.append(obs.get("screenshot_status", "missing"))
                for original in obs.get("comment_screenshots") or ():
                    if unavailable and (not isinstance(original, dict) or original.get("kind") != "unavailable"):
                        continue
                    checked = materialize_screenshot(original, source_base, source_base, *key)
                    if checked is None:
                        statuses.append("partial")
                    elif checked["path"] not in seen:
                        seen.add(checked["path"])
                        shots.append(checked)
            status = ("missing" if not shots else "captured" if all(s == "captured" for s in statuses) else "partial")
            cache[cache_key] = shots, status
        shots, status = cache[cache_key]
        matched = row.get("matched_comment") or {}
        target_id = str(matched.get("comment_id") or "")
        text = matched.get("text")
        exact = [s for s in shots if (target_id and target_id in s.get("comment_ids", ()))
                 or (not target_id and text and text in s.get("comment_texts", ()))
                 or s["path"] == (row.get("capture") or {}).get("evidence_path")]
        if matched and not exact and status == "captured":
            status = "partial"
        row["comment_screenshots"] = exact or shots
        row["screenshot_status"] = status
        rows_with_images += bool(shots)
    review_rows = [r for r in report["results"] if r.get("review_action") != "carried_forward"]
    report["summary"]["screenshot_evidence"] = {
        "reviewed_rows": len(review_rows), "rows_with_images": rows_with_images,
        "rows_with_incomplete_images": sum(r["screenshot_status"] != "captured" for r in review_rows),
        "notes_with_images": len({(r["platform"], r["target_key"]) for r in review_rows if r["comment_screenshots"]})}
