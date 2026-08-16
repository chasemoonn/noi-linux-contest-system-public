import importlib.util
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_v1_independent_teacher_install_evidence.py"
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("teacher_evidence_builder", SCRIPT)
module = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module)


def ordinary():
    return {"schema_version": 1, "homepage_status": 200, "login_status": 200,
        "prep_health_ok": True, "prep_database_ok": True,
        "processes": [{"name": name, "pid": index + 10, "restart_time": 0, "status": "online"}
                      for index, name in enumerate(("caddy", "hydro-sandbox", "hydrooj", "mongodb"))]}


def machine_artifacts(root: Path) -> dict:
    plan_id = "a" * 64; backup = "b" * 64
    receipt = module.transaction.initial_journal(plan_id, backup, module.transaction.CLEAN_PHASES)
    receipt.update({"status": "rollback_verified", "next_phase": None, "in_progress": None,
        "completed": list(module.transaction.CLEAN_PHASES),
        "receipts": {phase: {"phase": phase, "action": "apply", "status": "verified",
            "evidence_sha256": str(index + 1) * 64}
            for index, phase in enumerate(module.transaction.CLEAN_PHASES)},
        "rollback_completed": list(module.transaction.CLEAN_ROLLBACK_ORDER),
        "failure": "InjectedPhaseFailure"})
    execution = {"status": "passed", "mode": "phase-failure",
        "phase": "post_install_verification", "terminal": "rollback_verified",
        "plan_id": plan_id, "backup_manifest_sha256": backup}
    values = {"install.log": execution, "rollback.json": receipt,
              "before.json": ordinary(), "after.json": ordinary(), "matrix.json": {"matrix": True}}
    for name, value in values.items():
        (root / name).write_bytes((json.dumps(value, sort_keys=True) + "\n").encode())
        (root / name).chmod(0o600)
    return {"install_log": "install.log", "rollback_receipt": "rollback.json",
            "ordinary_oj_before": "before.json", "ordinary_oj_after": "after.json",
            "clean_install_rehearsal": "matrix.json"}


