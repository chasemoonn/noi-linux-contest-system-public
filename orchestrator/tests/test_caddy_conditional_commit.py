import importlib.util
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]; SCRIPT=ROOT/"scripts/commit_v1_caddy_config.py"
spec=importlib.util.spec_from_file_location("caddy_commit",SCRIPT); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

class Fake:
    def __init__(self,status=200,live=None): self.status=status; self.live=live or {"apps":{"old":True}}; self.requests=[]
    def request(self,method,path,body=None,headers=None):
        self.requests.append((method,path,body,headers or {}))
        if method=="GET": return 200,{"etag":"\"/config/ abc\""},module.canonical(self.live)
        if path=="/config/" and self.status in {200,201}: self.live=__import__("json").loads(body)
        return self.status,{},b""

class CaddyCommitTests(unittest.TestCase):
    def test_single_conditional_commit_and_post_verify(self):
        admin=Fake(); expected={"apps":{"old":True}}; desired={"apps":{"new":True}}
        result=module.commit(admin,expected,desired,reread_disk=lambda:b"disk",expected_disk_sha256=__import__("hashlib").sha256(b"disk").hexdigest())
        posts=[row for row in admin.requests if row[0]=="POST"]
        self.assertEqual(len(posts),1); self.assertEqual(posts[0][1],"/config/")
        self.assertEqual(posts[0][3]["If-Match"],'"/config/ abc"'); self.assertEqual(result["status"],"verified")
    def test_baseline_drift_disk_drift_and_412_fail_without_retry(self):
        with self.assertRaisesRegex(module.CaddyCommitError,"baseline"):
            module.commit(Fake(),{"wrong":True},{"new":True},reread_disk=lambda:b"disk",expected_disk_sha256="0"*64)
        with self.assertRaisesRegex(module.CaddyCommitError,"candidate file"):
            module.commit(Fake(),{"apps":{"old":True}},{"new":True},reread_disk=lambda:b"changed",expected_disk_sha256="0"*64)
        admin=Fake(status=412)
        with self.assertRaisesRegex(module.CaddyCommitError,"ETag race"):
            module.commit(admin,{"apps":{"old":True}},{"new":True},reread_disk=lambda:b"disk",expected_disk_sha256=__import__("hashlib").sha256(b"disk").hexdigest())
        self.assertEqual(len([row for row in admin.requests if row[0]=="POST"]),1)
    def test_restore_is_conditional_idempotent_and_refuses_external_change(self):
        desired={"apps":{"new":True}}; baseline={"apps":{"old":True}}
        already=Fake(live=baseline); result=module.restore(already,desired,baseline)
        self.assertTrue(result["already_restored"]); self.assertEqual(len([r for r in already.requests if r[0]=="POST"]),0)
        admin=Fake(live=desired); result=module.restore(admin,desired,baseline)
        self.assertFalse(result["already_restored"]); self.assertEqual(admin.live,baseline)
        with self.assertRaisesRegex(module.CaddyCommitError,"outside"):
            module.restore(Fake(live={"external":True}),desired,baseline)
        race=Fake(status=412,live=desired)
        with self.assertRaisesRegex(module.CaddyCommitError,"rollback lost"):
            module.restore(race,desired,baseline)
        self.assertEqual(len([r for r in race.requests if r[0]=="POST"]),1)
if __name__=="__main__": unittest.main()
