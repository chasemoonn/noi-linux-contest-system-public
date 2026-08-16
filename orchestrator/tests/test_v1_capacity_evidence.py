import copy
import importlib.util
import json
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_v1_capacity_evidence.py"
SPEC = importlib.util.spec_from_file_location("verify_v1_capacity_evidence", SCRIPT)
capacity = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(capacity)


def valid_evidence():
    artifact_names = sorted(capacity.ARTIFACT_NAMES)
    evidence = {
        "$schema": "v1-capacity-evidence.schema.json",
        "schema_version": 1,
        "status": "passed",
        "session_id": "1" * 64,
        "source": {"revision": "a" * 40, "tree": "b" * 40},
        "components": {
            "orchestrator_image_digest": "sha256:" + "2" * 64,
            "desktop_image_id": "sha256:" + "3" * 64,
            "desktop_source_revision": "a" * 40,
            "hydro_plugin_sha256": "4" * 64,
        },
        "environment": {
            "profile": capacity.PROFILE,
            "instance_type": "ecs.g8i.4xlarge",
            "region": "cn-hangzhou",
            "network_profile_sha256": "5" * 64,
        },
        "probes": {kind: "8" * 64 for kind in capacity.PROBE_KINDS},
        "window": {
            "started_at": "2026-08-12T00:00:00Z",
            "ended_at": "2026-08-12T01:00:00Z",
            "duration_seconds": 3600,
            "sample_interval_seconds": 10,
            "sample_count": 361,
        },
        "seats": {
            "formal": 15,
            "spare": 2,
            "verified": 17,
            "unique_container_ids": 17,
            "unexpected_restart_events": 0,
            "cross_seat_access_failures": 0,
        },
        "workload": {
            "login_successes": 15,
            "material_open_successes": 15,
            "compile_successes": 45,
            "submission_successes": 45,
            "failed_submissions": 0,
            "collection_successes": 15,
            "failed_collections": 0,
            "final_source_mismatches": 0,
        },
        "faults": {
            "spare_takeovers": 1,
            "spare_takeovers_recovered": 1,
            "planned_restart_events": 1,
            "planned_restart_recoveries": 1,
            "controller_network_interruptions": 1,
            "controller_network_recoveries": 1,
        },
        "isolation": {
            "ordinary_oj_errors": 0,
            "ordinary_oj_restarts": 0,
            "ordinary_oj_pid_changes": 0,
            "credential_leaks": 0,
            "result_leaks": 0,
        },
        "shutdown": {
            "active_seats": 0,
            "managed_rules": 0,
            "conflict_rules": 0,
            "cloud_state": "STOPPED",
            "delivery_queues": 0,
            "notification_queues": 0,
        },
        "metrics": {
            "host_cpu_peak_percent": 70.0,
            "host_memory_peak_percent": 75.0,
            "container_memory_peak_bytes": 2147483648,
            "egress_peak_mbps": 80.0,
            "rtt_p95_ms": 50.0,
            "packet_loss_percent": 0.1,
            "websocket_reconnects": 1,
            "key_to_frame_p50_ms": 90.0,
            "key_to_frame_p95_ms": 160.0,
        },
        "thresholds": {
            "host_cpu_peak_percent_max": 85.0,
            "host_memory_peak_percent_max": 85.0,
            "container_memory_peak_bytes_max": 3221225472,
            "egress_peak_mbps_max": 100.0,
            "rtt_p95_ms_max": 100.0,
            "packet_loss_percent_max": 1.0,
            "websocket_reconnects_max": 3,
            "key_to_frame_p95_ms_max": 250.0,
            "thresholds_sha256": "0" * 64,
            "capacity_margin_accepted": True,
        },
        "artifacts": [
            {
                "name": name,
                "reference": f"raw/{name}.jsonl",
                "sha256": "7" * 64,
                "bytes": 10,
            }
            for name in artifact_names
        ],
    }
    evidence["thresholds"]["thresholds_sha256"] = capacity.threshold_policy_sha256(
        evidence["thresholds"]
    )
    return evidence


