import tempfile
from pathlib import Path
import unittest

from services.static_check import (
    check_answer_tree,
    check_code,
    check_submission,
    strip_cpp_comments,
)


class StaticCheckTests(unittest.TestCase):
    def test_valid_freopen(self):
        code = 'freopen("apple.in","r",stdin); freopen("apple.out","w",stdout);'
        self.assertEqual(check_code(code, "apple"), [])

    def test_commented_freopen_does_not_pass(self):
        code = '// freopen("apple.in","r",stdin);\n/* freopen("apple.out","w",stdout); */'
        self.assertIn("未检测到完整的 freopen", check_code(code, "apple")[0])

    def test_comment_markers_inside_string_are_preserved(self):
        source = 'const char* s = "//not a comment"; // real\nint x;'
        stripped = strip_cpp_comments(source)
        self.assertIn('"//not a comment"', stripped)
        self.assertNotIn("real", stripped)

    def test_case_mismatch_is_invalid_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "Apple.cpp").write_text("int main(){}", encoding="utf-8")
            report = check_submission(directory, ["apple"])
            self.assertEqual(report["apple"]["status"], "invalid_filename")

    def test_official_answer_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "BJ-001" / "apple" / "apple.cpp"
            source.parent.mkdir(parents=True)
            source.write_text(
                '#include <cstdio>\nint main(){freopen("apple.in","r",stdin);'
                'freopen("apple.out","w",stdout);}\n',
                encoding="utf-8",
            )
            report = check_answer_tree(directory, "BJ-001", ["apple"])
            self.assertEqual(report["apple"]["status"], "ok")
            self.assertEqual(report["apple"]["file"], "BJ-001/apple/apple.cpp")

    def test_official_answer_tree_ignores_local_compile_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            problem = Path(directory) / "BJ-001" / "apple"
            problem.mkdir(parents=True)
            (problem / "apple.cpp").write_text(
                '#include <cstdio>\nint main(){freopen("apple.in","r",stdin);'
                'freopen("apple.out","w",stdout);}\n',
                encoding="utf-8",
            )
            (problem / "apple.bin").write_bytes(b"local binary")
            (problem / "apple.in").write_text("1 2\n", encoding="utf-8")
            (problem / "apple.out").write_text("3\n", encoding="utf-8")
            (problem / "build").mkdir()
            report = check_answer_tree(directory, "BJ-001", ["apple"])
            self.assertEqual(report["apple"]["status"], "ok")
            self.assertEqual(report["apple"]["issues"], [])


if __name__ == "__main__":
    unittest.main()
