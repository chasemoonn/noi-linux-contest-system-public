import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from services.config import load_config


CONFIG = """
cloud:
  provider: aliyun
  aliyun:
    access_key_id: "${TEST_ACCESS_KEY}"
    access_key_secret: secret
    region_id: cn-test
    instance_id: i-test
contest_server:
  ssh_user: ubuntu
  ssh_key: /keys/key
  known_hosts: /keys/known_hosts
  strict_host_key: true
  seats_root: /data/seats
  docker_image: noi-linux-sim:latest
  docker_network: seats
hydro:
  public_base_url: https://example.test
  internal_base_url: http://127.0.0.1:8888
  mongo_uri: mongodb://127.0.0.1/hydro
  domain_id: system
  submit_enabled: true
  orchestrator_token: 12345678901234567890123456789012
orchestrator:
  admin_password: 1234567890123456
  db: /data/state.db
  collected_dir: /data/collected
"""


class ConfigTests(unittest.TestCase):
    def test_environment_expansion_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(CONFIG, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                cfg = load_config(path)
        self.assertEqual(cfg["cloud"]["aliyun"]["access_key_id"], "key-id")

    def test_missing_environment_variable_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(CONFIG, encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "TEST_ACCESS_KEY"):
                    load_config(path)

    def test_pinned_fingerprint_can_replace_known_hosts(self):
        pinned = CONFIG.replace(
            "  known_hosts: /keys/known_hosts\n",
            "  host_key_sha256: SHA256:" + "A" * 43 + "\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(pinned, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                cfg = load_config(path)
        self.assertNotIn("known_hosts", cfg["contest_server"])
        self.assertTrue(cfg["contest_server"]["host_key_sha256"].startswith("SHA256:"))

    def test_invalid_pinned_fingerprint_fails(self):
        invalid = CONFIG.replace(
            "  known_hosts: /keys/known_hosts\n",
            "  host_key_sha256: SHA256:not-valid\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(invalid, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                with self.assertRaisesRegex(ValueError, "host_key_sha256"):
                    load_config(path)

    def test_invalid_desktop_resolution_fails(self):
        invalid = CONFIG.replace(
            "  docker_network: seats\n",
            "  docker_network: seats\n  resolution: fullscreen\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(invalid, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                with self.assertRaisesRegex(ValueError, "resolution"):
                    load_config(path)

    def test_invalid_remote_desktop_tuning_fails(self):
        for key, value in (
            ("frame_rate", "120"),
            ("no_vnc_quality", "10"),
            ("no_vnc_compression", "fast"),
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                invalid = CONFIG.replace(
                    "  docker_network: seats\n",
                    f"  docker_network: seats\n  {key}: {value}\n",
                )
                path = Path(directory) / "config.yaml"
                path.write_text(invalid, encoding="utf-8")
                with patch.dict(
                    os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False
                ):
                    with self.assertRaisesRegex(ValueError, key):
                        load_config(path)

    def test_direct_eip_http_config_accepts_public_student_cidr(self):
        direct = CONFIG.replace(
            "    instance_id: i-test\n",
            "    instance_id: i-test\n"
            "    desktop_access:\n"
            "      enabled: true\n"
            "      security_group_id: sg-test123\n"
            "      source_cidr: 0.0.0.0/0\n"
            "      management_source_cidrs: [203.0.113.7/32]\n"
            "      port: 80\n"
            "      priority: 20\n",
        ).replace(
            "  docker_network: seats\n",
            "  docker_network: seats\n"
            "  gateway_listen: 80\n"
            "  gateway_scheme: http\n"
            "  gateway_public_base_url: http://198.51.100.10\n",
        ).replace(
            "  submit_enabled: true\n",
            "  submit_enabled: true\n"
            "  notify_enabled: true\n"
            "  notify_allowed_https_hosts: [exam.example.test]\n",
        ).replace(
            "  collected_dir: /data/collected\n",
            "  collected_dir: /data/collected\n"
            "  public_base_url: https://exam.example.test\n",
        ) + """
frontend_proxy:
  provider: caddy
  domain: exam.example.test
  snippet_path: /data/caddy-exam.conf
  caddyfile_path: /data/Caddyfile
  admin_url: http://127.0.0.1:2019
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(direct, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                cfg = load_config(path)

        self.assertTrue(cfg["cloud"]["aliyun"]["desktop_access"]["enabled"])
        self.assertEqual(
            cfg["contest_server"]["gateway_public_base_url"],
            "http://198.51.100.10",
        )

    def test_direct_access_rejects_unverifiable_null_frontend(self):
        direct = CONFIG.replace(
            "    instance_id: i-test\n",
            "    instance_id: i-test\n"
            "    desktop_access:\n"
            "      enabled: true\n"
            "      security_group_id: sg-test123\n"
            "      source_cidr: 0.0.0.0/0\n"
            "      management_source_cidrs: [203.0.113.7/32]\n"
            "      port: 80\n",
        ).replace(
            "  docker_network: seats\n",
            "  docker_network: seats\n  gateway_listen: 80\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(direct, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                with self.assertRaisesRegex(ValueError, "provider=caddy"):
                    load_config(path)

    def test_direct_eip_notification_requires_allowlisted_https_redirect(self):
        direct = CONFIG.replace(
            "    instance_id: i-test\n",
            "    instance_id: i-test\n"
            "    desktop_access:\n"
            "      enabled: true\n"
            "      security_group_id: sg-test123\n"
            "      source_cidr: 0.0.0.0/0\n"
            "      management_source_cidrs: [203.0.113.7/32]\n"
            "      port: 80\n",
        ).replace(
            "  docker_network: seats\n",
            "  docker_network: seats\n"
            "  gateway_listen: 80\n"
            "  gateway_scheme: http\n",
        ).replace(
            "  submit_enabled: true\n",
            "  submit_enabled: true\n"
            "  notify_enabled: true\n"
            "  notify_allowed_https_hosts: [exam.example.test]\n",
        ).replace(
            "  collected_dir: /data/collected\n",
            "  collected_dir: /data/collected\n"
            "  public_base_url: https://wrong.example.test\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(direct, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                with self.assertRaisesRegex(ValueError, "HTTPS 根域名"):
                    load_config(path)

    def test_direct_access_rejects_domain_https_and_port_drift(self):
        base = CONFIG.replace(
            "    instance_id: i-test\n",
            "    instance_id: i-test\n"
            "    desktop_access:\n"
            "      enabled: true\n"
            "      security_group_id: sg-test123\n"
            "      source_cidr: 0.0.0.0/0\n"
            "      management_source_cidrs: [203.0.113.7/32]\n"
            "      port: 80\n",
        )
        cases = (
            (
                "  gateway_public_base_url: https://desktop.example.test\n",
                "必须使用 http",
            ),
            (
                "  gateway_public_base_url: http://desktop.example.test\n",
                "必须使用 IP",
            ),
            (
                "  gateway_public_base_url: http://198.51.100.10:8080\n",
                "端口必须省略或为 80",
            ),
            ("  gateway_listen: 8080\n", "gateway_listen"),
        )
        for insertion, expected in cases:
            with self.subTest(insertion=insertion), tempfile.TemporaryDirectory() as directory:
                invalid = base.replace(
                    "  docker_network: seats\n",
                    "  docker_network: seats\n" + insertion,
                )
                path = Path(directory) / "config.yaml"
                path.write_text(invalid, encoding="utf-8")
                with patch.dict(
                    os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False
                ):
                    with self.assertRaisesRegex(ValueError, expected):
                        load_config(path)

    def test_direct_access_requires_oj_management_host_as_ipv4_32(self):
        invalid = CONFIG.replace(
            "    instance_id: i-test\n",
            "    instance_id: i-test\n"
            "    desktop_access:\n"
            "      enabled: true\n"
            "      security_group_id: sg-test123\n"
            "      source_cidr: 0.0.0.0/0\n"
            "      management_source_cidrs: [203.0.113.0/24]\n"
            "      port: 80\n",
        ).replace(
            "  docker_network: seats\n",
            "  docker_network: seats\n  gateway_listen: 80\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(invalid, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                with self.assertRaisesRegex(ValueError, "IPv4 /32"):
                    load_config(path)

    def test_direct_access_requires_exactly_one_oj_management_host(self):
        invalid = CONFIG.replace(
            "    instance_id: i-test\n",
            "    instance_id: i-test\n"
            "    desktop_access:\n"
            "      enabled: true\n"
            "      security_group_id: sg-test123\n"
            "      source_cidr: 0.0.0.0/0\n"
            "      management_source_cidrs: "
            "[203.0.113.7/32, 203.0.113.8/32]\n"
            "      port: 80\n",
        ).replace(
            "  docker_network: seats\n",
            "  docker_network: seats\n  gateway_listen: 80\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(invalid, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                with self.assertRaisesRegex(ValueError, "必须且只能"):
                    load_config(path)

    def test_direct_access_student_source_cannot_equal_oj_management_host(self):
        invalid = CONFIG.replace(
            "    instance_id: i-test\n",
            "    instance_id: i-test\n"
            "    desktop_access:\n"
            "      enabled: true\n"
            "      security_group_id: sg-test123\n"
            "      source_cidr: 203.0.113.7/32\n"
            "      management_source_cidrs: [203.0.113.7/32]\n"
            "      port: 80\n",
        ).replace(
            "  docker_network: seats\n",
            "  docker_network: seats\n  gateway_listen: 80\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(invalid, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                with self.assertRaisesRegex(ValueError, "不能与 OJ"):
                    load_config(path)

    def test_invalid_realtime_judge_tuning_fails(self):
        for key, value in (
            ("realtime_judge_lease_seconds", "20"),
            ("realtime_judge_idle_seconds", "0"),
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                invalid = CONFIG.replace(
                    "  collected_dir: /data/collected\n",
                    f"  collected_dir: /data/collected\n  {key}: {value}\n",
                )
                path = Path(directory) / "config.yaml"
                path.write_text(invalid, encoding="utf-8")
                with patch.dict(
                    os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False
                ):
                    with self.assertRaisesRegex(ValueError, key):
                        load_config(path)

    def test_artifact_generation_config_is_strict_and_optional(self):
        enabled = CONFIG + """
artifact_generation:
  enabled: true
  ai:
    endpoint: https://ai.example.test/v1/chat/completions
    api_key_env: TEST_ARTIFACT_AI_KEY
    model: fixture-model
  tools:
    approved_roots: [/opt/noi-artifact-tools]
    validators:
      apple:
        executable: /opt/noi-artifact-tools/apple-validator
    oracles:
      apple:
        executable: /opt/noi-artifact-tools/apple-oracle
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(enabled, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                cfg = load_config(path)
        self.assertTrue(cfg["artifact_generation"]["enabled"])

        broken = enabled.replace(
            "    oracles:\n      apple:\n        executable: /opt/noi-artifact-tools/apple-oracle\n",
            "",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(broken, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                with self.assertRaisesRegex(ValueError, "oracles"):
                    load_config(path)

    def test_seat_pool_formal_and_total_limits_are_both_validated(self):
        cases = (
            ("seat_pool_maximum: 31", "seat_pool_maximum"),
            ("seat_pool_total_maximum: 41", "seat_pool_total_maximum"),
            (
                "seat_pool_maximum: 15\n  seat_pool_total_maximum: 16\n"
                "  default_max_participants: 15\n  default_spare_seats: 2",
                r"default_max_participants \+ default_spare_seats",
            ),
        )
        for settings, expected in cases:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                invalid = CONFIG.replace(
                    "  collected_dir: /data/collected\n",
                    "  collected_dir: /data/collected\n  " + settings + "\n",
                )
                path = Path(directory) / "config.yaml"
                path.write_text(invalid, encoding="utf-8")
                with patch.dict(os.environ, {"TEST_ACCESS_KEY": "key-id"}, clear=False):
                    with self.assertRaisesRegex(ValueError, expected):
                        load_config(path)


if __name__ == "__main__":
    unittest.main()
