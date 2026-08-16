import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "collect_v1_image_host_fact.py"
SPEC = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(collector)


class ImageHostFactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.revision = "a" * 40
        self.image_id = "sha256:" + "b" * 64
        self.labels = {
            "org.noi.desktop.contract": "finalizer-status-v1",
            "org.noi.iso.sha256": "c" * 64,
            "org.opencontainers.image.revision": self.revision,
        }
        self.archive_name = "noi-linux-local_test.tar"
        (self.bundle / self.archive_name).write_bytes(b"archive bytes")
        manifest = {
            "$schema": "local-image-bundle-manifest.schema.json",
            "schema_version": 1,
            "created_at": "2026-08-12T00:00:00Z",
            "image": {
                "tag": "noi-linux-local:test",
                "id": self.image_id,
                "source_revision": self.revision,
                "labels": self.labels,
            },
            "archive": {
                "file": self.archive_name,
                "format": "docker-archive",
                "compression": "none",
                "sha256": self.digest(self.bundle / self.archive_name),
                "size_bytes": (self.bundle / self.archive_name).stat().st_size,
            },
        }
        self.write_json(self.bundle / "manifest.json", manifest)
        (self.bundle / "local-image-bundle-manifest.schema.json").write_text(
            '{"type":"object"}\n', encoding="utf-8", newline="\n"
        )
        (self.bundle / "import-local-image-bundle.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n"
        )
        payload_names = [
            self.archive_name,
            "manifest.json",
            "local-image-bundle-manifest.schema.json",
            "import-local-image-bundle.sh",
        ]
        (self.bundle / "SHA256SUMS").write_text(
            "".join(f"{self.digest(self.bundle / name)}  {name}\n" for name in payload_names),
            encoding="ascii",
            newline="\n",
        )
        manifest_digest = self.digest(self.bundle / "manifest.json")
        checksums_digest = self.digest(self.bundle / "SHA256SUMS")
        self.release = self.root / "release-manifest.json"
        self.write_json(
            self.release,
            {
                "$schema": "release-manifest.schema.json",
                "schema_version": 1,
                "release": {
                    "version": "0.2.0-test",
                    "git_revision": self.revision,
                    "created_at": "2026-08-12T00:00:00Z",
                },
                "profile": "aliyun-hydro5-pm2-direct-v1",
                "components": {
                    "orchestrator": {},
                    "hydro_plugin": {},
                    "desktop": {
                        "delivery": "offline",
                        "bundle_manifest_sha256": manifest_digest,
                        "bundle_checksums_sha256": checksums_digest,
                        "source_revision": self.revision,
                        "image_tag": "noi-linux-local:test",
                        "image_id": self.image_id,
                        "contract": "finalizer-status-v1",
                        "iso_sha256": "c" * 64,
                    },
                },
                "verification": {},
            },
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def write_json(path, value):
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_exact_bundle_binds_every_payload_and_release_field(self):
        fact, labels = collector.exact_bundle(self.bundle, self.release)
        self.assertEqual(fact["image_id"], self.image_id)
        self.assertEqual(fact["source_revision"], self.revision)
        self.assertEqual(labels, self.labels)

    def test_changed_importer_is_rejected_even_when_archive_is_unchanged(self):
        (self.bundle / "import-local-image-bundle.sh").write_text(
            "#!/usr/bin/env bash\nexit 99\n", encoding="utf-8", newline="\n"
        )
        with self.assertRaisesRegex(collector.FactError, "checksum differs"):
            collector.exact_bundle(self.bundle, self.release)

    def test_checksum_row_cannot_be_added_or_omitted(self):
        with (self.bundle / "SHA256SUMS").open("a", encoding="ascii") as handle:
            handle.write("0" * 64 + "  extra.txt\n")
        with self.assertRaisesRegex(collector.FactError, "exact bundle payload"):
            collector.exact_bundle(self.bundle, self.release)

    def test_release_cannot_point_at_another_image(self):
        release = json.loads(self.release.read_text(encoding="utf-8"))
        release["components"]["desktop"]["image_id"] = "sha256:" + "d" * 64
        self.write_json(self.release, release)
        with self.assertRaisesRegex(collector.FactError, "image_id differs"):
            collector.exact_bundle(self.bundle, self.release)

    def test_clean_git_status_is_valid_empty_output(self):
        result = subprocess.CompletedProcess(
            ["git", "status", "--porcelain=v1", "--untracked-files=no"],
            0,
            stdout="",
            stderr="",
        )
        with mock.patch.object(collector.subprocess, "run", return_value=result):
            self.assertEqual(collector.git_status_porcelain(), "")


if __name__ == "__main__":
    unittest.main()
