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
SCRIPT = ROOT / "scripts" / "verify_v1_install_backup.py"
spec = importlib.util.spec_from_file_location("install_backup", SCRIPT)
module = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module)
BUILDER = ROOT / "scripts" / "build_v1_install_backup_manifest.py"
builder_spec = importlib.util.spec_from_file_location("install_backup_builder", BUILDER)
builder = importlib.util.module_from_spec(builder_spec); assert builder_spec.loader; builder_spec.loader.exec_module(builder)


class InstallBackupTests(unittest.TestCase):
    def controller_definition(self):
        identity={"container_id":"a"*64,"name":"/noi-orchestrator","image_id":"sha256:"+"b"*64,
                  "config":{},"host_config":{},"mounts":[]}
        canonical=json.dumps(identity,sort_keys=True,separators=(",",":")).encode()
        return {"schema_version":1,"present":True,"container":{"container_id":"a"*64,
            "name":"/noi-orchestrator","image_id":"sha256:"+"b"*64,"running":True,"restart_count":0,
            "immutable_identity":identity,"immutable_identity_sha256":hashlib.sha256(canonical).hexdigest()}}

    def cloud(self):
        return {"schema_version":1,"enabled":True,"desired_open":False,"open":False,"closed":True,
                "healthy":True,"managed_count":0,"conflict_count":0,"management_healthy":True,
                "management_missing_count":0,"instance_state":"STOPPED"}

    def create_artifacts(self, root: Path):
        for name, filename in module.REQUIRED.items():
            if name == "controller_definition":
                raw = (json.dumps(self.controller_definition())+"\n").encode()
            elif name == "controller_image":
                raw = (json.dumps({"schema_version":1,"present":True,"image_id":"sha256:"+"b"*64})+"\n").encode()
            elif name == "cloud_snapshot":
                raw = (json.dumps(self.cloud())+"\n").encode()
            elif name == "ordinary_oj_snapshot":
                raw = (json.dumps({"schema_version":1,"homepage_status":200,"login_status":200,
                    "prep_health_ok":True,"prep_database_ok":True,"processes":[
                    {"name":proc,"pid":index+1,"restart_time":0,"status":"online"}
                    for index,proc in enumerate(("caddy","hydro-sandbox","hydrooj","mongodb"))]})+"\n").encode()
            else:
                raw = (name + "\n").encode()
            (root / filename).write_bytes(raw)

    def create_backup(self, root: Path) -> dict:
        artifacts = {}
        for name, filename in {**module.REQUIRED, **module.OPTIONAL}.items():
            present = name in module.REQUIRED
            if present:
                raw = (name + "\n").encode(); (root / filename).write_bytes(raw); os.chmod(root / filename, 0o600)
                artifacts[name] = {"filename": filename, "present": True, "bytes": len(raw),
                                   "mode": stat.S_IMODE((root / filename).stat().st_mode),
                                   "sha256": hashlib.sha256(raw).hexdigest()}
            else:
                artifacts[name] = {"filename": filename, "present": False, "bytes": None,
                                   "mode": None, "sha256": None}
        manifest = {"$schema": "v1-install-backup-manifest.schema.json", "schema_version": 1,
                    "plan_id": "1" * 64, "source": {"revision": "2" * 40, "manifest_sha256": "3" * 64},
                    "created_at": "2026-08-13T12:00:00Z", "artifacts": artifacts}
        (root / "backup-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(root / "backup-manifest.json", 0o600)
        return manifest

    def test_exact_backup_passes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); manifest = self.create_backup(root)
            result = module.validate_manifest(manifest, root, expected_plan_id="1" * 64)
            self.assertEqual(result["plan_id"], "1" * 64)

    def test_missing_required_and_unmanifested_files_fail(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); manifest = self.create_backup(root)
            (root / module.REQUIRED["caddy_active"]).unlink()
            with self.assertRaisesRegex(module.BackupError, "missing"):
                module.validate_manifest(manifest, root)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); manifest = self.create_backup(root); (root / "secret-copy").write_text("x")
            with self.assertRaisesRegex(module.BackupError, "unmanifested"):
                module.validate_manifest(manifest, root)

    def test_optional_absence_is_explicit_and_wrong_plan_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); manifest = self.create_backup(root)
            with self.assertRaisesRegex(module.BackupError, "plan ID"):
                module.validate_manifest(manifest, root, expected_plan_id="9" * 64)
            entry = manifest["artifacts"]["pm2_dump_backup"]
            entry["sha256"] = "0" * 64
            with self.assertRaisesRegex(module.BackupError, "absent"):
                module.validate_manifest(manifest, root)

    def test_clean_manifest_requires_exact_noi_absence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); artifacts = {}
            definitions = {**module.REQUIRED, **module.OPTIONAL, **module.CLEAN_ONLY}
            for name, filename in definitions.items():
                present = name in module.CLEAN_REQUIRED
                if present:
                    content = (name + "\n").encode(); (root / filename).write_bytes(content)
                    artifacts[name] = {"filename": filename, "present": True,
                        "bytes": len(content), "mode": stat.S_IMODE((root / filename).stat().st_mode),
                        "sha256": hashlib.sha256(content).hexdigest()}
                else:
                    artifacts[name] = {"filename": filename, "present": False,
                        "bytes": None, "mode": None, "sha256": None}
            manifest = {"$schema": "v1-clean-install-backup-manifest.schema.json",
                "schema_version": 1, "operation": "clean-install", "plan_id": "7" * 64,
                "source": {"revision": "8" * 40, "manifest_sha256": "9" * 64},
                "created_at": "2026-08-14T00:00:00Z", "artifacts": artifacts}
            (root / "backup-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            module.validate_manifest(manifest, root, expected_plan_id="7" * 64)
            entry = manifest["artifacts"]["hydro_plugin_token"]
            content = b"unexpected\n"; (root / entry["filename"]).write_bytes(content)
            entry.update({"present": True, "bytes": len(content),
                          "mode": stat.S_IMODE((root / entry["filename"]).stat().st_mode),
                          "sha256": hashlib.sha256(content).hexdigest()})
            with self.assertRaisesRegex(module.BackupError, "must be absent"):
                module.validate_manifest(manifest, root)

    def test_builder_seals_all_files_and_refuses_unknown_input(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); self.create_artifacts(root)
            with mock.patch.object(builder, "safe_directory", return_value=root), \
                    mock.patch.object(builder, "seal_file", side_effect=module.safe_file), \
                    mock.patch.object(builder, "fsync_directory"), \
                    mock.patch.object(builder, "verify_tree_archive"), \
                    mock.patch.object(builder, "verify_pm2"), \
                    mock.patch.object(builder, "verify_hydro_backup") as semantic, \
                    mock.patch("verify_v1_controller_install_backup.verify") as controller_semantic, \
                    mock.patch("verify_v1_cloud_install_backup.verify") as cloud_semantic, \
                    mock.patch("verify_v1_ordinary_oj_install_backup.verify") as ordinary_semantic:
                value = builder.build(root, "4" * 64, "5" * 40, "6" * 64,
                                      now=__import__("datetime").datetime(2026, 8, 13, tzinfo=__import__("datetime").timezone.utc))
            self.assertEqual(value["artifacts"]["pm2_dump_backup"]["present"], False)
            self.assertTrue((root / "backup-manifest.json").is_file())
            module.validate_manifest(value, root, expected_plan_id="4" * 64)
            semantic.assert_called_once_with(root.resolve(), "4" * 64)
            controller_semantic.assert_called_once_with(root.resolve(), "4" * 64)
            cloud_semantic.assert_called_once_with(root.resolve(), "4" * 64)
            ordinary_semantic.assert_called_once_with(root.resolve(), "4" * 64)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); self.create_artifacts(root); (root / "unknown").write_text("x")
            with mock.patch.object(builder, "safe_directory", return_value=root), \
                    self.assertRaisesRegex(builder.BuildError, "unmanifested"):
                builder.build(root, "4" * 64, "5" * 40, "6" * 64)


if __name__ == "__main__":
    unittest.main()
