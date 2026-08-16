import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0,str(ROOT/"scripts"))
spec=importlib.util.spec_from_file_location("ordinary_install",ROOT/"scripts/verify_v1_ordinary_oj_install_backup.py")
ordinary=importlib.util.module_from_spec(spec);spec.loader.exec_module(ordinary)


class OrdinaryOjInstallBackupTests(unittest.TestCase):
    def row(self):
        return {"schema_version":1,"homepage_status":200,"login_status":200,"prep_health_ok":True,
            "prep_database_ok":True,"processes":[{"name":name,"pid":index+10,"restart_time":index,"status":"online"}
            for index,name in enumerate(ordinary.NAMES)]}

    def test_hydro_restart_is_allowed_but_three_other_processes_are_not(self):
        before=self.row();after=self.row();after["processes"][2]["pid"]+=100;after["processes"][2]["restart_time"]+=1
        ordinary.compare(before,after)
        after=self.row();after["processes"][0]["pid"]+=1
        with self.assertRaisesRegex(ordinary.OrdinaryBackupError,"stable"):
            ordinary.compare(before,after)

    def test_exact_four_process_contract(self):
        ordinary.validate(self.row());value=self.row();value["processes"].reverse()
        with self.assertRaisesRegex(ordinary.OrdinaryBackupError,"order"):
            ordinary.validate(value)


if __name__=="__main__":unittest.main()
