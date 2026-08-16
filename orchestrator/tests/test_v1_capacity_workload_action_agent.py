import base64
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "v1_capacity_workload_action_agent.py"
BUILDER = ROOT / "scripts" / "build_v1_capacity_workload_action_agent.py"
spec = importlib.util.spec_from_file_location("workload_action", SCRIPT)
agent = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(agent)
builder_spec = importlib.util.spec_from_file_location("workload_action_builder", BUILDER)
builder = importlib.util.module_from_spec(builder_spec); assert builder_spec.loader; builder_spec.loader.exec_module(builder)


def config():
    seats = []
    for slot in range(1, 16):
        seats.append({"slot_no": slot, "candidate": f"9999{slot:08d}",
                      "container_id": f"{slot:064x}", "container_name": f"seat-12345678-slot-{slot:03d}",
                      "image_id": "sha256:" + "a" * 64, "pid": 1000 + slot,
                      "started_at": "2026-08-13T00:00:00Z", "restart_count": 0})
    return {"schema_version": 1, "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
            "contest_id": "b" * 24, "seat_set_sha256": "c" * 64,
            "problem_slugs": ["alpha", "beta", "gamma"], "seats": seats,
            "docker_path": "/usr/bin/docker", "docker_socket": "/var/run/docker.sock",
            "browser_envelope": "/root/private/browser.json",
            "browser_signer": "browser-agent", "browser_public_key": "ssh-ed25519 " + "A" * 68,
            "browser_max_age_seconds": 60, "signer": "workload-agent",
            "signing_public_key": "ssh-ed25519 " + "B" * 68,
            "signing_key_path": "/root/private/workload-key", "ssh_keygen_path": "/usr/bin/ssh-keygen",
            "lock_path": "/root/private/workload.lock", "receipt_path": "/root/private/workload-receipt.json",
            "output_path": "/root/private/workload-envelope.json"}


class WorkloadActionAgentTests(unittest.TestCase):
    def test_receipt_binds_runtime_agent_sha(self):
        self.assertIn("agent_sha256", SCRIPT.read_text(encoding="utf-8"))

    def test_config_binds_exact_fifteen_seat_lifecycles(self):
        row = agent.validate_config(config())
        self.assertEqual([seat["slot_no"] for seat in row["seats"]], list(range(1, 16)))
        duplicate = config(); duplicate["seats"][-1]["container_id"] = duplicate["seats"][0]["container_id"]
        with self.assertRaisesRegex(agent.ActionAgentError, "unique"):
            agent.validate_config(duplicate)

    def test_execute_actions_uses_frozen_containers_material_and_forty_five_compiles(self):
        row = agent.validate_config(config()); calls = []
        def fake(command, label, **kwargs):
            calls.append((command, label))
            if label == "workload source compile": agent.time.sleep(0.01)
            return mock.Mock(returncode=0, stdout=b"")
        with mock.patch.object(agent, "regular", side_effect=lambda path, label, **kwargs: path), \
                mock.patch.object(agent, "inspect_seat") as inspect, \
                mock.patch.object(agent, "run_command", side_effect=fake):
            materials, pairs, peak = agent.execute_actions(row)
        self.assertEqual(materials, list(range(1, 16))); self.assertEqual(len(pairs), 45); self.assertEqual(peak, 15)
        self.assertEqual(inspect.call_count, 30)
        self.assertEqual(sum(label == "workload material open" for _, label in calls), 15)
        self.assertEqual(sum(label == "workload source compile" for _, label in calls), 45)
        compile_calls = [command for command, label in calls if label == "workload source compile"]
        self.assertTrue(all("/usr/bin/g++" in command and "-std=c++14" in command for command in compile_calls))
        self.assertTrue(any("/usr/bin/evince" in command for command, label in calls if label == "workload material open"))

    def test_browser_envelope_must_be_current_signed_and_exact_seat_set(self):
        row = agent.validate_config(config()); payload = {
            "schema_version": 1, "transport_profile": "direct_http",
            "qualification_marker": row["qualification_marker"], "seat_set_sha256": row["seat_set_sha256"],
            "formal_seat_count": 15, "sequence": 1, "window_started_at": "2026-08-13T00:00:00Z",
            "observed_at": "2026-08-13T00:00:01Z", "rtt_samples_ms": [1] * 5,
            "packet_loss_percent": 0, "websocket_reconnects": 0, "key_to_frame_samples_ms": [1] * 5}
        envelope = {"schema_version": 1, "namespace": agent.BROWSER_NAMESPACE,
                    "signer": row["browser_signer"], "payload": payload,
                    "signature_base64": base64.b64encode(b"s" * 64).decode()}
        with mock.patch.object(agent, "read_private", return_value=agent.canonical(envelope)), \
                mock.patch.object(agent, "verify_signature") as verify:
            digest = agent.verify_browser(row, agent.datetime(2026, 8, 13, 0, 0, 50, tzinfo=agent.timezone.utc))
        self.assertRegex(digest, r"^[a-f0-9]{64}$"); verify.assert_called_once()
        envelope["payload"]["seat_set_sha256"] = "f" * 64
        with mock.patch.object(agent, "read_private", return_value=agent.canonical(envelope)):
            with self.assertRaisesRegex(agent.ActionAgentError, "15-seat login"):
                agent.verify_browser(row, agent.datetime(2026, 8, 13, 0, 0, 50, tzinfo=agent.timezone.utc))

    def test_builder_embeds_and_compiles_exact_configuration(self):
        raw = builder.render(agent.validate_config(config())); text = raw.decode()
        self.assertNotIn(builder.MARKER, text); self.assertIn("seat-12345678-slot-015", text)
        compile(text, "<workload-action-test>", "exec")


if __name__ == "__main__": unittest.main()
