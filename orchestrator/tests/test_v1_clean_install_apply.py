import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import tempfile
import unittest
from unittest import mock


ROOT=Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0,str(ROOT/"scripts"))
spec=importlib.util.spec_from_file_location("apply_clean",ROOT/"scripts/apply_v1_clean_install.py")
clean=importlib.util.module_from_spec(spec);assert spec.loader;spec.loader.exec_module(clean)


class CleanInstallApplyTests(unittest.TestCase):
    def row(self,private:Path):
        plan="1"*64;release="2"*40+"-"+"3"*12;root=Path("/opt/noi-linux-contest-system")
        return {"schema_version":1,"operation":"clean-install","plan_id":plan,"scope":"production","source_plan_id":"4"*64,
            "source_release":release,"candidate":"/root/candidate","candidate_manifest_sha256":"5"*64,
            "backup_directory":"/root/backup","backup_manifest_sha256":"6"*64,
            "transaction_directory":str(private/"transaction"),"install_root":str(root),
            "expected_contract":str(private/"post-install-contract.json"),
            "private_artifact_sha256":{"expected_contract":"7"*64,"desired_controller_definition":"8"*64,
                "desired_config":"9"*64,"desired_env":"a"*64,"desired_plugin_env":"b"*64,"desired_plugin_token":"c"*64},
            "desired_controller_definition":str(private/"desired-controller-definition.json"),
            "desired_config":str(private/"desired-config.yaml"),"desired_env":str(private/"desired.env"),
            "desired_plugin_env":str(private/"desired-plugin.env"),"desired_plugin_token":str(private/"desired-plugin-token"),
            "project_config":str(root/"orchestrator/config.yaml"),"project_env":str(root/"orchestrator/.env"),
            "database":str(root/"orchestrator/data/orchestrator.db"),"caddyfile":"/root/.hydro/Caddyfile",
            "snippet":str(root/"orchestrator/runtime/caddy-exam.conf"),"hydro_domain":"oj.example.test",
            "frontend_domain":"exam.example.test","orchestrator_upstream":"http://127.0.0.1:8600",
            "executables":{"python":"/usr/bin/python3","bash":"/bin/bash","pm2":"/nix/pm2","node":"/nix/node",
                           "docker_socket":"/var/run/docker.sock"}}

    def test_private_clean_plan_is_exact_sha_pinned_and_layout_bound(self):
        parent="/root" if platform.system().lower()=="linux" and os.geteuid()==0 else None
        with tempfile.TemporaryDirectory(dir=parent) as raw:
            private=Path(raw);path=private/"private-clean-install-plan.json";row=self.row(private)
            content=(json.dumps(row)+"\n").encode();path.write_bytes(content);path.chmod(0o600)
            check=mock.patch.object(clean,"absolute",side_effect=lambda value,label:Path(value)) if platform.system().lower()!="linux" else mock.patch.object(clean,"absolute",wraps=clean.absolute)
            with check:
                loaded=clean.load_plan(path,hashlib.sha256(content).hexdigest())
            self.assertEqual(loaded["operation"],"clean-install")
            with self.assertRaisesRegex(clean.ApplyInstallError,"trust pin"):
                clean.load_plan(path,"0"*64)
            row["desired_plugin_token"]="/root/unbound-token";content=(json.dumps(row)+"\n").encode();path.write_bytes(content)
            check=mock.patch.object(clean,"absolute",side_effect=lambda value,label:Path(value)) if platform.system().lower()!="linux" else mock.patch.object(clean,"absolute",wraps=clean.absolute)
            with check,self.assertRaisesRegex(clean.ApplyInstallError,"layout"):
                clean.load_plan(path,hashlib.sha256(content).hexdigest())

    def test_driver_wiring_has_exact_clean_sequence(self):
        if platform.system().lower()=="linux":self.skipTest("candidate trust is exercised after root staging")
        with tempfile.TemporaryDirectory() as raw:
            private=Path(raw);row=self.row(private)
            for name in ("python","bash","pm2","node"):
                path=private/name;path.write_text("x");path.chmod(0o755);row["executables"][name]=str(path.resolve())
            phase,final=clean.drivers(row,{})
            self.assertEqual(set(phase),{"source_release","clean_materials","hydro_integration","closed_frontend",
                                         "controller","post_install_verification"})
            self.assertEqual(phase["clean_materials"].desired_plugin_token,Path(row["desired_plugin_token"]))
            self.assertEqual(final.source_release_name,row["source_release"])


if __name__=="__main__":unittest.main()
