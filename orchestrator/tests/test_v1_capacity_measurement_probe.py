import importlib.util
from datetime import datetime, timezone
import base64
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
PROBE_PATH = ROOT / "scripts" / "v1_capacity_measurement_probe.py"
BUILDER_PATH = ROOT / "scripts" / "build_v1_capacity_probe.py"

probe_spec = importlib.util.spec_from_file_location("v1_capacity_measurement_probe", PROBE_PATH)
probe = importlib.util.module_from_spec(probe_spec)
assert probe_spec.loader is not None
probe_spec.loader.exec_module(probe)

builder_spec = importlib.util.spec_from_file_location("build_v1_capacity_probe", BUILDER_PATH)
builder = importlib.util.module_from_spec(builder_spec)
assert builder_spec.loader is not None
builder_spec.loader.exec_module(builder)


def valid_config():
    return {
        "schema_version": 1,
        "container_ids": [f"{index:064x}" for index in range(1, 18)],
        "network_interface": "eth0",
        "docker_socket": "/var/run/docker.sock",
        "telemetry_envelope": "/root/noi-v1-browser/telemetry-envelope.json",
        "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
        "telemetry_transport_profile": "direct_http",
        "telemetry_seat_set_sha256": "a" * 64,
        "telemetry_signer": "hangzhou-browser-agent",
        "telemetry_public_key": "ssh-ed25519 " + "A" * 68,
        "ssh_keygen_path": "/usr/bin/ssh-keygen",
        "measurement_seconds": 2,
        "telemetry_max_age_seconds": 30,
        "telemetry_samples_min": 5,
        "ordinary_oj_envelope": "/root/noi-v1-ordinary/envelope.json",
        "ordinary_oj_signer": "ordinary-oj-agent",
        "ordinary_oj_public_key": "ssh-ed25519 " + "B" * 68,
        "ordinary_oj_pm2_fingerprint_sha256": "c" * 64,
        "ordinary_oj_max_age_seconds": 30,
    }


