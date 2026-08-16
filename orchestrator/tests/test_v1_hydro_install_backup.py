import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import unittest


ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "verify_v1_hydro_install_backup.py"
spec = importlib.util.spec_from_file_location("hydro_backup", SCRIPT)
module = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module)


def tree_archive(root, files):
    entries=[]
    for name, content in files.items():
        entries.append({"path":name,"type":"file","mode":0o600,"bytes":len(content),
                        "sha256":hashlib.sha256(content).hexdigest()})
    state={"schema_version":1,"root":root,"present":bool(files),
           "root_mode":0o755 if files else None,"entries":entries}
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode="w:") as tar:
        raw=(json.dumps(state,separators=(",",":"))+"\n").encode(); info=tarfile.TarInfo("tree-state.json")
        info.size=len(raw); info.mode=0o600; tar.addfile(info,io.BytesIO(raw))
        for name,content in files.items():
            info=tarfile.TarInfo("tree/"+name); info.size=len(content); info.mode=0o600
            tar.addfile(info,io.BytesIO(content))
    return output.getvalue()


class HydroInstallBackupTests(unittest.TestCase):
    def test_tree_archive_binds_every_payload(self):
        raw=tree_archive("/root/.hydro/addons/orchestrator-submit",{"index.js":b"x"})
        result=module.verify_tree_archive(raw,"hydro-addon-tree.tar")
        self.assertTrue(result["present"])

    def test_tree_archive_rejects_extra_and_traversal_payloads(self):
        raw=tree_archive("/root/.hydro/addons/orchestrator-submit",{"../escape":b"x"})
        with self.assertRaisesRegex(module.HydroBackupError,"unsafe"):
            module.verify_tree_archive(raw,"hydro-addon-tree.tar")
        valid=tree_archive("/root/.hydro/addons/orchestrator-submit",{"index.js":b"x"})
        source=io.BytesIO(valid); output=io.BytesIO()
        with tarfile.open(fileobj=source,mode="r:") as old, tarfile.open(fileobj=output,mode="w:") as new:
            for member in old.getmembers(): new.addfile(member,old.extractfile(member) if member.isfile() else None)
            extra=tarfile.TarInfo("tree/extra"); extra.size=1; extra.mode=0o600; new.addfile(extra,io.BytesIO(b"x"))
        with self.assertRaisesRegex(module.HydroBackupError,"unexpected"):
            module.verify_tree_archive(output.getvalue(),"hydro-addon-tree.tar")

    def test_tree_archive_rejects_a_missing_parent_directory(self):
        raw=tree_archive("/root/.hydro/addons/orchestrator-submit",{"nested/file":b"x"})
        with self.assertRaisesRegex(module.HydroBackupError,"parent is missing"):
            module.verify_tree_archive(raw,"hydro-addon-tree.tar")

    def test_pm2_definition_exactly_binds_dump_row(self):
        row={"name":"hydrooj","env":{"A":"1","ORCHESTRATOR_X":"y","unique_id":"volatile"}}
        for key in module.LAUNCH_KEYS: row.setdefault(key,None)
        normalized=dict(row["env"]); normalized.pop("unique_id")
        prefix={"ORCHESTRATOR_X":"y"}; top={}
        definition={"schema_version":1,"name":"hydrooj",
                    "dump_row_sha256":hashlib.sha256(module.canonical(row)).hexdigest(),
                    "normalized_env_sha256":hashlib.sha256(module.canonical(normalized)).hexdigest(),
                    "orchestrator_prefix_sha256":hashlib.sha256(module.canonical(prefix)).hexdigest(),
                    "top_orchestrator_prefix_sha256":hashlib.sha256(module.canonical(top)).hexdigest(),
                    "launch":{key:row.get(key) for key in module.LAUNCH_KEYS}}
        result=module.verify_pm2(json.dumps([row]).encode(),json.dumps(definition).encode())
        self.assertEqual(result["name"],"hydrooj")
        row["pm_cwd"]="changed"
        with self.assertRaisesRegex(module.HydroBackupError,"does not match"):
            module.verify_pm2(json.dumps([row]).encode(),json.dumps(definition).encode())

    def test_pm2_definition_normalizes_only_single_fork_instances(self):
        row={"name":"hydrooj","env":{},"exec_mode":"fork_mode","instances":None}
        for key in module.LAUNCH_KEYS: row.setdefault(key,None)
        definition={"schema_version":1,"name":"hydrooj",
                    "dump_row_sha256":hashlib.sha256(module.canonical(row)).hexdigest(),
                    "normalized_env_sha256":hashlib.sha256(module.canonical({})).hexdigest(),
                    "orchestrator_prefix_sha256":hashlib.sha256(module.canonical({})).hexdigest(),
                    "top_orchestrator_prefix_sha256":hashlib.sha256(module.canonical({})).hexdigest(),
                    "launch":module.normalized_launch(row)}
        self.assertEqual(module.verify_pm2(json.dumps([row]).encode(),json.dumps(definition).encode())["launch"]["instances"],1)
        definition["launch"]["instances"]=2
        with self.assertRaisesRegex(module.HydroBackupError,"launch definition differs"):
            module.verify_pm2(json.dumps([row]).encode(),json.dumps(definition).encode())


if __name__=="__main__": unittest.main()
