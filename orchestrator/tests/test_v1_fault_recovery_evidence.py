import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_v1_fault_recovery_evidence.py"
SPEC = importlib.util.spec_from_file_location("fault_evidence", SCRIPT)
fault = importlib.util.module_from_spec(SPEC); assert SPEC.loader; SPEC.loader.exec_module(fault)

SOURCE = {"revision": "a" * 40, "tree": "b" * 40}
COMPONENTS = {"orchestrator_image_digest": "sha256:" + "c" * 64,
              "desktop_image_id": "sha256:" + "d" * 64,
              "desktop_source_revision": "a" * 40, "hydro_plugin_sha256": "e" * 64}
PUBLIC_KEY = "ssh-ed25519 " + "A" * 68
AGENT_SHA = "f" * 64

def capacity():
    return {"session_id": "1" * 64, "source": SOURCE, "components": COMPONENTS,
            "window": {"started_at": "2026-08-13T01:00:00Z", "ended_at": "2026-08-13T02:00:00Z"}}

def action(scenario):
    common = {"ordinary_oj_errors": 0, "ordinary_oj_restarts": 0,
              "ordinary_oj_pid_changes": 0, "duplicate_oj_records": 0,
              "final_source_mismatches": 0, "other_seat_failures": 0}
    payloads = {
      "control_restart": {"restart_events": 1, "restart_recoveries": 1,
          "pending_jobs_before": 2, "pending_jobs_resumed": 2, "controller_identity_preserved": True},
      "collection_retry": {"injected_failures": 1, "retry_attempts": 1,
          "successful_deliveries": 1, "collection_receipt_unique": True},
      "power_loss_recovery": {"durable_marker_created": True,
          "abrupt_termination_observed": True, "startup_blocked_pending": True,
          "recovery_completed": True, "baseline_restored": True, "active_seats": 0,
          "managed_rules": 0, "cloud_state": "STOPPED"},
    }
    return {"$schema":"v1-fault-recovery-action-fact.schema.json","schema_version":1,
      "kind":"fault_recovery_action","scenario":scenario,"session_id":"1"*64,
      "source":dict(SOURCE),"components":dict(COMPONENTS),
      "qualification_marker":"NOI-V1-QUAL-ABCDEFGHIJKLMNOP","started_at":"2026-08-13T01:10:00Z",
      "ended_at":"2026-08-13T01:11:00Z","collector":{"mode":"trusted_action_agent","agent_sha256":AGENT_SHA},
      "signer":"qualification","signing_public_key":PUBLIC_KEY,"payload":{**common,**payloads[scenario]},
      "signature":"A"*64}

class FaultRecoveryEvidenceTests(unittest.TestCase):
    def validate(self, scenario, row=None):
        with mock.patch.object(fault, "verify_signature"):
            return fault.validate_action(row or action(scenario), scenario, capacity(), Path("ssh-keygen"), PUBLIC_KEY, AGENT_SHA)

    def test_three_action_scenarios_pass_exact_semantics(self):
        for scenario in sorted(fault.SCENARIOS):
            with self.subTest(scenario=scenario): self.assertEqual(self.validate(scenario)["scenario"], scenario)

    def test_duplicate_record_ordinary_oj_or_agent_drift_fails(self):
        row = action("collection_retry"); row["payload"]["duplicate_oj_records"] = 1
        with self.assertRaisesRegex(fault.EvidenceError, "must equal 0"): self.validate("collection_retry", row)
        row = action("control_restart"); row["payload"]["ordinary_oj_restarts"] = 1
        with self.assertRaisesRegex(fault.EvidenceError, "must equal 0"): self.validate("control_restart", row)
        with mock.patch.object(fault, "verify_signature"):
            with self.assertRaisesRegex(fault.EvidenceError, "agent SHA256"):
                fault.validate_action(action("power_loss_recovery"), "power_loss_recovery", capacity(), Path("x"), PUBLIC_KEY, "0"*64)

    def test_missing_recovery_and_time_drift_fail(self):
        row = action("control_restart"); row["payload"]["restart_recoveries"] = 0
        with self.assertRaisesRegex(fault.EvidenceError, "must equal 1"): self.validate("control_restart", row)
        row = action("power_loss_recovery"); row["ended_at"] = "2026-08-13T03:00:00Z"
        with self.assertRaisesRegex(fault.EvidenceError, "outside"): self.validate("power_loss_recovery", row)

    def test_combined_evidence_requires_all_six_scenarios_and_exact_inputs(self):
        actions = {name: action(name) for name in fault.SCENARIOS}
        value = {"$schema":"v1-fault-recovery-evidence.schema.json","schema_version":2,"status":"passed",
          "session_id":"1"*64,"source":SOURCE,"components":COMPONENTS,
          "scenarios":{name:True for name in ("control_restart","desktop_reconnect","single_seat_replace","network_interruption","collection_retry","power_loss_recovery")},
          "ordinary_oj_isolation":{"errors":0,"restarts":0,"pid_changes":0},"signer":"qualification",
          "signing_public_key":PUBLIC_KEY,
          "signing_public_key_sha256":fault.hashlib.sha256(PUBLIC_KEY.encode()).hexdigest(),
          "action_agent_sha256":{name:AGENT_SHA for name in fault.SCENARIOS},
          "actions":actions,
          "inputs":[{"name":"capacity","reference":"capacity.json","sha256":"3"*64}] +
                   [{"name":name,"reference":name+".json",
                     "sha256":fault.hashlib.sha256(fault.canonical(actions[name])).hexdigest()}
                    for name in sorted(fault.SCENARIOS)]}
        self.assertEqual(fault.validate_combined(value)["status"], "passed")
        capacity_raw = b"capacity evidence"
        value["inputs"][0]["sha256"] = fault.hashlib.sha256(capacity_raw).hexdigest()
        with mock.patch.object(fault, "verify_signature"):
            self.assertEqual(fault.validate_combined(
                value, capacity=capacity(), capacity_raw=capacity_raw
            )["status"], "passed")
        with self.assertRaisesRegex(fault.EvidenceError, "capacity evidence SHA256"):
            fault.validate_combined(value, capacity=capacity(), capacity_raw=b"wrong")
        value["actions"]["control_restart"]["payload"]["restart_events"] = 0
        with self.assertRaisesRegex(fault.EvidenceError, "SHA256 differs"):
            fault.validate_combined(value)
        value["actions"]["control_restart"] = action("control_restart")
        value["inputs"].pop()
        with self.assertRaisesRegex(fault.EvidenceError, "inputs differ"): fault.validate_combined(value)

if __name__ == "__main__": unittest.main()
