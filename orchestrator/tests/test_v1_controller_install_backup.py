import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
from types import SimpleNamespace
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0,str(ROOT/"scripts"))

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

verify=load("controller_backup_verify",ROOT/"scripts/verify_v1_controller_install_backup.py")
build=load("controller_backup_build",ROOT/"scripts/build_v1_controller_install_backup.py")


class ControllerInstallBackupTests(unittest.TestCase):
    def inspect(self):
        return {"Id":"a"*64,"Name":"/noi-orchestrator","Image":"sha256:"+"b"*64,"RestartCount":0,
                "State":{"Running":True},"Config":{"Hostname":"host","Domainname":"","User":"",
                "Env":["SECRET=x"],"Cmd":["run"],"Image":"candidate","Volumes":None,"WorkingDir":"/app",
                "Entrypoint":None,"Labels":{"com.docker.compose.service":"orchestrator"},"StopSignal":None,"StopTimeout":1},
                "HostConfig":{"Binds":["/x:/app/x:ro"],"NetworkMode":"host","RestartPolicy":{"Name":"unless-stopped","MaximumRetryCount":0},
                "ReadonlyRootfs":False,"SecurityOpt":["no-new-privileges:true"],"Tmpfs":{"/tmp":""},"CapAdd":None,"CapDrop":None,
                "Privileged":False,"GroupAdd":None,"Devices":None,"Sysctls":None,"ShmSize":67108864,"Init":False,"LogConfig":{"Type":"json-file","Config":{}}},
                "Mounts":[{"Type":"bind","Source":"/x","Destination":"/app/x","RW":False,"Propagation":"rprivate"}]}

    def definition(self):
        value=self.inspect(); identity=build.immutable_identity(value)
        return {"schema_version":1,"present":True,"container":{"container_id":value["Id"],"name":value["Name"],
                "image_id":value["Image"],"running":True,"restart_count":0,"immutable_identity":identity,
                "immutable_identity_sha256":hashlib.sha256(verify.canonical(identity)).hexdigest()}}

    def test_exact_definition_and_image_pass(self):
        definition=self.definition(); self.assertTrue(verify.validate_definition(definition)["present"])
        image={"schema_version":1,"present":True,"image_id":"sha256:"+"b"*64}
        self.assertEqual(verify.validate_image(image,definition)["image_id"],image["image_id"])

    def test_definition_binds_secret_env_without_printing_it(self):
        definition=self.definition(); definition["container"]["immutable_identity"]["config"]["Env"]=["SECRET=changed"]
        with self.assertRaisesRegex(verify.ControllerBackupError,"identity"):
            verify.validate_definition(definition)

    def test_absent_controller_is_explicit(self):
        definition={"schema_version":1,"present":False,"container":None}
        image={"schema_version":1,"present":False,"image_id":None}
        verify.validate_definition(definition); verify.validate_image(image,definition)

    def test_collector_normalization_is_stable(self):
        first=build.immutable_identity(self.inspect()); changed=self.inspect(); changed["Mounts"].reverse()
        self.assertEqual(first,build.immutable_identity(changed))

    def test_created_controller_must_match_every_requested_field(self):
        value=self.inspect(); desired={"schema_version":1,"plan_id":"1"*64,
            "source_release":"2"*40+"-"+"3"*12,"image_id":value["Image"],
            "config":{"Image":value["Image"],"WorkingDir":"/app","Labels":{
                "org.noi.install.plan":"1"*64,"org.noi.source.release":"2"*40+"-"+"3"*12}},
            "host_config":{"NetworkMode":"host","RestartPolicy":{"Name":"unless-stopped","MaximumRetryCount":0},"Privileged":False}}
        value["Config"].update(desired["config"]);value["HostConfig"].update(desired["host_config"])
        value["Config"]["Env"]=None
        apply_module=load("controller_apply",ROOT/"scripts/apply_v1_controller.py")
        image={"Id":value["Image"],"Config":{"Env":None,"Labels":None}}
        self.assertTrue(apply_module.created_matches_desired(value,desired,image))
        value["HostConfig"]["Privileged"]=True
        self.assertFalse(apply_module.created_matches_desired(value,desired,image))

    def test_created_controller_binds_only_requested_and_image_environment(self):
        apply_module=load("controller_apply_effective_config",ROOT/"scripts/apply_v1_controller.py")
        image_id="sha256:"+"b"*64
        desired={"plan_id":"1"*64,"source_release":"2"*40+"-"+"3"*12,"image_id":image_id,
            "config":{"Image":image_id,"Env":["SITE=value","PATH=/site/bin"],"Labels":{
                "org.noi.install.plan":"1"*64,"org.noi.source.release":"2"*40+"-"+"3"*12}},
            "host_config":{"NetworkMode":"host","RestartPolicy":{"Name":"unless-stopped"},"Privileged":False}}
        image={"Id":image_id,"Config":{"Env":["PATH=/image/bin","IMAGE_DEFAULT=yes"],
            "Labels":{"org.opencontainers.image.revision":"4"*40}}}
        value={"Image":image_id,"Config":{"Image":image_id,
            "Env":["SITE=value","PATH=/site/bin","IMAGE_DEFAULT=yes"],"Labels":{
                "org.noi.install.plan":"1"*64,"org.noi.source.release":"2"*40+"-"+"3"*12,
                "org.opencontainers.image.revision":"4"*40}},"HostConfig":desired["host_config"]}
        self.assertTrue(apply_module.created_matches_desired(value,desired,image))
        value["Config"]["Env"].append("UNPLANNED=value")
        self.assertFalse(apply_module.created_matches_desired(value,desired,image))
        value["Config"]["Env"].pop();value["Config"]["Labels"]["unplanned"]="value"
        self.assertFalse(apply_module.created_matches_desired(value,desired,image))

    def test_image_probe_requires_the_exact_immutable_id(self):
        apply_module=load("controller_apply_image",ROOT/"scripts/apply_v1_controller.py")
        docker=apply_module.Docker(Path("/unused"))
        docker.request=lambda *args,**kwargs:(200,json.dumps({"Id":"sha256:"+"b"*64}).encode())
        expected="sha256:"+"b"*64
        self.assertEqual(docker.image(expected)["Id"],expected)
        with self.assertRaisesRegex(apply_module.ControllerPhaseError,"identity"):
            docker.image("sha256:"+"c"*64)

    def test_orderly_controller_stop_has_a_bounded_http_margin(self):
        apply_module=load("controller_apply_stop",ROOT/"scripts/apply_v1_controller.py")
        docker=apply_module.Docker(Path("/unused"));calls=[]
        docker.request=lambda *args,**kwargs:calls.append((args,kwargs)) or (204,b"")
        docker.stop("a"*64)
        self.assertEqual(calls[0][1]["timeout"],105)
        self.assertIn("t=90",calls[0][0][1])

    def test_replace_then_rollback_restores_exact_old_controller_quiesced(self):
        apply_module=load("controller_apply_lifecycle",ROOT/"scripts/apply_v1_controller.py")
        old=self.inspect(); old_id=old["Id"]; image_id="sha256:"+"c"*64
        desired={"schema_version":1,"plan_id":"1"*64,
            "source_release":"2"*40+"-"+"3"*12,"image_id":image_id,
            "config":{"Image":image_id,"WorkingDir":"/app","Labels":{
                "org.noi.install.plan":"1"*64,"org.noi.source.release":"2"*40+"-"+"3"*12}},
            "host_config":{"NetworkMode":"host","RestartPolicy":{"Name":"unless-stopped","MaximumRetryCount":0},"Privileged":False}}

        class FakeDocker:
            def __init__(self,row):self.rows={row["Id"]:row};self.names={"noi-orchestrator":row["Id"]};self.counter=0
            def inspect(self,key,allow_absent=False):
                row=self.rows.get(self.names.get(key,key))
                if row is None and not allow_absent:raise AssertionError(f"missing {key}")
                return row
            def image(self,key):
                if key!=image_id:raise AssertionError(key)
                return {"Id":key}
            def stop(self,key):self.rows[key]["State"]["Running"]=False
            def start(self,key):self.rows[key]["State"]["Running"]=True
            def rename(self,key,name):
                for current,identifier in list(self.names.items()):
                    if identifier==key:del self.names[current]
                self.names[name]=key;self.rows[key]["Name"]="/"+name
            def remove(self,key):
                self.rows.pop(key)
                for current,identifier in list(self.names.items()):
                    if identifier==key:del self.names[current]
            def create(self,name,body):
                self.counter+=1;identifier=f"{self.counter:064x}"
                row={"Id":identifier,"Name":"/"+name,"Image":body["Image"],"RestartCount":0,
                     "State":{"Running":False},"Config":dict(body),"HostConfig":body["HostConfig"],"Mounts":[]}
                row["Config"].pop("HostConfig",None);self.rows[identifier]=row;self.names[name]=identifier;return identifier

        temporary_parent="/root" if platform.system().lower()=="linux" and os.geteuid()==0 else None
        with tempfile.TemporaryDirectory(dir=temporary_parent) as directory:
            root=Path(directory); backup=root/"backup"; backup.mkdir()
            project=root/"project";project.mkdir()
            inputs=root/"inputs";inputs.mkdir()
            paths={"config":project/"config.yaml","env":project/".env","db":project/"db.sqlite",
                   "wal":project/"db.sqlite-wal","shm":project/"db.sqlite-shm"}
            files={"orchestrator_config":"orchestrator-config.yaml","orchestrator_env":"orchestrator.env",
                   "orchestrator_database":"orchestrator.db","orchestrator_database_wal":"orchestrator.db-wal",
                   "orchestrator_database_shm":"orchestrator.db-shm"}
            manifest={"artifacts":{}}
            for index,(key,filename) in enumerate(files.items()):
                present=key not in {"orchestrator_database_wal","orchestrator_database_shm"}
                manifest["artifacts"][key]={"present":present,"filename":filename,"mode":0o600 if present else None}
                if present and key!="orchestrator_database":(backup/filename).write_bytes(f"old-{index}".encode())
            for index,path in enumerate(paths.values()):
                if index<2:path.write_bytes(f"old-{index}".encode())
            import sqlite3
            for path in (paths["db"],backup/"orchestrator.db"):
                connection=sqlite3.connect(path);connection.execute("CREATE TABLE state(value TEXT)")
                connection.execute("INSERT INTO state VALUES ('old')");connection.commit();connection.close();path.chmod(0o600)
            desired_config=inputs/"config.yaml";desired_env=inputs/"env";desired_config.write_bytes(b"new-config");desired_env.write_bytes(b"new-env")
            if platform.system().lower()=="linux":
                desired_config.chmod(0o600);desired_env.chmod(0o600)
            args=SimpleNamespace(plan_id="1"*64,desired_config=desired_config,desired_env=desired_env,
                project_config=paths["config"],project_env=paths["env"],database=paths["db"],
                backup_manifest_sha256="4"*64)
            definition=self.definition(); docker=FakeDocker(old)
            original_health=apply_module.local_health;apply_module.local_health=lambda:None
            try:
                result=apply_module.apply(args,docker,backup,manifest,definition,desired,desired["source_release"])
            finally:apply_module.local_health=original_health
            self.assertTrue(result["healthy"]);self.assertNotEqual(docker.inspect("noi-orchestrator")["Id"],old_id)
            rollback=apply_module.rollback(args,docker,backup,manifest,definition,desired["source_release"])
            self.assertTrue(rollback["controller_quiesced"])
            restored=docker.inspect("noi-orchestrator");self.assertEqual(restored["Id"],old_id)
            self.assertFalse(restored["State"]["Running"])
            self.assertEqual(paths["config"].read_bytes(),b"old-0")
            self.assertEqual(paths["env"].read_bytes(),b"old-1")
            connection=sqlite3.connect(paths["db"]);self.assertEqual(connection.execute("SELECT value FROM state").fetchone(),("old",));connection.close()

    def test_live_database_digest_detects_semantic_drift(self):
        apply_module=load("controller_apply_db",ROOT/"scripts/apply_v1_controller.py")
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);first=root/"a.db";second=root/"b.db"
            import sqlite3
            for path,value in ((first,"a"),(second,"a")):
                connection=sqlite3.connect(path);connection.execute("CREATE TABLE state(value TEXT)")
                connection.execute("INSERT INTO state VALUES (?)",(value,));connection.commit();connection.close()
            self.assertEqual(apply_module.sqlite_digest(first),apply_module.sqlite_digest(second))
            connection=sqlite3.connect(second);connection.execute("UPDATE state SET value='b'");connection.commit();connection.close()
            self.assertNotEqual(apply_module.sqlite_digest(first),apply_module.sqlite_digest(second))

    def test_sealed_wal_database_digest_is_side_effect_free(self):
        apply_module=load("controller_apply_immutable_db",ROOT/"scripts/apply_v1_controller.py")
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"sealed.db"
            import sqlite3
            connection=sqlite3.connect(path)
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone(),("wal",))
            connection.execute("CREATE TABLE state(value TEXT)")
            connection.execute("INSERT INTO state VALUES ('sealed')")
            connection.commit();connection.close()
            for suffix in ("-wal","-shm"):
                sidecar=Path(str(path)+suffix)
                if sidecar.exists():sidecar.unlink()
            digest=apply_module.sqlite_digest(path,immutable=True)
            self.assertRegex(digest,r"^[a-f0-9]{64}$")
            self.assertFalse(Path(str(path)+"-wal").exists())
            self.assertFalse(Path(str(path)+"-shm").exists())

    def test_clean_baseline_rejects_new_controller_files(self):
        apply_module=load("controller_apply_clean",ROOT/"scripts/apply_v1_controller.py")
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);args=SimpleNamespace(project_config=root/"config.yaml",
                project_env=root/".env",database=root/"orchestrator.db")
            manifest={"artifacts":{
                "orchestrator_config":{"present":False,"filename":"orchestrator-config.yaml"},
                "orchestrator_env":{"present":False,"filename":"orchestrator.env"},
                "orchestrator_database":{"present":False,"filename":"orchestrator.db"},
            }}
            apply_module.live_files_match_backup(args,root,manifest)
            args.project_env.write_text("appeared")
            with self.assertRaisesRegex(apply_module.ControllerPhaseError,"appeared"):
                apply_module.live_files_match_backup(args,root,manifest)

    def test_clean_apply_accepts_only_exact_clean_materials_outputs(self):
        apply_module=load("controller_apply_clean_materials",ROOT/"scripts/apply_v1_controller.py")
        temporary_parent="/root" if platform.system().lower()=="linux" and os.geteuid()==0 else None
        with tempfile.TemporaryDirectory(dir=temporary_parent) as directory:
            root=Path(directory);backup=root/"backup";backup.mkdir()
            inputs=root/"inputs";inputs.mkdir();project=root/"project";project.mkdir()
            desired_config=inputs/"config.yaml";desired_env=inputs/"env"
            desired_config.write_bytes(b"sealed-config");desired_env.write_bytes(b"sealed-env")
            project_config=project/"config.yaml";project_env=project/".env"
            project_config.write_bytes(desired_config.read_bytes());project_env.write_bytes(desired_env.read_bytes())
            for path in (desired_config,desired_env,project_config,project_env):path.chmod(0o600)
            args=SimpleNamespace(desired_config=desired_config,desired_env=desired_env,
                project_config=project_config,project_env=project_env,database=project/"orchestrator.db")
            manifest={"operation":"clean-install","artifacts":{
                "orchestrator_config":{"present":False,"filename":"orchestrator-config.yaml"},
                "orchestrator_env":{"present":False,"filename":"orchestrator.env"},
                "orchestrator_database":{"present":False,"filename":"orchestrator.db"},
            }}
            apply_module.live_files_match_apply_inputs(args,backup,manifest)
            project_env.write_bytes(b"untrusted-drift")
            with self.assertRaisesRegex(apply_module.ControllerPhaseError,"sealed input"):
                apply_module.live_files_match_apply_inputs(args,backup,manifest)

    def test_clean_apply_does_not_accept_an_early_database(self):
        apply_module=load("controller_apply_clean_database",ROOT/"scripts/apply_v1_controller.py")
        temporary_parent="/root" if platform.system().lower()=="linux" and os.geteuid()==0 else None
        with tempfile.TemporaryDirectory(dir=temporary_parent) as directory:
            root=Path(directory);backup=root/"backup";backup.mkdir()
            inputs=root/"inputs";inputs.mkdir();project=root/"project";project.mkdir()
            desired_config=inputs/"config.yaml";desired_env=inputs/"env"
            project_config=project/"config.yaml";project_env=project/".env"
            for path,value in ((desired_config,b"config"),(desired_env,b"env"),
                               (project_config,b"config"),(project_env,b"env")):
                path.write_bytes(value);path.chmod(0o600)
            database=project/"orchestrator.db";database.write_bytes(b"appeared");database.chmod(0o600)
            args=SimpleNamespace(desired_config=desired_config,desired_env=desired_env,
                project_config=project_config,project_env=project_env,database=database)
            manifest={"operation":"clean-install","artifacts":{
                "orchestrator_config":{"present":False,"filename":"orchestrator-config.yaml"},
                "orchestrator_env":{"present":False,"filename":"orchestrator.env"},
                "orchestrator_database":{"present":False,"filename":"orchestrator.db"},
            }}
            with self.assertRaisesRegex(apply_module.ControllerPhaseError,"database appeared"):
                apply_module.live_files_match_apply_inputs(args,backup,manifest)

    def test_rollback_stops_exact_baseline_before_restoring_private_files(self):
        apply_module=load("controller_apply_rollback_order",ROOT/"scripts/apply_v1_controller.py")
        baseline=self.definition();row=self.inspect();events=[]
        class Docker:
            def inspect(self,key,allow_absent=False):return None if key.startswith("noi-orchestrator-v1-old-") else row
            def stop(self,key):events.append("stop");row["State"]["Running"]=False
        args=SimpleNamespace(plan_id="1"*64,backup_manifest_sha256="2"*64,
            project_config=Path("/config"),project_env=Path("/env"),database=Path("/db"))
        original_wait=apply_module.wait_running;original_restore=apply_module.restore_file
        apply_module.wait_running=lambda *unused:events.append("wait") or row
        apply_module.restore_file=lambda *unused:events.append("restore")
        try:apply_module.rollback(args,Docker(),Path("/backup"),{"artifacts":{}},baseline,"")
        finally:apply_module.wait_running=original_wait;apply_module.restore_file=original_restore
        self.assertEqual(events[:2],["stop","wait"]);self.assertEqual(events[2:], ["restore"]*5)


if __name__=="__main__": unittest.main()
