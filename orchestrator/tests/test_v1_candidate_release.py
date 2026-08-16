import copy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


qualification = load_script("verify_v1_qualification.py")


class V1CandidateReleaseTests(unittest.TestCase):
    def setUp(self):
        self.pending = json.loads(
            (ROOT / "release" / "v1-qualification-report.example.json").read_text(
                encoding="utf-8"
            )
        )

    def qualified_report(self):
        report = copy.deepcopy(self.pending)
        report["source_revision"] = "a" * 40
        report["components"]["desktop_source_revision"] = "a" * 40
        evidence = report["evidence"]
        for row in evidence.values():
            row["status"] = "passed"
            row["reference"] = "evidence/example.json"
            row["evidence_sha256"] = "b" * 64
        evidence["linux_ci"]["reference"] = "v1-linux-ci-evidence.json"
        evidence["cross_machine_import_rollback"]["reference"] = (
            "v1-cross-machine-image-evidence.json"
        )
        evidence["independent_teacher_install"]["reference"] = (
            "v1-independent-teacher-install-evidence.json"
        )
        evidence["single_seat"]["reference"] = "single-seat-evidence.json"
        evidence["fault_recovery"]["reference"] = (
            "v1-fault-recovery-evidence.json"
        )
        evidence["single_seat"]["checks"] = {
            "materials": True,
            "desktop": True,
            "compile": True,
            "manual_submit": True,
            "cutoff_submit": True,
            "oj_record": True,
            "collection": True,
            "shutdown": True,
            "test_cleanup": True,
        }
        evidence["fault_recovery"]["scenarios"] = {
            "control_restart": True,
            "desktop_reconnect": True,
            "single_seat_replace": True,
            "network_interruption": True,
            "collection_retry": True,
            "power_loss_recovery": True,
        }
        evidence["ordinary_oj_isolation"].update(
            ordinary_oj_errors=0,
            ordinary_oj_restarts=0,
            ordinary_oj_pid_changes=0,
        )
        evidence["capacity_15_plus_2"].update(
            reference="capacity-evidence.json",
            formal_seats=15,
            spare_seats=2,
            duration_seconds=3600,
            unexpected_seat_restarts=0,
            failed_submissions=0,
            failed_collections=0,
            verified_seats=17,
            spare_takeovers=1,
            planned_restart_recoveries=1,
            controller_network_recoveries=1,
            capacity_margin_accepted=True,
        )
        report["reviewers"] = ["teacher-reviewer", "operations-reviewer"]
        report["production_qualified"] = True
        return report

    def test_pending_example_is_valid_but_not_qualified(self):
        validated = qualification.validate_report(self.pending)
        self.assertFalse(validated["production_qualified"])
        with self.assertRaises(qualification.ReportError):
            qualification.validate_report(self.pending, require_qualified=True)

    def test_exact_passed_report_is_qualified(self):
        validated = qualification.validate_report(
            self.qualified_report(), require_qualified=True
        )
        self.assertTrue(validated["production_qualified"])

    def test_capacity_cannot_claim_less_than_one_hour(self):
        report = self.qualified_report()
        report["evidence"]["capacity_15_plus_2"]["duration_seconds"] = 3599
        with self.assertRaisesRegex(qualification.ReportError, "at least 3600"):
            qualification.validate_report(report)

    def test_capacity_requires_exact_15_plus_2(self):
        report = self.qualified_report()
        report["evidence"]["capacity_15_plus_2"]["formal_seats"] = 14
        with self.assertRaisesRegex(qualification.ReportError, "must equal 15"):
            qualification.validate_report(report)

    def test_pending_capacity_cannot_retain_stale_summary(self):
        report = copy.deepcopy(self.pending)
        report["evidence"]["capacity_15_plus_2"]["verified_seats"] = 17
        with self.assertRaisesRegex(qualification.ReportError, "null summary"):
            qualification.validate_report(report)

    def test_ordinary_oj_isolation_requires_zero_changes(self):
        report = self.qualified_report()
        report["evidence"]["ordinary_oj_isolation"]["ordinary_oj_restarts"] = 1
        with self.assertRaisesRegex(qualification.ReportError, "must be zero"):
            qualification.validate_report(report)

    def test_component_revision_must_match_source(self):
        report = self.qualified_report()
        report["components"]["desktop_source_revision"] = "c" * 40
        with self.assertRaisesRegex(qualification.ReportError, "differs"):
            qualification.validate_report(report)

    def test_passed_single_seat_reference_is_fixed(self):
        report = self.qualified_report()
        report["evidence"]["single_seat"]["reference"] = "notes/single-seat.json"
        with self.assertRaisesRegex(qualification.ReportError, "must reference"):
            qualification.validate_report(report)

    def test_passed_linux_and_cross_machine_references_are_fixed(self):
        report = self.qualified_report()
        report["evidence"]["linux_ci"]["reference"] = "notes/ci.json"
        with self.assertRaisesRegex(qualification.ReportError, "v1-linux-ci-evidence"):
            qualification.validate_report(report)
        report = self.qualified_report()
        report["evidence"]["cross_machine_import_rollback"]["reference"] = "cross.json"
        with self.assertRaisesRegex(qualification.ReportError, "v1-cross-machine-image-evidence"):
            qualification.validate_report(report)

    def test_independent_teacher_install_reference_is_fixed(self):
        report = self.qualified_report()
        report["evidence"]["independent_teacher_install"]["reference"] = "teacher-notes.json"
        with self.assertRaisesRegex(
            qualification.ReportError, "v1-independent-teacher-install-evidence"
        ):
            qualification.validate_report(report)

    def test_fault_recovery_reference_is_fixed(self):
        report = self.qualified_report()
        report["evidence"]["fault_recovery"]["reference"] = "fault-notes.json"
        with self.assertRaisesRegex(
            qualification.ReportError, "v1-fault-recovery-evidence"
        ):
            qualification.validate_report(report)

    def test_qualification_flag_cannot_get_ahead_of_evidence(self):
        report = copy.deepcopy(self.pending)
        report["production_qualified"] = True
        with self.assertRaisesRegex(qualification.ReportError, "exactly match"):
            qualification.validate_report(report)

    def test_two_distinct_reviewers_are_required(self):
        report = self.qualified_report()
        report["reviewers"] = ["same", "same"]
        with self.assertRaisesRegex(qualification.ReportError, "distinct"):
            qualification.validate_report(report)

    def test_release_schemas_and_examples_are_json(self):
        for name in (
            "v1-source-candidate.schema.json",
            "v1-qualification-report.schema.json",
            "v1-qualification-report.example.json",
            "v1-image-host-fact.schema.json",
            "v1-cross-machine-image-evidence.schema.json",
            "v1-single-seat-phase-fact.schema.json",
            "v1-single-seat-session.schema.json",
            "v1-single-seat-evidence.schema.json",
            "v1-capacity-evidence.schema.json",
            "v1-capacity-session.schema.json",
            "v1-capacity-probe-config.schema.json",
            "v1-fault-recovery-action-fact.schema.json",
            "v1-fault-recovery-evidence.schema.json",
            "v1-control-restart-action-agent-config.schema.json",
            "v1-collection-retry-action-agent-config.schema.json",
            "v1-power-loss-recovery-action-agent-config.schema.json",
            "v1-independent-teacher-install-evidence.schema.json",
            "v1-independent-teacher-install-observation.schema.json",
        ):
            with self.subTest(name=name):
                json.loads((ROOT / "release" / name).read_text(encoding="utf-8"))

    def test_candidate_schema_requires_submission_fault_injection_gate(self):
        schema = json.loads(
            (ROOT / "release" / "v1-source-candidate.schema.json").read_text(
                encoding="utf-8"
            )
        )
        gates = schema["properties"]["static_gates"]
        self.assertEqual(
            gates["required"],
            [
                "submission_fault_injection",
                "v1_product_contract",
                "public_release_boundary",
            ],
        )
        self.assertEqual(
            gates["properties"]["submission_fault_injection"],
            {"const": "passed"},
        )

    def test_linux_ci_evidence_schema_is_json(self):
        schema = json.loads(
            (ROOT / "release" / "v1-linux-ci-evidence.schema.json").read_text(
                encoding="utf-8"
            )
        )
        environment = schema["properties"]["environment"]["properties"]
        self.assertEqual(environment["system"], {"const": "linux"})
        self.assertEqual(schema["properties"]["gates"]["minItems"], 10)
        self.assertEqual(schema["properties"]["gates"]["maxItems"], 10)

    def test_single_seat_runbook_uses_machine_collected_fresh_facts(self):
        guide = (ROOT / "deploy" / "V1_SINGLE_SEAT_REHEARSAL.md").read_text(
            encoding="utf-8"
        )
        for marker in (
            "collect_v1_components.py",
            "collect_v1_ordinary_oj_observation.py",
            "init_v1_single_seat_session.py",
            "collect_v1_single_seat_phase_fact.py",
            "120 秒",
            "禁止手工填造 RID",
            ".pending-*",
            "root-only",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, guide)

    def test_candidate_tools_do_not_mutate_services(self):
        source = "\n".join(
            (ROOT / "scripts" / name).read_text(encoding="utf-8")
            for name in (
                "build_v1_candidate.py",
                "build_v1_qualification_report.py",
                "verify_v1_candidate.py",
                "verify_v1_qualification.py",
                "run_v1_fault_injection.py",
                "run_v1_linux_ci.py",
                "verify_v1_linux_ci_evidence.py",
                "collect_v1_image_host_fact.py",
                "verify_v1_cross_machine_image_evidence.py",
                "collect_v1_components.py",
                "collect_v1_ordinary_oj_observation.py",
                "init_v1_single_seat_session.py",
                "collect_v1_single_seat_phase_fact.py",
                "verify_v1_single_seat_evidence.py",
                "verify_v1_capacity_evidence.py",
                "collect_v1_capacity_evidence.py",
                "build_v1_capacity_probe.py",
                "v1_capacity_measurement_probe.py",
                "v1_capacity_ordinary_oj_agent.py",
                "build_v1_capacity_ordinary_oj_agent.py",
                "install_v1_capacity_ordinary_oj_telemetry.py",
                "publish_v1_capacity_ordinary_oj_telemetry.py",
                "v1_capacity_shutdown_probe.py",
                "build_v1_capacity_shutdown_probe.py",
            )
        ).lower()
        for forbidden in (
            "docker restart",
            "docker stop",
            "docker compose up",
            "pm2 restart",
            "systemctl restart",
            "authorizesecuritygroup",
            "revokesecuritygroup",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_imported_image_promotion_is_not_a_rebuild_path(self):
        source = (
            ROOT / "deploy" / "promote-imported-contest-image-local.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("--expected-image-id", source)
        self.assertIn("image-promotion.pending", source)
        self.assertIn("ROLLBACK_SOURCE_TARGET", source)
        self.assertNotIn("docker build", source)
        self.assertNotIn("docker image load", source)


if __name__ == "__main__":
    unittest.main()