class CapacityMeasurementProbeTests(unittest.TestCase):
    def test_configuration_binds_exactly_seventeen_unique_containers(self):
        value = valid_config()
        value["container_ids"] = value["container_ids"][:-1]
        with self.assertRaisesRegex(probe.ProbeError, "17 unique"):
            probe.validate_config(value)

    def test_configuration_rejects_mutable_or_implicit_inputs(self):
        value = valid_config()
        value["docker_socket"] = "/tmp/docker.sock"
        with self.assertRaisesRegex(probe.ProbeError, "canonical Docker socket"):
            probe.validate_config(value)

    def test_percentile_uses_nearest_rank(self):
        self.assertEqual(probe.percentile([1, 9, 3, 7, 5], 0.50), 5)
        self.assertEqual(probe.percentile([1, 9, 3, 7, 5], 0.95), 9)

    def test_signed_envelope_is_bound_to_the_qualification_marker(self):
        value = valid_config()
        payload = {
            "schema_version": 1,
            "qualification_marker": value["qualification_marker"],
            "seat_set_sha256": value["telemetry_seat_set_sha256"],
            "transport_profile": value["telemetry_transport_profile"],
            "formal_seat_count": 15,
            "sequence": 7,
            "window_started_at": "2026-08-13T00:00:00Z",
            "observed_at": "2026-08-13T00:00:05Z",
            "rtt_samples_ms": [10, 11, 12, 13, 14],
            "packet_loss_percent": 0,
            "websocket_reconnects": 0,
            "key_to_frame_samples_ms": [20, 21, 22, 23, 24],
        }
        envelope = {
            "namespace": probe.NAMESPACE,
            "payload": payload,
            "schema_version": 1,
            "signature_base64": base64.b64encode(b"s" * 64).decode(),
            "signer": value["telemetry_signer"],
        }
        raw = probe.canonical_json(envelope)
        with (
            mock.patch.object(probe, "read_private_file", return_value=raw),
            mock.patch.object(probe, "require_root_binary", return_value=Path("/usr/bin/ssh-keygen")),
            mock.patch.object(probe.subprocess, "run", return_value=mock.Mock(returncode=0)),
        ):
            row, digest = probe.verify_telemetry(
                value, datetime(2026, 8, 13, 0, 0, 10, tzinfo=timezone.utc)
            )
        self.assertEqual(row["qualification_marker"], value["qualification_marker"])
        self.assertEqual(len(digest), 64)

        envelope["payload"]["qualification_marker"] = "NOI-V1-QUAL-FEDCBA0987654321"
        with mock.patch.object(
            probe, "read_private_file", return_value=probe.canonical_json(envelope)
        ):
            with self.assertRaisesRegex(probe.ProbeError, "qualification marker differs"):
                probe.verify_telemetry(
                    value, datetime(2026, 8, 13, 0, 0, 10, tzinfo=timezone.utc)
                )

        envelope["payload"]["qualification_marker"] = value["qualification_marker"]
        envelope["payload"]["seat_set_sha256"] = "b" * 64
        with mock.patch.object(
            probe, "read_private_file", return_value=probe.canonical_json(envelope)
        ):
            with self.assertRaisesRegex(probe.ProbeError, "seat set SHA256 differs"):
                probe.verify_telemetry(
                    value, datetime(2026, 8, 13, 0, 0, 10, tzinfo=timezone.utc)
                )

    def test_signed_ordinary_oj_envelope_is_fresh_and_baseline_bound(self):
        value = valid_config()
        payload = {
            "schema_version": 1, "qualification_marker": value["qualification_marker"],
            "sequence": 9, "observed_at": "2026-08-13T00:00:05Z",
            "homepage_status": 200, "login_status": 200, "prep_health_ok": True,
            "prep_database_ok": True, "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0,
            "ordinary_oj_pid_changes": 0, "credential_leaks": 0, "result_leaks": 0,
            "pm2_fingerprint_sha256": "c" * 64,
        }
        envelope = {"schema_version": 1, "namespace": probe.ORDINARY_OJ_NAMESPACE,
                    "signer": value["ordinary_oj_signer"], "payload": payload,
                    "signature_base64": base64.b64encode(b"s" * 64).decode()}
        with mock.patch.object(probe, "read_private_file", return_value=probe.canonical_json(envelope)), \
                mock.patch.object(probe, "require_root_binary", return_value=Path("/usr/bin/ssh-keygen")), \
                mock.patch.object(probe.subprocess, "run", return_value=mock.Mock(returncode=0)):
            row, digest = probe.verify_ordinary_oj(
                value, datetime(2026, 8, 13, 0, 0, 10, tzinfo=timezone.utc)
            )
        self.assertEqual(row["sequence"], 9)
        self.assertEqual(len(digest), 64)
        envelope["payload"]["ordinary_oj_restarts"] = 1
        with mock.patch.object(probe, "read_private_file", return_value=probe.canonical_json(envelope)):
            with self.assertRaisesRegex(probe.ProbeError, "restarts is non-zero"):
                probe.verify_ordinary_oj(
                    value, datetime(2026, 8, 13, 0, 0, 10, tzinfo=timezone.utc)
                )

    def test_collection_derives_peaks_and_hides_identities(self):
        value = valid_config()
        telemetry = {
            "sequence": 7,
            "seat_set_sha256": value["telemetry_seat_set_sha256"],
            "rtt_samples_ms": [10, 20, 30, 40, 50],
            "packet_loss_percent": 0.2,
            "websocket_reconnects": 1,
            "key_to_frame_samples_ms": [100, 120, 140, 160, 180],
        }
        ordinary = {
            "schema_version": 1, "qualification_marker": value["qualification_marker"],
            "sequence": 11, "observed_at": "2026-08-13T00:00:05Z",
            "homepage_status": 200, "login_status": 200, "prep_health_ok": True,
            "prep_database_ok": True, "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0,
            "ordinary_oj_pid_changes": 0, "credential_leaks": 0, "result_leaks": 0,
            "pm2_fingerprint_sha256": "c" * 64,
        }
        cpu = iter([(100, 20), (200, 30), (300, 35)])
        tx = iter([1_000_000, 2_000_000, 4_000_000])
        memory = iter([50.0, 55.0, 52.0])
        cgroup_values = iter(range(100, 151))
        with (
            mock.patch.object(probe, "verify_telemetry", return_value=(telemetry, "f" * 64)),
            mock.patch.object(probe, "verify_ordinary_oj", return_value=(ordinary, "e" * 64)),
            mock.patch.object(probe, "docker_inspect", side_effect=lambda _socket, cid: int(cid, 16)),
            mock.patch.object(probe, "cgroup_memory_path", side_effect=lambda pid, _cid: Path(f"/cgroup/{pid}")),
            mock.patch.object(probe, "cgroup_memory_bytes", side_effect=lambda _path: next(cgroup_values)),
            mock.patch.object(probe, "cpu_counters", side_effect=lambda: next(cpu)),
            mock.patch.object(probe, "network_tx_bytes", side_effect=lambda _iface: next(tx)),
            mock.patch.object(probe, "memory_percent", side_effect=lambda: next(memory)),
        ):
            result = probe.collect(value, sleep=lambda _seconds: None)
        metrics = result["metrics"]
        self.assertEqual(metrics["host_cpu_peak_percent"], 95.0)
        self.assertEqual(metrics["host_memory_peak_percent"], 55.0)
        self.assertEqual(metrics["container_memory_peak_bytes"], 150)
        self.assertEqual(metrics["egress_peak_mbps"], 16.0)
        self.assertEqual(metrics["rtt_p95_ms"], 50)
        self.assertEqual(metrics["key_to_frame_p50_ms"], 140)
        self.assertEqual(result["telemetry"], {"sequence": 7, "sha256": "f" * 64})
        self.assertEqual(result["ordinary_oj"]["sequence"], 11)
        self.assertEqual(result["ordinary_oj"]["sha256"], "e" * 64)
        self.assertNotIn("container_ids", json.dumps(result))

    def test_builder_embeds_config_and_removes_template_marker(self):
        raw = builder.render(probe.validate_config(valid_config()))
        text = raw.decode("utf-8")
        self.assertNotIn(builder.MARKER, text)
        self.assertIn("hangzhou-browser-agent", text)
        compile(text, "<capacity-probe-test>", "exec")

    def test_builder_rejects_relative_config_path_before_open(self):
        with self.assertRaisesRegex(builder.BuildError, "absolute and canonical"):
            builder.read_private_json(Path("relative-config.json"))


if __name__ == "__main__":
    unittest.main()
