import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT=Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0,str(ROOT/"scripts"))
spec=importlib.util.spec_from_file_location("controller_quiesce",ROOT/"scripts/quiesce_v1_controller.py")
module=importlib.util.module_from_spec(spec);assert spec.loader;spec.loader.exec_module(module)


class ControllerQuiesceTests(unittest.TestCase):
    def baseline(self):
        return {"present":True,"container":{"container_id":"a"*64,"running":True}}

    def test_quiesce_binds_before_and_after_stop(self):
        baseline=self.baseline();row={"Id":"a"*64,"State":{"Running":True}}
        class Docker:
            def inspect(self,name,allow_absent=False):return row
            def stop(self,identifier):row["State"]["Running"]=False
        args=SimpleNamespace(plan_id="1"*64,backup_manifest_sha256="2"*64)
        checks=[]
        original_match=module.inspect_matches;original_files=module.live_files_match_backup;original_wait=module.wait_running
        original_cloud=module.cloud_matches;original_dependencies=module.dependencies_match
        module.inspect_matches=lambda value,base:value["Id"]==base["container"]["container_id"]
        module.live_files_match_backup=lambda *unused:checks.append(row["State"]["Running"])
        module.wait_running=lambda *unused:row
        module.cloud_matches=lambda *unused:None;module.dependencies_match=lambda *unused,**kwargs:None
        try:result=module.quiesce(args,Docker(),Path("/backup"),{},baseline,rollback=False)
        finally:
            module.inspect_matches=original_match;module.live_files_match_backup=original_files;module.wait_running=original_wait
            module.cloud_matches=original_cloud;module.dependencies_match=original_dependencies
        self.assertEqual(checks,[True,False]);self.assertTrue(result["quiesced"]);self.assertTrue(result["changed"])

    def test_rollback_never_restarts_controller(self):
        baseline=self.baseline();row={"Id":"a"*64,"State":{"Running":False}}
        class Docker:
            def inspect(self,name,allow_absent=False):return row
            def stop(self,identifier):raise AssertionError("already stopped")
            def start(self,identifier):raise AssertionError("rollback must not start")
        args=SimpleNamespace(plan_id="1"*64,backup_manifest_sha256="2"*64)
        original_match=module.inspect_matches;original_files=module.live_files_match_backup;original_restore=module.restore_controller_state
        original_cloud=module.cloud_matches;original_dependencies=module.dependencies_match
        module.inspect_matches=lambda value,base:value["Id"]==base["container"]["container_id"]
        module.live_files_match_backup=lambda *unused:None
        module.restore_controller_state=lambda *unused:None
        module.cloud_matches=lambda *unused:None;module.dependencies_match=lambda *unused,**kwargs:None
        try:result=module.quiesce(args,Docker(),Path("/backup"),{},baseline,rollback=True)
        finally:
            module.inspect_matches=original_match;module.live_files_match_backup=original_files
            module.restore_controller_state=original_restore
            module.cloud_matches=original_cloud;module.dependencies_match=original_dependencies
        self.assertEqual(result["status"],"rollback_verified");self.assertFalse(result["changed"])

    def test_rollback_restores_sealed_private_state_before_rebinding(self):
        baseline=self.baseline();row={"Id":"a"*64,"State":{"Running":False}};events=[]
        class Docker:
            def inspect(self,name,allow_absent=False):return row
            def stop(self,identifier):raise AssertionError("rollback must keep baseline stopped")
        args=SimpleNamespace(plan_id="1"*64,backup_manifest_sha256="2"*64)
        originals=(module.inspect_matches,module.live_files_match_backup,module.restore_controller_state,
                   module.cloud_matches,module.dependencies_match)
        module.inspect_matches=lambda *unused:True
        module.restore_controller_state=lambda *unused:events.append("restore")
        module.live_files_match_backup=lambda *unused:events.append("bind")
        module.cloud_matches=lambda *unused:events.append("cloud")
        module.dependencies_match=lambda *unused,**kwargs:events.append("dependencies")
        try:module.quiesce(args,Docker(),Path("/backup"),{},baseline,rollback=True)
        finally:(module.inspect_matches,module.live_files_match_backup,module.restore_controller_state,
                 module.cloud_matches,module.dependencies_match)=originals
        self.assertEqual(events,["restore","bind","bind","dependencies"])


if __name__=="__main__":unittest.main()