def artifact_payloads(evidence):
    session_id = evidence["session_id"]
    window = evidence["window"]
    samples = []
    for index in range(window["sample_count"]):
        second = index * window["sample_interval_seconds"]
        hour, remainder = divmod(second, 3600)
        minute, sec = divmod(remainder, 60)
        observed_at = f"2026-08-12T{hour:02d}:{minute:02d}:{sec:02d}Z"
        sample = {
            "observed_at": observed_at,
            "telemetry": {"sequence": index + 1, "sha256": f"{index + 1:064x}"},
            "ordinary_oj": {
                "schema_version": 1, "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
                "sequence": index + 1, "observed_at": observed_at,
                "homepage_status": 200, "login_status": 200, "prep_health_ok": True,
                "prep_database_ok": True, "ordinary_oj_errors": 0,
                "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0,
                "credential_leaks": 0, "result_leaks": 0,
                "pm2_fingerprint_sha256": "9" * 64,
                "sha256": f"{index + 100000:064x}",
            },
            **evidence["metrics"],
        }
        sample["websocket_reconnects"] = 1 if index == 0 else 0
        samples.append(sample)
    seats = evidence["seats"]
    formal = [f"{index:064x}" for index in range(1, 16)]
    spare = [f"{index:064x}" for index in range(16, 18)]
    return {
        "sample_series": {
            "schema_version": 1,
            "kind": "sample_series",
            "session_id": session_id,
            "source": evidence["source"],
            "components": evidence["components"],
            "environment": evidence["environment"],
            "thresholds": evidence["thresholds"],
            "started_at": window["started_at"],
            "ended_at": window["ended_at"],
            "sample_interval_seconds": window["sample_interval_seconds"],
            "measurement_probe_sha256": "8" * 64,
            "samples": samples,
        },
        "seat_inventory": {
            "schema_version": 1,
            "kind": "seat_inventory",
            "session_id": session_id,
            "observed_at": "2026-08-12T01:00:00Z",
            "collector": {"mode": "trusted_probe", "probe_sha256": "8" * 64},
            "formal_container_ids": formal,
            "spare_container_ids": spare,
            "verified_container_ids": formal + spare,
            "unexpected_restart_events": seats["unexpected_restart_events"],
            "planned_restart_events": evidence["faults"]["planned_restart_events"],
            "planned_restart_recoveries": evidence["faults"]["planned_restart_recoveries"],
            "cross_seat_access_failures": seats["cross_seat_access_failures"],
        },
        "workload_events": {
            "schema_version": 1,
            "kind": "workload_events",
            "session_id": session_id,
            "observed_at": "2026-08-12T01:00:00Z",
            "collector": {"mode": "trusted_probe", "probe_sha256": "8" * 64},
            **evidence["workload"],
        },
        "fault_events": {
            "schema_version": 1,
            "kind": "fault_events",
            "session_id": session_id,
            "observed_at": "2026-08-12T01:00:00Z",
            "collector": {"mode": "trusted_probe", "probe_sha256": "8" * 64},
            **evidence["faults"],
        },
        "ordinary_oj_observations": {
            "schema_version": 1,
            "kind": "ordinary_oj_observations",
            "session_id": session_id,
            "observed_at": "2026-08-12T01:00:00Z",
            "collector": {"mode": "trusted_probe", "probe_sha256": "8" * 64},
            **evidence["isolation"],
        },
        "shutdown_observation": {
            "schema_version": 1,
            "kind": "shutdown_observation",
            "session_id": session_id,
            "observed_at": "2026-08-12T01:00:00Z",
            "collector": {"mode": "trusted_probe", "probe_sha256": "8" * 64},
            **evidence["shutdown"],
        },
    }


