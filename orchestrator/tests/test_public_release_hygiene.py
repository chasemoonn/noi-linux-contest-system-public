from pathlib import Path
import json
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PublicReleaseHygieneTests(unittest.TestCase):
    def test_generic_entrypoints_do_not_contain_site_identity(self):
        generic_paths = (
            ROOT / "README.md",
            ROOT / "orchestrator" / "config.example.yaml",
            ROOT / "orchestrator" / ".env.example",
            ROOT / "orchestrator" / "cloud_admin.py",
            ROOT / "deploy" / "install-hydro-host.sh",
            ROOT / "deploy" / "deploy-contest-image-from-oj.sh",
        )
        forbidden = (
            ".".join(("8", "210", "61", "7")),
            ".".join(("114", "55", "0", "198")),
            "i-" + "bp1fjgtm0njvcgwks2y3",
            "exam." + "xwje" + "du.cn",
            "exam." + "quxi" + "nao.com",
        )
        for path in generic_paths:
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                with self.subTest(path=path.relative_to(ROOT), value=value):
                    self.assertNotIn(value, text)

    def test_generic_installer_requires_site_specific_identity(self):
        installer = (ROOT / "deploy" / "install-hydro-host.sh").read_text(
            encoding="utf-8"
        )
        for variable in (
            "NOI_ALIYUN_REGION_ID",
            "NOI_ALIYUN_INSTANCE_ID",
            "NOI_CONTEST_SOURCE_CIDR",
            "NOI_CONTEST_SSH_HOST_KEY_SHA256",
            "NOI_STUDENT_DESKTOP_SOURCE_CIDR",
        ):
            with self.subTest(variable=variable):
                self.assertIn(f"${{{variable}:?", installer)

        image_deploy = (
            ROOT / "deploy" / "deploy-contest-image-from-oj.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "${CONTEST_SSH_HOST_KEY_SHA256:?set CONTEST_SSH_HOST_KEY_SHA256}",
            image_deploy,
        )
        self.assertIn("${NOI_SECRET_INPUT:?", installer)
        self.assertIn("stat -c '%u:%a:%h'", installer)
        self.assertNotIn("secret_input=/tmp/noi-deploy-secrets.env", installer)

    def test_large_and_local_image_artifacts_are_ignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for pattern in (
            "/local-release/",
            "*.tar",
            "*.tar.zst",
            "*.oci.tar",
            "*.oci.tar.zst",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignore)

    def test_orchestrator_docker_context_excludes_runtime_material(self):
        ignore = (ROOT / "orchestrator" / ".dockerignore").read_text(
            encoding="utf-8"
        ).splitlines()
        for pattern in (
            ".env",
            "config.yaml",
            "secrets/",
            "keys/",
            "*.pem",
            "known_hosts",
            "*.db",
            "*.sqlite",
            "*.log",
            "*.jsonl",
            "*.tar",
            "*.tar.zst",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignore)

    def test_official_image_requires_an_explicit_student_password(self):
        entrypoint = (
            ROOT
            / "noi-linux-official"
            / "rootfs"
            / "usr"
            / "local"
            / "bin"
            / "contest-entrypoint.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '${STUDENT_PASSWORD:?STUDENT_PASSWORD is required}', entrypoint
        )
        self.assertNotIn("noilinux123", entrypoint)

    def test_local_bundle_manifest_examples_are_valid_json(self):
        release = ROOT / "release"
        schema = json.loads(
            (release / "local-image-bundle-manifest.schema.json").read_text(
                encoding="utf-8"
            )
        )
        example = json.loads(
            (release / "local-image-bundle-manifest.example.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(example["schema_version"], 1)
        self.assertEqual(
            example["image"]["labels"]["org.noi.desktop.contract"],
            "finalizer-status-v1",
        )
        self.assertRegex(example["image"]["id"], r"^sha256:[a-f0-9]{64}$")
        self.assertRegex(example["image"]["source_revision"], r"^[a-f0-9]{40}$")
        self.assertEqual(
            example["image"]["labels"]["org.opencontainers.image.revision"],
            example["image"]["source_revision"],
        )
        self.assertIn(
            "org.opencontainers.image.revision",
            schema["properties"]["image"]["properties"]["labels"]["required"],
        )
        self.assertRegex(example["archive"]["sha256"], r"^[a-f0-9]{64}$")

        release_schema = json.loads(
            (release / "release-manifest.schema.json").read_text(encoding="utf-8")
        )
        release_example = json.loads(
            (release / "release-manifest.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(release_schema["$defs"]["evidence"]["type"], "object")
        self.assertEqual(
            release_example["profile"], "aliyun-hydro5-pm2-direct-v1"
        )
        self.assertEqual(
            release_example["verification"]["capacity_15_plus_2"]["status"],
            "pending",
        )
        desktop_schema = release_schema["properties"]["components"]["properties"][
            "desktop"
        ]
        self.assertIn("bundle_checksums_sha256", desktop_schema["required"])
        self.assertRegex(
            release_example["components"]["desktop"]["bundle_checksums_sha256"],
            r"^[a-f0-9]{64}$",
        )
        self.assertEqual(
            desktop_schema["properties"]["image_tag"]["not"]["pattern"],
            ":latest$",
        )

    def test_local_image_bundle_records_an_explicit_source_revision(self):
        export_script = (ROOT / "scripts" / "export-local-image-bundle.sh").read_text(
            encoding="utf-8"
        )
        import_script = (ROOT / "scripts" / "import-local-image-bundle.sh").read_text(
            encoding="utf-8"
        )
        identity_verifier = (
            ROOT / "scripts" / "verify_docker_archive_identity.py"
        ).read_text(encoding="utf-8")
        self.assertIn("--source-revision", export_script)
        self.assertIn("source_revision", export_script)
        self.assertIn("org.opencontainers.image.revision", export_script)
        self.assertIn("Manifest SHA256", export_script)
        self.assertIn("Checksums SHA256", export_script)
        self.assertIn("SHA256SUMS", export_script)
        self.assertIn("import-local-image-bundle.sh", export_script)
        self.assertIn("--release-manifest", import_script)
        self.assertIn("bundle_manifest_sha256", import_script)
        self.assertIn("bundle_checksums_sha256", import_script)
        self.assertIn("sha256sum --check --strict", import_script)
        self.assertIn('os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW', import_script)
        self.assertIn('os.O_RDONLY | os.O_NOFOLLOW', import_script)
        self.assertIn('if set(entries) != expected_payload:', import_script)
        self.assertIn('expected_physical = expected_payload | {"SHA256SUMS"}', import_script)
        self.assertIn('initial_names = os.listdir(source_fd)', import_script)
        self.assertIn('final_names = os.listdir(source_fd)', import_script)
        self.assertIn('bundle_dir="${snapshot_dir}"', import_script)
        self.assertLess(
            import_script.index('bundle_dir="${snapshot_dir}"'),
            import_script.index('sha256sum --check --strict'),
        )
        self.assertIn("source_revision", import_script)
        self.assertIn("org.opencontainers.image.revision", import_script)
        self.assertIn("verify_docker_archive_identity.py", export_script)
        self.assertIn("verify_docker_archive_identity.py", import_script)
        self.assertIn("OCI identity graph", identity_verifier)
        self.assertIn("expected image identity", identity_verifier)
        self.assertLess(
            import_script.index("comparisons = {"),
            import_script.index("tag_inspect_status=0"),
        )
        self.assertIn(
            'source_revision": "0000000000000000000000000000000000000000',
            (ROOT / "release" / "local-image-bundle-manifest.example.json").read_text(
                encoding="utf-8"
            ),
        )

    def test_local_image_bundle_embedded_python_compiles(self):
        for name in (
            "export-local-image-bundle.sh",
            "import-local-image-bundle.sh",
        ):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            blocks = re.findall(r"<<'PY'\n(.*?)\nPY", source, flags=re.DOTALL)
            self.assertTrue(blocks, name)
            for index, block in enumerate(blocks):
                with self.subTest(script=name, block=index):
                    compile(block, f"{name}:embedded-python-{index}", "exec")


if __name__ == "__main__":
    unittest.main()
