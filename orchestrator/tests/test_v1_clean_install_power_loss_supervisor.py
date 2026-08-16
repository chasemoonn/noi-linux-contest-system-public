import importlib.util
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("clean_power_loss_supervisor",
    ROOT / "scripts/run_v1_clean_install_power_loss_supervisor.py")
module = importlib.util.module_from_spec(SPEC); assert SPEC.loader; SPEC.loader.exec_module(module)


def plan(root: Path):
    return {"scope": "qualification-lab", "plan_id": "1" * 64,
        "backup_manifest_sha256": "2" * 64,
        "executables": {"python": "/usr/bin/python3"},
        "transaction_directory": str(root / "transaction")}


class FakeChild:
    def __init__(self, ready: Path, row: dict, phase: str, returncode=-getattr(signal, "SIGKILL", 9)):
        self.pid = 4321; self.ready = ready; self.row = row; self.phase = phase
        self.returncode = returncode; self.polled = False

    def poll(self):
        if not self.polled:
            self.ready.write_text(json.dumps({"schema_version": 1, "plan_id": self.row["plan_id"],
                "mode": "power_loss", "phase": self.phase, "pid": self.pid}))
            self.ready.chmod(0o600); self.polled = True
            return None
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class CleanPowerLossSupervisorTests(unittest.TestCase):
    def run_case(self, *, returncode=None):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); ready = root / "ready.json"; child_log = root / "child.log"
            resume_log = root / "resume.log"; row = plan(root)
            (root / "transaction").mkdir(mode=0o700)
            (root / "transaction" / "service-install.pending.json").write_text("pending")
            (root / "transaction" / "service-install.pending.json").chmod(0o600)
            fake = FakeChild(ready, row, "controller",
                -module.SIGKILL if returncode is None else returncode)
            def spawn(*_args, **_kwargs): return fake
            def resume(*_args, **kwargs):
                value = {"status": "passed", "mode": "resume", "phase": None,
                    "terminal": "rollback_verified", "plan_id": row["plan_id"],
                    "backup_manifest_sha256": row["backup_manifest_sha256"]}
                kwargs["stdout"].write((json.dumps(value) + "\n").encode())
                return mock.Mock(pid=9876, wait=mock.Mock(return_value=0))
            result = module.supervise(row, root / "plan.json", "3" * 64, "controller",
                ready, child_log, resume_log, 10, popen=spawn, resume_popen=resume,
                monotonic=mock.Mock(side_effect=[0, 0, 1]), sleep=lambda _seconds: None,
                contain=lambda _pid: None)
            return result

    def test_requires_real_sigkill_then_exact_resume(self):
        result = self.run_case()
        self.assertEqual((result["child_signal"], result["terminal"]),
                         ("SIGKILL", "rollback_verified"))

    def test_non_sigkill_is_rejected_after_resume(self):
        with self.assertRaisesRegex(module.PowerLossSupervisorError, "not terminated by SIGKILL"):
            self.run_case(returncode=1)

    def test_scope_phase_timeout_and_existing_output_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); row = plan(root); row["scope"] = "production"
            with self.assertRaisesRegex(module.PowerLossSupervisorError, "scope"):
                module.supervise(row, root / "p", "3" * 64, "controller", root / "r",
                    root / "c", root / "x", 10)
            row["scope"] = "qualification-lab"
            (root / "transaction").mkdir(mode=0o700)
            with self.assertRaisesRegex(module.PowerLossSupervisorError, "timeout"):
                module.supervise(row, root / "p", "3" * 64, "controller", root / "r",
                    root / "c", root / "x", 1)
            (root / "r").write_text("preexisting")
            with self.assertRaisesRegex(module.PowerLossSupervisorError, "already exists"):
                module.supervise(row, root / "p", "3" * 64, "controller", root / "r",
                    root / "c", root / "x", 10)


if __name__ == "__main__":
    unittest.main()
