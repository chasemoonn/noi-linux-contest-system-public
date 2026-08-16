import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "v1_control_restart_action_agent.py"
BUILDER = ROOT / "scripts" / "build_v1_control_restart_action_agent.py"
spec = importlib.util.spec_from_file_location("v1_control_restart_action_agent", SCRIPT)
agent = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(agent)
builder_spec = importlib.util.spec_from_file_location("build_v1_control_restart_action_agent", BUILDER)
builder = importlib.util.module_from_spec(builder_spec); assert builder_spec.loader; builder_spec.loader.exec_module(builder)


def config(prefix="/root/control-restart"):
    return {
        "schema_version": 1, "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
        "session_id": "1" * 64, "contest_id": "a" * 24,
        "source": {"revision": "b" * 40, "tree": "c" * 40},
        "components": {"orchestrator_image_digest": "sha256:" + "d" * 64,
            "desktop_image_id": "sha256:" + "e" * 64, "desktop_source_revision": "b" * 40,
            "hydro_plugin_sha256": "f" * 64},
        "docker_socket": "/var/run/docker.sock",
        "controller": {"container_id": "1" * 64, "image_id": "sha256:" + "d" * 64,
            "name": "/noi-orchestrator", "identity_sha256": "2" * 64, "restart_count": 0},
        "database_path": f"{prefix}/orchestrator.db", "expected_pending_count": 1,
        "expected_pending_set_sha256": "3" * 64,
        "controller_health": {"url": "http://127.0.0.1:8600/healthz", "timeout_seconds": 3, "deadline_seconds": 30},
        "submission_status": {"url": "http://127.0.0.1:8888/orchestrator/submit/status",
            "token_path": f"{prefix}/token", "timeout_seconds": 3},
        "ordinary_oj": {"pm2_path": f"{prefix}/pm2", "pm2_home": "/root/.pm2",
            "processes": [{"name": name, "pid": index + 100, "restart_time": 0, "status": "online"}
                          for index, name in enumerate(("caddy", "hydro-sandbox", "hydrooj", "mongodb"))],
            "http_probes": [{"url": f"http://127.0.0.1:80{path}", "host": "oj.example",
                "status": 200, "body_contains": body} for path, body in (("/", "Hydro"), ("/login", "login"), ("/prep/health", "ready"))]},
        "signer": "control-restart-agent", "signing_public_key": "ssh-ed25519 " + "A" * 68,
        "signing_key_path": f"{prefix}/id_ed25519", "ssh_keygen_path": f"{prefix}/ssh-keygen",
        "lock_path": f"{prefix}/agent.lock", "recovery_state_path": f"{prefix}/pending.json",
        "receipt_path": f"{prefix}/receipt.json", "output_path": f"{prefix}/action.json",
    }


def pending():
    return [{"id": 7, "tid": "a" * 24, "uid": 9, "problem": "sum", "sha256": "4" * 64,
        "size": 42, "submission_id": "5" * 64, "submission_session": "6" * 32,
        "judge_pid": "sum", "judge_lang": "cc", "judge_sha256": "7" * 64,
        "judge_state": "pending", "judge_kind": "realtime", "accepted_at_ms": 1, "rid": ""}]


