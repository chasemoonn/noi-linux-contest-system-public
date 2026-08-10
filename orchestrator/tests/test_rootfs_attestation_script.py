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
SCRIPT = ROOT / "deploy" / "attest-cached-noi-rootfs-once.sh"
DOC = ROOT / "deploy" / "ROOTFS_ATTESTATION.md"


class RootfsAttestationScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_is_pinned_to_single_known_image_and_iso(self):
        self.assertIn(
            'EXPECTED_SOURCE_ID="sha256:fed2063bb95263b9241368420215a4acc538e0f0253b3f4b51bdc4e1769c7631"',
            self.source,
        )
        self.assertIn(
            'EXPECTED_ISO_SHA256="C8824240736352E5E4AAF3F6532B40961F75FA9F23D670BB78881355A49D5878"',
            self.source,
        )
        self.assertIn('current_id="$(image_id "${TARGET_TAG}")"', self.source)
        self.assertIn('if [[ "${current_id}" != "${EXPECTED_SOURCE_ID}" ]]', self.source)

    def test_never_builds_imports_or_commits_a_filesystem_layer(self):
        for forbidden in ("docker build", "docker import", "docker commit"):
            self.assertNotIn(forbidden, self.source)
        self.assertIn('docker image save --output "${source_archive}"', self.source)
        self.assertIn('labels[label_name] = iso_sha', self.source)
        self.assertIn(
            'if [[ "$(layers_json "${CANDIDATE_TAG}")" != "${source_layers}" ]]',
            self.source,
        )
        self.assertIn(
            'if [[ "$(layers_json "${TARGET_TAG}")" != "${source_layers}" ]]',
            self.source,
        )

    def test_preserves_rollback_and_restores_on_post_tag_failure(self):
        create = 'docker image tag "${EXPECTED_SOURCE_ID}" "${ROLLBACK_TAG}"'
        promote = 'docker image tag "${candidate_id}" "${TARGET_TAG}"'
        self.assertIn(create, self.source)
        self.assertIn('promotion_may_have_happened=1', self.source)
        self.assertIn('docker image tag "${ROLLBACK_TAG}" "${TARGET_TAG}"', self.source)
        self.assertNotIn('docker image rm "${ROLLBACK_TAG}"', self.source)
        self.assertLess(self.source.index(create), self.source.index(promote))

    def test_shares_deploy_lock_and_refuses_active_seats_before_image_work(self):
        self.assertIn(
            'DEPLOY_LOCK_FILE="/var/lock/noi-official-image-deploy.lock"',
            self.source,
        )
        self.assertIn('exec 8>"${DEPLOY_LOCK_FILE}"', self.source)
        self.assertIn("flock -n 8", self.source)
        seat_guard = 'docker ps -q --filter label=noi.contest'
        self.assertIn(seat_guard, self.source)
        guard_position = self.source.index(seat_guard)
        for operation in (
            'docker image tag "${EXPECTED_SOURCE_ID}" "${ROLLBACK_TAG}"',
            'docker image save --output "${source_archive}"',
            'docker run --rm --network none',
        ):
            self.assertLess(guard_position, self.source.index(operation))

    def test_key_packages_are_checked_in_restricted_container(self):
        for expected in (
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges",
            'test "$(gcc -dumpfullversion -dumpversion)" = "9.3.0"',
            'test "$(fpc -iV)" = "3.0.4"',
            "codeblocks lazarus geany ibus ibus-libpinyin",
            "/usr/local/arbiter/local/arbiter_local",
        ):
            self.assertIn(expected, self.source)

    def test_command_is_not_wired_into_general_build_or_release(self):
        needle = SCRIPT.name
        for path in (
            ROOT / "deploy" / "build-noi-official-image.sh",
            ROOT / "deploy" / "deploy-contest-image-from-oj.sh",
            ROOT / "noi-linux-official" / "Dockerfile",
        ):
            self.assertNotIn(needle, path.read_text(encoding="utf-8"))
        self.assertIn("不能用它绕过 ISO 校验", DOC.read_text(encoding="utf-8"))

    def test_archive_rewriter_changes_only_config_and_manifest(self):
        marker = "<<'PY'\n"
        embedded = self.source.split(marker, 1)[1].split("\nPY\n", 1)[0]
        layer_payload = b"synthetic-rootfs-layer"
        diff_id = f"sha256:{hashlib.sha256(layer_payload).hexdigest()}"
        config = {
            "architecture": "amd64",
            "os": "linux",
            "config": {"Labels": {"unrelated": "preserved"}},
            "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        }
        config_payload = json.dumps(config, separators=(",", ":")).encode()
        old_digest = hashlib.sha256(config_payload).hexdigest()
        old_config_name = f"{old_digest}.json"
        manifest = [
            {
                "Config": old_config_name,
                "RepoTags": None,
                "Layers": ["layer/layer.tar"],
            }
        ]

        with tempfile.TemporaryDirectory() as directory:
            source_tar = Path(directory) / "source.tar"
            output_tar = Path(directory) / "output.tar"
            with tarfile.open(source_tar, "w") as archive:
                for name, payload in (
                    (old_config_name, config_payload),
                    ("layer/layer.tar", layer_payload),
                    ("manifest.json", json.dumps(manifest).encode()),
                ):
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))

            completed = subprocess.run(
                [
                    sys.executable,
                    "-",
                    str(source_tar),
                    str(output_tar),
                    "noi-linux-official-rootfs:test-candidate",
                    f"sha256:{old_digest}",
                    "ABCDEF",
                    "org.noi.iso.sha256",
                ],
                input=embedded,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            with tarfile.open(output_tar, "r") as archive:
                output_manifest = json.load(archive.extractfile("manifest.json"))
                new_config_name = output_manifest[0]["Config"]
                output_config_payload = archive.extractfile(new_config_name).read()
                output_config = json.loads(output_config_payload)
                output_layer = archive.extractfile("layer/layer.tar").read()

        self.assertEqual(output_layer, layer_payload)
        self.assertEqual(output_config["rootfs"], config["rootfs"])
        self.assertEqual(output_config["config"]["Labels"]["unrelated"], "preserved")
        self.assertEqual(
            output_config["config"]["Labels"]["org.noi.iso.sha256"], "ABCDEF"
        )
        self.assertEqual(
            new_config_name, f"{hashlib.sha256(output_config_payload).hexdigest()}.json"
        )
        self.assertEqual(
            output_manifest[0]["RepoTags"],
            ["noi-linux-official-rootfs:test-candidate"],
        )


if __name__ == "__main__":
    unittest.main()
