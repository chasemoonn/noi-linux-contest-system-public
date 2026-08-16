import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_v1_cross_machine_image_evidence.py"
SPEC = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
verifier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verifier)


class CrossMachineImageEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.revision = "a" * 40
        self.candidate = "sha256:" + "1" * 64
        self.baseline = "sha256:" + "2" * 64
        self.baseline_source = "image-releases/20260812T010203Z"
        self.candidate_source = "image-releases/20260812T020304Z"
        self.facts = {}
        start = datetime(2026, 8, 12, tzinfo=timezone.utc)
        for index, phase in enumerate(verifier.PHASES):
            self.facts[phase] = self.fact(
                phase,
                (start + timedelta(minutes=index)).isoformat().replace("+00:00", "Z"),
            )
        self.digests = {
            phase: character * 64
            for phase, character in zip(verifier.PHASES, "56789a")
        }

    def state(self, phase):
        empty = {
            "candidate_tag_image_id": self.candidate,
            "current_promoted_image_id": None,
            "current_rollback_image_id": None,
            "current_rollback_source_target": None,
            "current_source_revision": None,
            "current_source_target": None,
            "formal_image_id": None,
            "pending_transaction": False,
            "running_contest_seats": 0,
        }
        if phase == "export":
            return empty
        if phase in {"imported", "rolled_back", "restored"}:
            empty.update(
                current_promoted_image_id=self.baseline,
                current_source_revision="b" * 40,
                current_source_target=self.baseline_source,
                formal_image_id=self.baseline,
            )
            return empty
        empty.update(
            current_promoted_image_id=self.candidate,
            current_rollback_image_id=self.baseline,
            current_rollback_source_target=self.baseline_source,
            current_source_revision=self.revision,
            current_source_target=self.candidate_source,
            formal_image_id=self.candidate,
        )
        return empty

    def fact(self, phase, observed_at):
        host_id = "3" * 64 if phase == "export" else "4" * 64
        return {
            "$schema": "v1-image-host-fact.schema.json",
            "schema_version": 1,
            "phase": phase,
            "session_id": "c" * 64,
            "observed_at": observed_at,
            "host": {
                "anonymous_id": host_id,
                "architecture": "x86_64",
                "docker_server": "28.0.1",
                "kernel": "6.8.0",
            },
            "source": {"revision": self.revision, "tree": "d" * 40},
            "bundle": {
                "archive_sha256": "e" * 64,
                "bundle_checksums_sha256": "f" * 64,
                "bundle_manifest_sha256": "0" * 64,
                "contract": "finalizer-status-v1",
                "image_id": self.candidate,
                "image_tag": "noi-linux-local:test",
                "iso_sha256": "6" * 64,
                "release_manifest_sha256": "7" * 64,
                "source_revision": self.revision,
            },
            "state": self.state(phase),
        }

    def validated(self):
        return {
            phase: verifier.validate_fact(copy.deepcopy(self.facts[phase]), phase)
            for phase in verifier.PHASES
        }

    def test_complete_two_host_round_trip_passes(self):
        evidence = verifier.combine(self.validated(), self.digests, self.revision)
        verifier.validate_combined(
            evidence, expected_revision=self.revision, expected_tree="d" * 40,
            expected_image_id=self.candidate,
        )
        self.assertEqual(evidence["status"], "passed")
        self.assertEqual(evidence["hosts"], {"export": "3" * 64, "import": "4" * 64})
        self.assertEqual(evidence["transitions"]["baseline_image_id"], self.baseline)
        self.assertEqual(len(evidence["transitions"]["facts"]), 6)

    def test_combined_evidence_cannot_change_image_or_fact_order(self):
        evidence = verifier.combine(self.validated(), self.digests, self.revision)
        evidence["transitions"]["facts"][0]["phase"] = "restored"
        with self.assertRaisesRegex(verifier.EvidenceError, "fact identity"):
            verifier.validate_combined(evidence, expected_revision=self.revision)
        evidence = verifier.combine(self.validated(), self.digests, self.revision)
        with self.assertRaisesRegex(verifier.EvidenceError, "desktop image"):
            verifier.validate_combined(evidence, expected_image_id="sha256:" + "9" * 64)

    def test_same_export_and_import_host_is_rejected(self):
        for phase in verifier.PHASES[1:]:
            self.facts[phase]["host"]["anonymous_id"] = "3" * 64
        with self.assertRaisesRegex(verifier.EvidenceError, "two distinct"):
            verifier.combine(self.validated(), self.digests, self.revision)

    def test_out_of_order_observation_is_rejected(self):
        self.facts["restored"]["observed_at"] = "2026-08-11T23:59:00Z"
        with self.assertRaisesRegex(verifier.EvidenceError, "not strictly ordered"):
            verifier.combine(self.validated(), self.digests, self.revision)

    def test_wrong_rollback_pair_is_rejected(self):
        self.facts["promoted"]["state"]["current_rollback_image_id"] = "sha256:" + "8" * 64
        with self.assertRaisesRegex(verifier.EvidenceError, "rollback pair"):
            verifier.combine(self.validated(), self.digests, self.revision)

    def test_final_restore_must_return_to_original_baseline(self):
        self.facts["restored"]["state"]["formal_image_id"] = self.candidate
        with self.assertRaisesRegex(verifier.EvidenceError, "final restoration"):
            verifier.combine(self.validated(), self.digests, self.revision)

    def test_second_promotion_may_use_a_new_release_directory(self):
        self.facts["repromoted"]["state"]["current_source_target"] = (
            "image-releases/20260812T030405Z"
        )
        evidence = verifier.combine(self.validated(), self.digests, self.revision)
        self.assertEqual(evidence["status"], "passed")

    def test_export_fact_cannot_claim_import_host_state(self):
        self.facts["export"]["state"]["formal_image_id"] = self.candidate
        with self.assertRaisesRegex(verifier.EvidenceError, "export fact"):
            self.validated()

    def test_pending_transaction_is_rejected(self):
        self.facts["promoted"]["state"]["pending_transaction"] = True
        with self.assertRaisesRegex(verifier.EvidenceError, "not quiescent"):
            self.validated()

    def test_running_contest_seat_is_rejected(self):
        self.facts["imported"]["state"]["running_contest_seats"] = 1
        with self.assertRaisesRegex(verifier.EvidenceError, "not quiescent"):
            self.validated()

    def test_session_or_bundle_drift_is_rejected(self):
        self.facts["repromoted"]["session_id"] = "9" * 64
        with self.assertRaisesRegex(verifier.EvidenceError, "one session"):
            verifier.combine(self.validated(), self.digests, self.revision)


if __name__ == "__main__":
    unittest.main()