class ControlRestartActionAgentTests(unittest.TestCase):
    def test_config_binds_local_endpoints_exact_controller_and_pending_set(self):
        row = agent.validate_config(config())
        self.assertEqual(row["controller"]["name"], "/noi-orchestrator")
        wrong = config(); wrong["controller_health"]["url"] = "http://192.0.2.1:8600/healthz"
        with self.assertRaisesRegex(agent.AgentError, "local endpoints"):
            agent.validate_config(wrong)
        wrong = config(); wrong["controller"]["image_id"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(agent.AgentError, "controller identity"):
            agent.validate_config(wrong)

    def test_pending_set_is_frozen_only_after_controller_stop(self):
        row = config(); rows = pending(); row["expected_pending_set_sha256"] = agent.hashlib.sha256(agent.canonical(rows)).hexdigest()
        with mock.patch.object(agent, "read_rows", return_value=rows):
            self.assertEqual(agent.freeze_pending(agent.validate_config(row)), rows)
        rows[0]["sha256"] = "8" * 64
        with mock.patch.object(agent, "read_rows", return_value=rows):
            with self.assertRaisesRegex(agent.AgentError, "pending delivery set differs"):
                agent.freeze_pending(agent.validate_config(row))

    def test_wait_delivered_rejects_source_identity_change(self):
        row = config(); before = pending(); changed = [dict(before[0], judge_state="submitted", rid="8" * 24, sha256="9" * 64)]
        with mock.patch.object(agent, "read_rows", return_value=changed):
            with self.assertRaisesRegex(agent.AgentError, "payload changed"):
                agent.wait_delivered(row, before)

    def test_success_stops_freezes_starts_and_emits_verifier_compatible_action(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); row = config(root.as_posix()); before = pending()
            row["expected_pending_set_sha256"] = agent.hashlib.sha256(agent.canonical(before)).hexdigest()
            after = [dict(before[0], judge_state="submitted", rid="8" * 24)]
            order = []
            def atomic(path, payload): Path(path).write_bytes(payload)
            def unlink(path):
                try: Path(path).unlink()
                except FileNotFoundError: pass
            running = {"State": {"Running": True}}
            with mock.patch.object(agent, "validate_config", return_value=row), \
                    mock.patch.object(agent, "file_sha256", return_value="9" * 64), \
                    mock.patch.object(agent, "regular"), mock.patch.object(agent, "acquire_lock", return_value=9), \
                    mock.patch.object(agent, "ordinary_snapshot", side_effect=[[{"name": "same"}], [{"name": "same"}]]), \
                    mock.patch.object(agent, "inspect_controller", return_value=(running, "2" * 64)), \
                    mock.patch.object(agent, "stop_controller", side_effect=lambda _r: order.append("stop")), \
                    mock.patch.object(agent, "freeze_pending", side_effect=lambda _r: order.append("freeze") or before), \
                    mock.patch.object(agent, "start_controller", side_effect=lambda _r: order.append("start")), \
                    mock.patch.object(agent, "health_ready", side_effect=lambda _r: order.append("health")), \
                    mock.patch.object(agent, "wait_delivered", return_value=after), \
                    mock.patch.object(agent, "verify_unique_records", side_effect=lambda *_: order.append("unique")), \
                    mock.patch.object(agent, "atomic_write", side_effect=atomic), \
                    mock.patch.object(agent, "unlink_durable", side_effect=unlink), \
                    mock.patch.object(agent, "sign", return_value="c2ln"), \
                    mock.patch.object(agent, "verify_signature"), mock.patch.object(agent.os, "close"):
                result = agent.run(row)
            self.assertEqual(order[:4], ["stop", "freeze", "start", "health"])
            self.assertIn("unique", order)
            action = json.loads(Path(row["output_path"]).read_text())
            self.assertEqual(action["scenario"], "control_restart")
            self.assertEqual(action["payload"]["pending_jobs_resumed"], 1)
            self.assertTrue(action["payload"]["controller_identity_preserved"])
            self.assertEqual(result["status"], "passed")

    def test_failure_after_stop_recovers_same_controller(self):
        with tempfile.TemporaryDirectory() as raw:
            row = config(Path(raw).as_posix()); order = []; states = iter([
                ({"State": {"Running": True}}, "2" * 64),
                ({"State": {"Running": False}}, "2" * 64),
            ])
            with mock.patch.object(agent, "validate_config", return_value=row), \
                    mock.patch.object(agent, "file_sha256", return_value="9" * 64), mock.patch.object(agent, "regular"), \
                    mock.patch.object(agent, "acquire_lock", return_value=9), mock.patch.object(agent, "ordinary_snapshot", return_value=[]), \
                    mock.patch.object(agent, "inspect_controller", side_effect=lambda *_a, **_k: next(states)), \
                    mock.patch.object(agent, "stop_controller", side_effect=lambda _r: order.append("stop")), \
                    mock.patch.object(agent, "freeze_pending", side_effect=agent.AgentError("bad set")), \
                    mock.patch.object(agent, "start_controller", side_effect=lambda _r: order.append("recover")), \
                    mock.patch.object(agent, "health_ready"), mock.patch.object(agent, "atomic_write", side_effect=lambda p, b: Path(p).write_bytes(b)), \
                    mock.patch.object(agent, "sign", return_value="c2ln"), mock.patch.object(agent, "verify_signature"), \
                    mock.patch.object(agent.os, "close"):
                with self.assertRaisesRegex(agent.AgentError, "bad set"): agent.run(row)
            self.assertEqual(order, ["stop", "recover"])

    def test_signing_trust_root_is_checked_before_any_lifecycle_change(self):
        with tempfile.TemporaryDirectory() as raw:
            row = config(Path(raw).as_posix())
            with mock.patch.object(agent, "validate_config", return_value=row), \
                    mock.patch.object(agent, "file_sha256", return_value="9" * 64), mock.patch.object(agent, "regular"), \
                    mock.patch.object(agent, "acquire_lock", return_value=9), mock.patch.object(agent, "sign", return_value="c2ln"), \
                    mock.patch.object(agent, "verify_signature", side_effect=agent.AgentError("trust root differs")), \
                    mock.patch.object(agent, "stop_controller") as stop, mock.patch.object(agent, "inspect_controller", side_effect=agent.AgentError("absent")), \
                    mock.patch.object(agent.os, "close"):
                with self.assertRaisesRegex(agent.AgentError, "trust root differs"): agent.run(row)
            stop.assert_not_called()

    def test_builder_freezes_exact_configuration(self):
        raw = builder.render(agent.validate_config(config()))
        self.assertNotIn(builder.MARKER, raw.decode())
        self.assertIn("expected_pending_set_sha256", raw.decode())
        compile(raw.decode(), "<control-restart-agent-test>", "exec")


if __name__ == "__main__": unittest.main()
