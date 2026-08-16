import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0,str(ROOT/"scripts"))

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

verify=load("cloud_verify",ROOT/"scripts/verify_v1_cloud_install_backup.py")
build=load("cloud_build",ROOT/"scripts/build_v1_cloud_install_backup.py")


class CloudInstallBackupTests(unittest.TestCase):
    def closed(self):
        return {"schema_version":1,"enabled":True,"desired_open":False,"open":False,"closed":True,
                "healthy":True,"managed_count":0,"conflict_count":0,"management_healthy":True,
                "management_missing_count":0,"instance_state":"STOPPED"}

    def test_only_exact_closed_stopped_state_passes(self):
        self.assertTrue(verify.validate(self.closed())["closed"])
        for key,value in (("managed_count",1),("conflict_count",1),("instance_state","RUNNING"),("closed",False)):
            row=self.closed();row[key]=value
            with self.assertRaisesRegex(verify.CloudBackupError,"closed"):
                verify.validate(row)

    def test_builder_sanitizes_to_the_fixed_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source={**self.closed(),"security_group_id":"secret","managed_rule_ids":["secret"]}
            result=build.build(root,source);self.assertTrue(result["closed"])
            raw=(root/"cloud-before.json").read_text()
            self.assertNotIn("security_group",raw);self.assertNotIn("managed_rule_ids",raw)


if __name__=="__main__":unittest.main()
