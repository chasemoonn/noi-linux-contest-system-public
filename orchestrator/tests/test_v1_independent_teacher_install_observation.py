import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("teacher_install_observation",
    ROOT / "scripts" / "collect_v1_independent_teacher_install_observation.py")
module = importlib.util.module_from_spec(SPEC); assert SPEC.loader; SPEC.loader.exec_module(module)


def ordinary():
    return {"schema_version": 1, "homepage_status": 200, "login_status": 200,
        "prep_health_ok": True, "prep_database_ok": True,
        "processes": [{"name": name, "pid": index + 10, "restart_time": 0, "status": "online"}
                      for index, name in enumerate(("caddy", "hydro-sandbox", "hydrooj", "mongodb"))]}


def write(path: Path, value) -> bytes:
    raw = value if isinstance(value, bytes) else module.canonical(value)
    path.write_bytes(raw); path.chmod(0o600); return raw


def plan(root: Path) -> dict:
    baseline = write(root / "fresh-baseline.json", b'{"baseline":true}\n')
    return {"plan_id": "1" * 64, "backup_manifest_sha256": hashlib.sha256(baseline).hexdigest()}


def sealed_case(root: Path, plan_value: dict, *, corrupt_receipt: bool = False) -> None:
    phases = module.transaction.CLEAN_PHASES
    receipt = module.transaction.initial_journal(plan_value["plan_id"],
        plan_value["backup_manifest_sha256"], phases)
    receipt.update({"status": "rollback_verified", "next_phase": None, "in_progress": None,
        "completed": list(phases),
        "receipts": {phase: {"phase": phase, "action": "apply", "status": "verified",
            "evidence_sha256": (str(index + 1) * 64)[:64]}
            for index, phase in enumerate(phases)},
        "rollback_completed": list(module.transaction.CLEAN_ROLLBACK_ORDER),
        "failure": "InjectedPhaseFailure"})
    if corrupt_receipt:
        receipt["receipts"]["controller"]["evidence_sha256"] = "bad"
    execution = {"status": "passed", "mode": "phase-failure",
        "phase": "post_install_verification", "terminal": "rollback_verified",
        "plan_id": plan_value["plan_id"],
        "backup_manifest_sha256": plan_value["backup_manifest_sha256"]}
    values = {
        "execution_log": ("execution.log", module.canonical(execution)),
        "ordinary_after": ("ordinary-after.json", module.canonical(ordinary())),
        "fresh_baseline": ("fresh-baseline.json", b'{"baseline":true}\n'),
        "ordinary_before": ("ordinary-before.json", module.canonical(ordinary())),
        "terminal_receipt": ("terminal-receipt.json", module.canonical(receipt)),
    }
    artifacts = {}
    for name, (filename, raw) in values.items():
        write(root / filename, raw)
        artifacts[name] = {"filename": filename, "bytes": len(raw),
                           "sha256": hashlib.sha256(raw).hexdigest()}
    observation = {"$schema": "v1-clean-install-rehearsal-observation.schema.json",
        "schema_version": 1, "plan_id": plan_value["plan_id"],
        "private_plan_sha256": "2" * 64,
        "backup_manifest_sha256": plan_value["backup_manifest_sha256"],
        "kind": "phase_failure", "phase": "post_install_verification",
        "result": {"terminal": "rollback_verified", "clean_target": True,
            "caddy_restored": True, "hydro_restored": True, "controller_absent": True,
            "cloud_state": "STOPPED", "pending_markers": 0, "ordinary_oj_errors": 0,
            "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0},
        "artifacts": artifacts}
    write(root / "observation.json", observation)


def matrix():
    revision = "1" * 40
    return {"session_id": "a" * 64,
        "host": {"anonymous_id": "b" * 64},
        "source": {"revision": revision, "tree": "2" * 40},
        "components": {"orchestrator_image_digest": "sha256:" + "3" * 64,
            "desktop_image_id": "sha256:" + "4" * 64,
            "desktop_source_revision": revision, "hydro_plugin_sha256": "5" * 64}}


class TeacherInstallObservationTests(unittest.TestCase):
    def test_assembles_exact_unsigned_observation(self):
        value = matrix()
        result = module.assemble(value["source"], value["components"],
            {"anonymous_id": "6" * 64, "architecture": "x86_64", "kernel": "6.8",
             "os_release_sha256": "7" * 64},
            "NOI-V1-TEACHER-1234567890ABCDEF", "8" * 64, "2026-08-14T00:00:00Z")
        self.assertTrue(result["checks"]["candidate_verified"])
        self.assertTrue(result["checks"]["rollback_verified"])
        self.assertEqual(result["artifacts"], module.ARTIFACTS)

    def test_rejects_same_machine_as_matrix(self):
        value = matrix(); machine = b"machine-one"
        value["host"]["anonymous_id"] = hashlib.sha256(
            value["session_id"].encode() + b":" + machine).hexdigest()
        with self.assertRaisesRegex(module.CollectionError, "different machine"):
            module.current_host(value, "NOI-V1-TEACHER-1234567890ABCDEF",
                                machine=machine, release=b"OS=test\n")
        other = module.current_host(value, "NOI-V1-TEACHER-1234567890ABCDEF",
                                    machine=b"machine-two", release=b"OS=test\n")
        self.assertNotEqual(other["anonymous_id"], value["host"]["anonymous_id"])

    def test_accepts_only_full_final_phase_rollback_case(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); value = plan(root); case = root / "case"; case.mkdir(); case.chmod(0o700)
            sealed_case(case, value)
            observed, receipt = module.load_teacher_case(case, value, "2" * 64)
            self.assertEqual(observed["phase"], "post_install_verification")
            self.assertEqual(receipt["completed"], list(module.transaction.CLEAN_PHASES))

    def test_rejects_unverified_phase_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); value = plan(root); case = root / "case"; case.mkdir(); case.chmod(0o700)
            sealed_case(case, value, corrupt_receipt=True)
            with self.assertRaisesRegex(module.CollectionError, "every clean-install phase"):
                module.load_teacher_case(case, value, "2" * 64)


if __name__ == "__main__": unittest.main()
