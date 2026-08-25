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
SCRIPT = ROOT / "scripts" / "v1_capacity_workload_probe.py"
BUILDER = ROOT / "scripts" / "build_v1_capacity_workload_probe.py"
spec = importlib.util.spec_from_file_location("v1_capacity_workload_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(probe)
build_spec = importlib.util.spec_from_file_location("build_v1_capacity_workload_probe", BUILDER)
builder = importlib.util.module_from_spec(build_spec); assert build_spec.loader; build_spec.loader.exec_module(builder)


def config(root: Path | None = None):
    prefix = "/root/workload" if root is None else root.as_posix()
    return {
        "schema_version": 1,
        "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
        "seat_set_sha256": "a" * 64,
        "database_path": f"{prefix}/orchestrator.db",
        "contest_id": "b" * 24,
        "submission_session": "c" * 32,
        "problem_slugs": ["alpha", "beta", "gamma"],
        "seat_bindings": [{"slot_no": index, "uid": index + 100, "candidate": f"9999{index:08d}"}
                          for index in range(1, 16)],
        "action_envelope": f"{prefix}/actions.json",
        "action_receipt": f"{prefix}/actions-receipt.json",
        "action_agent_sha256": "f" * 64,
        "action_signer": "workload-agent",
        "action_public_key": "ssh-ed25519 " + "A" * 68,
        "action_max_age_seconds": 7200,
        "ssh_keygen_path": f"{prefix}/ssh-keygen",
        "capacity_session_dir": f"{prefix}/capacity-session",
    }


def action_payload(row):
    return {
        "schema_version": 1,
        "qualification_marker": row["qualification_marker"],
        "seat_set_sha256": row["seat_set_sha256"],
        "contest_id_sha256": probe.hashlib.sha256(row["contest_id"].encode()).hexdigest(),
        "observed_at": "2026-08-13T00:00:00Z",
        "operation_receipt_sha256": "d" * 64,
        "login_slots": list(range(1, 16)),
        "material_open_slots": list(range(1, 16)),
        "compile_pairs": [
            {"slot_no": slot, "problem": problem}
            for slot in range(1, 16) for problem in row["problem_slugs"]
        ],
    }


def make_database(path: Path, row: dict, collection: Path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE contests(tid TEXT, files TEXT, submission_session TEXT, state TEXT,
          collection_dir TEXT, collection_run_id TEXT, collection_receipt_sha256 TEXT);
        CREATE TABLE seats(tid TEXT, uid INTEGER, uname TEXT, candidate TEXT);
        CREATE TABLE web_submissions(id INTEGER PRIMARY KEY AUTOINCREMENT, tid TEXT, uid INTEGER, problem TEXT, judge_state TEXT,
          submission_id TEXT, rid TEXT, submission_session TEXT, judge_kind TEXT,
          sha256 TEXT, judge_sha256 TEXT);
    """)
    connection.execute(
        "INSERT INTO contests VALUES(?,?,?,?,?,?,?)",
        (row["contest_id"], json.dumps(row["problem_slugs"]), row["submission_session"],
         "safe_wait", str(collection), "run-1", "0" * 64),
    )
    for item in row["seat_bindings"]:
        connection.execute("INSERT INTO seats VALUES(?,?,?,?)", (row["contest_id"], item["uid"],
                           f"user-{item['slot_no']:03d}", item["candidate"]))
        for problem_index, problem in enumerate(row["problem_slugs"], start=1):
            source_path = collection / str(item["uid"]) / "web" / f"{problem}.cpp"
            digest = probe.sha256_file(source_path)
            rid = f"{item['slot_no'] * 10 + problem_index:024x}"
            submission_id = f"{item['slot_no'] * 10 + problem_index:064x}"
            connection.execute(
                "INSERT INTO web_submissions(tid,uid,problem,judge_state,submission_id,rid,submission_session,judge_kind,sha256,judge_sha256) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (row["contest_id"], item["uid"], problem, "submitted", submission_id, rid,
                 row["submission_session"], "realtime", digest, digest),
            )
    connection.commit(); connection.close()


def make_collection(path: Path, row: dict) -> str:
    path.mkdir()
    report = {}; folder_report = {}; web_report = {}; selection = {}; submit_log = {}
    for item in row["seat_bindings"]:
        uname = f"user-{item['slot_no']:03d}"
        report[uname] = {}; folder_report[uname] = {}; web_report[uname] = {}; selection[uname] = {}; submit_log[uname] = {}
        for problem_index, problem in enumerate(row["problem_slugs"], start=1):
            rid = f"{item['slot_no'] * 10 + problem_index:024x}"
            relative = f"{item['candidate']}/{problem}/{problem}.cpp"
            folder_source = path / "slots" / f"{item['slot_no']:03d}" / "answers" / relative
            folder_source.parent.mkdir(parents=True, exist_ok=True)
            folder_source.write_text(f"// changed folder {item['slot_no']} {problem}\n")
            web_source = path / str(item["uid"]) / "web" / f"{problem}.cpp"
            web_source.parent.mkdir(parents=True, exist_ok=True)
            web_source.write_text(f"// submitted web {item['slot_no']} {problem}\n")
            digest = probe.sha256_file(web_source)
            folder_report[uname][problem] = {"status": "ok", "file": relative}
            web_report[uname][problem] = {"status": "ok", "file": f"{problem}.cpp", "sha256": digest}
            selection[uname][problem] = "web_submit"
            report[uname][problem] = {"status": "ok", "submission_source": "web_submit",
                                      "reuses_confirmed_submission": True, "sha256": digest,
                                      "file": f"{problem}.cpp"}
            submit_log[uname][problem] = {"ok": True, "rid": rid, "reused_realtime": True}
    payloads = {
        "folder_report.json": folder_report, "web_report.json": web_report, "selection.json": selection,
        "report.json": report, "submit_log.json": submit_log,
    }
    digests = {}
    for name, value in payloads.items():
        raw = (json.dumps(value, sort_keys=True) + "\n").encode(); (path / name).write_bytes(raw)
        digests[name] = probe.hashlib.sha256(raw).hexdigest()
    manifest = probe.archive_manifest(path)
    raw_manifest = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    (path / "archive_manifest.json").write_bytes(raw_manifest)
    digests["archive_manifest.json"] = probe.hashlib.sha256(raw_manifest).hexdigest()
    receipt = {"schema_version": 1, "tid": row["contest_id"], "run_id": "run-1",
               "completed_at_ms": 1, "cutoff_at_ms": 1, "shutdown_after_ms": 2,
               "seat_count": 15, "problem_count": 3, "submit_failures": 0, "files": digests}
    raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    (path / "collection_receipt.json").write_bytes(raw)
    return probe.hashlib.sha256(raw).hexdigest()


class WorkloadProbeTests(unittest.TestCase):
    def test_config_binds_exact_fifteen_by_three_workload(self):
        row = probe.validate_config(config())
        self.assertEqual([item["slot_no"] for item in row["seat_bindings"]], list(range(1, 16)))
        duplicate = config(); duplicate["seat_bindings"][-1]["uid"] = duplicate["seat_bindings"][0]["uid"]
        with self.assertRaisesRegex(probe.WorkloadProbeError, "unique"):
            probe.validate_config(duplicate)

    def test_action_envelope_requires_exact_slots_and_compile_matrix(self):
        row = probe.validate_config(config()); payload = action_payload(row)
        receipt = {"schema_version": 1, "qualification_marker": row["qualification_marker"],
                   "contest_id_sha256": probe.hashlib.sha256(row["contest_id"].encode()).hexdigest(),
                   "seat_set_sha256": row["seat_set_sha256"], "browser_envelope_sha256": "e" * 64,
                   "agent_sha256": row["action_agent_sha256"],
                   "started_at": "2026-08-12T23:59:00Z", "completed_at": payload["observed_at"],
                   "seat_identities": [{"slot_no": item["slot_no"],
                                        "candidate_sha256": probe.hashlib.sha256(item["candidate"].encode()).hexdigest(),
                                        "container_identity_sha256": f"{item['slot_no']:064x}"}
                                       for item in row["seat_bindings"]],
                   "material_open_count": 15, "compile_count": 45, "compile_peak_concurrency": 15}
        receipt_raw = probe.canonical(receipt)
        payload["operation_receipt_sha256"] = probe.hashlib.sha256(receipt_raw).hexdigest()
        envelope = {"schema_version": 1, "namespace": probe.NAMESPACE,
                    "signer": row["action_signer"], "payload": payload,
                    "signature_base64": base64.b64encode(b"s" * 64).decode()}
        completed = mock.Mock(returncode=0)
        with mock.patch.object(probe, "read_bounded", side_effect=[probe.canonical(envelope), receipt_raw]), \
                mock.patch.object(probe, "private_file", return_value=Path("/usr/bin/ssh-keygen")), \
                mock.patch.object(probe, "verify_sample_window"), \
                mock.patch.object(probe.subprocess, "run", return_value=completed):
            result = probe.verify_action_envelope(
                row, datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc)
            )
        self.assertEqual(len(result["compile_pairs"]), 45)
        receipt["compile_peak_concurrency"] = 14
        changed_receipt = probe.canonical(receipt)
        envelope["payload"]["operation_receipt_sha256"] = probe.hashlib.sha256(changed_receipt).hexdigest()
        with mock.patch.object(probe, "read_bounded", side_effect=[probe.canonical(envelope), changed_receipt]), \
                mock.patch.object(probe, "verify_sample_window"):
            with self.assertRaisesRegex(probe.WorkloadProbeError, "receipt identity"):
                probe.verify_action_envelope(row, datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc))
        receipt["compile_peak_concurrency"] = 15
        envelope["payload"]["operation_receipt_sha256"] = probe.hashlib.sha256(receipt_raw).hexdigest()
        envelope["payload"]["compile_pairs"].pop()
        with mock.patch.object(probe, "read_bounded", return_value=probe.canonical(envelope)), \
                mock.patch.object(probe, "verify_sample_window"):
            with self.assertRaisesRegex(probe.WorkloadProbeError, "15 by 3"):
                probe.verify_action_envelope(row, datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc))

    def test_action_must_be_inside_complete_capacity_sample_window(self):
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
                    self.assertRaisesRegex(probe.WorkloadProbeError, "outside"):
                probe.verify_sample_window(config(), datetime(2026, 8, 12, 23, 59, tzinfo=timezone.utc),
                                           datetime(2026, 8, 13, 0, 20, tzinfo=timezone.utc))

    def test_collect_cross_binds_database_and_collection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); row = probe.validate_config(config()); collection = root / "collection"
            # The production contract deliberately accepts POSIX absolute paths only.
            # Swap the already-validated path for this Windows-only SQLite fixture.
            row["database_path"] = str(root / "orchestrator.db")
            receipt_digest = make_collection(collection, row)
            make_database(root / "orchestrator.db", row, collection)
            connection = sqlite3.connect(root / "orchestrator.db")
            connection.execute("UPDATE contests SET collection_receipt_sha256=?", (receipt_digest,))
            connection.commit(); connection.close()
            with mock.patch.object(probe, "verify_action_envelope", return_value=action_payload(row)), \
                    mock.patch.object(probe, "private_file", side_effect=lambda path, label, executable=False: path), \
                    mock.patch.object(probe, "private_directory", side_effect=lambda path, label: path):
                result = probe.collect(row, now=datetime(2026, 8, 13, 0, 2, tzinfo=timezone.utc))
            self.assertEqual(result["submission_successes"], 45)
            self.assertEqual(result["collection_successes"], 15)
            submit_log = json.loads((collection / "submit_log.json").read_text())
            submit_log["user-001"]["alpha"]["rid"] = "f" * 24
            raw_log = (json.dumps(submit_log, sort_keys=True) + "\n").encode()
            (collection / "submit_log.json").write_bytes(raw_log)
            receipt = json.loads((collection / "collection_receipt.json").read_text())
            receipt["files"]["submit_log.json"] = probe.hashlib.sha256(raw_log).hexdigest()
            raw_receipt = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            (collection / "collection_receipt.json").write_bytes(raw_receipt)
            connection = sqlite3.connect(root / "orchestrator.db")
            connection.execute("UPDATE contests SET collection_receipt_sha256=?",
                               (probe.hashlib.sha256(raw_receipt).hexdigest(),))
            connection.commit(); connection.close()
            with mock.patch.object(probe, "verify_action_envelope", return_value=action_payload(row)), \
                    mock.patch.object(probe, "private_file", side_effect=lambda path, label, executable=False: path), \
                    mock.patch.object(probe, "private_directory", side_effect=lambda path, label: path):
                with self.assertRaisesRegex(probe.WorkloadProbeError, "tree differs|delivery receipt"):
                    probe.collect(row, now=datetime(2026, 8, 13, 0, 2, tzinfo=timezone.utc))

    def test_builder_freezes_exact_config(self):
        raw = builder.render(probe.validate_config(config()))
        text = raw.decode(); self.assertNotIn(builder.MARKER, text)
        self.assertIn("submission_session", text); compile(text, "<workload-probe-test>", "exec")


if __name__ == "__main__":
    unittest.main()
