import importlib.util
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]; sys.path.insert(0,str(ROOT/"scripts"))
SCRIPT=ROOT/"scripts"/"v1_power_loss_recovery_action_agent.py"; BUILDER=ROOT/"scripts"/"build_v1_power_loss_recovery_action_agent.py"
spec=importlib.util.spec_from_file_location("v1_power_loss_recovery_action_agent",SCRIPT); agent=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(agent)
builder_spec=importlib.util.spec_from_file_location("build_v1_power_loss_recovery_action_agent",BUILDER); builder=importlib.util.module_from_spec(builder_spec); assert builder_spec.loader; builder_spec.loader.exec_module(builder)

def configuration(prefix="/root/power-loss"):
    processes=[{"name":name,"pid":100+i,"restart_time":0,"status":"online"} for i,name in enumerate(("caddy","hydro-sandbox","hydrooj","mongodb"))]
    probes=[{"url":f"http://127.0.0.1:80{path}","host":"oj.example","status":200,"body_contains":body} for path,body in (("/","Hydro"),("/login","login"),("/prep/health","ready"))]
    return {"schema_version":1,"qualification_marker":"NOI-V1-QUAL-1234567890ABCDEF","session_id":"1"*64,
        "source":{"revision":"2"*40,"tree":"3"*40},"components":{"orchestrator_image_digest":"sha256:"+"4"*64,
        "desktop_image_id":"sha256:"+"5"*64,"desktop_source_revision":"2"*40,"hydro_plugin_sha256":"6"*64},
        "common_library_path":f"{prefix}/common.py","common_library_sha256":"7"*64,"docker_socket":"/var/run/docker.sock","app_root":f"{prefix}/app",
        "promotion_script":f"{prefix}/promote.sh","promotion_script_sha256":"8"*64,"recovery_script":f"{prefix}/recover.sh",
        "recovery_script_sha256":"9"*64,"bash_path":"/bin/bash","candidate_tag":"noi-linux:candidate-v1",
        "candidate_image_id":"sha256:"+"5"*64,"source_root":f"{prefix}/source","source_revision":"2"*40,
        "old_image_id":"sha256:"+"a"*64,"old_source_target":"image-releases/20260813T000000Z-old",
        "ready_path":f"{prefix}/ready.json","controller_health":{"url":"http://127.0.0.1:8600/healthz","timeout_seconds":3},
        "ordinary_oj":{"pm2_path":f"{prefix}/pm2","pm2_home":"/root/.pm2","processes":processes,"http_probes":probes},
        "signer":"power-loss-agent","signing_public_key":"ssh-ed25519 "+"A"*68,"signing_key_path":f"{prefix}/id_ed25519",
        "ssh_keygen_path":f"{prefix}/ssh-keygen","lock_path":f"{prefix}/lock","state_path":f"{prefix}/state.json",
        "receipt_path":f"{prefix}/receipt.json","output_path":f"{prefix}/action.json"}

class Common:
    def file_sha256(self,path): return "f"*64
    def regular(self,path,*_a,**_k): return Path(path)
    def acquire_lock(self,_path): return 8
    def sign(self,_row,_payload): return "c2ln"
    def verify_signature(self,*_a): return None
    def atomic_write(self,path,raw): Path(path).parent.mkdir(parents=True,exist_ok=True); Path(path).write_bytes(raw)
    def unlink_durable(self,path): Path(path).unlink(missing_ok=True)

class PowerLossAgentTests(unittest.TestCase):
    def test_config_binds_source_image_and_private_paths(self):
        row=agent.validate_config(configuration()); self.assertEqual(row["old_source_target"],"image-releases/20260813T000000Z-old")
        wrong=configuration(); wrong["components"]["desktop_image_id"]="sha256:"+"0"*64
        with self.assertRaisesRegex(agent.AgentError,"component identity"): agent.validate_config(wrong)
        wrong=configuration(); wrong["candidate_tag"]="noi-linux:latest"
        with self.assertRaisesRegex(agent.AgentError,"identity"): agent.validate_config(wrong)

    def test_wait_ready_requires_exact_stopped_process_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            row=configuration(Path(raw).as_posix())
            if platform.system().lower() == "linux":
                process=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])
                try:
                    os.kill(process.pid,signal.SIGSTOP)
                    Path(row["ready_path"]).write_text(json.dumps({"schema_version":1,"qualification_marker":row["qualification_marker"],
                        "phase":"marker_durable_before_mutation","pid":process.pid}),encoding="utf-8")
                    Path(row["ready_path"]).chmod(0o600)
                    agent.wait_ready(row,process)
                finally:
                    os.kill(process.pid,signal.SIGCONT)
                    process.terminate(); process.wait(timeout=10)
            else:
                process=mock.Mock(pid=123,poll=mock.Mock(return_value=None))
                Path(row["ready_path"]).write_text(json.dumps({"schema_version":1,"qualification_marker":row["qualification_marker"],
                    "phase":"marker_durable_before_mutation","pid":123}),encoding="utf-8")
                Path(row["ready_path"]).chmod(0o600)
                agent.wait_ready(row,process)

    def test_builder_freezes_configuration(self):
        raw=builder.render(agent.validate_config(configuration()))
        self.assertNotIn(builder.MARKER,raw.decode()); self.assertIn("candidate_image_id",raw.decode()); compile(raw.decode(),"<power-loss-test>","exec")

    def test_success_requires_kill_block_two_recoveries_and_signed_fact(self):
        with tempfile.TemporaryDirectory() as raw:
            row=configuration(Path(raw).as_posix()); common=Common(); app=Path(row["app_root"]); app.mkdir(parents=True)
            process=mock.Mock(pid=123,poll=mock.Mock(side_effect=[None,-9]),wait=mock.Mock(return_value=-9)); commands=[]
            def ready(_row,_process):
                Path(row["ready_path"]).write_text("{}",encoding="utf-8"); Path(row["ready_path"]).chmod(0o600)
                (app/"image-promotion.pending").write_text("marker",encoding="utf-8"); (app/"image-promotion.pending").chmod(0o600)
            def program(argv,_env,expect=0):
                commands.append((list(argv),expect))
                if "--expected-marker-sha256" in argv:
                    (app/"image-promotion.pending").unlink(missing_ok=True); (app/"image-promotion-recovery.receipt").write_text("ok")
                return mock.Mock(stderr=b"unfinished image transaction found")
            with mock.patch.object(agent,"validate_config",return_value=row),mock.patch.object(agent,"load_common",return_value=common), \
                    mock.patch.object(agent,"script",side_effect=[Path(row["promotion_script"]),Path(row["recovery_script"])]), \
                    mock.patch.object(agent,"baseline",return_value=[{"same":True}]),mock.patch.object(agent.subprocess,"Popen",return_value=process), \
                    mock.patch.object(agent,"wait_ready",side_effect=ready),mock.patch.object(agent,"run_program",side_effect=program), \
                    mock.patch.object(agent.os,"kill"),mock.patch.object(agent.os,"close"):
                result=agent.run(row)
            self.assertEqual(result["status"],"passed"); self.assertEqual(len(commands),3); self.assertEqual(commands[0][1],1)
            self.assertEqual(commands[1][0],commands[2][0])
            action=json.loads(Path(row["output_path"]).read_text()); self.assertTrue(action["payload"]["baseline_restored"])

if __name__=="__main__": unittest.main()
