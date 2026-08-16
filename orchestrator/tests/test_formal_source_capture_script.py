import importlib.util
import base64
import hashlib
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "noi-linux-official"
    / "rootfs"
    / "usr"
    / "local"
    / "bin"
    / "capture-formal-source.py"
)


class FormalSourceCaptureScriptTests(unittest.TestCase):
    def test_helper_contains_linux_descriptor_safety_contract(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for required in (
            "os.O_NOFOLLOW",
            "os.O_DIRECTORY",
            "dir_fd=",
            "follow_symlinks=False",
            "source_before.st_nlink != 1",
            "st_mtime_ns",
            "st_ctime_ns",
            "hashlib.sha256(payload).hexdigest()",
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)

    @unittest.skipIf(os.name == "nt", "descriptor-relative POSIX test")
    def test_regular_file_capture_and_symlink_rejection(self):
        spec = importlib.util.spec_from_file_location("formal_capture", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "answers"
            problem_dir = root / "CSP003" / "apple"
            problem_dir.mkdir(parents=True)
            payload = b"int main(){return 0;}\n"
            source = problem_dir / "apple.cpp"
            source.write_bytes(payload)

            captured = module._capture_tree(str(root), "CSP003", "apple", 1024)
            self.assertEqual(captured["size"], len(payload))
            self.assertEqual(captured["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(base64.b64decode(captured["base64"]), payload)

            source.unlink()
            (problem_dir / "real.cpp").write_bytes(payload)
            source.symlink_to(problem_dir / "real.cpp")
            with self.assertRaises(OSError):
                module._capture_tree(str(root), "CSP003", "apple", 1024)

    @unittest.skipIf(os.name == "nt", "descriptor-relative POSIX test")
    def test_hard_link_and_symlinked_directory_are_rejected(self):
        spec = importlib.util.spec_from_file_location("formal_capture_links", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "answers"
            problem_dir = root / "CSP003" / "apple"
            problem_dir.mkdir(parents=True)
            source = problem_dir / "apple.cpp"
            source.write_text("int main(){}\n", encoding="utf-8")
            os.link(source, problem_dir / "second-link.cpp")
            with self.assertRaisesRegex(RuntimeError, "single-link"):
                module._capture_tree(str(root), "CSP003", "apple", 1024)

            (problem_dir / "second-link.cpp").unlink()
            source.unlink()
            problem_dir.rmdir()
            real_problem = base / "outside-apple"
            real_problem.mkdir()
            (real_problem / "apple.cpp").write_text("int main(){}\n", encoding="utf-8")
            problem_dir.symlink_to(real_problem, target_is_directory=True)
            with self.assertRaises(OSError):
                module._capture_tree(str(root), "CSP003", "apple", 1024)


if __name__ == "__main__":
    unittest.main()
