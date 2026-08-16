import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("clean_rehearsal_case",
    ROOT / "scripts/run_v1_clean_install_rehearsal_case.py")
module = importlib.util.module_from_spec(SPEC); assert SPEC.loader; SPEC.loader.exec_module(module)


def row():
    return {"scope": "qualification-lab", "plan_id": "1" * 64,
        "backup_manifest_sha256": "2" * 64, "executables": {"python": "/usr/bin/python3"}}


def sealed(kind, phase):
    return {"kind": kind, "phase": phase, "plan_id": "1" * 64}


class CleanInstallRehearsalCaseTests(unittest.TestCase):
    def test_success_and_phase_failure_use_scenario_process_then_collector(self):
        for kind, phase in (("success", None), ("phase_failure", "controller")):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw); calls = []
                class Child:
                    pid = 1234
                    def wait(self, timeout=None): return 0
                def popen(_command, **kwargs):
                    calls.append(kwargs); kwargs["stdout"].write(b"result\n"); return Child()
                result = module.execute(row(), root / "plan", "3" * 64, kind, phase,
                    root / "case", 10, popen=popen, contain=lambda _pid: None,
                    collector=lambda *_args: sealed(kind, phase))
                self.assertEqual(result["status"], "sealed"); self.assertEqual(len(calls), 1)
                self.assertTrue((root / "case" / "execution.log").is_file())

    def test_power_loss_uses_supervisor_without_second_scenario_process(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); called = []
            def supervise(_row, _plan, _sha, phase, ready, child, resume, _timeout):
                called.append(phase)
                for path in (ready, child, resume): path.write_bytes(b"evidence"); path.chmod(0o600)
                return {"status": "passed"}
            result = module.execute(row(), root / "plan", "3" * 64, "power_loss", "source_release",
                root / "case", 10, popen=lambda *_a, **_k: self.fail("unexpected run"),
                power_supervise=supervise,
                collector=lambda *_args: sealed("power_loss", "source_release"))
            self.assertEqual(called, ["source_release"]); self.assertEqual(result["status"], "sealed")

    def test_rejects_production_scope_and_existing_case_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); value = row(); value["scope"] = "production"
            with self.assertRaisesRegex(module.CaseError, "scope"):
                module.execute(value, root / "p", "3" * 64, "success", None, root / "case", 10)
            value["scope"] = "qualification-lab"; (root / "case").mkdir()
            with self.assertRaisesRegex(module.CaseError, "already exists"):
                module.execute(value, root / "p", "3" * 64, "success", None, root / "case", 10)

    def test_logged_timeout_contains_process_group(self):
        with tempfile.TemporaryDirectory() as raw:
            calls = []
            class Child:
                pid = 7654
                count = 0
                def wait(self, timeout=None):
                    self.count += 1
                    if self.count == 1: raise module.subprocess.TimeoutExpired("case", timeout)
                    return -module.power.SIGKILL
            result = module.run_logged(["case"], Path(raw) / "case.log", 10,
                popen=lambda *_a, **_k: Child(), contain=lambda pid: calls.append(pid))
            self.assertEqual(result, (-module.power.SIGKILL, True))
            self.assertGreaterEqual(calls.count(7654), 2)


if __name__ == "__main__": unittest.main()
