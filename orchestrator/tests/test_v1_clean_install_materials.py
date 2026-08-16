import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock


ROOT=Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0,str(ROOT/"scripts"))
spec=importlib.util.spec_from_file_location("clean_materials",ROOT/"scripts/prepare_v1_clean_install_materials.py")
module=importlib.util.module_from_spec(spec);assert spec.loader;spec.loader.exec_module(module)


class CleanInstallMaterialTests(unittest.TestCase):
    def test_apply_and_rollback_are_exact_and_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            base=Path(raw);root=base/"install";root.mkdir();tx=base/"tx";tx.mkdir()
            desired={"config":b"config\n","env":b"env\n","plugin_env":b"ORCHESTRATOR_X=x\n","token":b"t"*64+b"\n"}
            args=SimpleNamespace(plan_id="1"*64,backup_manifest_sha256="2"*64)
            def fake_inputs(unused):return tx,root,desired,{}
            local_targets={"config":root/"orchestrator/config.yaml","env":root/"orchestrator/.env",
                           "plugin_env":base/"hydro/plugin.env","token":base/"hydro/token"}
            (base/"hydro").mkdir()
            with mock.patch.object(module,"inputs",side_effect=fake_inputs), \
                 mock.patch.object(module,"targets",return_value=local_targets), \
                 mock.patch.object(module,"safe_root",side_effect=lambda p:p), \
                 mock.patch.object(module,"fsync_dir"),mock.patch.object(module.os,"chown",create=True):
                self.assertTrue(module.apply(args));self.assertFalse(module.apply(args))
                self.assertEqual(local_targets["token"].read_bytes(),desired["token"])
                self.assertTrue(module.rollback(args));self.assertFalse(module.rollback(args))
            self.assertTrue(all(not path.exists() for path in local_targets.values()))
            self.assertFalse((root/"orchestrator").exists())

    def test_rollback_refuses_an_altered_owned_file(self):
        with tempfile.TemporaryDirectory() as raw:
            base=Path(raw);root=base/"install";root.mkdir();tx=base/"tx";tx.mkdir();(root/"orchestrator").mkdir()
            desired={"config":b"expected","env":b"e","plugin_env":b"p","token":b"t"*64}
            target=root/"orchestrator/config.yaml";target.write_bytes(b"changed")
            args=SimpleNamespace(plan_id="1"*64,backup_manifest_sha256="2"*64)
            paths={"config":target,"env":base/"missing-env","plugin_env":base/"missing-plugin","token":base/"missing-token"}
            with mock.patch.object(module,"inputs",return_value=(tx,root,desired,{})), \
                 mock.patch.object(module,"targets",return_value=paths),mock.patch.object(module,"fsync_dir"), \
                 self.assertRaisesRegex(module.MaterialError,"changed outside"):
                module.rollback(args)


if __name__=="__main__":unittest.main()
