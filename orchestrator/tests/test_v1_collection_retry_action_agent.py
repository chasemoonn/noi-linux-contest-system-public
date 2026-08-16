import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "v1_collection_retry_action_agent.py"
BUILDER = ROOT / "scripts" / "build_v1_collection_retry_action_agent.py"
spec = importlib.util.spec_from_file_location("v1_collection_retry_action_agent", SCRIPT)
agent = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(agent)
builder_spec = importlib.util.spec_from_file_location("build_v1_collection_retry_action_agent", BUILDER)
builder = importlib.util.module_from_spec(builder_spec); assert builder_spec.loader; builder_spec.loader.exec_module(builder)


def config(prefix="/root/collection-retry"):
    return {"schema_version": 1, "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
        "session_id": "1" * 64, "contest_id": "a" * 24,
        "source": {"revision": "b" * 40, "tree": "c" * 40},
        "components": {"orchestrator_image_digest": "sha256:" + "d" * 64,
            "desktop_image_id": "sha256:" + "e" * 64, "desktop_source_revision": "b" * 40,
            "hydro_plugin_sha256": "f" * 64},
        "common_library_path": f"{prefix}/common.py", "common_library_sha256": "0" * 64,
        "docker_socket": "/var/run/docker.sock",
        "controller": {"container_id": "1" * 64, "image_id": "sha256:" + "d" * 64,
            "name": "/noi-orchestrator", "identity_sha256": "2" * 64, "restart_count": 0},
        "database_path": f"{prefix}/orchestrator.db", "expected_failed_row_sha256": "3" * 64,
        "failure_marker_host_path": f"{prefix}/qualification/collection-retry.json", "failure_marker_container_path": "/app/data/qualification/collection-retry.json",
        "controller_config_host_path": f"{prefix}/config.yaml", "controller_config_sha256": "4" * 64,
        "collected_root_host_path": f"{prefix}/collected", "collected_root_container_path": "/app/data/collected",
        "admin_url": "http://127.0.0.1:8600/admin", "admin_credentials_path": f"{prefix}/admin.json",
        "controller_health": {"url": "http://127.0.0.1:8600/healthz", "timeout_seconds": 3, "deadline_seconds": 30},
        "submission_status": {"url": "http://127.0.0.1:8888/orchestrator/submit/status", "token_path": f"{prefix}/token", "timeout_seconds": 3},
        "ordinary_oj": {"pm2_path": f"{prefix}/pm2", "pm2_home": "/root/.pm2",
            "processes": [{"name": name, "pid": index + 100, "restart_time": 0, "status": "online"}
                          for index, name in enumerate(("caddy", "hydro-sandbox", "hydrooj", "mongodb"))],
            "http_probes": [{"url": f"http://127.0.0.1:80{path}", "host": "oj.example", "status": 200, "body_contains": body}
                            for path, body in (("/", "Hydro"), ("/login", "login"), ("/prep/health", "ready"))]},
        "signer": "collection-retry-agent", "signing_public_key": "ssh-ed25519 " + "A" * 68,
        "signing_key_path": f"{prefix}/id_ed25519", "ssh_keygen_path": f"{prefix}/ssh-keygen",
        "lock_path": f"{prefix}/agent.lock", "recovery_state_path": f"{prefix}/pending.json",
        "receipt_path": f"{prefix}/receipt.json", "output_path": f"{prefix}/action.json"}


def failed_row():
    return {"id": 7, "tid": "a" * 24, "uid": 9, "problem": "sum", "sha256": "5" * 64, "size": 42,
        "submission_id": "6" * 64, "submission_session": "7" * 32, "judge_pid": "P1", "judge_lang": "cc",
        "judge_sha256": "8" * 64, "judge_state": "permanent_failed", "judge_kind": "realtime",
        "accepted_at_ms": 1, "rid": "", "attempts": 1}


