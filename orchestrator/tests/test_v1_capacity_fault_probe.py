import base64
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "orchestrator"))
SCRIPT = ROOT / "scripts" / "v1_capacity_fault_probe.py"
BUILDER = ROOT / "scripts" / "build_v1_capacity_fault_probe.py"
spec = importlib.util.spec_from_file_location("v1_capacity_fault_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(probe)
build_spec = importlib.util.spec_from_file_location("build_v1_capacity_fault_probe", BUILDER)
builder = importlib.util.module_from_spec(build_spec); assert build_spec.loader; build_spec.loader.exec_module(builder)
from services.seat_pool import SeatPoolState


def config(root: Path | None = None):
    prefix = "/root/fault" if root is None else root.as_posix()
    return {
        "schema_version": 1,
        "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
        "database_path": f"{prefix}/orchestrator.db",
        "contest_id": "a" * 24,
        "expected_pool_revision": 47,
        "failed_slot": 1,
        "replacement_slot": 16,
        "seat_inventory_probe": f"{prefix}/seat-inventory",
        "seat_inventory_probe_sha256": "b" * 64,
        "controller_probe_target_sha256": "c" * 64,
        "network_action_envelope": f"{prefix}/network.json",
        "network_action_receipt": f"{prefix}/network-receipt.json",
        "network_action_agent_sha256": "d" * 64,
        "network_action_signer": "network-agent",
        "network_action_public_key": "ssh-ed25519 " + "A" * 68,
        "network_action_max_age_seconds": 1800,
        "ssh_keygen_path": f"{prefix}/ssh-keygen",
        "capacity_session_dir": f"{prefix}/capacity-session",
    }


def network_payload(row):
    return {
        "schema_version": 1,
        "qualification_marker": row["qualification_marker"],
        "contest_id_sha256": probe.hashlib.sha256(row["contest_id"].encode()).hexdigest(),
        "seat_inventory_probe_sha256": row["seat_inventory_probe_sha256"],
        "controller_probe_target_sha256": row["controller_probe_target_sha256"],
        "fault_method": "controller-egress-deny",
        "operation_receipt_sha256": "d" * 64,
        "observed_at": "2026-08-13T00:03:00Z",
        "events": [
            {"phase": "before_interrupt", "observed_at": "2026-08-13T00:00:00Z",
             "consecutive_probe_successes": 3, "consecutive_probe_failures": 0},
            {"phase": "during_interrupt", "observed_at": "2026-08-13T00:01:00Z",
             "consecutive_probe_successes": 0, "consecutive_probe_failures": 3},
            {"phase": "after_recovery", "observed_at": "2026-08-13T00:02:00Z",
             "consecutive_probe_successes": 3, "consecutive_probe_failures": 0},
        ],
    }


def pool_after_fault(row):
    pool = SeatPoolState.create(
        row["contest_id"], max_participants=15, spare_count=2,
        begin_at_ms=10_000_000, release_lead_ms=1,
    )
    for item in pool.seats:
        pool = pool.mark_warming(
            item.slot_no, now_ms=1, command_id=f"warm:{item.slot_no}",
            expected_revision=pool.revision,
        ).state
        pool = pool.mark_verified(
            item.slot_no, container_ref=f"container-{item.slot_no}",
            image_digest="sha256:image", material_digest="sha256:material",
            now_ms=2, command_id=f"verify:{item.slot_no}",
            expected_revision=pool.revision,
        ).state
    for uid in range(1, 16):
        pool = pool.reserve(
            uid, f"user-{uid}", now_ms=3,
            command_id=f"reserve:{uid}", expected_revision=pool.revision,
        ).state
    row["expected_pool_revision"] = pool.revision
    replaced = pool.replace_failed(
        row["failed_slot"], reason="qualification fault", now_ms=4,
        teacher_approved=True,
        command_id=f"replace:{row['contest_id']}:r{pool.revision}:{row['failed_slot']}",
        expected_revision=pool.revision,
    )
    pool = replaced.state
    pool = pool.mark_warming(
        row["failed_slot"], now_ms=5,
        command_id=f"repair:warm:{row['contest_id']}:{row['failed_slot']}:failure:1",
        expected_revision=pool.revision,
    ).state
    pool = pool.mark_verified(
        row["failed_slot"], container_ref="container-1-rebuilt",
        image_digest="sha256:image", material_digest="sha256:material",
        now_ms=6,
        command_id=f"repair:verify:{row['contest_id']}:{row['failed_slot']}:failure:1",
        expected_revision=pool.revision,
    ).state
    return pool


def make_database(path: Path, row: dict):
    pool = pool_after_fault(row)
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE contests(tid TEXT, state TEXT);
        CREATE TABLE seat_pools(tid TEXT, revision INTEGER, state_json TEXT);
        CREATE TABLE seat_pool_resources(tid TEXT, slot_no INTEGER, container TEXT,
          image_digest TEXT, material_digest TEXT);
        CREATE TABLE seats(tid TEXT, uid INTEGER, container TEXT);
    """)
    connection.execute("INSERT INTO contests VALUES(?,?)", (row["contest_id"], "ready"))
    connection.execute(
        "INSERT INTO seat_pools VALUES(?,?,?)",
        (row["contest_id"], pool.revision, json.dumps(pool.to_dict(), sort_keys=True)),
    )
    for item in pool.seats:
        connection.execute(
            "INSERT INTO seat_pool_resources VALUES(?,?,?,?,?)",
            (row["contest_id"], item.slot_no, item.container_ref,
             item.image_digest, item.material_digest),
        )
        if item.uid is not None:
            connection.execute(
                "INSERT INTO seats VALUES(?,?,?)",
                (row["contest_id"], item.uid, item.container_ref),
            )
    connection.commit(); connection.close()
    return pool


def inventory():
    ids = [f"{index:064x}" for index in range(1, 18)]
    return {
        "observed_at": "2026-08-13T00:03:00Z",
        "formal_container_ids": ids[:15], "spare_container_ids": ids[15:],
        "verified_container_ids": ids,
        "unexpected_restart_events": 0, "planned_restart_events": 1,
        "planned_restart_recoveries": 1, "cross_seat_access_failures": 0,
    }


class CapacityFaultProbeTests(unittest.TestCase):
    def test_config_binds_distinct_formal_and_spare_fault_slots(self):
        row = probe.validate_config(config())
        self.assertEqual((row["failed_slot"], row["replacement_slot"]), (1, 16))
        wrong = config(); wrong["replacement_slot"] = 15
        with self.assertRaisesRegex(probe.FaultProbeError, "slot binding"):
            probe.validate_config(wrong)

    def test_network_envelope_requires_three_exact_monotonic_phases(self):
        row = probe.validate_config(config()); payload = network_payload(row)
        envelope = {"schema_version": 1, "namespace": probe.NAMESPACE,
                    "signer": row["network_action_signer"], "payload": payload,
                    "signature_base64": base64.b64encode(b"s" * 64).decode()}
        receipt = {
            "schema_version": 1, "qualification_marker": row["qualification_marker"],
            "contest_id_sha256": payload["contest_id_sha256"],
            "seat_inventory_probe_sha256": row["seat_inventory_probe_sha256"],
            "controller_probe_target_sha256": row["controller_probe_target_sha256"],
            "agent_sha256": row["network_action_agent_sha256"],
            "controller_identity_sha256": "e" * 64, "rule_identity_sha256": "f" * 64,
            "started_at": "2026-08-12T23:59:59Z", "rule_installed_at": "2026-08-13T00:00:30Z",
            "rule_removed_at": "2026-08-13T00:01:30Z", "completed_at": "2026-08-13T00:03:00Z",
            "before_successes": 3, "during_failures": 3, "after_successes": 3,
        }
        receipt_raw = probe.canonical(receipt)
        payload["operation_receipt_sha256"] = probe.hashlib.sha256(receipt_raw).hexdigest()
        envelope["payload"] = payload
        with mock.patch.object(probe, "read_bounded", side_effect=[probe.canonical(envelope), receipt_raw]), \
                mock.patch.object(probe, "verify_signature"), \
                mock.patch.object(probe, "verify_sample_window"):
            probe.verify_network_envelope(
                row, datetime(2026, 8, 13, 0, 4, tzinfo=timezone.utc)
            )
        envelope["payload"]["events"][1]["consecutive_probe_failures"] = 2
        with mock.patch.object(probe, "read_bounded", return_value=probe.canonical(envelope)), \
                mock.patch.object(probe, "verify_sample_window"):
            with self.assertRaisesRegex(probe.FaultProbeError, "event result"):
                probe.verify_network_envelope(
                    row, datetime(2026, 8, 13, 0, 4, tzinfo=timezone.utc)
                )

    def test_network_fault_must_be_inside_complete_capacity_sample_window(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw); samples = directory / "samples"; samples.mkdir()
            for index in range(1, 62):
                (samples / f"{index:06d}.json").write_text("{}")
            session = {"$schema": "v1-capacity-session.schema.json", "schema_version": 1,
                       "session_id": "1" * 64, "created_at": "2026-08-13T00:00:00Z",
                       "source": {}, "components": {}, "environment": {}, "thresholds": {}, "probes": {},
                       "duration_seconds": 3600, "sample_interval_seconds": 60}
            def sample(sequence, observed):
                return {"schema_version": 1, "kind": "capacity_sample", "session_id": "1" * 64,
                        "sequence": sequence, "observed_at": observed, "metrics": {}, "telemetry": {},
                        "ordinary_oj": {"qualification_marker": config()["qualification_marker"]},
                        "collector": {}}
            values = [probe.canonical(session), probe.canonical(sample(1, "2026-08-13T00:00:00Z")),
                      probe.canonical(sample(61, "2026-08-13T01:00:00Z"))]
            with mock.patch.object(probe, "private_directory", side_effect=lambda path, label: directory if label == "capacity session directory" else samples), \
                    mock.patch.object(probe, "read_bounded", side_effect=values):
                probe.verify_sample_window(config(), datetime(2026, 8, 13, 0, 10, tzinfo=timezone.utc),
                                           datetime(2026, 8, 13, 0, 20, tzinfo=timezone.utc))
            values = [probe.canonical(session), probe.canonical(sample(1, "2026-08-13T00:00:00Z")),
                      probe.canonical(sample(61, "2026-08-13T01:00:00Z"))]
            with mock.patch.object(probe, "private_directory", side_effect=lambda path, label: directory if label == "capacity session directory" else samples), \
                    mock.patch.object(probe, "read_bounded", side_effect=values), \
                    self.assertRaisesRegex(probe.FaultProbeError, "outside"):
                probe.verify_sample_window(config(), datetime(2026, 8, 13, 0, 10, tzinfo=timezone.utc),
                                           datetime(2026, 8, 13, 1, 1, tzinfo=timezone.utc))

    def test_collect_cross_binds_replace_repair_inventory_and_network(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); row = probe.validate_config(config())
            row["database_path"] = str(root / "orchestrator.db")
            pool = make_database(root / "orchestrator.db", row)
            self.assertEqual(pool.revision, row["expected_pool_revision"] + 3)
            with mock.patch.object(probe, "validate_config", return_value=row), \
                    mock.patch.object(probe, "private_file", side_effect=lambda path, label, executable=False: path), \
                    mock.patch.object(probe, "run_seat_probe", return_value=inventory()), \
                    mock.patch.object(probe, "verify_network_envelope", return_value=network_payload(row)):
                result = probe.collect(
                    row, now=datetime(2026, 8, 13, 0, 4, tzinfo=timezone.utc)
                )
            self.assertEqual(set(result.values()) - {result["observed_at"]}, {1})

            connection = sqlite3.connect(root / "orchestrator.db")
            state = json.loads(connection.execute("SELECT state_json FROM seat_pools").fetchone()[0])
            state["seats"][0]["failure_count"] = 0
            connection.execute("UPDATE seat_pools SET state_json=?", (json.dumps(state),))
            connection.commit(); connection.close()
            with mock.patch.object(probe, "private_file", side_effect=lambda path, label, executable=False: path):
                with self.assertRaisesRegex(probe.FaultProbeError, "terminal pool|receipt semantics"):
                    probe.verify_pool_history(row)

    def test_builder_freezes_exact_config(self):
        raw = builder.render(probe.validate_config(config()))
        text = raw.decode(); self.assertNotIn(builder.MARKER, text)
        self.assertIn("controller_probe_target_sha256", text)
        compile(text, "<fault-probe-test>", "exec")


if __name__ == "__main__":
    unittest.main()
