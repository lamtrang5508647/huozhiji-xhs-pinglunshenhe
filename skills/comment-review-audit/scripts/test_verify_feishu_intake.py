import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).with_name("verify_feishu_intake.py")
SPEC = importlib.util.spec_from_file_location("verify_feishu_intake", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class VerifyFeishuIntakeTests(unittest.TestCase):
    def test_unset_config_uses_openclaw_default(self):
        self.assertEqual(MODULE.parse_media_max_mb(""), 30)

    def test_default_limit_is_rejected_for_image_heavy_workbooks(self):
        with self.assertRaisesRegex(ValueError, "at least 100 MB"):
            MODULE.validate_media_max_mb(30)

    def test_configured_large_file_limit_is_accepted(self):
        MODULE.validate_media_max_mb(100)

    def test_invalid_config_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid"):
            MODULE.parse_media_max_mb("many")


if __name__ == "__main__":
    unittest.main()
