import copy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("verify_clean_rehearsal",
    ROOT / "scripts/verify_v1_clean_install_rehearsal.py")
module = importlib.util.module_from_spec(SPEC); assert SPEC.loader; SPEC.loader.exec_module(module)


def matrix():
    revision = "1" * 40
    common = {"private_plan_sha256": "e" * 64, "backup_manifest_sha256": "6" * 64,
              "fresh_baseline_sha256": "6" * 64, "execution_log_sha256": "7" * 64,
              "terminal_receipt_sha256": "8" * 64}
    scenarios = []
    for kind in ("phase_failure", "power_loss"):
        for phase in module.PHASES:
            scenarios.append({"kind": kind, "phase": phase, "terminal": "rollback_verified",
                "clean_target": True, "caddy_restored": True, "hydro_restored": True,
                "controller_absent": True, "cloud_state": "STOPPED", "pending_markers": 0,
                "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0,
                "ordinary_oj_before_sha256": "9" * 64,
                "ordinary_oj_after_sha256": "d" * 64, **common})
    return {"$schema": "v1-clean-install-rehearsal-matrix.schema.json", "schema_version": 2,
        "source": {"revision": revision, "tree": "2" * 40},
        "components": {"orchestrator_image_digest": "sha256:" + "3" * 64,
            "desktop_image_id": "sha256:" + "4" * 64, "desktop_source_revision": revision,
            "hydro_plugin_sha256": "5" * 64}, "session_id": "a" * 64,
        "plan": {"plan_id": "d" * 64},
        "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF", "observed_at": "2026-08-14T00:00:00Z",
        "host": {"anonymous_id": "b" * 64, "architecture": "x86_64", "kernel": "6.8",
                 "os_release_sha256": "c" * 64},
        "success": {"terminal": "committed", "controller_healthy": True, "closed_frontend": True,
            "active_seats": 0, "managed_rules": 0, "cloud_state": "STOPPED", "pending_markers": 0,
            "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0,
            "ordinary_oj_before_sha256": "9" * 64, "ordinary_oj_after_sha256": "a" * 64, **common},
        "rollback_scenarios": scenarios}


class CleanInstallRehearsalTests(unittest.TestCase):
    def test_accepts_exact_thirteen_scenario_matrix(self):
        row = matrix()
        self.assertIs(module.validate(row, expected_revision="1" * 40,
            expected_tree="2" * 40, expected_components=row["components"]), row)

    def test_rejects_missing_duplicate_or_failed_scenario(self):
        row = matrix(); row["rollback_scenarios"].pop()
        with self.assertRaisesRegex(module.RehearsalError, "matrix size"):
            module.validate(row)
        row = matrix(); row["rollback_scenarios"][-1] = copy.deepcopy(row["rollback_scenarios"][0])
        with self.assertRaisesRegex(module.RehearsalError, "identity"):
            module.validate(row)
        row = matrix(); row["rollback_scenarios"][0]["caddy_restored"] = False
        with self.assertRaisesRegex(module.RehearsalError, "result"):
            module.validate(row)

    def test_accepts_distinct_sealed_ordinary_snapshots_and_rejects_unclosed_success(self):
        row = matrix()
        self.assertIs(module.validate(row), row)
        row = matrix(); scenario = row["rollback_scenarios"][0]
        scenario["private_plan_sha256"] = "b" * 64
        scenario["backup_manifest_sha256"] = scenario["fresh_baseline_sha256"] = "c" * 64
        self.assertIs(module.validate(row), row)
        row = matrix(); row["success"]["cloud_state"] = "RUNNING"
        with self.assertRaisesRegex(module.RehearsalError, "committed scenario"):
            module.validate(row)

    def test_rejects_plan_or_scenario_baseline_drift(self):
        row = matrix(); row["success"]["backup_manifest_sha256"] = "f" * 64
        with self.assertRaisesRegex(module.RehearsalError, "success baseline"):
            module.validate(row)
        row = matrix(); row["rollback_scenarios"][0]["fresh_baseline_sha256"] = "f" * 64
        with self.assertRaisesRegex(module.RehearsalError, "rollback baseline"):
            module.validate(row)

    def test_schema_is_strict_and_requires_twelve_rollbacks(self):
        schema = json.loads((ROOT / "release/v1-clean-install-rehearsal-matrix.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["rollback_scenarios"]["minItems"], 12)
        self.assertEqual(schema["properties"]["rollback_scenarios"]["maxItems"], 12)


if __name__ == "__main__":
    unittest.main()
