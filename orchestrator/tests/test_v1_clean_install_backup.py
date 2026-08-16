import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT / "scripts"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module)
    return module


collector = load("clean_collector", ROOT / "scripts/build_v1_clean_install_backup.py")
verifier = load("clean_verifier", ROOT / "scripts/verify_v1_clean_install_backup.py")
builder = load("clean_builder", ROOT / "scripts/build_v1_clean_install_backup_manifest.py")
import verify_v1_install_backup as backup_module


class CleanInstallBackupTests(unittest.TestCase):
    def target(self):
        return {"schema_version": 1, "operation": "clean-install", "paths": {
            key: {"path": f"/clean/{index}", "present": False}
            for index, key in enumerate(sorted(verifier.PATH_KEYS))
        }}

    def test_clean_target_contract_is_exact_and_all_absent(self):
        value = self.target()
        self.assertEqual(verifier.validate_clean_target(value), value)
        value["paths"]["database"]["present"] = True
        with self.assertRaisesRegex(verifier.CleanBackupError, "database"):
            verifier.validate_clean_target(value)

    def test_collector_binds_absence_and_every_semantic_baseline(self):
        class Args: pass
        args = Args(); args.output_directory = Path("/backup"); args.install_root = Path("/opt/noi")
        args.source_pointer = Path("/opt/noi/current-source"); args.project_config = Path("/opt/noi/orchestrator/config.yaml")
        args.project_env = Path("/opt/noi/orchestrator/.env"); args.database = Path("/opt/noi/orchestrator/data/orchestrator.db")
        args.caddyfile = Path("/root/.hydro/Caddyfile"); args.snippet = Path("/opt/noi/orchestrator/runtime/caddy-exam.conf")
        args.frontend_domain = "exam.example.test"; args.orchestrator_upstream = "http://127.0.0.1:8600"
        args.pm2_bin = Path("/pm2"); args.docker_socket = Path("/docker.sock")
        args.oj_origin = "https://oj.example.test"; args.cloud_snapshot = Path("/cloud.json")
        args.plan_id = "1" * 64; args.source_revision = "2" * 40; args.candidate_manifest_sha256 = "3" * 64
        events = []
        manifest = {"artifacts": {"x": {}}}; sealed = b"sealed\n"
        def mark(name, result=None):
            def call(*unused, **kwargs): events.append(name); return result
            return call
        cloud = {"schema_version": 1, "enabled": True, "desired_open": False,
                 "open": False, "closed": True, "healthy": True, "managed_count": 0,
                 "conflict_count": 0, "management_healthy": True,
                 "management_missing_count": 0, "instance_state": "STOPPED"}
        with mock.patch.object(collector, "require_absent", side_effect=mark("absent")), \
             mock.patch.object(collector, "safe_ancestors"), \
             mock.patch.object(collector, "private_output", return_value=Path("/sealed")), \
             mock.patch.object(collector, "capture_caddy", side_effect=mark("caddy")), \
             mock.patch.object(collector, "copy_file", side_effect=mark("copy", True)), \
             mock.patch.object(collector, "collect_hydro", side_effect=mark("hydro")), \
             mock.patch.object(collector, "collect_controller", side_effect=mark("controller", {
                 "controller_present": False})), \
             mock.patch.object(collector, "build_ordinary", side_effect=mark("ordinary")), \
             mock.patch.object(collector, "safe_file", side_effect=[
                 ((json.dumps(cloud) + "\n").encode(), mock.Mock()), (sealed, mock.Mock())]), \
             mock.patch.object(collector, "validate_cloud", side_effect=mark("cloud-validate", cloud)), \
             mock.patch.object(collector, "atomic", side_effect=mark("atomic")), \
             mock.patch.object(collector, "seal_manifest", side_effect=mark("seal", manifest)):
            result = collector.collect(args)
        self.assertGreaterEqual(events.count("absent"), len(verifier.PATH_KEYS))
        for name in ("caddy", "hydro", "controller", "ordinary", "cloud-validate"):
            self.assertLess(events.index(name), events.index("seal"))
        self.assertEqual(result["backup_manifest_sha256"], hashlib.sha256(sealed).hexdigest())
        self.assertEqual(result["operation"], "clean-install")

    def test_clean_builder_seals_only_explicit_absence(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);definitions={**backup_module.REQUIRED,**backup_module.OPTIONAL,
                                        **backup_module.CLEAN_ONLY}
            for name,filename in definitions.items():
                if name not in backup_module.CLEAN_REQUIRED:continue
                if name=="clean_target":value=self.target()
                elif name=="controller_definition":value={"schema_version":1,"present":False,"container":None}
                elif name=="controller_image":value={"schema_version":1,"present":False,"image_id":None}
                elif name=="cloud_snapshot":value={"schema_version":1,"enabled":True,"desired_open":False,
                    "open":False,"closed":True,"healthy":True,"managed_count":0,"conflict_count":0,
                    "management_healthy":True,"management_missing_count":0,"instance_state":"STOPPED"}
                elif name=="ordinary_oj_snapshot":value={"schema_version":1,"homepage_status":200,
                    "login_status":200,"prep_health_ok":True,"prep_database_ok":True,"processes":[]}
                else:value=name
                content=((json.dumps(value) if not isinstance(value,str) else value)+"\n").encode()
                (root/filename).write_bytes(content);os.chmod(root/filename,0o600)
            with mock.patch.object(builder,"safe_directory",return_value=root), \
                 mock.patch.object(builder,"seal_file",side_effect=backup_module.safe_file), \
                 mock.patch.object(builder,"fsync_directory"), \
                 mock.patch.object(builder,"verify_tree_archive",return_value={"present":False}), \
                 mock.patch.object(builder,"verify_pm2"), \
                 mock.patch.object(builder,"validate_cloud"), \
                 mock.patch.object(builder,"validate_ordinary"), \
                 mock.patch.object(builder,"verify_clean_backup") as final_verify:
                value=builder.build(root,"4"*64,"5"*40,"6"*64,
                    now=__import__("datetime").datetime(2026,8,14,tzinfo=__import__("datetime").timezone.utc))
            self.assertEqual(value["operation"],"clean-install")
            self.assertFalse(value["artifacts"]["hydro_plugin_token"]["present"])
            final_verify.assert_called_once_with(root,"4"*64)


if __name__ == "__main__": unittest.main()
