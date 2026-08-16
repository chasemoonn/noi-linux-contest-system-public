import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "scripts"))
BACKUP = ROOT / "scripts/verify_v1_install_backup.py"
spec = importlib.util.spec_from_file_location("backup", BACKUP); backup = importlib.util.module_from_spec(spec); spec.loader.exec_module(backup)
SCRIPT = ROOT / "scripts/verify_v1_install_rollback.py"
spec2 = importlib.util.spec_from_file_location("rollback", SCRIPT); rollback = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(rollback)


class InstallRollbackTests(unittest.TestCase):
    def make(self, parent: Path):
        before = parent / "before"; after = parent / "after"; output = parent / "output"
        before.mkdir(); after.mkdir(); output.mkdir()
        for directory in (before, after, output):
            directory.chmod(0o700)
        artifacts = {}
        for name, filename in {**backup.REQUIRED, **backup.OPTIONAL}.items():
            present = name in backup.REQUIRED
            if present:
                raw = (name + "\n").encode()
                for root in (before, after): root.joinpath(filename).write_bytes(raw)
                mode = stat.S_IMODE((before / filename).stat().st_mode)
                artifacts[name] = {"filename": filename, "present": True, "bytes": len(raw),
                                   "mode": mode, "sha256": hashlib.sha256(raw).hexdigest()}
            else:
                artifacts[name] = {"filename": filename, "present": False, "bytes": None, "mode": None, "sha256": None}
        manifest = {"$schema": "v1-install-backup-manifest.schema.json", "schema_version": 1,
                    "plan_id": "1" * 64, "source": {"revision": "2" * 40, "manifest_sha256": "3" * 64},
                    "created_at": "2026-08-13T12:00:00Z", "artifacts": artifacts}
        (before / "backup-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return before, after, output

    def test_exact_restore_creates_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            before, after, output = self.make(Path(raw)); receipt = output / "receipt.json"
            def write_receipt(path, value):
                content = rollback.canonical(value); path.write_bytes(content); return content
            with mock.patch.object(rollback, "safe_directory", side_effect=lambda path: path.resolve()), \
                    mock.patch.object(rollback, "atomic_receipt", side_effect=write_receipt):
                result = rollback.verify(before, after, "1" * 64, Path(raw) / "pending", receipt)
            self.assertEqual(result["status"], "rollback_verified")
            self.assertEqual(json.loads(receipt.read_text())["restored_artifacts"], 21)

    def test_changed_artifact_and_pending_marker_fail(self):
        with tempfile.TemporaryDirectory() as raw:
            before, after, output = self.make(Path(raw)); (after / backup.REQUIRED["ordinary_oj_snapshot"]).write_text("changed")
            with self.assertRaisesRegex(rollback.RollbackError, "ordinary_oj_snapshot"):
                rollback.compare(before, after, "1" * 64)
        with tempfile.TemporaryDirectory() as raw:
            before, after, output = self.make(Path(raw)); pending = Path(raw) / "pending"; pending.write_text("x")
            with self.assertRaisesRegex(rollback.RollbackError, "pending"):
                rollback.verify(before, after, "1" * 64, pending, output / "receipt.json")


if __name__ == "__main__": unittest.main()
