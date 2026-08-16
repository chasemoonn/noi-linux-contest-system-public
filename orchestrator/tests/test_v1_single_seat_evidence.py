import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_v1_single_seat_evidence.py"
SPEC = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
verifier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verifier)


class SingleSeatEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.revision = "a" * 40
        self.cutoff = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
        self.context = {
            "contest_id_sha256": "1" * 64,
            "seat_id_sha256": "2" * 64,
            "candidate_id": "999900000001",
            "seat_candidate": "CSP001",
            "problem_slug": "apple",
            "cutoff_at_ms": int(self.cutoff.timestamp() * 1000),
        }
        self.source = {"revision": self.revision, "tree": "b" * 40}
        self.components = {
            "orchestrator_image_digest": "sha256:" + "3" * 64,
            "desktop_image_id": "sha256:" + "4" * 64,
            "desktop_source_revision": self.revision,
            "hydro_plugin_sha256": "5" * 64,
        }
        self.facts = {}
        observed = [
            self.cutoff - timedelta(minutes=20),
            self.cutoff - timedelta(minutes=18),
            self.cutoff - timedelta(minutes=16),
            self.cutoff - timedelta(minutes=10),
            self.cutoff + timedelta(minutes=1),
            self.cutoff + timedelta(minutes=2),
            self.cutoff + timedelta(minutes=3),
            self.cutoff + timedelta(minutes=35),
            self.cutoff + timedelta(minutes=45),
        ]
        for phase, instant in zip(verifier.PHASES, observed):
            self.facts[phase] = self.fact(phase, instant)
        self.digests = {phase: f"{index + 6:x}" * 64 for index, phase in enumerate(verifier.PHASES)}

    def ordinary(self, observed_at=None):
        return {
            "homepage_status": 200,
            "login_status": 200,
            "prep_health_ok": True,
            "prep_database_ok": True,
            "errors": 0,
            "restarts": 0,
            "pm2_fingerprint_sha256": "e" * 64,
            "observed_at": (
                observed_at or (self.cutoff - timedelta(minutes=20))
            ).isoformat().replace("+00:00", "Z"),
        }

    def observations(self, phase):
        manual_sha = "6" * 64
        final_sha = "7" * 64
        manual_rid = "8" * 24
        final_rid = "9" * 24
        receipt = "a" * 64
        cleanup = {
            "cleanup_verified_at_ms": int((self.cutoff + timedelta(minutes=45)).timestamp() * 1000),
            "contest_absent": True,
            "contest_id_sha256": self.context["contest_id_sha256"],
            "verification_method": "hydro_mongo_post_delete_absence",
            "discussion_count": 0,
            "linked_record_count": 0,
            "registration_status_count": 0,
            "scheduled_task_count": 0,
        }
        cleanup["cleanup_receipt_sha256"] = hashlib.sha256(
            json.dumps(cleanup, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        rows = {
            "materials": {
                "desktop_paper_sha256": "b" * 64,
                "material_manifest_sha256": "c" * 64,
                "oj_publication_receipt_sha256": "d" * 64,
                "paper_sha256": "b" * 64,
                "practice_pairs": [
                    {"group": 1, "input_sha256": "1" * 64, "output_sha256": "2" * 64},
                    {"group": 2, "input_sha256": "3" * 64, "output_sha256": "4" * 64},
                ],
            },
            "desktop": {
                "candidate_path": "CSP001/apple/apple.cpp",
                "desktop_contract": "finalizer-status-v1",
                "entries": {
                    "answer_directory": True,
                    "instructions": True,
                    "paper": True,
                    "practice_data": True,
                    "submission_portal": True,
                },
                "page_status": 200,
                "websocket_status": 101,
            },
            "compile": {
                "actual_output_sha256": "f" * 64,
                "binary_sha256": "0" * 64,
                "exit_code": 0,
                "expected_output_sha256": "f" * 64,
                "input_sha256": "1" * 64,
                "source_sha256": manual_sha,
            },
            "manual_submit": {
                "judge_state": "submitted",
                "rid": manual_rid,
                "source_sha256": manual_sha,
                "submission_id": "2" * 64,
            },
            "cutoff_submit": {
                "deadline_state": "frozen",
                "final_rid": final_rid,
                "final_submission_id": "3" * 64,
                "frozen_source_sha256": final_sha,
                "last_confirmed_source_sha256": manual_sha,
                "supplemental_submitted": True,
            },
            "oj_record": {
                "final_rid": final_rid,
                "final_source_sha256": final_sha,
                "final_score_source": "last_record",
                "manual_rid": manual_rid,
                "manual_source_sha256": manual_sha,
                "record_count": 2,
                "student_history_visible": True,
                "teacher_source_visible": True,
            },
            "collection": {
                "archive_manifest_sha256": "4" * 64,
                "collection_receipt_sha256": receipt,
                "delivery_safe": True,
                "final_rid": final_rid,
                "final_source_sha256": final_sha,
                "state": "safe_wait",
                "submit_failures": 0,
                "submit_log_sha256": "5" * 64,
            },
            "shutdown": {
                "cloud_state": "STOPPED",
                "collection_receipt_sha256": receipt,
                "conflict_rules": 0,
                "desktop_closed": True,
                "managed_rules": 0,
                "running_seats": 0,
                "shutdown_verified_at_ms": int((self.cutoff + timedelta(minutes=35)).timestamp() * 1000),
            },
            "test_cleanup": cleanup,
        }
        return rows[phase]

    def fact(self, phase, observed_at):
        role = verifier.ROLES[phase]
        host = {"control": "a" * 64, "desktop": "b" * 64, "oj": "c" * 64}[role]
        return {
            "$schema": "v1-single-seat-phase-fact.schema.json",
            "schema_version": 1,
            "phase": phase,
            "session_id": "d" * 64,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "collector": {"anonymous_host_id": host, "role": role},
            "source": copy.deepcopy(self.source),
            "components": copy.deepcopy(self.components),
            "context": copy.deepcopy(self.context),
            "ordinary_oj": self.ordinary(observed_at - timedelta(seconds=1)),
            "observations": self.observations(phase),
            "artifacts": [
                {"reference": f"{phase}/capture.json", "sha256": "f" * 64}
            ],
        }

    def validated(self):
        return {phase: verifier.validate_fact(copy.deepcopy(self.facts[phase]), phase) for phase in verifier.PHASES}

    def test_complete_single_seat_chain_passes(self):
        evidence = verifier.combine(self.validated(), self.digests, self.revision)
        validated = verifier.validate_combined(
            evidence,
            expected_revision=self.revision,
            expected_components=self.components,
        )
        self.assertEqual(validated["status"], "passed")
        self.assertTrue(all(validated["checks"].values()))
        self.assertEqual(len(validated["facts"]), 9)

    def test_changed_cutoff_source_is_required(self):
        self.facts["cutoff_submit"]["observations"]["frozen_source_sha256"] = "6" * 64
        with self.assertRaisesRegex(verifier.EvidenceError, "changed-source"):
            self.validated()

    def test_oj_must_bind_both_exact_source_versions(self):
        self.facts["oj_record"]["observations"]["final_source_sha256"] = "f" * 64
        with self.assertRaisesRegex(verifier.EvidenceError, "OJ records"):
            verifier.combine(self.validated(), self.digests, self.revision)

    def test_ordinary_oj_fingerprint_must_not_change(self):
        self.facts["collection"]["ordinary_oj"]["pm2_fingerprint_sha256"] = "f" * 64
        with self.assertRaisesRegex(verifier.EvidenceError, "fingerprint changed"):
            verifier.combine(self.validated(), self.digests, self.revision)

    def test_cutoff_phase_cannot_be_observed_before_cutoff(self):
        observed = self.cutoff - timedelta(minutes=1)
        self.facts["cutoff_submit"]["observed_at"] = observed.isoformat().replace(
            "+00:00", "Z"
        )
        self.facts["cutoff_submit"]["ordinary_oj"]["observed_at"] = (
            observed - timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        with self.assertRaisesRegex(verifier.EvidenceError, "cutoff evidence"):
            verifier.combine(self.validated(), self.digests, self.revision)

    def test_shutdown_must_bind_collection_receipt(self):
        self.facts["shutdown"]["observations"]["collection_receipt_sha256"] = "f" * 64
        with self.assertRaisesRegex(verifier.EvidenceError, "collection and shutdown"):
            verifier.combine(self.validated(), self.digests, self.revision)

    def test_cleanup_must_target_the_same_contest(self):
        self.facts["test_cleanup"]["observations"]["contest_id_sha256"] = "f" * 64
        with self.assertRaisesRegex(verifier.EvidenceError, "different contest"):
            self.validated()

    def test_cleanup_must_be_prompt(self):
        instant = self.cutoff + timedelta(minutes=66)
        self.facts["test_cleanup"]["observed_at"] = instant.isoformat().replace(
            "+00:00", "Z"
        )
        self.facts["test_cleanup"]["ordinary_oj"]["observed_at"] = (
            instant - timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        self.facts["test_cleanup"]["observations"]["cleanup_verified_at_ms"] = int(
            instant.timestamp() * 1000
        )
        cleanup = self.facts["test_cleanup"]["observations"]
        cleanup.pop("cleanup_receipt_sha256")
        cleanup["cleanup_receipt_sha256"] = hashlib.sha256(
            json.dumps(cleanup, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(verifier.EvidenceError, "30-minute"):
            verifier.combine(self.validated(), self.digests, self.revision)

    def test_cleanup_requires_no_linked_records(self):
        self.facts["test_cleanup"]["observations"]["linked_record_count"] = 1
        with self.assertRaisesRegex(verifier.EvidenceError, "linked_record_count"):
            self.validated()

    def test_cleanup_timestamp_must_match_machine_fact(self):
        self.facts["test_cleanup"]["observations"]["cleanup_verified_at_ms"] -= 121_000
        cleanup = self.facts["test_cleanup"]["observations"]
        cleanup.pop("cleanup_receipt_sha256")
        cleanup["cleanup_receipt_sha256"] = hashlib.sha256(
            json.dumps(cleanup, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(verifier.EvidenceError, "stale or future-dated"):
            verifier.combine(self.validated(), self.digests, self.revision)

    def test_control_and_desktop_must_be_distinct_hosts(self):
        for phase in ("desktop", "compile"):
            self.facts[phase]["collector"]["anonymous_host_id"] = "a" * 64
        with self.assertRaisesRegex(verifier.EvidenceError, "two distinct"):
            verifier.combine(self.validated(), self.digests, self.revision)

    def test_raw_artifact_digest_is_verified(self):
        fact = verifier.validate_fact(copy.deepcopy(self.facts["materials"]), "materials")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "materials").mkdir()
            payload = b"sanitized capture\n"
            (root / "materials" / "capture.json").write_bytes(payload)
            fact["artifacts"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
            verifier.verify_artifacts(fact, root)
            (root / "materials" / "capture.json").write_bytes(b"changed\n")
            with self.assertRaisesRegex(verifier.EvidenceError, "SHA256 differs"):
                verifier.verify_artifacts(fact, root)


if __name__ == "__main__":
    unittest.main()
