import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


ROOT=Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0,str(ROOT/"scripts"))
spec=importlib.util.spec_from_file_location("install_backup_collector",ROOT/"scripts/build_v1_install_backup.py")
collector=importlib.util.module_from_spec(spec);assert spec.loader;spec.loader.exec_module(collector)


class InstallBackupCollectorTests(unittest.TestCase):
    def test_sqlite_online_backup_is_complete_and_independent(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);source=root/"live.db";target=root/"backup.db"
            connection=sqlite3.connect(source)
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone(),("wal",))
            connection.execute("CREATE TABLE state(value TEXT)")
            connection.execute("INSERT INTO state VALUES ('sealed')");connection.commit();connection.close()
            with mock.patch.object(collector,"safe_ancestors"):
                collector.backup_database(source,target)
            connection=sqlite3.connect(source);connection.execute("UPDATE state SET value='changed'");connection.commit();connection.close()
            sealed=sqlite3.connect(target)
            self.assertEqual(sealed.execute("PRAGMA integrity_check").fetchone(),("ok",))
            self.assertEqual(sealed.execute("SELECT value FROM state").fetchone(),("sealed",))
            self.assertEqual(sealed.execute("PRAGMA journal_mode").fetchone(),("delete",));sealed.close()
            self.assertFalse(Path(str(target)+"-wal").exists())
            self.assertFalse(Path(str(target)+"-shm").exists())

    def test_atomic_write_refuses_existing_target_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);target=root/"artifact";collector.atomic(target,b"one")
            self.assertEqual(target.read_bytes(),b"one")
            with self.assertRaisesRegex(collector.CollectInstallError,"already exists"):
                collector.atomic(target,b"two")
            self.assertEqual(sorted(path.name for path in root.iterdir()),["artifact"])

    def test_collection_wires_every_semantic_collector_before_seal(self):
        class Args:pass
        args=Args();args.output_directory=Path("/backup");args.source_pointer=Path("/current")
        args.project_config=Path("/config");args.project_env=Path("/env");args.database=Path("/db")
        args.caddyfile=Path("/Caddyfile");args.snippet=Path("/snippet");args.pm2_bin=Path("/pm2")
        args.docker_socket=Path("/docker.sock");args.oj_origin="https://oj.example.test"
        args.plan_id="1"*64;args.source_revision="2"*40;args.candidate_manifest_sha256="3"*64
        manifest={"artifacts":{"x":{}}};raw=b"{\"sealed\":true}\n";events=[]
        def mark(name,result=None):
            def call(*unused,**kwargs):events.append(name);return result
            return call
        with mock.patch.object(collector,"private_output",return_value=Path("/sealed")), \
             mock.patch.object(collector,"source_pointer",side_effect=mark("source")), \
             mock.patch.object(collector,"copy_file",side_effect=mark("copy",True)), \
             mock.patch.object(collector,"backup_database",side_effect=mark("database")), \
             mock.patch.object(collector,"caddy",side_effect=mark("caddy")), \
             mock.patch.object(collector,"collect_hydro",side_effect=mark("hydro")), \
             mock.patch.object(collector,"collect_controller",side_effect=mark("controller")), \
             mock.patch.object(collector,"build_ordinary",side_effect=mark("ordinary")), \
             mock.patch.object(collector,"build_cloud",side_effect=mark("cloud")), \
             mock.patch.object(collector,"seal_manifest",side_effect=mark("seal",manifest)), \
             mock.patch.object(collector,"safe_file",return_value=(raw,mock.Mock())):
            result=collector.collect(args)
        self.assertLess(events.index("source"),events.index("seal"));self.assertLess(events.index("database"),events.index("seal"))
        for name in ("caddy","hydro","controller","ordinary","cloud"):self.assertLess(events.index(name),events.index("seal"))
        self.assertEqual(result["backup_manifest_sha256"],hashlib.sha256(raw).hexdigest())


if __name__=="__main__":unittest.main()
