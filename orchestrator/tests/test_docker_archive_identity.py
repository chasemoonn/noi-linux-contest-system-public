import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_docker_archive_identity.py"
TAG = "noi-linux-official:qualification-test"


def encoded(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def add(handle: tarfile.TarFile, name: str, raw: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(raw)
    info.mode = 0o644
    handle.addfile(info, io.BytesIO(raw))


class DockerArchiveIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = encoded({
            "architecture": "amd64",
            "config": {"Labels": {"org.noi.desktop.contract": "finalizer-status-v1"}},
            "os": "linux",
        })
        self.config_digest = hashlib.sha256(self.config).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def run_verifier(self, archive: Path, image_id: str, output: Path | None = None):
        command = [sys.executable, str(SCRIPT), "--archive", str(archive),
                   "--expected-tag", TAG, "--expected-image-id", image_id]
        if output is not None:
            command += ["--config-output", str(output)]
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def test_accepts_classic_config_digest_identity(self):
        archive = self.root / "classic.tar"
        config_name = self.config_digest + ".json"
        manifest = encoded([{"Config": config_name, "Layers": [], "RepoTags": [TAG]}])
        with tarfile.open(archive, "w") as handle:
            add(handle, config_name, self.config)
            add(handle, "manifest.json", manifest)
        output = self.root / "classic-config.json"
        completed = self.run_verifier(
            archive, "sha256:" + self.config_digest, output
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["identity_mode"], "legacy-config")
        self.assertEqual(output.read_bytes(), self.config)

    def modern_archive(self, *, direct_manifest: bool = False,
                       wrong_config_binding: bool = False) -> tuple[Path, str]:
        archive = self.root / "modern.tar"
        bound_config = "0" * 64 if wrong_config_binding else self.config_digest
        platform_manifest = encoded({
            "config": {
                "digest": "sha256:" + bound_config,
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": len(self.config),
            },
            "layers": [],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        })
        platform_digest = hashlib.sha256(platform_manifest).hexdigest()
        runnable = {
                "digest": "sha256:" + platform_digest,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "platform": {"architecture": "amd64", "os": "linux"},
                "size": len(platform_manifest),
            }
        attestation = encoded({
            "annotations": {"test.noi/attestation": "true"},
            "config": {
                "digest": "sha256:" + self.config_digest,
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": len(self.config),
            },
            "layers": [],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        })
        attestation_digest = hashlib.sha256(attestation).hexdigest()
        identity = encoded({
            "manifests": [runnable, {
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
                "digest": "sha256:" + attestation_digest,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "platform": {"architecture": "unknown", "os": "unknown"},
                "size": len(attestation),
            }],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        })
        identity_digest = hashlib.sha256(identity).hexdigest()
        if direct_manifest:
            identity = platform_manifest
            identity_digest = platform_digest
        outer = encoded({
            "manifests": [{
                "digest": "sha256:" + identity_digest,
                "mediaType": (
                    "application/vnd.oci.image.manifest.v1+json" if direct_manifest
                    else "application/vnd.oci.image.index.v1+json"
                ),
                "size": len(identity),
            }],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        })
        manifest = encoded([{
            "Config": "blobs/sha256/" + self.config_digest,
            "Layers": [],
            "RepoTags": [TAG],
        }])
        with tarfile.open(archive, "w") as handle:
            add(handle, "blobs/sha256/" + self.config_digest, self.config)
            add(handle, "blobs/sha256/" + platform_digest, platform_manifest)
            add(handle, "blobs/sha256/" + attestation_digest, attestation)
            if identity_digest != platform_digest:
                add(handle, "blobs/sha256/" + identity_digest, identity)
            add(handle, "index.json", outer)
            add(handle, "manifest.json", manifest)
            add(handle, "oci-layout", encoded({"imageLayoutVersion": "1.0.0"}))
        return archive, "sha256:" + identity_digest

    def test_accepts_containerd_oci_index_identity(self):
        archive, image_id = self.modern_archive()
        output = self.root / "modern-config.json"
        completed = self.run_verifier(archive, image_id, output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["identity_mode"], "oci-index")
        self.assertEqual(output.read_bytes(), self.config)

    def test_accepts_containerd_direct_oci_manifest_identity(self):
        archive, image_id = self.modern_archive(direct_manifest=True)
        completed = self.run_verifier(archive, image_id)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["identity_mode"], "oci-manifest")

    def test_rejects_oci_identity_that_does_not_bind_config(self):
        archive, image_id = self.modern_archive(wrong_config_binding=True)
        completed = self.run_verifier(archive, image_id)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("does not bind the Docker image config", completed.stderr)


if __name__ == "__main__":
    unittest.main()
