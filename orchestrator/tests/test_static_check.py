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

    def test_comments_do_not_determine_a_score(self):
        code = '// freopen("apple.in","r",stdin);\n/* freopen("apple.out","w",stdout); */'
        self.assertEqual(check_code(code, "apple"), [])

    def test_legal_file_io_styles_are_not_rejected(self):
        for code in (
            'std::ifstream in("apple.in"); std::ofstream out("apple.out");',
            '#define TASK "apple"\nfreopen(TASK ".in","r",stdin); freopen(TASK ".out","w",stdout);',
            'FILE* in=fopen("apple.in","r"); FILE* out=fopen("apple.out","w");',
        ):
            with self.subTest(code=code):
                self.assertEqual(check_code(code, "apple"), [])

    def test_literal_mismatch_is_only_an_advisory(self):
        warnings = check_code('freopen("other.in","r",stdin);', "apple")
        self.assertTrue(warnings)
        self.assertIn("以 OJ 评测为准", warnings[0])
        self.assertNotIn("0 分", warnings[0])

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
