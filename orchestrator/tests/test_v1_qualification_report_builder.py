import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_v1_qualification_report.py"
spec = importlib.util.spec_from_file_location("qualification_builder", SCRIPT)
builder = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(builder)


def evidence():
    revision, tree = "a" * 40, "b" * 40
    components = {
        "orchestrator_image_digest": "sha256:" + "c" * 64,
        "desktop_image_id": "sha256:" + "d" * 64,
        "desktop_source_revision": revision,
        "hydro_plugin_sha256": "e" * 64,
    }
    linux = {"source": {"revision": revision, "tree": tree}}
    cross = {"source": {"revision": revision, "tree": tree},
             "bundle": {"image_id": components["desktop_image_id"]}}
    single = {"source": {"revision": revision, "tree": tree}, "components": components,
              "checks": {name: True for name in builder.validate_report.__globals__["SINGLE_SEAT_CHECKS"]},
              "ordinary_oj_isolation": {"errors": 0, "restarts": 0, "pid_changes": 0}}
    capacity = {"source": {"revision": revision, "tree": tree}, "components": components,
                "environment": {"profile": "aliyun-hydro5-pm2-direct-v1"},
                "isolation": {"ordinary_oj_errors": 0, "ordinary_oj_restarts": 0,
                              "ordinary_oj_pid_changes": 0},
                "seats": {"formal": 15, "spare": 2, "verified": 17,
                          "unexpected_restart_events": 0},
                "window": {"duration_seconds": 3600},
                "workload": {"failed_submissions": 0, "failed_collections": 0},
                "faults": {"spare_takeovers": 1, "planned_restart_recoveries": 1,
                           "controller_network_recoveries": 1},
                "thresholds": {"capacity_margin_accepted": True}}
    return linux, cross, single, capacity


def fault_evidence(capacity):
    return {
        "session_id": "f" * 64,
        "source": capacity["source"],
        "components": capacity["components"],
        "scenarios": {
            name: True
            for name in builder.validate_report.__globals__["FAULT_SCENARIOS"]
        },
    }

def teacher_evidence(capacity):
    return {"source": capacity["source"], "components": capacity["components"]}


class QualificationReportBuilderTests(unittest.TestCase):
    def test_compiler_derives_every_available_gate_and_keeps_fault_pending(self):
        linux, cross, single, capacity = evidence()
        report = builder.compile_report(
            linux_raw=b"linux", linux=linux, cross_raw=b"cross", cross=cross,
            single_raw=b"single", single=single, capacity_raw=b"capacity",
            capacity=capacity, reviewers=["teacher", "operations"],
        )
        self.assertFalse(report["production_qualified"])
        self.assertEqual(report["evidence"]["fault_recovery"], {
            "status": "pending", "reference": None, "evidence_sha256": None,
            "scenarios": None,
        })
        self.assertEqual(report["evidence"]["independent_teacher_install"], {
            "status": "pending", "reference": None, "evidence_sha256": None,
        })
        self.assertTrue(all(report["evidence"][name]["status"] == "passed" for name in (
            "linux_ci", "cross_machine_import_rollback", "single_seat",
            "ordinary_oj_isolation", "capacity_15_plus_2",
        )))

    def test_source_component_and_isolation_drift_fail_closed(self):
        linux, cross, single, capacity = evidence(); linux["source"]["tree"] = "9" * 40
        with self.assertRaisesRegex(builder.BuildError, "source identity"):
            builder.compile_report(linux_raw=b"l", linux=linux, cross_raw=b"c", cross=cross,
                single_raw=b"s", single=single, capacity_raw=b"p", capacity=capacity,
                reviewers=["teacher", "operations"])

    def test_compiler_accepts_only_identity_bound_fault_evidence(self):
        linux, cross, single, capacity = evidence()
        capacity["session_id"] = "f" * 64
        fault = fault_evidence(capacity)
        report = builder.compile_report(
            linux_raw=b"linux", linux=linux, cross_raw=b"cross", cross=cross,
            single_raw=b"single", single=single, capacity_raw=b"capacity",
            capacity=capacity, fault_raw=b"fault", fault=fault,
            reviewers=["teacher", "operations"],
        )
        self.assertEqual(report["evidence"]["fault_recovery"], {
            "status": "passed",
            "reference": "v1-fault-recovery-evidence.json",
            "evidence_sha256": builder.sha256(b"fault"),
            "scenarios": fault["scenarios"],
        })
        fault["session_id"] = "0" * 64
        with self.assertRaisesRegex(builder.BuildError, "identity differs"):
            builder.compile_report(
                linux_raw=b"linux", linux=linux, cross_raw=b"cross", cross=cross,
                single_raw=b"single", single=single, capacity_raw=b"capacity",
                capacity=capacity, fault_raw=b"fault", fault=fault,
                reviewers=["teacher", "operations"],
            )
        linux, cross, single, capacity = evidence(); single["ordinary_oj_isolation"]["errors"] = 1
        with self.assertRaisesRegex(builder.BuildError, "not clean"):
            builder.compile_report(linux_raw=b"l", linux=linux, cross_raw=b"c", cross=cross,
                single_raw=b"s", single=single, capacity_raw=b"p", capacity=capacity,
                reviewers=["teacher", "operations"])

    def test_compiler_qualifies_only_with_fault_and_teacher_evidence(self):
        linux, cross, single, capacity = evidence(); capacity["session_id"] = "f" * 64
        fault = fault_evidence(capacity); teacher = teacher_evidence(capacity)
        report = builder.compile_report(
            linux_raw=b"linux", linux=linux, cross_raw=b"cross", cross=cross,
            single_raw=b"single", single=single, capacity_raw=b"capacity", capacity=capacity,
            fault_raw=b"fault", fault=fault, teacher_raw=b"teacher", teacher=teacher,
            reviewers=["teacher", "operations"],
        )
        self.assertTrue(report["production_qualified"])
        self.assertEqual(report["evidence"]["independent_teacher_install"], {
            "status": "passed", "reference": "v1-independent-teacher-install-evidence.json",
            "evidence_sha256": builder.sha256(b"teacher"),
        })


if __name__ == "__main__":
    unittest.main()
