import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from services.materials import (
    MaterialError,
    approved_material_paths,
    paper_path,
    read_pdf_upload,
    read_testdata_upload,
    save_paper,
    save_testdata_archive,
    sha256_file,
    testdata_archive_path,
)


class MaterialsTests(unittest.TestCase):
    def test_approved_generated_revision_is_consumed_without_legacy_copy(self):
        tid = "f" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "artifacts" / tid / "ai-r1"
            (generated / "student").mkdir(parents=True)
            paper = generated / "student" / "paper.pdf"
            data = generated / "student" / "testdata.tar.gz"
            paper.write_bytes(b"%PDF-1.7\nfixture")
            data.write_bytes(b"archive")
            resolved = approved_material_paths(
                materials_root=root / "materials",
                artifact_root=root / "artifacts",
                contest={
                    "tid": tid,
                    "active_material_revision": "ai-r1",
                    "testdata_sha256": "1" * 64,
                },
                artifact={
                    "revision": "ai-r1",
                    "state": "approved",
                    "root_path": str(generated),
                },
            )
            self.assertEqual(resolved, (paper.resolve(), data.resolve()))
            self.assertFalse((root / "materials" / tid).exists())

    def test_approved_revision_path_cannot_escape_artifact_root(self):
        tid = "e" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(MaterialError, "越过"):
                approved_material_paths(
                    materials_root=root / "materials",
                    artifact_root=root / "artifacts",
                    contest={
                        "tid": tid,
                        "active_material_revision": "ai-r1",
                        "testdata_sha256": "",
                    },
                    artifact={
                        "revision": "ai-r1",
                        "state": "approved",
                        "root_path": str(root / "outside" / "ai-r1"),
                    },
                )

    def test_upload_is_validated_saved_and_hashed(self):
        tid = "a" * 24
        payload = b"%PDF-1.7\nminimal-test-pdf"
        name, data, digest = read_pdf_upload(
            io.BytesIO(payload), "C:\\fakepath\\two-problems.pdf", 1024
        )
        with tempfile.TemporaryDirectory() as directory:
            path = save_paper(directory, tid, data)
            self.assertEqual(path, paper_path(directory, tid))
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(sha256_file(path), digest)
        self.assertEqual(name, "two-problems.pdf")

    def test_non_pdf_is_rejected(self):
        with self.assertRaisesRegex(MaterialError, "不是有效"):
            read_pdf_upload(io.BytesIO(b"not a pdf"), "paper.pdf", 1024)

    def test_oversized_pdf_is_rejected(self):
        with self.assertRaisesRegex(MaterialError, "超过"):
            read_pdf_upload(io.BytesIO(b"%PDF-1.7\n12345"), "paper.pdf", 8)

    def test_tid_cannot_escape_materials_directory(self):
        with self.assertRaisesRegex(MaterialError, "24 位"):
            paper_path(Path("/materials"), "../outside")

    @staticmethod
    def _zip(files: dict[str, bytes]) -> io.BytesIO:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in files.items():
                archive.writestr(name, payload)
        output.seek(0)
        return output

    def test_testdata_zip_is_validated_normalized_and_saved(self):
        stream = self._zip(
            {
                "practice/apple/1.in": b"1 2\n",
                "practice/apple/1.out": b"3\n",
                "practice/apple/2.in": b"3 4\n",
                "practice/apple/2.out": b"7\n",
                "practice/banana/1.in": b"4\n",
                "practice/banana/1.out": b"8\n",
                "practice/banana/2.in": b"5\n",
                "practice/banana/2.out": b"10\n",
            }
        )
        name, payload, digest, count, expanded = read_testdata_upload(
            stream, "自测数据.zip", 1024 * 1024, 1024 * 1024, 20, ["apple", "banana"]
        )
        self.assertEqual(name, "自测数据.zip")
        self.assertEqual(count, 8)
        self.assertEqual(expanded, 21)
        self.assertEqual(payload[4:8], b"\0\0\0\0")
        repeated = read_testdata_upload(
            self._zip(
                {
                    "practice/apple/1.in": b"1 2\n",
                    "practice/apple/1.out": b"3\n",
                    "practice/apple/2.in": b"3 4\n",
                    "practice/apple/2.out": b"7\n",
                    "practice/banana/1.in": b"4\n",
                    "practice/banana/1.out": b"8\n",
                    "practice/banana/2.in": b"5\n",
                    "practice/banana/2.out": b"10\n",
                }
            ),
            "自测数据.zip",
            1024 * 1024,
            1024 * 1024,
            20,
            ["apple", "banana"],
        )
        self.assertEqual(repeated[1], payload)
        self.assertEqual(repeated[2], digest)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            self.assertEqual(archive.extractfile("apple/1.in").read(), b"1 2\n")
            self.assertEqual(archive.getmember("banana/1.out").mode, 0o444)
        with tempfile.TemporaryDirectory() as directory:
            path = save_testdata_archive(directory, "b" * 24, payload)
            self.assertEqual(path, testdata_archive_path(directory, "b" * 24))
            self.assertEqual(sha256_file(path), digest)

    def test_testdata_zip_rejects_traversal(self):
        stream = self._zip({"../apple/1.in": b"1\n"})
        with self.assertRaisesRegex(MaterialError, "非法路径"):
            read_testdata_upload(
                stream, "data.zip", 1024, 1024, 10, ["apple"]
            )

    def test_testdata_zip_accepts_windows_paths_and_numbered_problem_folders(self):
        stream = self._zip(
            {
                r"26模拟赛-学生测试数据\T1_books\1.in": b"1\n",
                r"26模拟赛-学生测试数据\T1_books\1.out": b"2\n",
                r"26模拟赛-学生测试数据\T1_books\2.in": b"2\n",
                r"26模拟赛-学生测试数据\T1_books\2.out": b"3\n",
                r"26模拟赛-学生测试数据\T2_study\1.in": b"3\n",
                r"26模拟赛-学生测试数据\T2_study\1.out": b"4\n",
                r"26模拟赛-学生测试数据\T2_study\2.in": b"4\n",
                r"26模拟赛-学生测试数据\T2_study\2.out": b"5\n",
                r"26模拟赛-学生测试数据\T3_board\1.in": b"4\n",
                r"26模拟赛-学生测试数据\T3_board\1.out": b"5\n",
                r"26模拟赛-学生测试数据\T3_board\2.in": b"5\n",
                r"26模拟赛-学生测试数据\T3_board\2.out": b"6\n",
                r"26模拟赛-学生测试数据\T4_wall\1.in": b"5\n",
                r"26模拟赛-学生测试数据\T4_wall\1.out": b"6\n",
                r"26模拟赛-学生测试数据\T4_wall\2.in": b"6\n",
                r"26模拟赛-学生测试数据\T4_wall\2.out": b"7\n",
            }
        )
        _, payload, _, count, _ = read_testdata_upload(
            stream,
            "学生测试数据.zip",
            1024 * 1024,
            1024 * 1024,
            20,
            ["books", "study", "board", "wall"],
        )
        self.assertEqual(count, 16)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            self.assertEqual(archive.extractfile("books/1.in").read(), b"1\n")
            self.assertEqual(archive.extractfile("study/1.in").read(), b"3\n")
            self.assertEqual(archive.extractfile("board/1.in").read(), b"4\n")
            self.assertEqual(archive.extractfile("wall/1.in").read(), b"5\n")

    def test_testdata_zip_requires_paired_input_and_output(self):
        stream = self._zip(
            {
                "apple/1.in": b"1\n",
                "apple/1.out": b"1\n",
                "apple/2.in": b"2\n",
                "apple/2.out": b"2\n",
                "banana/1.in": b"3\n",
                "banana/2.in": b"4\n",
                "banana/2.out": b"4\n",
            }
        )
        with self.assertRaisesRegex(MaterialError, "没有成对"):
            read_testdata_upload(
                stream, "data.zip", 1024, 1024, 10, ["apple", "banana"]
            )

    def test_testdata_zip_enforces_selected_group_count(self):
        stream = self._zip(
            {
                "apple/1.in": b"1\n",
                "apple/1.out": b"1\n",
                "apple/2.in": b"2\n",
                "apple/2.out": b"2\n",
            }
        )
        with self.assertRaisesRegex(MaterialError, "恰好包含 3 组"):
            read_testdata_upload(
                stream, "data.zip", 1024, 1024, 10, ["apple"], 3
            )


if __name__ == "__main__":
    unittest.main()
