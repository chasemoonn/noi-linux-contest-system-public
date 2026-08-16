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


ROOT=Path(__file__).resolve().parents[2]; sys.path.insert(0,str(ROOT/"scripts"))
SCRIPT=ROOT/"scripts"/"restore_v1_hydro_install_backup.py"
spec=importlib.util.spec_from_file_location("hydro_restore",SCRIPT)
module=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module)
builder=__import__("build_v1_hydro_install_backup")


class HydroInstallRestoreTests(unittest.TestCase):
    def state(self, root, files):
        directories=set()
        for name in files:
            parent=Path(name).parent
            while str(parent) not in {"","."}:
                directories.add(parent.as_posix()); parent=parent.parent
        entries=[{"path":name,"type":"directory","mode":0o755,"bytes":None,"sha256":None}
                 for name in directories]
        for name,content in files.items():
            entries.append({"path":name,"type":"file","mode":0o600,"bytes":len(content),
                            "sha256":hashlib.sha256(content).hexdigest()})
        entries.sort(key=lambda item:item["path"])
        return {"schema_version":1,"root":str(root),"present":True,"root_mode":0o700,"entries":entries}

    def test_tree_stage_materializes_exact_files(self):
        with tempfile.TemporaryDirectory() as raw:
            parent=Path(raw); files={"a":b"one","nested/b":b"two"}; state=self.state(parent/"target",files)
            stage=module.create_stage(parent,".stage",state,files)
            self.assertTrue(module.tree_matches(stage,state,files))
            self.assertEqual((stage/"nested/b").read_bytes(),b"two")

    def test_restore_absent_tree_moves_only_the_exact_target(self):
        with tempfile.TemporaryDirectory() as raw:
            parent=Path(raw); target=parent/"target"; target.mkdir(); (target/"x").write_text("x")
            state={"schema_version":1,"root":str(target),"present":False,"root_mode":None,"entries":[]}
            with mock.patch.object(module,"archive_payload",return_value=(state,{})), \
                    mock.patch.object(module,"safe_parent",return_value=parent.resolve()):
                module.restore_tree(target,b"raw","hydro-addon-tree.tar","1"*64)
            self.assertFalse(target.exists())
            self.assertEqual(list(parent.iterdir()),[])

    @unittest.skipUnless(sys.platform.startswith("linux"),"renameat2 is a Linux contract")
    def test_restore_existing_tree_uses_exchange_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            parent=Path(raw); target=parent/"target"; target.mkdir(mode=0o700); (target/"old").write_text("old")
            files={"new":b"new"}; state=self.state(target,files)
            with mock.patch.object(module,"archive_payload",return_value=(state,{})), \
                    mock.patch.object(module,"safe_parent",return_value=parent.resolve()), \
                    mock.patch.object(module,"archive_payload",return_value=(state,files)):
                module.restore_tree(target,b"raw","hydro-addon-tree.tar","2"*64)
                module.restore_tree(target,b"raw","hydro-addon-tree.tar","2"*64)
            self.assertTrue(module.tree_matches(target,state,files))
            self.assertEqual({p.name for p in parent.iterdir()},{"target"})

    def test_live_pm2_verifier_ignores_only_unique_id(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); environment={"A":"1","unique_id":"before"}; prefix={}; top={}
            launch={key:None for key in module.LAUNCH_KEYS}; launch["name"]="hydrooj"
            definition={"normalized_env_sha256":hashlib.sha256(module.canonical({"A":"1"})).hexdigest(),
                        "orchestrator_prefix_sha256":hashlib.sha256(module.canonical(prefix)).hexdigest(),
                        "top_orchestrator_prefix_sha256":hashlib.sha256(module.canonical(top)).hexdigest(),
                        "launch":launch}
            (root/"hydro-pm2-definition.json").write_text(json.dumps(definition))
            live_definition={**launch,"env":{**environment,"unique_id":"after"}}
            with mock.patch("build_v1_hydro_install_backup.pm2_jlist",
                            return_value=[{"name":"hydrooj","pm2_env":live_definition}]):
                module.verify_live_pm2(Path("pm2"),root)
            live_definition["env"]["A"]="changed"
            with mock.patch("build_v1_hydro_install_backup.pm2_jlist",
                            return_value=[{"name":"hydrooj","pm2_env":live_definition}]), \
                    self.assertRaisesRegex(module.RestoreError,"differs"):
                module.verify_live_pm2(Path("pm2"),root)

    def test_absent_optional_file_cleanup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            parent=Path(raw); target=parent/"dump.pm2.bak"; displaced=parent/(".dump.pm2.bak.v1-displaced-"+"1"*12)
            displaced.write_bytes(b"old")
            with mock.patch.object(module,"safe_parent",return_value=parent.resolve()):
                module.restore_optional_file(parent/"unused",target,False,"1"*64)
            self.assertFalse(displaced.exists())

    def test_exact_live_baseline_short_circuits_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); manifest={"artifacts":{"pm2_dump_backup":{"present":False}}}
            with mock.patch.object(module,"live_matches_backup",return_value=True), \
                    mock.patch.object(module,"safe_directory",side_effect=lambda path:Path(path)), \
                    mock.patch.object(module,"safe_file",return_value=(b"manifest",mock.Mock())), \
                    mock.patch.object(module,"verify_backup"), \
                    mock.patch.object(module,"json",wraps=json) as json_module, \
                    mock.patch.object(module,"run_pm2") as run:
                json_module.loads.return_value=manifest
                result=module.restore(root,root,"1"*64,hashlib.sha256(b"manifest").hexdigest(),Path("pm2"))
            self.assertFalse(result["changed"]); run.assert_not_called()


if __name__=="__main__": unittest.main()
