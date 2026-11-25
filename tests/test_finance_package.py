import base64
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from zipfile import ZipFile

from huozhiji_audit import audit_workbook
from huozhiji_audit._engine.note_links import checked_note_url
from huozhiji_audit._engine.screenshot_evidence import NoteScreenshots, image_bytes, screenshots_valid
from test_portable_api import fixture

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aAL8AAAAASUVORK5CYII=")


class FinanceWorkbookTests(unittest.TestCase):
    def test_post_link_preserves_the_assigned_share_and_rejects_other_targets(self):
        xhs = "https://www.xiaohongshu.com/explore/example?xsec_token=fixture"
        self.assertEqual(checked_note_url(xhs, "xiaohongshu", "example"), xhs)
        for value in (xhs.replace("example?", "other?"), xhs.replace(".com/", ".com.invalid/"),
                      xhs.replace("www.", "user:pass@www."), xhs.replace("https://", "file://")):
            self.assertEqual(checked_note_url(value, "xiaohongshu", "example"), "")
        dy = "https://www.douyin.com/discover?modal_id=123"
        self.assertEqual(checked_note_url(dy,"douyin","123"), dy)
        self.assertEqual(checked_note_url(dy,"douyin","456"), "")
        short = "https://xhslink.com/o/sampleCode"
        self.assertEqual(checked_note_url(short,"xiaohongshu","sampleCode"), short)
        self.assertEqual(checked_note_url(short,"xiaohongshu","anotherCode"), "")

    def test_embedded_image_in_standalone_workbook_and_corrupt_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkout, output = root / "取证", root / "财务"
            checkout.mkdir()
            output.mkdir()
            source = checkout / "source.xlsx"
            observations = fixture(source)
            obs = observations["observations"][0]
            writer = NoteScreenshots(checkout / "评论截图", obs["platform"], obs["target_key"])
            card = obs["comments"][0]
            card["comment_id"] = "sample-comment"
            shot = writer.save(Mock(screenshot=Mock(return_value=PNG)), "comment", [card["comment_id"]])
            card["evidence_path"] = shot["path"]
            obs.update(comment_screenshots=[shot], screenshot_status="captured")
            self.assertTrue(screenshots_valid(obs, writer.directory))
            self.assertIsNone(image_bytes(shot, writer.directory, "douyin", obs["target_key"]))
            self.assertIsNone(image_bytes(dict(shot, path="../" + shot["path"]), writer.directory, "xiaohongshu", obs["target_key"]))
            file = output / "审核结果.xlsx"
            report = audit_workbook(source, observations, file, evidence_base=checkout)
            self.assertEqual(report["summary"]["screenshot_evidence"]["notes_with_images"], 1)
            self.assertEqual(list(output.iterdir()), [file])
            with ZipFile(file) as book:
                images = [sha256(book.read(n)).hexdigest() for n in book.namelist() if n.startswith("xl/media/")]
                self.assertIn(shot["sha256"], images)
            image = checkout / shot["path"]
            image.write_bytes(b"broken")
            self.assertFalse(screenshots_valid(obs, writer.directory))
            report = audit_workbook(source, observations, output / "second.xlsx", evidence_base=checkout)
            self.assertEqual(report["summary"]["screenshot_evidence"]["notes_with_images"], 0)
            self.assertEqual(report["results"][0]["screenshot_status"], "missing")