class V1CapacityEvidenceTests(unittest.TestCase):
    def test_exact_capacity_evidence_passes(self):
        evidence = valid_evidence()
        validated = capacity.validate_capacity_evidence(
            evidence,
            expected_revision="a" * 40,
            expected_components=evidence["components"],
        )
        self.assertEqual(validated, evidence)
        self.assertEqual(capacity.capacity_summary(evidence)["verified_seats"], 17)

    def test_sparse_samples_fail(self):
        evidence = valid_evidence()
        evidence["window"]["sample_count"] = 360
        with self.assertRaisesRegex(capacity.EvidenceError, "too sparse"):
            capacity.validate_capacity_evidence(evidence)

    def test_planned_restart_requires_matching_recovery(self):
        evidence = valid_evidence()
        evidence["faults"]["planned_restart_events"] = 2
        with self.assertRaisesRegex(capacity.EvidenceError, "must equal"):
            capacity.validate_capacity_evidence(evidence)

    def test_unexpected_restart_is_rejected(self):
        evidence = valid_evidence()
        evidence["seats"]["unexpected_restart_events"] = 1
        with self.assertRaisesRegex(capacity.EvidenceError, "must be zero"):
            capacity.validate_capacity_evidence(evidence)

    def test_metric_over_threshold_fails(self):
        evidence = valid_evidence()
        evidence["metrics"]["key_to_frame_p95_ms"] = 251.0
        with self.assertRaisesRegex(capacity.EvidenceError, "exceeds"):
            capacity.validate_capacity_evidence(evidence)

    def test_non_finite_metric_fails(self):
        evidence = valid_evidence()
        evidence["metrics"]["rtt_p95_ms"] = float("nan")
        with self.assertRaisesRegex(capacity.EvidenceError, "must be >="):
            capacity.validate_capacity_evidence(evidence)

    def test_component_drift_fails(self):
        evidence = valid_evidence()
        expected = copy.deepcopy(evidence["components"])
        expected["desktop_image_id"] = "sha256:" + "8" * 64
        with self.assertRaisesRegex(capacity.EvidenceError, "components differ"):
            capacity.validate_capacity_evidence(
                evidence, expected_revision="a" * 40, expected_components=expected
            )

    def test_tree_drift_fails(self):
        evidence = valid_evidence()
        with self.assertRaisesRegex(capacity.EvidenceError, "tree differs"):
            capacity.validate_capacity_evidence(evidence, expected_tree="c" * 40)

    def test_raw_artifacts_are_hash_checked_when_root_is_supplied(self):
        evidence = valid_evidence()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payloads = artifact_payloads(evidence)
            for row in evidence["artifacts"]:
                path = root / Path(*Path(row["reference"]).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(payloads[row["name"]], sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                row["bytes"] = path.stat().st_size
                row["sha256"] = capacity.sha256_file(path)
            capacity.validate_capacity_evidence(evidence, artifact_root=root)
            first = root / Path(*Path(evidence["artifacts"][0]["reference"]).parts)
            first.write_bytes(b"tampered!!")
            with self.assertRaisesRegex(capacity.EvidenceError, "bytes or SHA256 differ"):
                capacity.validate_capacity_evidence(evidence, artifact_root=root)

    def test_replayed_browser_telemetry_is_rejected(self):
        evidence = valid_evidence()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payloads = artifact_payloads(evidence)
            payloads["sample_series"]["samples"][1]["telemetry"] = dict(
                payloads["sample_series"]["samples"][0]["telemetry"]
            )
            for row in evidence["artifacts"]:
                path = root / Path(*Path(row["reference"]).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(payloads[row["name"]], sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                row["bytes"] = path.stat().st_size
                row["sha256"] = capacity.sha256_file(path)
            with self.assertRaisesRegex(capacity.EvidenceError, "replayed"):
                capacity.validate_capacity_evidence(evidence, artifact_root=root)

    def test_replayed_ordinary_oj_telemetry_or_pid_change_is_rejected(self):
        evidence = valid_evidence()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payloads = artifact_payloads(evidence)
            payloads["sample_series"]["samples"][1]["ordinary_oj"] = dict(
                payloads["sample_series"]["samples"][0]["ordinary_oj"]
            )
            for row in evidence["artifacts"]:
                path = root / Path(*Path(row["reference"]).parts); path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payloads[row["name"]], sort_keys=True) + "\n")
                row["bytes"] = path.stat().st_size; row["sha256"] = capacity.sha256_file(path)
            with self.assertRaisesRegex(capacity.EvidenceError, "ordinary OJ telemetry was replayed"):
                capacity.validate_capacity_evidence(evidence, artifact_root=root)

    def test_private_summary_cannot_disagree_with_combined_evidence(self):
        evidence = valid_evidence()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payloads = artifact_payloads(evidence)
            payloads["workload_events"]["submission_successes"] = 46
            for row in evidence["artifacts"]:
                path = root / Path(*Path(row["reference"]).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(payloads[row["name"]], sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                row["bytes"] = path.stat().st_size
                row["sha256"] = capacity.sha256_file(path)
            with self.assertRaisesRegex(capacity.EvidenceError, "differs"):
                capacity.validate_capacity_evidence(evidence, artifact_root=root)

    def test_stale_closeout_fact_fails(self):
        evidence = valid_evidence()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payloads = artifact_payloads(evidence)
            payloads["shutdown_observation"]["observed_at"] = "2026-08-12T01:45:01Z"
            for row in evidence["artifacts"]:
                path = root / Path(*Path(row["reference"]).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(payloads[row["name"]], sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                row["bytes"] = path.stat().st_size
                row["sha256"] = capacity.sha256_file(path)
            with self.assertRaisesRegex(capacity.EvidenceError, "within 2700 seconds"):
                capacity.validate_capacity_evidence(evidence, artifact_root=root)

    def test_seat_inventory_must_precede_collection_closeout(self):
        evidence = valid_evidence()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payloads = artifact_payloads(evidence)
            payloads["seat_inventory"]["observed_at"] = "2026-08-12T01:05:01Z"
            for row in evidence["artifacts"]:
                path = root / Path(*Path(row["reference"]).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(payloads[row["name"]], sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                row["bytes"] = path.stat().st_size
                row["sha256"] = capacity.sha256_file(path)
            with self.assertRaisesRegex(capacity.EvidenceError, "within 300 seconds"):
                capacity.validate_capacity_evidence(evidence, artifact_root=root)

    def test_schema_is_valid_json(self):
        schema = json.loads(
            (ROOT / "release" / "v1-capacity-evidence.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["seats"]["properties"]["formal"], {"const": 15})


if __name__ == "__main__":
    unittest.main()