class TeacherEvidenceBuilderTests(unittest.TestCase):
    def make_candidate(self, root: Path, *, extra: bool = False) -> tuple[Path, str]:
        candidate = root / "candidate"; candidate.mkdir(mode=0o700)
        verifier = b'#!/usr/bin/env python3\nimport json,sys\nm=json.load(open(sys.argv[1]+"/candidate-manifest.json"))\nprint(json.dumps({"revision":m["source"]["revision"],"tree":m["source"]["tree"],"archive_sha256":m["source"]["archive"]["sha256"],"manifest_sha256":"PLACEHOLDER"}))\n'
        # The verifier needs the final manifest hash, so use a constant-output
        # script after the manifest has been assembled and rebuild once.
        revision = "1" * 40; tree = "2" * 40
        files = {"scripts/verify_v1_candidate.py": verifier, "support.py": b"trusted\n"}
        def archive_bytes() -> bytes:
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:") as bundle:
                for directory in ("noi-linux-contest-system-v1", "noi-linux-contest-system-v1/scripts"):
                    info = tarfile.TarInfo(directory); info.type = tarfile.DIRTYPE; info.mode = 0o755
                    bundle.addfile(info)
                for relative, content in files.items():
                    info = tarfile.TarInfo(f"noi-linux-contest-system-v1/{relative}")
                    info.size = len(content); info.mode = 0o755 if relative.endswith(".py") else 0o644
                    bundle.addfile(info, io.BytesIO(content))
                if extra:
                    info = tarfile.TarInfo("noi-linux-contest-system-v1/extra.txt"); info.size = 1; info.mode = 0o644
                    bundle.addfile(info, io.BytesIO(b"x"))
            return stream.getvalue()
        archive = archive_bytes()
        manifest = {"source": {"revision": revision, "tree": tree,
            "archive": {"name": "source.tar", "sha256": hashlib.sha256(archive).hexdigest()},
            "tracked_file_count": len(files), "files": [
                {"path": name, "mode": "100755" if name.endswith(".py") else "100644",
                 "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in files.items()]}}
        manifest_raw = json.dumps(manifest, sort_keys=True).encode()
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        verifier = verifier.replace(b"PLACEHOLDER", manifest_sha.encode())
        files["scripts/verify_v1_candidate.py"] = verifier
        archive = archive_bytes()
        manifest["source"]["archive"]["sha256"] = hashlib.sha256(archive).hexdigest()
        manifest["source"]["files"][0]["bytes"] = len(verifier)
        manifest["source"]["files"][0]["sha256"] = hashlib.sha256(verifier).hexdigest()
        # Self-referential manifest hashes are deliberately avoided: the stub
        # emits its actual manifest digest at run time.
        verifier = b'#!/usr/bin/env python3\nimport hashlib,json,sys\np=sys.argv[1]+"/candidate-manifest.json"; raw=open(p,"rb").read(); m=json.loads(raw)\nprint(json.dumps({"revision":m["source"]["revision"],"tree":m["source"]["tree"],"archive_sha256":m["source"]["archive"]["sha256"],"manifest_sha256":hashlib.sha256(raw).hexdigest()}))\n'
        files["scripts/verify_v1_candidate.py"] = verifier
        archive = archive_bytes()
        manifest["source"]["archive"]["sha256"] = hashlib.sha256(archive).hexdigest()
        manifest["source"]["files"][0]["bytes"] = len(verifier)
        manifest["source"]["files"][0]["sha256"] = hashlib.sha256(verifier).hexdigest()
        manifest_raw = json.dumps(manifest, sort_keys=True).encode()
        (candidate / "source.tar").write_bytes(archive)
        (candidate / "candidate-manifest.json").write_bytes(manifest_raw)
        (candidate / "source.tar").chmod(0o600)
        (candidate / "candidate-manifest.json").chmod(0o600)
        return candidate, hashlib.sha256(manifest_raw).hexdigest()

    def test_candidate_verifier_runs_only_from_pinned_exact_archive(self):
        with __import__("tempfile").TemporaryDirectory() as raw:
            candidate, manifest_sha = self.make_candidate(Path(raw))
            result = module.verify_candidate(candidate, manifest_sha)
            self.assertEqual(result["manifest_sha256"], manifest_sha)
            with self.assertRaisesRegex(module.BuildError, "external trust pin"):
                module.verify_candidate(candidate, "0" * 64)

    def test_candidate_archive_rejects_unmanifested_file(self):
        with __import__("tempfile").TemporaryDirectory() as raw:
            candidate, manifest_sha = self.make_candidate(Path(raw), extra=True)
            with self.assertRaisesRegex(module.BuildError, "unexpected entry"):
                module.verify_candidate(candidate, manifest_sha)

    def test_artifact_set_and_before_after_are_exact(self):
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            rows = machine_artifacts(root)
            result = module.artifact_hashes(root, rows)
            self.assertEqual(result["ordinary_oj_before_sha256"], result["ordinary_oj_after_sha256"])

    def test_artifacts_reject_paths_and_changed_oj_baseline(self):
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            rows = machine_artifacts(root)
            (root / "after.json").write_bytes((json.dumps({**ordinary(), "homepage_status": 503}) + "\n").encode())
            with self.assertRaisesRegex(module.BuildError, "before/after"):
                module.artifact_hashes(root, rows)
            machine_artifacts(root)
            rows["install_log"] = "../secret"
            with self.assertRaisesRegex(module.BuildError, "unsafe"):
                module.artifact_hashes(root, rows)

    def test_rejects_handwritten_install_log_or_incomplete_receipt(self):
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw); rows = machine_artifacts(root)
            (root / "install.log").write_bytes(b'{"status":"passed"}\n')
            with self.assertRaisesRegex(module.BuildError, "execution log differs"):
                module.artifact_hashes(root, rows)
            machine_artifacts(root)
            receipt = json.loads((root / "rollback.json").read_text())
            receipt["completed"].pop(); receipt["receipts"].pop("post_install_verification")
            receipt["rollback_completed"].remove("post_install_verification")
            (root / "rollback.json").write_bytes((json.dumps(receipt) + "\n").encode())
            with self.assertRaisesRegex(module.BuildError, "complete install"):
                module.artifact_hashes(root, rows)


if __name__ == "__main__":
    unittest.main()
