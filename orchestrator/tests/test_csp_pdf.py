import shutil
from pathlib import Path
import tempfile
import unittest

from services.csp_pdf import (
    MarkdownDocument,
    PdfBuildError,
    inspect_pdf,
    render_csp_pdf,
    render_pdf_pages,
)


class CSPPdfTests(unittest.TestCase):
    def _documents(self):
        return [
            MarkdownDocument(
                slug="apple",
                title="Apple Sum",
                markdown=(
                    "# Apple Sum (apple)\n\n"
                    "## Description\n\nCompute the sum.\n\n"
                    "## Input\n\nRead `apple.in`.\n\n"
                    "## Sample\n\n```text\n3\n1 2 3\n```\n\n"
                    "| Group | Limit |\n|---|---|\n| 1 | 10 |\n"
                ),
                input_filename="apple.in",
                output_filename="apple.out",
                time_limit_ms=1000,
                memory_limit_mb=256,
            )
        ]

    def test_valid_pdf_is_generated_and_text_is_extractable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.pdf"
            result = render_csp_pdf(
                "CSP-J Fixture Contest", "NOI Linux", self._documents(), path
            )
            self.assertGreaterEqual(result.page_count, 2)
            self.assertGreater(result.byte_size, 1000)
            payload = path.read_bytes()
            self.assertTrue(payload.startswith(b"%PDF-"))
            # The CID fallback renders Chinese, while Latin identifiers must
            # use proportional/monospace Latin fonts instead of CJK full-width
            # glyph metrics.
            self.assertIn(b"/Helvetica", payload)
            self.assertIn(b"/Courier", payload)
            self.assertIn("CSP-J Fixture Contest", result.extracted_text)
            self.assertIn("apple.in", result.extracted_text)
            checked = inspect_pdf(
                path, required_text=("CSP-J Fixture Contest",), minimum_pages=2
            )
            self.assertEqual(checked.page_count, result.page_count)

    def test_valid_pdf_fixture_can_be_rendered_to_png(self):
        executable = shutil.which("pdftoppm")
        if not executable:
            self.skipTest("Poppler is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "paper.pdf"
            result = render_csp_pdf(
                "CSP-J Render Fixture", "NOI Linux", self._documents(), path
            )
            try:
                pages = render_pdf_pages(
                    result.path, root / "rendered", dpi=90, executable=executable
                )
            except PdfBuildError as exc:
                if "system cannot find the path" in str(exc).lower():
                    self.skipTest("Poppler wrapper is unavailable")
                raise
            self.assertEqual(len(pages), result.page_count)
            self.assertTrue(all(page.read_bytes().startswith(b"\x89PNG") for page in pages))

    def test_default_cid_mode_preserves_chinese_and_compact_latin_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed.pdf"
            document = MarkdownDocument(
                slug="apple",
                title="苹果 Apple Sum",
                markdown=(
                    "# 苹果 Apple Sum（apple）\n\n"
                    "## 文件读写 File I/O\n\n"
                    "程序从 `apple.in` 读入，并写入 `apple.out`。\n"
                ),
                input_filename="apple.in",
                output_filename="apple.out",
                time_limit_ms=1000,
                memory_limit_mb=256,
            )
            result = render_csp_pdf(
                "CSP-J 2026 模拟赛", "NOI Linux 复赛环境", [document], path
            )
            for text in (
                "CSP-J 2026 模拟赛",
                "苹果 Apple Sum",
                "apple.in",
                "apple.out",
            ):
                self.assertIn(text, result.extracted_text)
            payload = path.read_bytes()
            self.assertIn(b"/Helvetica-Bold", payload)
            self.assertIn(b"/Courier", payload)

    def test_long_wrapped_contest_title_passes_content_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrapped-title.pdf"
            title = (
                "NOI Linux V1 Alpha 最终双题验收 "
                "2026-08-25T17:25:10.616Z"
            )
            result = render_csp_pdf(title, "NOI Linux", self._documents(), path)
            self.assertGreaterEqual(result.page_count, 2)
            checked = inspect_pdf(path, required_text=(title,), minimum_pages=2)
            self.assertEqual(checked.page_count, result.page_count)

    def test_unclosed_code_block_is_rejected_without_partial_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.pdf"
            broken = [
                MarkdownDocument(
                    slug="bad",
                    title="Bad",
                    markdown="# Bad\n\n```text\nnot closed",
                    input_filename="bad.in",
                    output_filename="bad.out",
                    time_limit_ms=1000,
                    memory_limit_mb=256,
                )
            ]
            with self.assertRaisesRegex(PdfBuildError, "代码块"):
                render_csp_pdf("Broken Fixture", "NOI Linux", broken, path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
