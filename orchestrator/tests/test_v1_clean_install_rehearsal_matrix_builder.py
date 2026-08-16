import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("clean_rehearsal_matrix_builder",
    ROOT / "scripts/build_v1_clean_install_rehearsal_matrix.py")
module = importlib.util.module_from_spec(SPEC); assert SPEC.loader; SPEC.loader.exec_module(module)


def observation(kind, phase=None):
    success = {"terminal": "committed", "controller_healthy": True, "closed_frontend": True,
        "active_seats": 0, "managed_rules": 0, "cloud_state": "STOPPED", "pending_markers": 0,
        "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0}
    rollback = {"terminal": "rollback_verified", "clean_target": True, "caddy_restored": True,
        "hydro_restored": True, "controller_absent": True, "cloud_state": "STOPPED",
        "pending_markers": 0, "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0,
        "ordinary_oj_pid_changes": 0}
    artifacts = {"fresh_baseline": {"sha256": "6" * 64},
        "ordinary_before": {"sha256": "7" * 64},
        "ordinary_after": {"sha256": "8" * 64},
        "execution_log": {"sha256": "9" * 64}, "terminal_receipt": {"sha256": "a" * 64}}
    return {"kind": kind, "phase": phase, "plan_id": "d" * 64,
            "private_plan_sha256": "e" * 64, "backup_manifest_sha256": "6" * 64,
            "result": success if kind == "success" else rollback, "artifacts": artifacts}


class CleanInstallRehearsalMatrixBuilderTests(unittest.TestCase):
    def values(self):
        values = [observation("success")]
        for kind in ("phase_failure", "power_loss"):
            values += [observation(kind, phase) for phase in module.PHASES]
        return values

    def test_assembles_only_exact_thirteen_scenario_contract(self):
        source = {"revision": "1" * 40, "tree": "2" * 40}
        components = {"orchestrator_image_digest": "sha256:" + "3" * 64,
            "desktop_image_id": "sha256:" + "4" * 64, "desktop_source_revision": "1" * 40,
            "hydro_plugin_sha256": "5" * 64}
        plan = {"plan_id": "d" * 64, "backup_manifest_sha256": "6" * 64}
        host = {"anonymous_id": "b" * 64, "architecture": "x86_64", "kernel": "6.8",
                "os_release_sha256": "c" * 64}
        value = module.assemble(source, components, plan, self.values(), "f" * 64,
            "NOI-V1-QUAL-1234567890ABCDEF", host, "2026-08-14T00:00:00Z")
        self.assertEqual((value["success"]["terminal"], len(value["rollback_scenarios"])),
                         ("committed", 12))
        self.assertEqual(value["plan"], {"plan_id": "d" * 64})
        self.assertEqual(value["success"]["private_plan_sha256"], "e" * 64)

    def test_accepts_per_snapshot_plan_and_baseline_bindings(self):
        values = self.values()
        values[1]["private_plan_sha256"] = "b" * 64
        values[1]["backup_manifest_sha256"] = "c" * 64
        values[1]["artifacts"]["fresh_baseline"]["sha256"] = "c" * 64
        value = module.assemble(
            {"revision": "1" * 40, "tree": "2" * 40},
            {"orchestrator_image_digest": "sha256:" + "3" * 64,
             "desktop_image_id": "sha256:" + "4" * 64,
             "desktop_source_revision": "1" * 40, "hydro_plugin_sha256": "5" * 64},
            {"plan_id": "d" * 64}, values, "f" * 64,
            "NOI-V1-QUAL-1234567890ABCDEF",
            {"anonymous_id": "b" * 64, "architecture": "x86_64", "kernel": "6.8",
             "os_release_sha256": "c" * 64}, "2026-08-14T00:00:00Z")
        self.assertEqual(value["rollback_scenarios"][0]["backup_manifest_sha256"], "c" * 64)

    def test_missing_success_or_rollback_is_rejected(self):
        args = ({"revision": "1" * 40, "tree": "2" * 40}, {},
                {"plan_id": "d" * 64, "backup_manifest_sha256": "6" * 64})
        with self.assertRaisesRegex(module.MatrixBuildError, "coverage"):
            module.assemble(*args, self.values()[1:], "f" * 64,
                "NOI-V1-QUAL-1234567890ABCDEF", {}, "2026-08-14T00:00:00Z")

    def test_expected_directory_names_are_complete_and_unique(self):
        names = module.expected_directories()
        self.assertEqual(len(names), 13); self.assertEqual(len(set(names)), 13)
        self.assertIn("power_loss-post_install_verification", names)


if __name__ == "__main__": unittest.main()