class DummyCommon:
    def __init__(self): self.outputs = []
    def file_sha256(self, _path): return "9" * 64
    def regular(self, path, *_a, **_k): return path
    def acquire_lock(self, _path): return 99
    def sign(self, _row, _payload): return "c2ln"
    def verify_signature(self, *_a): return None
    def ordinary_snapshot(self, _row): return [{"same": True}]
    def health_ready(self, _row): return None
    def atomic_write(self, path, raw): Path(path).parent.mkdir(parents=True, exist_ok=True); Path(path).write_bytes(raw); self.outputs.append(Path(path))
    def unlink_durable(self, path):
        try: Path(path).unlink()
        except FileNotFoundError: pass
    def wait_delivered(self, _row, frozen): return [dict(frozen[0], judge_state="submitted", rid="a" * 24)]
    def verify_unique_records(self, *_a): return None


class CollectionRetryActionAgentTests(unittest.TestCase):
    def test_config_rejects_controller_component_or_nonlocal_admin_drift(self):
        row = agent.validate_config(config())
        self.assertEqual(row["controller"]["name"], "/noi-orchestrator")
        wrong = config(); wrong["controller"]["image_id"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(agent.AgentError, "component identity"): agent.validate_config(wrong)
        wrong = config(); wrong["admin_url"] = "http://192.0.2.1:8600/admin"
        with self.assertRaisesRegex(agent.AgentError, "local endpoint"): agent.validate_config(wrong)

    def test_failure_marker_is_exactly_one_submission(self):
        row = config(); frozen = failed_row()
        self.assertEqual(json.loads(agent.marker_bytes(row, frozen)), {
            "failure": "block_until_removed", "qualification_marker": row["qualification_marker"],
            "scenario": "collection_retry", "schema_version": 1, "submission_id": frozen["submission_id"]})

    def test_failed_submission_requires_one_seat_one_problem_latest_row(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "orchestrator.db"; connection = sqlite3.connect(path); frozen = failed_row()
            connection.executescript("CREATE TABLE contests(tid TEXT,state TEXT,files TEXT,pids TEXT,collection_run_id TEXT,collection_dir TEXT,collection_receipt_sha256 TEXT);"
                "CREATE TABLE seats(tid TEXT,uid INTEGER); CREATE TABLE web_submissions(id INTEGER,tid TEXT,uid INTEGER,problem TEXT,sha256 TEXT,size INTEGER,submission_id TEXT,submission_session TEXT,judge_pid TEXT,judge_lang TEXT,judge_sha256 TEXT,judge_state TEXT,judge_kind TEXT,accepted_at_ms INTEGER,rid TEXT,attempts INTEGER);")
            connection.execute("INSERT INTO contests VALUES(?,?,?,?,?,?,?)", ("a" * 24, "ready", '{"sum":"P1"}', '{"sum":"P1"}', "", "", ""))
            connection.execute("INSERT INTO seats VALUES(?,?)", ("a" * 24, 9))
            connection.execute("INSERT INTO web_submissions VALUES(" + ",".join("?" for _ in agent.ROW_COLUMNS) + ")", tuple(frozen[k] for k in agent.ROW_COLUMNS))
            connection.commit(); connection.close()
            row = config(Path(raw).as_posix()); row["database_path"] = path.as_posix(); row["expected_failed_row_sha256"] = agent.hashlib.sha256(agent.canonical(frozen)).hexdigest()
            self.assertEqual(agent.failed_submission(row), frozen)

    def test_success_orders_failure_removal_single_retry_and_signed_fact(self):
        with tempfile.TemporaryDirectory() as raw:
            row = config(Path(raw).as_posix()); frozen = failed_row(); common = DummyCommon(); order = []
            ready = {"state": "ready", "collection_run_id": "", "collection_dir": "", "collection_receipt_sha256": ""}
            failed = {"state": "error", "collection_run_id": "", "collection_dir": "", "collection_receipt_sha256": ""}
            completed = {"state": "safe_wait", "collection_run_id": "b" * 40, "collection_dir": "/app/data/collected/x/y", "collection_receipt_sha256": "c" * 64}
            with mock.patch.object(agent, "validate_config", return_value=row), mock.patch.object(agent, "load_common", return_value=common), \
                    mock.patch.object(agent, "validate_mounts_and_config", return_value="2" * 64), \
                    mock.patch.object(agent, "failed_submission", return_value=frozen), mock.patch.object(agent, "contest", return_value=ready), \
                    mock.patch.object(agent, "credentials", return_value={"username": "u", "password": "p"}), \
                    mock.patch.object(agent, "collect_once", side_effect=lambda *_: order.append("collect")), \
                    mock.patch.object(agent, "wait_contest", side_effect=[failed, completed]), \
                    mock.patch.object(agent, "submission_by_id", return_value=dict(frozen, judge_state="permanent_failed", attempts=2)), \
                    mock.patch.object(agent, "wait_final_delivered", return_value=dict(frozen, judge_state="submitted", judge_kind="final", rid="a" * 24, attempts=3)), \
                    mock.patch.object(agent, "verify_receipt", return_value="c" * 64), \
                    mock.patch.object(agent, "remove_marker", side_effect=lambda p: order.append("remove") or Path(p).unlink(missing_ok=True)), \
                    mock.patch.object(agent.os, "close"):
                result = agent.run(row)
            self.assertEqual(order[:3], ["collect", "remove", "collect"])
            action = json.loads(Path(row["output_path"]).read_text())
            self.assertEqual(action["payload"]["injected_failures"], 1)
            self.assertEqual(action["payload"]["retry_attempts"], 1)
            self.assertTrue(action["payload"]["collection_receipt_unique"])
            self.assertEqual(result["status"], "passed")

    def test_failure_always_removes_the_injection_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            row = config(Path(raw).as_posix()); frozen = failed_row(); common = DummyCommon()
            marker = Path(row["failure_marker_host_path"])
            ready = {"state": "ready", "collection_run_id": "", "collection_dir": "", "collection_receipt_sha256": ""}
            with mock.patch.object(agent, "validate_config", return_value=row), \
                    mock.patch.object(agent, "load_common", return_value=common), \
                    mock.patch.object(agent, "validate_mounts_and_config", return_value="2" * 64), \
                    mock.patch.object(agent, "failed_submission", return_value=frozen), \
                    mock.patch.object(agent, "contest", return_value=ready), \
                    mock.patch.object(agent, "credentials", return_value={"username": "u", "password": "p"}), \
                    mock.patch.object(agent, "collect_once", side_effect=agent.AgentError("expected stop")), \
                    mock.patch.object(agent.os, "close"):
                with self.assertRaisesRegex(agent.AgentError, "expected stop"):
                    agent.run(row)
            self.assertFalse(marker.exists())

    def test_final_delivery_allows_only_final_state_fields_to_change(self):
        row = config(); frozen = failed_row(); delivered = dict(frozen, judge_state="submitted", judge_kind="final", rid="a" * 24, attempts=3)
        with mock.patch.object(agent, "submission_by_id", return_value=delivered):
            self.assertEqual(agent.wait_final_delivered(row, frozen, 1), delivered)
        changed = dict(delivered, judge_sha256="0" * 64)
        with mock.patch.object(agent, "submission_by_id", return_value=changed):
            with self.assertRaisesRegex(agent.AgentError, "source identity changed"):
                agent.wait_final_delivered(row, frozen, 1)

    def test_builder_freezes_exact_configuration(self):
        raw = builder.render(agent.validate_config(config()))
        self.assertNotIn(builder.MARKER, raw.decode()); self.assertIn("expected_failed_row_sha256", raw.decode())
        compile(raw.decode(), "<collection-retry-agent-test>", "exec")


if __name__ == "__main__": unittest.main()
