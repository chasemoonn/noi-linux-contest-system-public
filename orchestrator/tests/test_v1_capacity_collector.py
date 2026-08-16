import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "collect_v1_capacity_evidence.py"
SPEC = importlib.util.spec_from_file_location("collect_v1_capacity_evidence", SCRIPT)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(collector)


def identity():
    value = {
        "source": {"revision": "a" * 40, "tree": "b" * 40},
        "components": {
            "orchestrator_image_digest": "sha256:" + "1" * 64,
            "desktop_image_id": "sha256:" + "2" * 64,
            "desktop_source_revision": "a" * 40,
            "hydro_plugin_sha256": "3" * 64,
        },
        "environment": {
            "profile": collector.PROFILE,
            "instance_type": "ecs.g8i.4xlarge",
            "region": "cn-hangzhou",
            "network_profile_sha256": "4" * 64,
        },
        "probes": {kind: "7" * 64 for kind in collector.PROBE_KINDS},
        "thresholds": {
            "host_cpu_peak_percent_max": 85.0,
            "host_memory_peak_percent_max": 85.0,
            "container_memory_peak_bytes_max": 3_221_225_472,
            "egress_peak_mbps_max": 100.0,
            "rtt_p95_ms_max": 100.0,
            "packet_loss_percent_max": 1.0,
            "websocket_reconnects_max": 3,
            "key_to_frame_p95_ms_max": 250.0,
            "thresholds_sha256": "0" * 64,
            "capacity_margin_accepted": True,
        },
    }
    value["thresholds"]["thresholds_sha256"] = collector.threshold_policy_sha256(
        value["thresholds"]
    )
    return value


def measurement(observed_at, reconnects=0):
    sequence = int(observed_at[11:13]) * 3600 + int(observed_at[14:16]) * 60 + int(observed_at[17:19]) + 1
    return {
        "observed_at": observed_at,
        "telemetry": {"sequence": sequence, "sha256": f"{sequence:064x}"},
        "ordinary_oj": {
            "schema_version": 1,
            "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
            "sequence": sequence,
            "observed_at": observed_at,
            "homepage_status": 200,
            "login_status": 200,
            "prep_health_ok": True,
            "prep_database_ok": True,
            "ordinary_oj_errors": 0,
            "ordinary_oj_restarts": 0,
            "ordinary_oj_pid_changes": 0,
            "credential_leaks": 0,
            "result_leaks": 0,
            "pm2_fingerprint_sha256": "d" * 64,
            "sha256": f"{sequence + 100000:064x}",
        },
        "metrics": {
            "host_cpu_peak_percent": 70.0,
            "host_memory_peak_percent": 75.0,
            "container_memory_peak_bytes": 2_147_483_648,
            "egress_peak_mbps": 80.0,
            "rtt_p95_ms": 50.0,
            "packet_loss_percent": 0.1,
            "websocket_reconnects": reconnects,
            "key_to_frame_p50_ms": 90.0,
            "key_to_frame_p95_ms": 160.0,
        },
    }


def fact_payloads():
    observed_at = "2026-08-12T01:00:00Z"
    formal = [f"{index:064x}" for index in range(1, 16)]
    spare = [f"{index:064x}" for index in range(16, 18)]
    return {
        "seat_inventory": {
            "observed_at": observed_at,
            "formal_container_ids": formal,
            "spare_container_ids": spare,
            "verified_container_ids": formal + spare,
            "unexpected_restart_events": 0,
            "planned_restart_events": 1,
            "planned_restart_recoveries": 1,
            "cross_seat_access_failures": 0,
        },
        "workload_events": {
            "observed_at": observed_at,
            "login_successes": 15,
            "material_open_successes": 15,
            "compile_successes": 45,
            "submission_successes": 45,
            "failed_submissions": 0,
            "collection_successes": 15,
            "failed_collections": 0,
            "final_source_mismatches": 0,
        },
        "fault_events": {
            "observed_at": observed_at,
            "spare_takeovers": 1,
            "spare_takeovers_recovered": 1,
            "planned_restart_events": 1,
            "planned_restart_recoveries": 1,
            "controller_network_interruptions": 1,
            "controller_network_recoveries": 1,
        },
        "ordinary_oj_observations": {
            "observed_at": observed_at,
            "ordinary_oj_errors": 0,
            "ordinary_oj_restarts": 0,
            "ordinary_oj_pid_changes": 0,
            "credential_leaks": 0,
            "result_leaks": 0,
        },
        "shutdown_observation": {
            "observed_at": observed_at,
            "active_seats": 0,
            "managed_rules": 0,
            "conflict_rules": 0,
            "cloud_state": "STOPPED",
            "delivery_queues": 0,
            "notification_queues": 0,
        },
    }


