import importlib.util
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "report_v1_launch_readiness.py"
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("report_v1_launch_readiness", SCRIPT)
module = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module)


class LaunchReadinessTests(unittest.TestCase):
    def test_pending_example_reports_all_seven_gates(self):
        report = json.loads((ROOT / "release" / "v1-qualification-report.example.json").read_text(encoding="utf-8"))
        result = module.build(report)
        self.assertEqual(result["summary"], {"total": 7, "passed": 0, "pending": 7, "failed": 0})
        self.assertEqual(result["next_actions"], [code for code, _ in module.GATES])
        self.assertFalse(result["production_install_plan_available"])
        self.assertFalse(result["production_install_apply_available"])
        self.assertEqual(result["software_delivery"]["next_actions"], [
            "linux_clean_install_rehearsal_matrix"
        ])
        self.assertFalse(result["software_delivery"]["complete"])

    def test_fully_qualified_report_opens_apply_readiness(self):
        report = json.loads((ROOT / "release" / "v1-qualification-report.example.json").read_text(encoding="utf-8"))
        digest = "a" * 64
        for code, reference in {
            "linux_ci": "v1-linux-ci-evidence.json",
            "cross_machine_import_rollback": "v1-cross-machine-image-evidence.json",
            "independent_teacher_install": "v1-independent-teacher-install-evidence.json",
        }.items():
            report["evidence"][code] = {
                "status": "passed", "reference": reference,
                "evidence_sha256": digest,
            }
        report["evidence"]["single_seat"] = {
            "status": "passed", "reference": "single-seat-evidence.json",
            "evidence_sha256": digest,
            "checks": {name: True for name in (
                "materials", "desktop", "compile", "manual_submit",
                "cutoff_submit", "oj_record", "collection", "shutdown",
                "test_cleanup",
            )},
        }
        report["evidence"]["fault_recovery"] = {
            "status": "passed", "reference": "v1-fault-recovery-evidence.json",
            "evidence_sha256": digest,
            "scenarios": {name: True for name in (
                "control_restart", "desktop_reconnect", "single_seat_replace",
                "network_interruption", "collection_retry",
                "power_loss_recovery",
            )},
        }
        report["evidence"]["ordinary_oj_isolation"] = {
            "status": "passed", "reference": "capacity-evidence.json",
            "evidence_sha256": digest, "ordinary_oj_errors": 0,
            "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0,
        }
        report["evidence"]["capacity_15_plus_2"] = {
            "status": "passed", "reference": "capacity-evidence.json",
            "evidence_sha256": digest, "formal_seats": 15, "spare_seats": 2,
            "duration_seconds": 3600, "unexpected_seat_restarts": 0,
            "failed_submissions": 0, "failed_collections": 0,
            "verified_seats": 17, "spare_takeovers": 1,
            "planned_restart_recoveries": 1,
            "controller_network_recoveries": 1,
            "capacity_margin_accepted": True,
        }
        report["production_qualified"] = True

        result = module.build(report)

        self.assertTrue(result["production_install_plan_available"])
        self.assertTrue(result["production_install_apply_available"])
        self.assertTrue(result["software_delivery"]["complete"])
        self.assertEqual(result["software_delivery"]["next_actions"], [])

    def test_invalid_claim_is_rejected_by_qualification_verifier(self):
        report = json.loads((ROOT / "release" / "v1-qualification-report.example.json").read_text(encoding="utf-8"))
        report["production_qualified"] = True
        with self.assertRaisesRegex(module.ReportError, "exactly match"):
            module.build(report)


if __name__ == "__main__":
    unittest.main()
