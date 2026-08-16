import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("clean_rehearsal_observation",
    ROOT / "scripts/collect_v1_clean_install_rehearsal_observation.py")
module = importlib.util.module_from_spec(SPEC); assert SPEC.loader; SPEC.loader.exec_module(module)


def ordinary():
    return {"schema_version": 1, "homepage_status": 200, "login_status": 200,
        "prep_health_ok": True, "prep_database_ok": True,
        "processes": [{"name": name, "pid": index + 10, "restart_time": 0, "status": "online"}
                      for index, name in enumerate(("caddy", "hydro-sandbox", "hydrooj", "mongodb"))]}


def write(path: Path, value) -> bytes:
    raw = value if isinstance(value, bytes) else (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw); path.chmod(0o600); return raw


def journal(row, kind, phase=None):
    phases = module.transaction.CLEAN_PHASES
    completed = list(phases if kind == "success" else phases[:phases.index(phase) + 1])
    value = module.transaction.initial_journal(row["plan_id"], row["backup_manifest_sha256"], phases)
    value.update({"status": "committed" if kind == "success" else "rollback_verified",
        "next_phase": None if len(completed) == len(phases) else phases[len(completed)],
        "in_progress": None, "completed": completed,
        "receipts": {name: {"phase": name, "action": "apply", "status": "verified",
                             "evidence_sha256": str(index + 3) * 64}
                     for index, name in enumerate(completed)},
        "rollback_completed": [] if kind == "success" else
            [name for name in module.transaction.CLEAN_ROLLBACK_ORDER if name in completed],
        "failure": None if kind == "success" else
            "InjectedPhaseFailure" if kind == "phase_failure" else "interrupted_apply"})
    return value


class CleanInstallRehearsalObservationTests(unittest.TestCase):
    def prepare(self, root: Path, kind: str, phase=None):
        backup = root / "backup"; output = root / "output"; transaction = root / "transaction"
        for directory in (backup, output, transaction): directory.mkdir(); directory.chmod(0o700)
        baseline_raw = write(backup / "backup-manifest.json", b'{"baseline":true}\n')
        write(backup / "ordinary-oj-before.json", ordinary())
        row = {"scope": "qualification-lab", "plan_id": "1" * 64,
            "backup_manifest_sha256": __import__("hashlib").sha256(baseline_raw).hexdigest(),
            "backup_directory": str(backup), "transaction_directory": str(transaction),
            "source_release": "2" * 40 + "-" + "3" * 12, "expected_contract": str(root / "contract"),
            "desired_controller_definition": str(root / "definition")}
        status = "committed" if kind == "success" else "rollback_verified"
        write(transaction / f"service-install.{status}-{row['plan_id']}.json", journal(row, kind, phase))
        mode = "success" if kind == "success" else "phase-failure"
        execution = {"status": "passed", "mode": mode, "phase": phase,
            "terminal": status, "plan_id": row["plan_id"],
            "backup_manifest_sha256": row["backup_manifest_sha256"]}
        write(output / "execution.log", execution)
        return row, output

    def test_collects_and_revalidates_success_artifacts(self):
        with tempfile.TemporaryDirectory() as raw:
            row, output = self.prepare(Path(raw), "success")
            value = module.collect(row, "4" * 64, output, "success", None,
                                   live_verifier=lambda *_: None, ordinary_collector=lambda _row: ordinary())
            observed = json.loads((output / "observation.json").read_text())
            self.assertEqual(value, observed)
            self.assertIs(module.validate_document(observed, output, row), observed)

    def test_collects_exact_phase_failure_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as raw:
            row, output = self.prepare(Path(raw), "phase_failure", "closed_frontend")
            value = module.collect(row, "4" * 64, output, "phase_failure", "closed_frontend",
                                   live_verifier=lambda *_: None, ordinary_collector=lambda _row: ordinary())
            self.assertEqual(value["result"]["terminal"], "rollback_verified")
            (output / "execution.log").write_text("tampered\n")
            with self.assertRaisesRegex(module.ObservationError, "artifact"):
                module.validate_document(value, output, row)

    def test_rollback_allows_planned_hydro_restart_but_rejects_stable_process_change(self):
        with tempfile.TemporaryDirectory() as raw:
            row, output = self.prepare(Path(raw), "phase_failure", "hydro_integration")
            after = ordinary()
            after["processes"][2].update({"pid": 999, "restart_time": 1})
            value = module.collect(row, "4" * 64, output, "phase_failure", "hydro_integration",
                                   live_verifier=lambda *_: None, ordinary_collector=lambda _row: after)
            self.assertNotEqual(value["artifacts"]["ordinary_before"]["sha256"],
                                value["artifacts"]["ordinary_after"]["sha256"])

        with tempfile.TemporaryDirectory() as raw:
            row, output = self.prepare(Path(raw), "phase_failure", "hydro_integration")
            after = ordinary()
            after["processes"][0].update({"pid": 999, "restart_time": 1})
            with self.assertRaisesRegex(module.OrdinaryBackupError, "stable PM2 process changed"):
                module.collect(row, "4" * 64, output, "phase_failure", "hydro_integration",
                               live_verifier=lambda *_: None, ordinary_collector=lambda _row: after)

    def test_rejects_wrong_failure_boundary(self):
        with tempfile.TemporaryDirectory() as raw:
            row, output = self.prepare(Path(raw), "phase_failure", "controller")
            receipt = output.parent / "transaction" / f"service-install.rollback_verified-{row['plan_id']}.json"
            value = json.loads(receipt.read_text()); value["failure"] = "interrupted_apply"; write(receipt, value)
            with self.assertRaisesRegex(module.ObservationError, "failure boundary"):
                module.collect(row, "4" * 64, output, "phase_failure", "controller",
                               live_verifier=lambda *_: None, ordinary_collector=lambda _row: ordinary())


if __name__ == "__main__": unittest.main()
