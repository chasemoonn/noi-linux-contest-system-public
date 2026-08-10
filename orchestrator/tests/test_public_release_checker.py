import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_public_release", ROOT / "scripts" / "check_public_release.py"
)
CHECKER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CHECKER)


class PublicReleaseCheckerTests(unittest.TestCase):
    def scan_exported_tree(self, base: Path):
        original = CHECKER.ROOT
        try:
            CHECKER.ROOT = base
            return CHECKER.scan()
        finally:
            CHECKER.ROOT = original

    def test_runtime_and_image_artifacts_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cases = {
                ".env": "runtime configuration/state file",
                "contest.log": "runtime log/event stream",
                "events.jsonl": "runtime log/event stream",
                "contest.pem": "credential or runtime database",
                "orchestrator.sqlite": "credential or runtime database",
                "database.sql.gz": "runtime backup/export",
                "mongo.bson": "runtime backup/export",
                "desktop.tar.zst": "disk/container image artifact",
                "desktop.raw": "disk/container image artifact",
                "desktop.squashfs": "disk/container image artifact",
            }
            for suffix in (
                ".tar.gz",
                ".tar.bz2",
                ".tar.xz",
                ".wim",
            ):
                cases[f"desktop{suffix}"] = "disk/container image artifact"
            for suffix in (
                ".bak",
                ".backup",
                ".dump",
                ".dump.gz",
                ".sql",
                ".sql.bz2",
                ".sql.xz",
                ".sql.zst",
                ".bson.gz",
            ):
                cases[f"database{suffix}"] = "runtime backup/export"
            for name, expected in cases.items():
                with self.subTest(name=name):
                    path = base / name
                    path.touch()
                    original = CHECKER.ROOT
                    try:
                        CHECKER.ROOT = base
                        self.assertEqual(CHECKER.blocked_runtime_file(path), expected)
                    finally:
                        CHECKER.ROOT = original

    def test_gitignore_excludes_runtime_logs_and_full_images(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for pattern in (
            "*.log",
            "*.jsonl",
            "*.tar.gz",
            "*.tar.bz2",
            "*.tar.xz",
            "*.iso",
            "*.img",
            "*.raw",
            "*.qcow",
            "*.qcow2",
            "*.vdi",
            "*.vhd",
            "*.vhdx",
            "*.vmdk",
            "*.ova",
            "*.ovf",
            "*.wim",
            "*.squashfs",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignore)

    def test_examples_and_source_files_are_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            for name in (".env.example", "config.example.yaml", "README.md"):
                with self.subTest(name=name):
                    path = base / name
                    path.touch()
                    original = CHECKER.ROOT
                    try:
                        CHECKER.ROOT = base
                        self.assertIsNone(CHECKER.blocked_runtime_file(path))
                    finally:
                        CHECKER.ROOT = original

    def test_high_confidence_secret_patterns(self):
        samples = (
            b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----",
            b"ghp_" + b"abcdefghijklmnopqrstuvwxyz0123456789ABCD",
            b"mongodb://operator:" + b"secret-value@database.example/test",
        )
        for sample in samples:
            with self.subTest(sample=sample[:16]):
                self.assertTrue(
                    any(pattern.search(sample) for _, pattern in CHECKER.SECRET_PATTERNS)
                )

    def test_safe_placeholders_do_not_match_secret_patterns(self):
        samples = (
            b"ALIYUN_ACCESS_KEY_SECRET=",
            b"HYDRO_ORCHESTRATOR_TOKEN=${HYDRO_ORCHESTRATOR_TOKEN}",
            b"mongodb://127.0.0.1:27017/hydro",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertFalse(
                    any(pattern.search(sample) for _, pattern in CHECKER.SECRET_PATTERNS)
                )

    def test_exported_tree_without_git_metadata_is_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "README.md").write_text("# Safe export\n", encoding="utf-8")
            (base / "scripts").mkdir()
            (base / "scripts" / "check.py").write_text(
                "print('safe')\n", encoding="utf-8"
            )

            report = self.scan_exported_tree(base)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["files_scanned"], 2)

    def test_exported_tree_still_rejects_runtime_material(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "README.md").write_text("# Safe export\n", encoding="utf-8")
            (base / ".env").write_text("PASSWORD=not-for-release\n", encoding="utf-8")

            report = self.scan_exported_tree(base)

            self.assertEqual(report["status"], "fail")
            self.assertTrue(
                any(
                    item["code"] == "BLOCKED_PUBLIC_FILE"
                    and item["path"] == ".env"
                    for item in report["failures"]
                )
            )

    def test_exported_tree_rejects_logs_images_and_backups(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            blocked = {
                "runtime.log",
                "events.jsonl",
                "desktop.raw",
                "desktop.tar.gz",
                "database.sql.zst",
                "mongo.bson.gz",
            }
            for name in blocked:
                (base / name).write_bytes(b"safe-looking placeholder\n")

            report = self.scan_exported_tree(base)

            self.assertEqual(report["status"], "fail")
            rejected = {
                item["path"]
                for item in report["failures"]
                if item["code"] == "BLOCKED_PUBLIC_FILE"
            }
            self.assertEqual(rejected, blocked)

    def test_exported_tree_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "target.txt"
            target.write_text("safe\n", encoding="utf-8")
            link = base / "linked.txt"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            report = self.scan_exported_tree(base)

            self.assertEqual(report["status"], "fail")
            self.assertTrue(
                any(
                    item["code"] == "UNSAFE_PUBLIC_SYMLINK"
                    and item["path"] == "linked.txt"
                    for item in report["failures"]
                )
            )

    @unittest.skipUnless(os.name == "nt", "NTFS junction regression")
    def test_windows_junction_is_rejected_without_reading_its_target(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            base = Path(temp)
            external = Path(outside)
            (external / "external.txt").write_text(
                "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789ABCD" + "\n",
                encoding="utf-8",
            )
            junction = base / "junction"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                check=False,
                capture_output=True,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
            try:
                for git_mode in (False, True):
                    with self.subTest(git_mode=git_mode):
                        if git_mode:
                            subprocess.run(
                                ["git", "init", "--quiet"], cwd=base, check=True
                            )
                        report = self.scan_exported_tree(base)
                        self.assertEqual(report["status"], "fail")
                        self.assertTrue(
                            any(
                                item["code"] == "UNSAFE_PUBLIC_REPARSE_POINT"
                                and item["path"] == "junction"
                                for item in report["failures"]
                            )
                        )
                        self.assertFalse(
                            any(
                                item.get("path") == "junction/external.txt"
                                or item["code"] == "GITHUB_TOKEN"
                                for item in report["failures"]
                            )
                        )
            finally:
                if os.path.lexists(junction):
                    os.rmdir(junction)


if __name__ == "__main__":
    unittest.main()
