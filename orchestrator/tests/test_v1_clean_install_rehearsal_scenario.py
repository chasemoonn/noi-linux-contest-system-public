import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("clean_rehearsal_scenario",
    ROOT / "scripts/run_v1_clean_install_rehearsal_scenario.py")
module = importlib.util.module_from_spec(SPEC); assert SPEC.loader; SPEC.loader.exec_module(module)


def row(root: Path):
    return {"scope": "qualification-lab", "plan_id": "1" * 64,
        "backup_manifest_sha256": "2" * 64, "transaction_directory": str(root)}


class CleanInstallRehearsalScenarioTests(unittest.TestCase):
    def scenario(self, mode, phase=None, marker=None, kill_process=None):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); value = row(root); calls = []
            def transaction_run(_directory, _plan, _backup, _drivers, _final, *, after_phase_committed=None):
                if after_phase_committed is not None:
                    for current in module.transaction.CLEAN_PHASES:
                        try: after_phase_committed(mock.Mock(), current, {"phase": current})
                        except module.InjectedPhaseFailure: return {"status": "rollback_verified"}
                return {"status": "committed" if mode == "success" else "rollback_verified"}
            patches = (mock.patch.object(module.clean, "verify_bindings", return_value={}),
                       mock.patch.object(module.clean, "drivers", return_value=({}, mock.Mock())),
                       mock.patch.object(module.transaction, "run_clean", side_effect=transaction_run))
            with patches[0], patches[1], patches[2]:
                return module.execute(value, mode, phase, marker,
                    kill_process=kill_process or (lambda *_: calls.append("kill")))

    def test_success_and_each_phase_failure_have_exact_terminal(self):
        self.assertEqual(self.scenario("success")["terminal"], "committed")
        for phase in module.transaction.CLEAN_PHASES:
            result = self.scenario("phase-failure", phase)
            self.assertEqual((result["phase"], result["terminal"]), (phase, "rollback_verified"))

    def test_production_plan_and_invalid_phase_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            value = row(Path(raw)); value["scope"] = "production"
            with self.assertRaisesRegex(module.ScenarioError, "qualification-lab"):
                module.execute(value, "success", None)
            with self.assertRaisesRegex(module.ScenarioError, "phase differs"):
                module.execute(row(Path(raw)), "phase-failure", "unknown")

    def test_power_loss_writes_durable_marker_before_sigkill(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); marker = root / "ready.json"; observed = []
            def killed(_pid, sig):
                observed.append((sig, marker.is_file()))
                raise SystemExit("killed")
            with mock.patch.object(module.clean, "verify_bindings", return_value={}), \
                    mock.patch.object(module.clean, "drivers", return_value=({}, mock.Mock())):
                def transaction_run(_directory, _plan, _backup, _drivers, _final, *, after_phase_committed=None):
                    after_phase_committed(mock.Mock(), "clean_materials", {"phase": "clean_materials"})
                with mock.patch.object(module.transaction, "run_clean", side_effect=transaction_run), \
                        self.assertRaisesRegex(SystemExit, "killed"):
                    module.execute(row(root), "power-loss-child", "clean_materials", marker, kill_process=killed)
            self.assertEqual(observed, [(module.SIGKILL, True)])


if __name__ == "__main__":
    unittest.main()
