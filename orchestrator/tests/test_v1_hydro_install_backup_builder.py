import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT=Path(__file__).resolve().parents[2]; sys.path.insert(0,str(ROOT/"scripts"))
SCRIPT=ROOT/"scripts"/"build_v1_hydro_install_backup.py"
spec=importlib.util.spec_from_file_location("hydro_builder",SCRIPT)
module=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module)


class HydroBackupBuilderTests(unittest.TestCase):
    def pm2_rows(self):
        row={"name":"hydrooj","env":{"A":"1","unique_id":"volatile"}}
        for key in module.LAUNCH_KEYS: row.setdefault(key,None)
        live={"name":"hydrooj","pm2_env":dict(row)}
        return row,live

    def test_pm2_builder_binds_equal_live_and_persistent_definitions(self):
        row,live=self.pm2_rows(); raw=json.dumps([row]).encode()
        definition=module.pm2_definition(raw,[live])
        parsed=json.loads(definition); self.assertEqual(parsed["name"],"hydrooj")
        self.assertEqual(parsed["dump_row_sha256"],hashlib.sha256(module.canonical(row)).hexdigest())

    def test_pm2_builder_rejects_live_drift(self):
        row,live=self.pm2_rows(); live["pm2_env"]["env"]={"A":"changed"}
        with self.assertRaisesRegex(module.CollectError,"environment differs"):
            module.pm2_definition(json.dumps([row]).encode(),[live])

    def test_pm2_builder_ignores_only_resurrected_unique_id(self):
        row,live=self.pm2_rows(); live["pm2_env"]["env"]=dict(live["pm2_env"]["env"])
        live["pm2_env"]["env"]["unique_id"]="resurrected"
        definition=json.loads(module.pm2_definition(json.dumps([row]).encode(),[live]))
        self.assertEqual(definition["name"],"hydrooj")
        live["pm2_env"]["env"]["A"]="changed"
        with self.assertRaisesRegex(module.CollectError,"environment differs"):
            module.pm2_definition(json.dumps([row]).encode(),[live])

    def test_pm2_builder_accepts_pm2_single_fork_instances_round_trip(self):
        row,live=self.pm2_rows()
        row["exec_mode"]="fork_mode"; row["instances"]=None
        live["pm2_env"]["exec_mode"]="fork_mode"; live["pm2_env"]["instances"]=1
        definition=json.loads(module.pm2_definition(json.dumps([row]).encode(),[live]))
        self.assertEqual(definition["launch"]["instances"],1)
        live["pm2_env"]["instances"]=2
        with self.assertRaisesRegex(module.CollectError,"launch definition differs"):
            module.pm2_definition(json.dumps([row]).encode(),[live])

    def test_tree_builder_is_deterministic_and_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); (root/"nested").mkdir(); (root/"nested/file").write_bytes(b"x")
            expected=module.ADDON_ROOT; module.ADDON_ROOT=root
            try:
                with mock.patch.object(module,"verify_tree_archive"):
                    first=module.build_tree_archive(root); second=module.build_tree_archive(root)
                self.assertEqual(first,second)
                try:
                    (root/"link").symlink_to(root/"nested/file")
                except OSError:
                    # Standard non-elevated Windows test hosts cannot create a
                    # symlink.  Linux CI exercises the real lstat branch.
                    pass
                else:
                    with mock.patch.object(module,"verify_tree_archive"), \
                            self.assertRaisesRegex(module.CollectError,"symlink"):
                        module.build_tree_archive(root)
            finally: module.ADDON_ROOT=expected


if __name__=="__main__": unittest.main()