class V1CapacityCollectorTests(unittest.TestCase):
    def test_append_only_publish_never_replaces_concurrent_output(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "fact.json"
            with mock.patch.object(
                collector.os,
                "link",
                side_effect=FileExistsError("appeared"),
            ):
                with self.assertRaisesRegex(collector.CollectorError, "appeared concurrently"):
                    collector.write_new_json(target, {"owner": "collector"})
            self.assertFalse(target.exists())

    def test_probe_timestamp_must_belong_to_current_invocation(self):
        stale = {
            "observed_at": "2020-01-01T00:00:00Z",
            "telemetry": {"sequence": 1, "sha256": "1" * 64},
            "ordinary_oj": measurement("2020-01-01T00:00:00Z")["ordinary_oj"],
            "metrics": measurement("2026-08-12T00:00:00Z")["metrics"],
        }
        result = mock.Mock(
            returncode=0,
            stdout=json.dumps(stale),
            stderr="",
        )
        with (
            mock.patch.object(collector, "require_trusted_probe", return_value=Path("/trusted/probe")),
            mock.patch.object(collector, "probe_sha256", return_value="7" * 64),
            mock.patch.object(collector.subprocess, "run", return_value=result),
        ):
            with self.assertRaisesRegex(collector.CollectorError, "not bound"):
                collector.run_json_probe(Path("/trusted/probe"), "measurement")

    def test_session_identity_is_exact(self):
        document = collector.build_session(
            identity(),
            session_id="6" * 64,
            created_at="2026-08-12T00:00:00Z",
            duration_seconds=3600,
            sample_interval_seconds=10,
        )
        self.assertEqual(collector.validate_session(document), document)
        self.assertEqual(document["source"]["tree"], "b" * 40)

    def test_measurement_rejects_non_integral_reconnects(self):
        row = measurement("2026-08-12T00:00:00Z")
        row["metrics"]["websocket_reconnects"] = 0.5
        with self.assertRaisesRegex(collector.CollectorError, "must be an integer"):
            collector.validate_measurement(row)

    def test_duplicate_fact_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            session_dir = root / "session"
            with mock.patch.object(collector, "require_git_identity"):
                collector.initialize_session(
                    identity(),
                    session_dir,
                    duration_seconds=3600,
                    sample_interval_seconds=10,
                    session_id="6" * 64,
                    created_at="2026-08-12T00:00:00Z",
                )
                payload = fact_payloads()["fault_events"]
                collector.record_fact(
                    session_dir,
                    "fault_events",
                    payload,
                    trusted_probe_sha256=identity()["probes"]["fault_events"],
                )
                with self.assertRaisesRegex(collector.CollectorError, "already exists"):
                    collector.record_fact(
                        session_dir,
                        "fault_events",
                        payload,
                        trusted_probe_sha256=identity()["probes"]["fault_events"],
                    )

    def test_fact_probe_must_match_frozen_session_digest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700); session_dir = root / "session"
            with mock.patch.object(collector, "require_git_identity"):
                collector.initialize_session(
                    identity(), session_dir, duration_seconds=3600,
                    sample_interval_seconds=10, session_id="6" * 64,
                    created_at="2026-08-12T00:00:00Z",
                )
                with self.assertRaisesRegex(collector.CollectorError, "frozen session"):
                    collector.record_fact(
                        session_dir, "fault_events", fact_payloads()["fault_events"],
                        trusted_probe_sha256="8" * 64,
                    )

    def test_sample_gap_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            session_dir = root / "session"
            with mock.patch.object(collector, "require_git_identity"):
                collector.initialize_session(
                    identity(),
                    session_dir,
                    duration_seconds=3600,
                    sample_interval_seconds=10,
                    session_id="6" * 64,
                    created_at="2026-08-12T00:00:00Z",
                )
                collector.record_sample(
                    session_dir,
                    measurement("2026-08-12T00:00:00Z"),
                    trusted_probe_sha256="7" * 64,
                )
                with self.assertRaisesRegex(collector.CollectorError, "cadence gap"):
                    collector.record_sample(
                        session_dir,
                        measurement("2026-08-12T00:00:13Z"),
                        trusted_probe_sha256="7" * 64,
                    )

    def test_browser_telemetry_replay_fails_immediately(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            session_dir = root / "session"
            with mock.patch.object(collector, "require_git_identity"):
                collector.initialize_session(
                    identity(),
                    session_dir,
                    duration_seconds=3600,
                    sample_interval_seconds=10,
                    session_id="6" * 64,
                    created_at="2026-08-12T00:00:00Z",
                )
                first = measurement("2026-08-12T00:00:00Z")
                collector.record_sample(
                    session_dir, first, trusted_probe_sha256="7" * 64
                )
                replay = measurement("2026-08-12T00:00:10Z")
                replay["telemetry"] = dict(first["telemetry"])
                with self.assertRaisesRegex(collector.CollectorError, "replayed"):
                    collector.record_sample(
                        session_dir, replay, trusted_probe_sha256="7" * 64
                    )

    def test_ordinary_oj_replay_or_pm2_change_fails_immediately(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700); session_dir = root / "session"
            with mock.patch.object(collector, "require_git_identity"):
                collector.initialize_session(
                    identity(), session_dir, duration_seconds=3600, sample_interval_seconds=10,
                    session_id="6" * 64, created_at="2026-08-12T00:00:00Z",
                )
                first = measurement("2026-08-12T00:00:00Z")
                collector.record_sample(session_dir, first, trusted_probe_sha256="7" * 64)
                replay = measurement("2026-08-12T00:00:10Z")
                replay["ordinary_oj"] = dict(first["ordinary_oj"])
                with self.assertRaisesRegex(collector.CollectorError, "ordinary OJ telemetry was replayed"):
                    collector.record_sample(session_dir, replay, trusted_probe_sha256="7" * 64)
                changed = measurement("2026-08-12T00:00:10Z")
                changed["ordinary_oj"]["pm2_fingerprint_sha256"] = "e" * 64
                with self.assertRaisesRegex(collector.CollectorError, "PM2 fingerprint changed"):
                    collector.record_sample(session_dir, changed, trusted_probe_sha256="7" * 64)

    def test_full_hour_derives_verified_capacity_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            session_dir = root / "session"
            with mock.patch.object(collector, "require_git_identity"):
                collector.initialize_session(
                    identity(),
                    session_dir,
                    duration_seconds=3600,
                    sample_interval_seconds=10,
                    session_id="6" * 64,
                    created_at="2026-08-12T00:00:00Z",
                )
                for index in range(361):
                    second = index * 10
                    hour, remainder = divmod(second, 3600)
                    minute, sec = divmod(remainder, 60)
                    collector.record_sample(
                        session_dir,
                        measurement(
                            f"2026-08-12T{hour:02d}:{minute:02d}:{sec:02d}Z",
                            reconnects=1 if index == 0 else 0,
                        ),
                        trusted_probe_sha256="7" * 64,
                    )
                for kind, payload in fact_payloads().items():
                    collector.record_fact(
                        session_dir,
                        kind,
                        payload,
                        trusted_probe_sha256="7" * 64,
                    )
                result = collector.finalize_session(session_dir)
                repeated = collector.finalize_session(session_dir)
            self.assertEqual(result["sample_count"], 361)
            self.assertEqual(repeated, result)
            evidence = json.loads(
                (session_dir / "capacity-evidence.json").read_text(encoding="utf-8")
            )
            self.assertEqual(evidence["seats"]["verified"], 17)
            self.assertEqual(evidence["metrics"]["websocket_reconnects"], 1)
            self.assertEqual(evidence["shutdown"]["cloud_state"], "STOPPED")

    def test_finalize_rejects_missing_fault_fact(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            session_dir = root / "session"
            with mock.patch.object(collector, "require_git_identity"):
                collector.initialize_session(
                    identity(),
                    session_dir,
                    duration_seconds=3600,
                    sample_interval_seconds=60,
                    session_id="6" * 64,
                    created_at="2026-08-12T00:00:00Z",
                )
                for minute in range(61):
                    collector.record_sample(
                        session_dir,
                        measurement(f"2026-08-12T{minute // 60:02d}:{minute % 60:02d}:00Z"),
                        trusted_probe_sha256="7" * 64,
                    )
                for kind, payload in fact_payloads().items():
                    if kind != "fault_events":
                        collector.record_fact(
                            session_dir,
                            kind,
                            payload,
                            trusted_probe_sha256="7" * 64,
                        )
                with self.assertRaisesRegex(collector.CollectorError, "file set differs"):
                    collector.finalize_session(session_dir)

    def test_manual_input_cannot_be_finalized_as_qualification(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            session_dir = root / "session"
            with mock.patch.object(collector, "require_git_identity"):
                collector.initialize_session(
                    identity(),
                    session_dir,
                    duration_seconds=3600,
                    sample_interval_seconds=60,
                    session_id="6" * 64,
                    created_at="2026-08-12T00:00:00Z",
                )
                for minute in range(61):
                    collector.record_sample(
                        session_dir,
                        measurement(f"2026-08-12T{minute // 60:02d}:{minute % 60:02d}:00Z"),
                    )
                for kind, payload in fact_payloads().items():
                    collector.record_fact(session_dir, kind, payload)
                with self.assertRaisesRegex(collector.CollectorError, "trusted probes"):
                    collector.finalize_session(session_dir)

    def test_measurement_probe_cannot_change_mid_window(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            session_dir = root / "session"
            with mock.patch.object(collector, "require_git_identity"):
                collector.initialize_session(
                    identity(),
                    session_dir,
                    duration_seconds=3600,
                    sample_interval_seconds=60,
                    session_id="6" * 64,
                    created_at="2026-08-12T00:00:00Z",
                )
                collector.record_sample(
                    session_dir,
                    measurement("2026-08-12T00:00:00Z"),
                    trusted_probe_sha256="7" * 64,
                )
                with self.assertRaisesRegex(collector.CollectorError, "frozen session"):
                    collector.record_sample(
                        session_dir,
                        measurement("2026-08-12T00:01:00Z"),
                        trusted_probe_sha256="8" * 64,
                    )


if __name__ == "__main__":
    unittest.main()
