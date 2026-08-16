import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "apply_v1_closed_frontend.py"
spec = importlib.util.spec_from_file_location("closed_frontend_phase", SCRIPT)
phase = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(phase)
import verify_v1_install_backup as backup_module


class FakeAdmin:
    def __init__(self, baseline, desired=None):
        self.live = baseline; self.desired = desired or {"apps": {"http": {"servers": {"desired": {}}}}}
        self.etag = 0

    def request(self, method, path, body=None, headers=None):
        if method == "GET" and path == "/config/":
            return 200, {"etag": f'"{self.etag}"'}, json.dumps(self.live).encode()
        if method == "POST" and path == "/adapt":
            raw = bytes(body)
            if b"noi-orchestrator-private-submit" in raw:
                result = self.desired
            else:
                result = self.live
            return 200, {}, json.dumps({"warnings": [], "result": result}).encode()
        if method == "POST" and path == "/config/":
            self.live = json.loads(bytes(body)); self.etag += 1
            return 200, {}, b""
        raise AssertionError((method, path))


class ClosedFrontendPhaseTests(unittest.TestCase):
    PLAN = "1" * 64

    def baseline_disk(self):
        return b''':80 {\n  respond 200\n}\noj.example.test {\n  reverse_proxy 127.0.0.1:8888\n}\n'''

    def backup(self, root: Path, disk: bytes, active: dict, snippet: bytes):
        artifacts = {}
        for name, filename in {**backup_module.REQUIRED, **backup_module.OPTIONAL}.items():
            present = name in backup_module.REQUIRED
            if present:
                if name == "caddyfile": raw = disk
                elif name == "caddy_active": raw = json.dumps(active).encode()
                elif name == "caddy_snippet": raw = snippet
                else: raw = (name + "\n").encode()
                (root / filename).write_bytes(raw); os.chmod(root / filename, 0o600)
                mode = stat.S_IMODE((root / filename).stat().st_mode)
                artifacts[name] = {"filename": filename, "present": True, "bytes": len(raw),
                                   "mode": mode, "sha256": hashlib.sha256(raw).hexdigest()}
            else:
                artifacts[name] = {"filename": filename, "present": False, "bytes": None,
                                   "mode": None, "sha256": None}
        manifest = {"$schema":"v1-install-backup-manifest.schema.json","schema_version":1,
                    "plan_id":self.PLAN,"source":{"revision":"2"*40,"manifest_sha256":"3"*64},
                    "created_at":"2026-08-13T12:00:00Z","artifacts":artifacts}
        raw=(json.dumps(manifest,sort_keys=True,separators=(",",":"))+"\n").encode()
        (root/"backup-manifest.json").write_bytes(raw); os.chmod(root/"backup-manifest.json",0o600)
        return hashlib.sha256(raw).hexdigest()

    def test_transform_is_closed_hardened_and_idempotent(self):
        result = phase.harden_caddyfile(self.baseline_disk(), "oj.example.test", Path("/opt/noi/snippet"))
        self.assertEqual(result, phase.harden_caddyfile(result, "oj.example.test", Path("/opt/noi/snippet")))
        self.assertEqual(result.count(b"noi-orchestrator-private-submit"), 2)
        self.assertEqual(result.count(b"import /opt/noi/snippet"), 1)
        closed = phase.render_closed("exam.example.test", "http://127.0.0.1:8600")
        self.assertIn('respond "\u6bd4\u8d5b\u684c\u9762\u5c1a\u672a\u5f00\u653e" 503'.encode(), closed)

    def test_stale_marker_without_exact_404_rule_is_rejected(self):
        stale = self.baseline_disk().replace(b"  respond 200\n", b"  # noi-orchestrator-private-submit\n  respond 200\n")
        with self.assertRaisesRegex(phase.ClosedFrontendError,"hardening rule"):
            phase.harden_caddyfile(stale,"oj.example.test",Path("/opt/noi/snippet"))

    def test_apply_and_rollback_restore_exact_disk_and_live_state(self):
        with tempfile.TemporaryDirectory() as raw:
            base=Path(raw); backup=base/"backup"; transaction=base/"transaction"
            backup.mkdir(); transaction.mkdir(); os.chmod(backup,0o700); os.chmod(transaction,0o700)
            disk=self.baseline_disk(); active={"apps":{"http":{"servers":{"baseline":{}}}}}
            snippet=b"old snippet\n"; pin=self.backup(backup,disk,active,snippet)
            caddyfile=base/"Caddyfile"; caddyfile.write_bytes(disk); os.chmod(caddyfile,0o600)
            snippet_path=base/"snippet"; snippet_path.write_bytes(snippet); os.chmod(snippet_path,0o600)
            admin=FakeAdmin(active)
            with mock.patch.object(phase,"safe_directory",side_effect=lambda p:Path(p).resolve()):
                applied=phase.apply_phase(backup,transaction,self.PLAN,pin,caddyfile,snippet_path,
                    "oj.example.test","exam.example.test","http://127.0.0.1:8600",admin)
                self.assertTrue(applied["closed"]); self.assertNotEqual(caddyfile.read_bytes(),disk)
                rolled=phase.rollback_phase(backup,transaction,self.PLAN,pin,caddyfile,snippet_path,admin)
            self.assertTrue(rolled["changed"]); self.assertEqual(caddyfile.read_bytes(),disk)
            self.assertEqual(snippet_path.read_bytes(),snippet); self.assertEqual(admin.live,active)

    def test_apply_rejects_disk_live_backup_drift_before_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            base=Path(raw); backup=base/"backup"; transaction=base/"transaction"
            backup.mkdir(); transaction.mkdir(); os.chmod(backup,0o700); os.chmod(transaction,0o700)
            disk=self.baseline_disk(); active={"apps":{"http":{"servers":{"baseline":{}}}}}
            pin=self.backup(backup,disk,active,b"old\n")
            caddyfile=base/"Caddyfile"; caddyfile.write_bytes(disk); snippet=base/"snippet"; snippet.write_bytes(b"old\n")
            admin=FakeAdmin({"different":{}})
            with mock.patch.object(phase,"safe_directory",side_effect=lambda p:Path(p).resolve()), \
                    self.assertRaisesRegex(phase.ClosedFrontendError,"live Caddy config"):
                phase.apply_phase(backup,transaction,self.PLAN,pin,caddyfile,snippet,
                    "oj.example.test","exam.example.test","http://127.0.0.1:8600",admin)
            self.assertEqual(caddyfile.read_bytes(),disk); self.assertEqual(snippet.read_bytes(),b"old\n")

    def test_clean_rollback_removes_transaction_owned_snippet(self):
        with tempfile.TemporaryDirectory() as raw:
            base=Path(raw); backup=base/"backup"; transaction=base/"transaction"
            backup.mkdir(); transaction.mkdir(); os.chmod(backup,0o700); os.chmod(transaction,0o700)
            disk=self.baseline_disk(); active={"apps":{"http":{"servers":{"baseline":{}}}}}
            self.backup(backup,disk,active,b"old\n")
            manifest_path=backup/"backup-manifest.json"; manifest=json.loads(manifest_path.read_text())
            manifest["$schema"]="v1-clean-install-backup-manifest.schema.json"
            manifest["operation"]="clean-install"
            for key in backup_module.CLEAN_MUST_BE_ABSENT:
                entry=manifest["artifacts"][key]
                if entry["present"]:(backup/entry["filename"]).unlink()
                entry.update({"present":False,"bytes":None,"mode":None,"sha256":None})
            target=b"{}\n";(backup/"clean-target.json").write_bytes(target)
            manifest["artifacts"]["clean_target"]={"filename":"clean-target.json","present":True,
                "bytes":len(target),"mode":stat.S_IMODE((backup/"clean-target.json").stat().st_mode),
                "sha256":hashlib.sha256(target).hexdigest()}
            encoded=(json.dumps(manifest,sort_keys=True,separators=(",",":"))+"\n").encode()
            manifest_path.write_bytes(encoded);pin=hashlib.sha256(encoded).hexdigest()
            caddyfile=base/"Caddyfile";caddyfile.write_bytes(disk);snippet=base/"snippet"
            admin=FakeAdmin(active)
            def adapt_after_import_exists(selected_admin, raw):
                if b"import " in raw:
                    self.assertTrue(snippet.is_file())
                    self.assertIn(b"respond", snippet.read_bytes())
                    return admin.desired
                return active
            with mock.patch.object(phase,"safe_directory",side_effect=lambda p:Path(p).resolve()), \
                    mock.patch.object(phase,"adapt",side_effect=adapt_after_import_exists):
                phase.apply_phase(backup,transaction,self.PLAN,pin,caddyfile,snippet,
                    "oj.example.test","exam.example.test","http://127.0.0.1:8600",admin)
                self.assertTrue(snippet.is_file())
                result=phase.rollback_phase(backup,transaction,self.PLAN,pin,caddyfile,snippet,admin)
            self.assertTrue(result["changed"]);self.assertFalse(os.path.lexists(snippet))
            self.assertEqual(caddyfile.read_bytes(),disk);self.assertEqual(admin.live,active)


if __name__ == "__main__": unittest.main()
