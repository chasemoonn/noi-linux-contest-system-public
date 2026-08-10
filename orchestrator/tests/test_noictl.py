import ast
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
NOICTL = REPO_ROOT / "scripts" / "noictl.py"

ACCESS_KEY_ID = "AKID-DO-NOT-LEAK-123456"
ACCESS_KEY_SECRET = "ALIYUN-SECRET-DO-NOT-LEAK-123456"
HYDRO_TOKEN = "HYDRO-TOKEN-DO-NOT-LEAK-1234567890"
ADMIN_PASSWORD = "ADMIN-PASSWORD-DO-NOT-LEAK-123456"
MONGO_PASSWORD = "MONGO-PASSWORD-DO-NOT-LEAK-123456"
ENTRY_HOST = "entry-do-not-leak.example.test"
UNKNOWN_KEY = "unknown_key_name_DO_NOT_LEAK"
UNKNOWN_VALUE = "UNKNOWN-VALUE-DO-NOT-LEAK-123456"

VALID_CONFIG = f"""
cloud:
  provider: aliyun
  aliyun:
    access_key_id: "${{ALIYUN_ACCESS_KEY_ID}}"
    access_key_secret: "${{ALIYUN_ACCESS_KEY_SECRET}}"
    region_id: "${{ALIYUN_REGION_ID:-cn-test}}"
    instance_id: i-private-do-not-leak
contest_server:
  ssh_user: root
  ssh_key: /keys/private-do-not-read.pem
  known_hosts: /keys/known-hosts-do-not-read
  strict_host_key: true
  seats_root: /data/seats
  docker_image: noi-linux-official:2.0
  docker_network: seats
hydro:
  public_base_url: https://{ENTRY_HOST}
  internal_base_url: http://127.0.0.1:8888
  mongo_uri: mongodb://mongo-user:{MONGO_PASSWORD}@127.0.0.1/hydro
  domain_id: system
  submit_enabled: true
  orchestrator_token: "${{HYDRO_ORCHESTRATOR_TOKEN}}"
orchestrator:
  admin_username: teacher-private
  admin_password: "${{ADMIN_PASSWORD}}"
  db: /data/state.db
  collected_dir: /data/collected
artifact_generation:
  enabled: false
  ai:
    model: 2026-08-09
{UNKNOWN_KEY}: {UNKNOWN_VALUE}
"""


class NoictlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.config_path = self.directory / "config.yaml"
        self.config_path.write_text(VALID_CONFIG, encoding="utf-8")
        self.environment = os.environ.copy()
        self.environment.pop("ORCHESTRATOR_CONFIG", None)
        self.environment.update(
            {
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "ALIYUN_ACCESS_KEY_ID": ACCESS_KEY_ID,
                "ALIYUN_ACCESS_KEY_SECRET": ACCESS_KEY_SECRET,
                "ALIYUN_REGION_ID": "cn-offline-test",
                "HYDRO_ORCHESTRATOR_TOKEN": HYDRO_TOKEN,
                "ADMIN_PASSWORD": ADMIN_PASSWORD,
            }
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_noictl(self, *arguments, cwd=None):
        return subprocess.run(
            [sys.executable, str(NOICTL), *map(str, arguments)],
            cwd=cwd or REPO_ROOT,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            check=False,
        )

    def assert_no_sensitive_output(self, *texts):
        combined = "\n".join(texts)
        for forbidden in (
            ACCESS_KEY_ID,
            ACCESS_KEY_SECRET,
            HYDRO_TOKEN,
            ADMIN_PASSWORD,
            MONGO_PASSWORD,
            ENTRY_HOST,
            UNKNOWN_KEY,
            UNKNOWN_VALUE,
            "i-private-do-not-leak",
            "/keys/private-do-not-read.pem",
        ):
            self.assertNotIn(forbidden, combined)

    def test_doctor_json_is_static_read_only(self):
        before = sorted(path.name for path in self.directory.iterdir())
        completed = self.run_noictl(
            "doctor", "--json", "--config", self.config_path
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["command"], "doctor")
        self.assertFalse(payload["changed"])
        checks = {item["code"]: item for item in payload["checks"]}
        self.assertEqual(checks["CONFIG_VALID"]["status"], "pass")
        self.assertEqual(
            checks["READ_ONLY_BOUNDARY"]["evidence"]["network_probes"], 0
        )
        self.assertEqual(
            checks["READ_ONLY_BOUNDARY"]["evidence"]["service_commands"], 0
        )
        self.assertEqual(before, sorted(path.name for path in self.directory.iterdir()))
        self.assert_no_sensitive_output(completed.stdout, completed.stderr)

    def test_config_validate_supports_global_json_option(self):
        completed = self.run_noictl(
            "--json", "--config", self.config_path, "config", "validate"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["command"], "config validate")
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["changed"])
        self.assert_no_sensitive_output(completed.stdout, completed.stderr)

    def test_config_show_redacts_values_unknown_keys_and_sources(self):
        completed = self.run_noictl(
            "config",
            "show",
            "--effective",
            "--redact",
            "--json",
            "--config",
            self.config_path,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        effective = payload["effective_config"]
        self.assertEqual(effective["cloud"]["provider"], "aliyun")
        self.assertEqual(
            effective["cloud"]["aliyun"]["access_key_id"],
            "<redacted:secret>",
        )
        self.assertEqual(
            effective["hydro"]["public_base_url"], "<redacted:entry>"
        )
        self.assertEqual(effective["contest_server"]["ssh_user"], "<redacted:identity>")
        self.assertEqual(
            effective["artifact_generation"]["ai"]["model"],
            "<redacted:unclassified>",
        )
        self.assertEqual(effective["unclassified_1"], "<redacted:unclassified>")
        self.assertEqual(
            effective["cloud"]["aliyun"]["region_id"],
            "<redacted:topology>",
        )
        self.assertEqual(
            effective["contest_server"]["docker_image"],
            "<redacted:topology>",
        )
        self.assertEqual(
            effective["contest_server"]["seats_root"], "<redacted:path>"
        )
        self.assertEqual(
            payload["sources"]["cloud.aliyun.region_id"], "redacted"
        )
        self.assertEqual(
            payload["sources"]["cloud.aliyun.access_key_secret"], "redacted"
        )
        self.assert_no_sensitive_output(completed.stdout, completed.stderr)

    def test_config_show_redacts_private_registry_topology(self):
        private_registry = "registry.corp.invalid/team/noi:2.0"
        self.config_path.write_text(
            VALID_CONFIG.replace(
                "noi-linux-official:2.0", private_registry
            ),
            encoding="utf-8",
        )
        completed = self.run_noictl(
            "config",
            "show",
            "--effective",
            "--redact",
            "--json",
            "--config",
            self.config_path,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(
            payload["effective_config"]["contest_server"]["docker_image"],
            "<redacted:topology>",
        )
        self.assertNotIn(private_registry, completed.stdout)

    def test_invalid_config_is_generic_and_uses_exit_2(self):
        invalid_provider = "evil-provider-value-DO-NOT-LEAK"
        self.config_path.write_text(
            VALID_CONFIG.replace("provider: aliyun", f"provider: {invalid_provider}"),
            encoding="utf-8",
        )
        completed = self.run_noictl(
            "config", "validate", "--json", "--config", self.config_path
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["checks"][0]["code"], "CONFIG_VALID")
        self.assertNotIn(invalid_provider, completed.stdout + completed.stderr)
        self.assert_no_sensitive_output(completed.stdout, completed.stderr)

    def test_doctor_uses_exit_3_outside_supported_static_profile(self):
        self.config_path.write_text(
            VALID_CONFIG.replace(
                "docker_image: noi-linux-official:2.0",
                "docker_image: noi-linux-sim:latest",
            ),
            encoding="utf-8",
        )
        completed = self.run_noictl(
            "doctor", "--json", "--config", self.config_path
        )
        self.assertEqual(completed.returncode, 3)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "error")
        checks = {item["code"]: item for item in payload["checks"]}
        self.assertEqual(checks["CONFIG_VALID"]["status"], "pass")
        self.assertEqual(checks["SUPPORTED_PROFILE_STATIC"]["status"], "warn")
        self.assert_no_sensitive_output(completed.stdout, completed.stderr)

    def test_obvious_network_config_path_is_refused_without_access(self):
        completed = self.run_noictl(
            "config",
            "validate",
            "--json",
            "--config",
            "//noictl-network-path.invalid/config.yaml",
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(
            payload["checks"][0]["evidence"]["error_kind"],
            "network_path_refused",
        )

    def test_unexpandable_config_path_stays_inside_json_contract(self):
        private_path = "~NOICTL-NO-SUCH-USER-SECRET/config.yaml"
        completed = self.run_noictl(
            "config", "validate", "--json", "--config", private_path
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["checks"][0]["code"], "CONFIG_VALID")
        self.assertNotIn(private_path, completed.stdout + completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires O_NOFOLLOW")
    def test_default_config_selection_does_not_follow_symlink(self):
        target = self.directory / "valid-target.yaml"
        self.config_path.replace(target)
        self.config_path.symlink_to(target)
        completed = self.run_noictl(
            "config", "validate", "--json", cwd=self.directory
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["checks"][0]["code"], "CONFIG_VALID")
        self.assertNotEqual(payload["status"], "ok")

    def test_windows_open_identity_change_is_rejected_before_read(self):
        module_spec = importlib.util.spec_from_file_location(
            "noictl_identity_test", NOICTL
        )
        self.assertIsNotNone(module_spec)
        self.assertIsNotNone(module_spec.loader)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
            actual = os.lstat(self.config_path)
            replaced_identity = SimpleNamespace(
                st_mode=actual.st_mode,
                st_file_attributes=0,
                st_dev=actual.st_dev,
                st_ino=actual.st_ino + 1,
            )
            with mock.patch.object(module.os, "name", "nt"), mock.patch.object(
                module.os, "lstat", return_value=replaced_identity
            ):
                with self.assertRaises(module.ConfigReadError) as raised:
                    module._load_config_state(self.config_path)
            self.assertEqual(raised.exception.kind, "file_changed_during_open")
        finally:
            sys.modules.pop(module_spec.name, None)

    def test_config_show_requires_both_safety_flags(self):
        completed = self.run_noictl(
            "config",
            "show",
            "--effective",
            "--json",
            "--config",
            self.config_path,
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["checks"][0]["code"], "CLI_ARGUMENTS_VALID")
        self.assertEqual(completed.stderr, "")
        self.assert_no_sensitive_output(completed.stdout, completed.stderr)

    def test_json_argument_error_never_echoes_unknown_value(self):
        argument_secret = "--TOKEN-ARGUMENT-DO-NOT-ECHO-123456"
        completed = self.run_noictl("doctor", "--json", argument_secret)
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["checks"][0]["code"], "CLI_ARGUMENTS_VALID")
        self.assertNotIn(argument_secret, completed.stdout + completed.stderr)

    def test_malformed_config_shapes_are_schema_errors(self):
        malformed_provider = """  aliyun:
    access_key_id: "${ALIYUN_ACCESS_KEY_ID}"
    access_key_secret: "${ALIYUN_ACCESS_KEY_SECRET}"
    region_id: "${ALIYUN_REGION_ID:-cn-test}"
    instance_id: i-private-do-not-leak
"""
        self.assertIn(malformed_provider, VALID_CONFIG)
        self.config_path.write_text(
            VALID_CONFIG.replace(malformed_provider, "  aliyun: []\n"),
            encoding="utf-8",
        )
        completed = self.run_noictl(
            "config", "validate", "--json", "--config", self.config_path
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["checks"][0]["code"], "CONFIG_VALID")
        self.assertEqual(
            payload["checks"][0]["evidence"]["error_kind"],
            "validation_failed",
        )
        self.assert_no_sensitive_output(completed.stdout, completed.stderr)

    def test_overflowing_config_number_is_a_schema_error(self):
        self.config_path.write_text(
            VALID_CONFIG.replace(
                "  docker_network: seats",
                "  docker_network: seats\n  frame_rate: .inf",
            ),
            encoding="utf-8",
        )
        completed = self.run_noictl(
            "config", "validate", "--json", "--config", self.config_path
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(
            payload["checks"][0]["evidence"]["error_kind"],
            "validation_failed",
        )

    def test_invalid_implicit_timestamp_is_invalid_yaml(self):
        self.config_path.write_text(
            VALID_CONFIG.replace("provider: aliyun", "provider: 2026-99-99"),
            encoding="utf-8",
        )
        completed = self.run_noictl(
            "config", "validate", "--json", "--config", self.config_path
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(
            payload["checks"][0]["evidence"]["error_kind"], "invalid_yaml"
        )

    def test_secret_guard_compares_leaf_values_not_public_keys(self):
        module_spec = importlib.util.spec_from_file_location(
            "noictl_guard_test", NOICTL
        )
        self.assertIsNotNone(module_spec)
        self.assertIsNotNone(module_spec.loader)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
            module._assert_no_secret_leak({"system": "Windows"}, {"system"})
            with self.assertRaises(module.UnsafeOutputError):
                module._assert_no_secret_leak(
                    {"safe_key": "secret-value-123456"},
                    {"secret-value-123456"},
                )
        finally:
            sys.modules.pop(module_spec.name, None)

    def test_emit_survives_ascii_only_redirected_stream(self):
        module_spec = importlib.util.spec_from_file_location(
            "noictl_encoding_test", NOICTL
        )
        self.assertIsNotNone(module_spec)
        self.assertIsNotNone(module_spec.loader)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
            result = module._base_result("doctor", "error", "中文诊断")
            for json_output in (True, False):
                payload = io.BytesIO()
                stream = io.TextIOWrapper(payload, encoding="ascii", errors="strict")
                with redirect_stdout(stream):
                    module._emit(result, json_output)
                stream.flush()
                rendered = payload.getvalue().decode("ascii")
                self.assertNotIn("Traceback", rendered)
                if json_output:
                    self.assertEqual(json.loads(rendered)["summary"], "中文诊断")
        finally:
            sys.modules.pop(module_spec.name, None)

    def test_support_bundle_contains_only_safe_diagnostics_and_hashes(self):
        output_name = "EXPLICIT-PATH-MUST-NOT-ECHO.json"
        output_path = self.directory / output_name
        completed = self.run_noictl(
            "support-bundle",
            "--json",
            "--config",
            self.config_path,
            "--output",
            output_name,
            cwd=self.directory,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(output_path.is_file())
        self.assertNotIn(output_name, completed.stdout)
        bundle_text = output_path.read_text(encoding="utf-8")
        bundle = json.loads(bundle_text)
        self.assertEqual(bundle["bundle_type"], "noictl-read-only-support")
        self.assertIsNone(bundle["tool"]["script_sha256"])
        self.assertEqual(
            bundle["tool"]["script_hash_status"],
            "omitted_no_runtime_reread",
        )
        self.assertEqual(bundle["collection"]["network_probes"], 0)
        self.assertEqual(
            bundle["collection"]["referenced_secret_files_read"], 0
        )
        self.assertNotIn("effective_config", bundle_text)
        self.assertIsNone(bundle["configuration"]["file"]["sha256"])
        self.assertFalse(bundle["configuration"]["file"]["metadata_collected"])
        self.assertIsNone(bundle["configuration"]["file"]["size_bytes"])
        self.assertNotIn(
            hashlib.sha256(self.config_path.read_bytes()).hexdigest(), bundle_text
        )
        self.assertEqual(
            bundle["configuration"]["file"]["hash_status"],
            "omitted_secret_bearing_input",
        )
        self.assertRegex(
            bundle["configuration"]["redacted_effective_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            result["actions"][0]["sha256"],
            hashlib.sha256(output_path.read_bytes()).hexdigest(),
        )
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(output_path.stat().st_mode), 0o600)
        self.assert_no_sensitive_output(
            completed.stdout, completed.stderr, bundle_text
        )

    def test_support_bundle_never_overwrites_existing_file(self):
        output_path = self.directory / "existing.json"
        output_path.write_text("keep-me", encoding="utf-8")
        completed = self.run_noictl(
            "support-bundle",
            "--json",
            "--config",
            self.config_path,
            "--output",
            output_path.name,
            cwd=self.directory,
        )
        self.assertEqual(completed.returncode, 4)
        self.assertEqual(output_path.read_text(encoding="utf-8"), "keep-me")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertFalse(payload["checks"][0]["evidence"]["file_written"])
        self.assert_no_sensitive_output(completed.stdout, completed.stderr)

    def test_support_bundle_rejects_paths_and_windows_ads_names(self):
        for unsafe_name in (
            str(self.directory / "outside.json"),
            "nested/output.json",
            "existing.json:stream",
            "CON.json",
        ):
            completed = self.run_noictl(
                "support-bundle",
                "--json",
                "--config",
                self.config_path,
                "--output",
                unsafe_name,
                cwd=self.directory,
            )
            self.assertEqual(completed.returncode, 2)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                payload["checks"][0]["code"],
                "SUPPORT_BUNDLE_OUTPUT_NAME_SAFE",
            )
            self.assertNotIn(unsafe_name, completed.stdout + completed.stderr)
            self.assert_no_sensitive_output(completed.stdout, completed.stderr)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires O_NOFOLLOW")
    def test_support_bundle_refuses_symlink_config_without_fingerprinting_target(self):
        target = self.directory / "secret-target.yaml"
        secret_bytes = b"PRIVATE-KEY-CONTENT-DO-NOT-READ-123456\n"
        target.write_bytes(secret_bytes)
        link = self.directory / "linked-config.yaml"
        link.symlink_to(target)
        output_path = self.directory / "symlink-support.json"
        completed = self.run_noictl(
            "support-bundle",
            "--json",
            "--config",
            link,
            "--output",
            output_path.name,
            cwd=self.directory,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        bundle_text = output_path.read_text(encoding="utf-8")
        bundle = json.loads(bundle_text)
        self.assertFalse(bundle["configuration"]["file"]["metadata_collected"])
        self.assertNotIn(hashlib.sha256(secret_bytes).hexdigest(), bundle_text)
        self.assertNotIn(secret_bytes.decode().strip(), bundle_text)

    def test_support_bundle_can_report_invalid_config_without_leaking_it(self):
        invalid_value = "INVALID-PROVIDER-DO-NOT-LEAK-123456"
        self.config_path.write_text(
            VALID_CONFIG.replace("provider: aliyun", f"provider: {invalid_value}"),
            encoding="utf-8",
        )
        output_path = self.directory / "invalid-config-support.json"
        completed = self.run_noictl(
            "support-bundle",
            "--json",
            "--config",
            self.config_path,
            "--output",
            output_path.name,
            cwd=self.directory,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        bundle_text = output_path.read_text(encoding="utf-8")
        bundle = json.loads(bundle_text)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(bundle["diagnostics"]["doctor"]["status"], "error")
        self.assertFalse(bundle["configuration"]["file"]["metadata_collected"])
        self.assertIsNone(bundle["configuration"]["file"]["sha256"])
        self.assertIsNone(bundle["configuration"]["file"]["size_bytes"])
        self.assertNotIn(invalid_value, completed.stdout + bundle_text)
        self.assert_no_sensitive_output(
            completed.stdout, completed.stderr, bundle_text
        )

    def test_support_bundle_default_creates_one_timestamped_local_file(self):
        completed = self.run_noictl(
            "support-bundle",
            "--json",
            "--config",
            self.config_path,
            cwd=self.directory,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        bundles = list(self.directory.glob("noictl-support-*.json"))
        self.assertEqual(len(bundles), 1)
        result = json.loads(completed.stdout)
        self.assertEqual(result["actions"][0]["name"], bundles[0].name)
        self.assertEqual(
            result["actions"][0]["target"], "default-current-directory"
        )
        self.assert_no_sensitive_output(
            completed.stdout,
            completed.stderr,
            bundles[0].read_text(encoding="utf-8"),
        )

    def test_cli_imports_no_network_service_or_process_clients(self):
        tree = ast.parse(NOICTL.read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
        self.assertTrue(
            imports.isdisjoint(
                {
                    "paramiko",
                    "pymongo",
                    "requests",
                    "socket",
                    "subprocess",
                    "urllib",
                }
            ),
            imports,
        )


if __name__ == "__main__":
    unittest.main()
