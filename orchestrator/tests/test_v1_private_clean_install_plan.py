import argparse
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location("build_v1_private_clean_install_plan",
    ROOT/"scripts/build_v1_private_clean_install_plan.py")
builder=importlib.util.module_from_spec(SPEC);assert SPEC.loader;SPEC.loader.exec_module(builder)


class PrivateCleanInstallPlanTests(unittest.TestCase):
    def plugin_values(self):
        return {"ORCHESTRATOR_TOKEN_FILE":"/root/.hydro/orchestrator-token","ORCHESTRATOR_DOMAIN":"system",
          "ORCHESTRATOR_MAX_CODE_BYTES":"524288","ORCHESTRATOR_IDEMPOTENCY_FILE":"/root/.hydro/orchestrator-state/submissions.json",
          "ORCHESTRATOR_IDEMPOTENCY_MAX_ENTRIES":"20000","ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE":"/root/.hydro/orchestrator-state/notifications.json",
          "ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_MAX_ENTRIES":"20000","ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE":"/root/.hydro/orchestrator-state/problem-drafts.json",
          "ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_MAX_ENTRIES":"2000","ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE":"/root/.hydro/orchestrator-state/materials.json",
          "ORCHESTRATOR_MATERIAL_IDEMPOTENCY_MAX_ENTRIES":"2000","ORCHESTRATOR_MATERIAL_MAX_BYTES":"201326592",
          "ORCHESTRATOR_NOTIFY_ALLOWED_HTTPS_HOSTS":"exam.example.test",
          "ORCHESTRATOR_TEACHER_ADMIN_URL":"https://exam.example.test/admin"}

    def test_clean_staging_recovery_uses_its_exact_allowlist(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);final=root/"private";plan="1"*64
            staging=root/f".private.v1-clean-install-{plan[:12]}.pending"
            staging.mkdir();staging.chmod(0o700)
            (staging/"desired-plugin.env").write_text("A=B\n");(staging/"desired-plugin.env").chmod(0o600)
            published,fresh=builder.private_staging(final,plan,allowed_files=builder.CLEAN_STAGING_FILES,
                                                    operation_slug="clean-install")
            self.assertEqual(published,final);self.assertEqual(list(fresh.iterdir()),[])

    def test_site_env_uses_generated_single_quote_semantics(self):
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/"site.env";content=("HYDRO_ORCHESTRATOR_TOKEN='"+"x"*32+"'\nPATH_VALUE='/srv/a b'\n").encode()
            path.write_bytes(content);path.chmod(0o600)
            with mock.patch.object(builder,"safe_private_file",return_value=(content,mock.Mock())):
                value=builder.env_file(path)
            self.assertEqual(value["HYDRO_ORCHESTRATOR_TOKEN"],"x"*32)
            self.assertEqual(value["PATH_VALUE"],"/srv/a b")

    def test_token_must_equal_site_shared_token(self):
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/"token";path.write_bytes(("x"*32+"\n").encode());path.chmod(0o600)
            with mock.patch.object(builder,"safe_private_file",return_value=(path.read_bytes(),mock.Mock())):
                self.assertEqual(builder.token_file(path,{"HYDRO_ORCHESTRATOR_TOKEN":"x"*32}),path.read_bytes())
                with self.assertRaisesRegex(builder.PrivatePlanError,"shared token"):
                    builder.token_file(path,{"HYDRO_ORCHESTRATOR_TOKEN":"y"*32})

    def test_contest_ssh_key_rejects_group_or_world_access(self):
        with tempfile.TemporaryDirectory() as raw:
            key=Path(raw)/"contest.pem";key.write_text("key");key.chmod(0o600)
            metadata=SimpleNamespace(st_mode=stat.S_IFREG|0o640,st_uid=0,st_nlink=1)
            with mock.patch.object(builder,"safe_ancestors"), \
                    mock.patch.object(builder.os,"lstat",return_value=metadata), \
                    mock.patch.object(builder.platform,"system",return_value="Linux"), \
                    self.assertRaisesRegex(builder.PrivatePlanError,"metadata"):
                builder.trusted_mount(str(key.resolve()),"contest SSH key",False,private=True)
            with mock.patch.object(builder,"safe_ancestors"), \
                    mock.patch.object(builder.os,"lstat",return_value=metadata), \
                    mock.patch.object(builder.platform,"system",return_value="Linux"):
                self.assertEqual(builder.trusted_mount(str(key.resolve()),"known hosts",False),key.resolve())

    def test_plugin_env_is_an_exact_fixed_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/"plugin.env";values=self.plugin_values()
            content="".join(f"{key}='{value}'\n" for key,value in values.items()).encode();path.write_bytes(content);path.chmod(0o600)
            with mock.patch.object(builder,"safe_private_file",return_value=(content,mock.Mock())):
                self.assertEqual(builder.plugin_env_file(path,"system","exam.example.test"),content)
            values["EXTRA"]="x";changed="".join(f"{key}='{value}'\n" for key,value in values.items()).encode()
            with mock.patch.object(builder,"safe_private_file",return_value=(changed,mock.Mock())), \
                    self.assertRaisesRegex(builder.PrivatePlanError,"contract"):
                builder.plugin_env_file(path,"system","exam.example.test")

    def test_desired_definition_has_fixed_runtime_and_no_docker_control(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);template=root/"controller.json";install=Path("/opt/noi-linux-contest-system")
            environment={"HYDRO_ORCHESTRATOR_TOKEN":"x"*32,"A":"B","FRONTEND_CADDY_DIR":"/srv/caddy",
                         "CONTEST_SSH_KEY":"/srv/contest.pem","CONTEST_KNOWN_HOSTS":"/srv/known_hosts",
                         "ARTIFACT_TOOLS_DIR":"/srv/artifact-tools"}
            value={"config":{"Env":[f"{key}={item}" for key,item in environment.items()],"Labels":{},"WorkingDir":"/app"},
                   "host_config":{"NetworkMode":"host","RestartPolicy":{"Name":"unless-stopped","MaximumRetryCount":0},
                                  "Privileged":False,"ReadonlyRootfs":True,"CapDrop":["ALL"],
                                  "SecurityOpt":["no-new-privileges:true"],"Tmpfs":{"/tmp":"rw,nosuid,nodev,noexec,size=268435456"},
                                  "Init":True,"Binds":[
                                    f"{install/'orchestrator/config.yaml'}:/app/config.yaml:ro",
                                    f"{install/'orchestrator/data'}:/app/data:rw",
                                    f"{install/'orchestrator/runtime'}:/app/runtime:rw",
                                    "/srv/caddy:/app/caddy:ro","/srv/contest.pem:/app/keys/contest.pem:ro",
                                    "/srv/known_hosts:/app/keys/known_hosts:ro",
                                    "/srv/artifact-tools:/opt/noi-artifact-tools:ro"]}}
            template.write_text(json.dumps(value));template.chmod(0o600)
            with mock.patch.object(builder,"safe_private_file",return_value=(template.read_bytes(),mock.Mock())), \
                    mock.patch.object(builder,"trusted_mount",side_effect=lambda value,*unused,**kwargs:value):
                result=builder.desired_from_template(template,"1"*64,"2"*40+"-"+"3"*12,
                                                     "sha256:"+"4"*64,install,environment)
            self.assertEqual(result["host_config"]["NetworkMode"],"host")
            value["host_config"]["Binds"].append("/var/run/docker.sock:/var/run/docker.sock")
            template.write_text(json.dumps(value))
            with mock.patch.object(builder,"safe_private_file",return_value=(template.read_bytes(),mock.Mock())), \
                    mock.patch.object(builder,"trusted_mount",side_effect=lambda value,*unused,**kwargs:value), \
                    self.assertRaisesRegex(builder.PrivatePlanError,"bind (set|syntax)"):
                builder.desired_from_template(template,"1"*64,"2"*40+"-"+"3"*12,
                                              "sha256:"+"4"*64,install,environment)

    def test_schema_is_strict_and_declares_clean_operation(self):
        schema=json.loads((ROOT/"release/v1-private-clean-install-plan.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["operation"]["const"],"clean-install")
        self.assertIn("desired_plugin_token",schema["required"])
        self.assertIn("private_artifact_sha256",schema["required"])

    def test_build_publishes_one_content_bound_plan_without_service_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);candidate=root/"candidate";candidate.mkdir();backup=root/"backup";backup.mkdir(mode=0o700)
            caddy=root/"Caddyfile";caddy.write_text("localhost\n");caddy.chmod(0o600)
            token="x"*32;environment={"HYDRO_ORCHESTRATOR_TOKEN":token,"FRONTEND_CADDY_DIR":"/srv/caddy",
                "CONTEST_SSH_KEY":"/srv/contest.pem","CONTEST_KNOWN_HOSTS":"/srv/known_hosts",
                "ARTIFACT_TOOLS_DIR":"/srv/artifact-tools","ORCHESTRATOR_CONFIG":"/app/config.yaml"}
            site_env=root/"site.env";site_env.write_bytes("".join(f"{k}='{v}'\n" for k,v in environment.items()).encode());site_env.chmod(0o600)
            site_config=root/"config.yaml";site_config.write_text("config\n");site_config.chmod(0o600)
            plugin_env=root/"plugin.env";plugin_env.write_bytes("".join(f"{k}='{v}'\n" for k,v in self.plugin_values().items()).encode());plugin_env.chmod(0o600)
            plugin_token=root/"token";plugin_token.write_bytes((token+"\n").encode());plugin_token.chmod(0o600)
            install=Path("/opt/noi-linux-contest-system");release="4"*40+"-"+"2"*12
            template={"config":{"Env":[f"{k}={v}" for k,v in environment.items()],"Labels":{},"WorkingDir":"/app"},
                "host_config":{"Binds":[f"{install/'orchestrator/config.yaml'}:/app/config.yaml:ro",
                    f"{install/'orchestrator/data'}:/app/data:rw",f"{install/'orchestrator/runtime'}:/app/runtime:rw",
                    "/srv/caddy:/app/caddy:ro","/srv/contest.pem:/app/keys/contest.pem:ro",
                    "/srv/known_hosts:/app/keys/known_hosts:ro","/srv/artifact-tools:/opt/noi-artifact-tools:ro"],
                    "NetworkMode":"host","RestartPolicy":{"Name":"unless-stopped","MaximumRetryCount":0},
                    "Privileged":False,"ReadonlyRootfs":True,"CapDrop":["ALL"],"SecurityOpt":["no-new-privileges:true"],
                    "Tmpfs":{"/tmp":"rw,nosuid,nodev,noexec,size=268435456"},"Init":True}}
            controller=root/"controller.json";controller.write_text(json.dumps(template));controller.chmod(0o600)
            def private(path,*unused,**kwargs):
                target=Path(path);return target.read_bytes(),target.stat()
            args=argparse.Namespace(plan_id="1"*64,expected_manifest_sha256="2"*64,controller_image_id="sha256:"+"3"*64,
                backup_manifest_sha256="5"*64,candidate=candidate,backup_directory=backup,output_directory=root/"private",
                install_root=install,site_config=site_config,site_env=site_env,plugin_env=plugin_env,plugin_token=plugin_token,
                controller_template=controller,caddyfile=caddy,python_bin=Path("/python"),bash_bin=Path("/bash"),
                pm2_bin=Path("/pm2"),node_bin=Path("/node"),docker_socket=Path("/docker.sock"),qualification_lab=True)
            manifest={"source":{"revision":"4"*40},"qualification":{"production_qualified":False,"report":None,"report_sha256":None}}
            candidate_row={"revision":"4"*40,"tree":"6"*40,"archive_sha256":"7"*64,"manifest_sha256":"2"*64}
            with mock.patch.object(builder,"trusted_executable",side_effect=lambda path:path), \
                    mock.patch.object(builder,"safe_private_file",side_effect=private), \
                    mock.patch.object(builder,"safe_docker_socket",return_value=args.docker_socket), \
                    mock.patch.object(builder,"safe_ancestors"),mock.patch.object(builder,"verify_clean_backup",return_value={"manifest_sha256":"5"*64}), \
                    mock.patch.object(builder,"candidate_identity",return_value=(manifest,b"archive",candidate_row)), \
                    mock.patch.object(builder,"public_identity",return_value=("1"*64,object())), \
                    mock.patch.object(builder,"effective_contract",return_value=("https://oj.example.test","https://exam.example.test","oj.example.test","system","exam.example.test")), \
                    mock.patch.object(builder,"qualification_image",return_value=args.controller_image_id), \
                    mock.patch.object(builder,"source_plan",return_value={"plan_id":"8"*64,"release_name":release}), \
                    mock.patch.object(builder,"desired_from_template",return_value={"schema_version":1,"plan_id":"1"*64,
                        "source_release":release,"image_id":args.controller_image_id,"config":{},"host_config":{}}), \
                    mock.patch.object(builder,"verify_definition"), \
                    mock.patch("apply_v1_clean_install.load_plan",return_value={}), \
                    mock.patch("apply_v1_clean_install.verify_bindings"):
                result=builder.build(args)
            self.assertEqual(result["service_mutations"],0)
            plan=json.loads((root/"private/private-clean-install-plan.json").read_text())
            self.assertEqual(plan["operation"],"clean-install")
            self.assertEqual(set(plan["private_artifact_sha256"]),{"expected_contract","desired_controller_definition",
                "desired_config","desired_env","desired_plugin_env","desired_plugin_token"})


if __name__=="__main__":unittest.main()
