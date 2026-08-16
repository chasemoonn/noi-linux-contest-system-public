import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_v1_private_upgrade_plan", ROOT / "scripts" / "build_v1_private_upgrade_plan.py"
)
builder = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(builder)


def baseline():
    return {"schema_version": 1, "present": True, "container": {
        "container_id": "8" * 64, "name": "/noi-orchestrator",
        "image_id": "sha256:" + "9" * 64, "running": True, "restart_count": 0,
        "immutable_identity": {
            "container_id": "8" * 64, "name": "/noi-orchestrator",
            "image_id": "sha256:" + "9" * 64,
            "config": {"Env": ["A=B"], "Image": "sha256:" + "9" * 64,
                       "Labels": {"school": "example"}, "WorkingDir": "/app"},
            "host_config": {"NetworkMode": "host", "RestartPolicy": {"Name": "unless-stopped"},
                            "Privileged": False, "OomKillDisable": None,
                            "Binds": ["/srv/config:/app/config.yaml:ro"]},
            "mounts": [],
        },
        "immutable_identity_sha256": "7" * 64,
    }}


class PrivateUpgradePlanTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() == 0,
                         "requires Linux root-owned path metadata")
    def test_trusted_executable_preserves_virtual_environment_entry_point(self):
        with tempfile.TemporaryDirectory(dir="/root") as raw:
            root = Path(raw)
            target = root / "python-real"
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
            entry_point = root / "python"
            entry_point.symlink_to(target.name)

            trusted = builder.trusted_executable(entry_point)

            self.assertEqual(trusted, Path(os.path.abspath(entry_point)))
            self.assertNotEqual(trusted, trusted.resolve(strict=True))

    def test_effective_contract_uses_hydro_domain_id_not_public_hostname(self):
        state = mock.Mock(effective={
            "hydro": {
                "public_base_url": "https://oj.example.test",
                "domain_id": "system",
            },
            "orchestrator": {"public_base_url": "https://exam.example.test"},
            "frontend_proxy": {
                "provider": "caddy",
                "domain": "exam.example.test",
                "orchestrator_upstream": "http://127.0.0.1:8600",
            },
        })
        self.assertEqual(builder.effective_contract(state), (
            "https://oj.example.test",
            "https://exam.example.test",
            "oj.example.test",
            "system",
            "exam.example.test",
        ))

    def test_effective_contract_rejects_missing_hydro_domain_id(self):
        state = mock.Mock(effective={
            "hydro": {"public_base_url": "https://oj.example.test"},
            "orchestrator": {"public_base_url": "https://exam.example.test"},
            "frontend_proxy": {
                "provider": "caddy",
                "domain": "exam.example.test",
                "orchestrator_upstream": "http://127.0.0.1:8600",
            },
        })
        with self.assertRaisesRegex(builder.PrivatePlanError, "Hydro domain ID"):
            builder.effective_contract(state)

    def test_incomplete_staging_is_only_removed_when_shape_is_exact(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); final = root / "private"; plan_id = "1" * 64
            staging = root / f".private.v1-upgrade-{plan_id[:12]}.pending"
            staging.mkdir(); staging.chmod(0o700)
            transaction = staging / "transaction"; transaction.mkdir(); transaction.chmod(0o700)
            artifact = staging / "desired.env"; artifact.write_text("A=B\n"); artifact.chmod(0o600)
            published, fresh = builder.private_staging(final, plan_id)
            self.assertEqual(published, final)
            self.assertEqual(fresh, staging.resolve())
            self.assertEqual(list(fresh.iterdir()), [])

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); final = root / "private"; plan_id = "1" * 64
            staging = root / f".private.v1-upgrade-{plan_id[:12]}.pending"
            staging.mkdir(); staging.chmod(0o700); (staging / "foreign").write_text("x")
            with self.assertRaisesRegex(builder.PrivatePlanError, "unexpected entry"):
                builder.private_staging(final, plan_id)
            self.assertTrue((staging / "foreign").exists())

    def test_desired_definition_preserves_runtime_and_adds_owner_labels(self):
        row = builder.desired_definition(
            baseline(), "1" * 64, "2" * 40 + "-" + "3" * 12,
            "sha256:" + "4" * 64,
        )
        self.assertEqual(row["config"]["Image"], "sha256:" + "4" * 64)
        self.assertEqual(row["config"]["Labels"]["school"], "example")
        self.assertEqual(row["config"]["Labels"][builder.LABEL_PLAN], "1" * 64)
        self.assertEqual(row["host_config"]["NetworkMode"], "host")
        self.assertIs(row["host_config"]["OomKillDisable"], False)

    def test_desired_definition_rejects_docker_control_and_unsafe_runtime(self):
        value = baseline()
        value["container"]["immutable_identity"]["host_config"]["Binds"] = [
            "/var/run/docker.sock:/var/run/docker.sock"
        ]
        with self.assertRaisesRegex(builder.PrivatePlanError, "Docker control"):
            builder.desired_definition(value, "1" * 64, "2" * 40 + "-" + "3" * 12,
                                       "sha256:" + "4" * 64)
        value = baseline()
        value["container"]["immutable_identity"]["host_config"]["Privileged"] = True
        with self.assertRaisesRegex(builder.PrivatePlanError, "safety baseline"):
            builder.desired_definition(value, "1" * 64, "2" * 40 + "-" + "3" * 12,
                                       "sha256:" + "4" * 64)

    def test_build_emits_exact_upgrade_plan_without_service_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate = root / "candidate"; candidate.mkdir()
            backup = root / "backup"; backup.mkdir()
            for name, content in (("orchestrator-config.yaml", b"config\n"),
                                  ("orchestrator.env", b"A=B\n")):
                (backup / name).write_bytes(content)
            live = {}
            for name in ("config.yaml", ".env", "Caddyfile", "snippet"):
                path = root / name; path.write_text(name); live[name] = path
            (root / "db").write_bytes(b"sqlite")
            output = root / "private"
            plan_id = "1" * 64; manifest_sha = "2" * 64; backup_sha = "3" * 64
            revision = "4" * 40; release = revision + "-" + manifest_sha[:12]
            args = argparse.Namespace(
                plan_id=plan_id, candidate=candidate,
                expected_manifest_sha256=manifest_sha,
                controller_image_id="sha256:" + "5" * 64,
                backup_directory=backup, backup_manifest_sha256=backup_sha,
                output_directory=output, install_root=root / "install",
                project_config=live["config.yaml"], project_env=live[".env"],
                database=root / "db", caddyfile=live["Caddyfile"], snippet=live["snippet"],
                python_bin=Path("/usr/bin/python3"), bash_bin=Path("/bin/bash"),
                pm2_bin=Path("/usr/bin/pm2"), node_bin=Path("/usr/bin/node"),
                docker_socket=root / "docker.sock", qualification_lab=True,
            )
            manifest = {"source": {"revision": revision}, "qualification": {
                "production_qualified": False, "report": None, "report_sha256": None}}
            verified = {"revision": revision, "tree": "6" * 40,
                        "archive_sha256": "7" * 64, "manifest_sha256": manifest_sha,
                        "production_qualified": False}
            fake_docker = mock.Mock()
            fake_docker.inspect.return_value = {"State": {"Running": True}}
            with mock.patch.object(builder, "candidate_identity", return_value=(manifest, b"archive", verified)), \
                    mock.patch.object(builder, "trusted_executable", side_effect=lambda path: path), \
                    mock.patch.object(builder, "safe_ancestors"), \
                    mock.patch.object(builder, "load_backup", return_value=(backup, {
                        "source": {"revision": revision, "manifest_sha256": manifest_sha}}, baseline())), \
                    mock.patch.object(builder, "live_inputs_match_backup"), \
                    mock.patch.object(builder, "runtime_environment", return_value={"A": "B"}), \
                    mock.patch.object(builder, "public_plan_identity", return_value=(plan_id, mock.Mock())), \
                    mock.patch.object(builder, "effective_contract", return_value=(
                        "https://oj.example.test", "https://exam.example.test",
                        "oj.example.test", "system", "exam.example.test")), \
                    mock.patch.object(builder, "qualification_image", return_value=args.controller_image_id), \
                    mock.patch.object(builder, "safe_docker_socket", return_value=args.docker_socket), \
                    mock.patch.object(builder, "Docker", return_value=fake_docker), \
                    mock.patch.object(builder, "inspect_matches", return_value=True), \
                    mock.patch.object(builder, "source_plan", return_value={
                        "plan_id": "8" * 64, "release_name": release}), \
                    mock.patch.object(builder, "verify_private_plan", return_value={}), \
                    mock.patch.object(builder, "verify_bindings"), \
                    mock.patch.object(builder, "verify_desired_definition"):
                result = builder.build(args)
            self.assertEqual(result["status"], "planned")
            self.assertEqual(result["service_mutations"], 0)
            plan = json.loads((output / "private-upgrade-plan.json").read_text())
            self.assertEqual(plan["operation"], "upgrade")
            self.assertEqual(plan["plan_id"], plan_id)
            self.assertEqual(plan["source_release"], release)
            self.assertEqual(plan["executables"]["docker_socket"], str(args.docker_socket))
            self.assertTrue((output / "transaction").is_dir())


if __name__ == "__main__":
    unittest.main()
