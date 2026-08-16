import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import sqlite3
import tempfile
import unittest
import urllib.error

ROOT=Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0,str(ROOT/"scripts"))
spec=importlib.util.spec_from_file_location("verify_post_install",ROOT/"scripts/verify_v1_post_install.py")
post=importlib.util.module_from_spec(spec);spec.loader.exec_module(post)


class PostInstallTests(unittest.TestCase):
    def health(self):
        return {"ok":True,"active_seats":0,"realtime_judge":{"thread_alive":True,"running":True,
            "error_count":0,"last_error":"","queue_counts":{"pending":0,"retry":0,"sending":0,
            "ambiguous":0,"permanent_failed":0}},"seat_notifications":{"enabled":True,"healthy":True,
            "counts":{"pending":0,"retry":0,"permanent_failed":0,"untracked":0,"missing_resource":0,"invalid_pool":0}},
            "desktop_access":{"enabled":True,"healthy":True,"desired_open":False,"open":False,"closed":True,
            "managed_count":0,"conflict_count":0,"management_healthy":True,"management_missing_count":0,
            "instance_state":"STOPPED"}}

    def test_health_requires_closed_cloud_and_quiet_queues(self):
        post.validate_health(self.health())
        value=self.health();value["desktop_access"]["instance_state"]="RUNNING"
        with self.assertRaisesRegex(post.PostInstallError,"desktop"):
            post.validate_health(value)
        value=self.health();value["realtime_judge"]["queue_counts"]["ambiguous"]=1
        with self.assertRaisesRegex(post.PostInstallError,"queue"):
            post.validate_health(value)

    def test_database_gate_requires_integrity_and_no_active_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"state.db";connection=sqlite3.connect(path)
            connection.executescript("""
              CREATE TABLE contests(state TEXT);
              CREATE TABLE web_submissions(judge_state TEXT);
              CREATE TABLE artifact_jobs(state TEXT);
              CREATE TABLE seat_notifications(state TEXT);
            """);connection.commit();connection.close();post.database_quiet(path)
            connection=sqlite3.connect(path);connection.execute("INSERT INTO contests VALUES ('ready')");connection.commit();connection.close()
            with self.assertRaisesRegex(post.PostInstallError,"active contest"):
                post.database_quiet(path)

    def test_contract_is_exact_and_private_identity_bound(self):
        parent="/root" if platform.system().lower()=="linux" and os.geteuid()==0 else None
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            path=Path(directory)/"contract.json";plan="1"*64;release="2"*40+"-"+"3"*12
            value={"schema_version":1,"plan_id":plan,"source_release":release,
                "controller_image_id":"sha256:"+"4"*64,"oj_origin":"https://oj.example.test",
                "exam_origin":"https://exam.example.test","source_pointer":"/opt/noi/current-source",
                "caddyfile":"/root/.hydro/Caddyfile","snippet":"/opt/noi/runtime/caddy-exam.conf",
                "project_config":"/opt/noi/config.yaml","project_env":"/opt/noi/.env",
                "database":"/opt/noi/data/orchestrator.db","pm2_bin":"/nix/pm2","docker_socket":"/var/run/docker.sock"}
            path.write_text(json.dumps(value));path.chmod(0o600);self.assertEqual(post.contract(path,plan,release)["plan_id"],plan)
            value["unexpected"]=1;path.write_text(json.dumps(value));path.chmod(0o600)
            with self.assertRaisesRegex(post.PostInstallError,"field set"):
                post.contract(path,plan,release)

    def test_expected_http_error_status_is_a_valid_probe_response(self):
        original=post.urllib.request.build_opener
        url="http://127.0.0.1:8888/orchestrator/submit"
        class Opener:
            def open(self,request,timeout):
                raise urllib.error.HTTPError(url,403,"Forbidden",{},io.BytesIO(b"denied"))
        post.urllib.request.build_opener=lambda *unused:Opener()
        try:
            self.assertEqual(post.request("http://127.0.0.1:8888","/orchestrator/submit",
                                          method="POST",expected={403}),(403,b"denied"))
            with self.assertRaisesRegex(post.PostInstallError,"status differs"):
                post.request("http://127.0.0.1:8888","/orchestrator/submit",
                             method="POST",expected={200})
        finally:post.urllib.request.build_opener=original


if __name__=="__main__":unittest.main()
