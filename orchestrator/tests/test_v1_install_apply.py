import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0,str(ROOT/"scripts"))
spec=importlib.util.spec_from_file_location("apply_install",ROOT/"scripts/apply_v1_install.py")
install=importlib.util.module_from_spec(spec);spec.loader.exec_module(install)


class InstallApplyTests(unittest.TestCase):
    def row(self):
        plan="1"*64;release="2"*40+"-"+"3"*12
        return {"schema_version":1,"operation":"upgrade","plan_id":plan,"scope":"production","source_plan_id":"4"*64,
            "source_release":release,"candidate":"/root/candidate","candidate_manifest_sha256":"5"*64,
            "backup_directory":"/root/backup","backup_manifest_sha256":"6"*64,
            "transaction_directory":"/root/tx","install_root":"/opt/noi-linux-contest-system",
            "expected_contract":"/root/expected.json","desired_controller_definition":"/root/desired.json",
            "private_artifact_sha256":{"expected_contract":"7"*64,"desired_controller_definition":"8"*64,
                "desired_config":"9"*64,"desired_env":"a"*64},
            "desired_config":"/root/config.yaml","desired_env":"/root/env","project_config":"/opt/noi/config.yaml",
            "project_env":"/opt/noi/.env","database":"/opt/noi/data/db","caddyfile":"/root/.hydro/Caddyfile",
            "snippet":"/opt/noi/runtime/caddy-exam.conf","hydro_domain":"oj.example.test",
            "frontend_domain":"exam.example.test","orchestrator_upstream":"http://127.0.0.1:8600",
            "executables":{"python":"/usr/bin/python3","bash":"/bin/bash","pm2":"/nix/pm2","node":"/nix/node","docker_socket":"/var/run/docker.sock"}}

    def test_private_plan_is_exact_and_sha_pinned(self):
        parent="/root" if platform.system().lower()=="linux" and os.geteuid()==0 else None
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            path=Path(directory)/"plan.json";raw=(json.dumps(self.row())+"\n").encode();path.write_bytes(raw);path.chmod(0o600)
            digest=hashlib.sha256(raw).hexdigest();self.assertEqual(install.load_plan(path,digest)["scope"],"production")
            with self.assertRaisesRegex(install.ApplyInstallError,"trust pin"):
                install.load_plan(path,"0"*64)
            value=self.row();value["unexpected"]=1;path.write_text(json.dumps(value));path.chmod(0o600)
            with self.assertRaisesRegex(install.ApplyInstallError,"field set"):
                install.load_plan(path,hashlib.sha256(path.read_bytes()).hexdigest())

    def test_driver_wiring_binds_source_release_and_all_six_phases(self):
        if platform.system().lower()=="linux":self.skipTest("candidate tree trust is covered after root staging")
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);row=self.row()
            for name in ("python","bash","pm2","node"):
                path=root/name;path.write_text("x");path.chmod(0o755);row["executables"][name]=str(path.resolve())
            phase,final=install.drivers(row,{"oj_origin":"https://oj.example.test"})
            self.assertEqual(set(phase),{"source_release","controller_quiesce","hydro_integration","closed_frontend","controller","post_install_verification"})
            self.assertEqual(phase["source_release"].expected_source_release,row["source_release"])
            self.assertEqual(phase["controller_quiesce"].source_release_name,row["source_release"])
            self.assertEqual(phase["controller"].source_release_name,row["source_release"])
            self.assertEqual(final.source_release_name,row["source_release"])


if __name__=="__main__":unittest.main()
