import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "noi-linux-official/rootfs/usr/local/bin/patch-geany-line-height.py"
spec = importlib.util.spec_from_file_location("geany_line_height", SCRIPT)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


class GeanyLineHeightTests(unittest.TestCase):
    def test_only_extra_descent_changes(self):
        original = "# official font unchanged\n[styling]\ndefault=default\nline_height=0;0;\n[settings]\nwordchars=_abc\n"
        self.assertEqual(patcher.patched_config(original), original.replace("line_height=0;0;", "line_height=0;2;"))

    def test_repeat_is_idempotent(self):
        original = "[styling]\nline_height=0;2;\n"
        self.assertEqual(patcher.patched_config(original), original)

    def test_preserves_whitespace_and_line_endings(self):
        original = "[styling]\r\n line_height = 0;0; \r\n"
        self.assertEqual(patcher.patched_config(original), original.replace("0;0;", "0;2;"))

    def test_preserves_no_final_newline(self):
        self.assertEqual(patcher.patched_config("[styling]\nline_height=0;0;"), "[styling]\nline_height=0;2;")

    def test_ignores_comments_and_other_sections(self):
        original = "[settings]\nline_height=7;7;\n[styling]\n# line_height=0;0;\nline_height=0;0;\n"
        expected = original.rsplit("line_height=0;0;", 1)
        self.assertEqual(patcher.patched_config(original), "line_height=0;2;".join(expected))

    def test_rejects_missing_duplicate_and_unknown_defaults(self):
        for original in ["", "[settings]\nline_height=0;0;\n", "[styling]\nline_height=0;0;\nline_height=0;0;\n", "[styling]\nline_height=1;3;\n"]:
            with self.subTest(original=original), self.assertRaises(ValueError):
                patcher.patched_config(original)

    def test_official_build_applies_patch(self):
        dockerfile = (ROOT / "noi-linux-official/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("RUN python3 /usr/local/bin/patch-geany-line-height.py", dockerfile)

    def test_image_verification_checks_system_default_without_user_override(self):
        verify = (ROOT / "deploy/verify-contest-image-local.sh").read_text(encoding="utf-8")
        self.assertIn('config.get("styling", "line_height") == "0;2;"', verify)
        self.assertIn("unexpected baked student style override", verify)
        self.assertLess(verify.index("Geany underscore clipping default is not fixed"), verify.index('seat_root="${test_root}/v1"'))


if __name__ == "__main__":
    unittest.main()
