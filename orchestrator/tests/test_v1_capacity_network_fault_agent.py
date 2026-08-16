import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "v1_capacity_network_fault_agent.py"
BUILDER = ROOT / "scripts" / "build_v1_capacity_network_fault_agent.py"
spec = importlib.util.spec_from_file_location("v1_capacity_network_fault_agent", SCRIPT)
agent = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(agent)
builder_spec = importlib.util.spec_from_file_location("build_v1_capacity_network_fault_agent", BUILDER)
builder = importlib.util.module_from_spec(builder_spec); assert builder_spec.loader; builder_spec.loader.exec_module(builder)


def config(prefix="/root/network-fault"):
    target = agent.hashlib.sha256(agent.canonical({"ipv4": "198.51.100.10", "port": 443})).hexdigest()
    return {
        "schema_version": 1, "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
        "contest_id": "a" * 24, "seat_inventory_probe_sha256": "b" * 64,
        "docker_socket": "/var/run/docker.sock",
        "controller": {"container_id": "c" * 64, "image_id": "sha256:" + "d" * 64,
                       "pid": 1234, "started_at": "2026-08-13T00:00:00Z", "restart_count": 0},
        "target_ipv4": "198.51.100.10", "target_port": 443,
        "controller_probe_target_sha256": target,
        "nsenter_path": f"{prefix}/nsenter", "iptables_path": f"{prefix}/iptables",
        "python_path": f"{prefix}/python3", "signer": "network-agent",
        "signing_public_key": "ssh-ed25519 " + "A" * 68,
        "signing_key_path": f"{prefix}/id_ed25519", "ssh_keygen_path": f"{prefix}/ssh-keygen",
        "lock_path": f"{prefix}/agent.lock", "recovery_state_path": f"{prefix}/pending.json",
        "receipt_path": f"{prefix}/receipt.json", "output_path": f"{prefix}/envelope.json",
    }


class NetworkFaultAgentTests(unittest.TestCase):
    def test_receipt_binds_runtime_agent_sha(self):
        self.assertIn("agent_sha256", SCRIPT.read_text(encoding="utf-8"))

    def test_config_binds_exact_controller_and_target(self):
        row = agent.validate_config(config())
        self.assertEqual(row["controller"]["pid"], 1234)
        wrong = config(); wrong["target_port"] = 80
        with self.assertRaisesRegex(agent.AgentError, "target SHA256"):
            agent.validate_config(wrong)

    def test_rule_is_exactly_controller_namespace_target_reject(self):
        row = agent.validate_config(config())
        self.assertEqual(agent.rule_args(row), [
            "OUTPUT", "-p", "tcp", "-d", "198.51.100.10", "--dport", "443",
            "-m", "comment", "--comment", row["qualification_marker"],
            "-j", "REJECT", "--reject-with", "tcp-reset",
        ])

    def test_stale_state_only_recovers_and_refuses_new_run(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); row = config(root.as_posix())
            pending = Path(row["recovery_state_path"]); pending.write_text("{}")
            with mock.patch.object(agent, "validate_config", return_value=row), \
                    mock.patch.object(agent, "file_sha256", return_value="a" * 64), \
                    mock.patch.object(agent, "private_regular"), \
                    mock.patch.object(agent, "acquire_lock", return_value=9), \
                    mock.patch.object(agent, "remove_rule") as remove, \
                    mock.patch.object(agent, "unlink_durable", side_effect=lambda p: p.unlink()), \
                    mock.patch.object(agent.os, "close"):
                with self.assertRaisesRegex(agent.AgentError, "stale network fault state was recovered"):
                    agent.run(row)
            remove.assert_called_once_with(row)
            self.assertFalse(pending.exists())

    def test_success_orders_probe_install_failure_remove_recovery_and_sign(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); row = config(root.as_posix())
            try: root.chmod(0o700)
            except OSError: pass
            order = []
            for key in ("receipt_path", "output_path", "recovery_state_path"):
                Path(row[key]).parent.mkdir(parents=True, exist_ok=True)
            controller = dict(row["controller"])
            times = iter([
                "2026-08-13T00:00:00Z", "2026-08-13T00:00:03Z",
                "2026-08-13T00:00:04Z", "2026-08-13T00:00:07Z",
                "2026-08-13T00:00:08Z", "2026-08-13T00:00:11Z",
                "2026-08-13T00:00:12Z",
            ])
            def probe(_row, success): order.append(f"probe:{success}")
            def install(_row): order.append("install")
            def remove(_row): order.append("remove")
            def sign(_row, _payload): order.append("sign"); return "c2ln"
            def verify(_row, _payload, _signature): order.append("verify")
            def atomic(path, payload): path.write_bytes(payload)
            def unlink(path):
                try: path.unlink()
                except FileNotFoundError: pass
            with mock.patch.object(agent, "validate_config", return_value=row), \
                    mock.patch.object(agent, "file_sha256", return_value="a" * 64), \
                    mock.patch.object(agent, "private_regular"), \
                    mock.patch.object(agent, "acquire_lock", return_value=9), \
                    mock.patch.object(agent, "controller_inspect", return_value=controller), \
                    mock.patch.object(agent, "probe_target", side_effect=probe), \
                    mock.patch.object(agent, "install_rule", side_effect=install), \
                    mock.patch.object(agent, "rule_present", return_value=True), \
                    mock.patch.object(agent, "remove_rule", side_effect=remove), \
                    mock.patch.object(agent, "safe_ancestors"), \
                    mock.patch.object(agent, "atomic_write", side_effect=atomic), \
                    mock.patch.object(agent, "unlink_durable", side_effect=unlink), \
                    mock.patch.object(agent, "utc_now", side_effect=lambda: next(times)), \
                    mock.patch.object(agent, "sign", side_effect=sign), \
                    mock.patch.object(agent, "verify_signature", side_effect=verify), \
                    mock.patch.object(agent.os, "close"):
                result = agent.run(row)
            self.assertEqual(order, [
                "sign", "verify", "probe:True", "install", "probe:False", "remove",
                "probe:True", "sign", "verify",
            ])
            self.assertEqual(result["status"], "passed")
            receipt = json.loads(Path(row["receipt_path"]).read_text())
            envelope = json.loads(Path(row["output_path"]).read_text())
            self.assertEqual(receipt["during_failures"], 3)
            self.assertEqual(receipt["agent_sha256"], "a" * 64)
            self.assertEqual(envelope["payload"]["events"][1]["consecutive_probe_failures"], 3)
            self.assertFalse(Path(row["recovery_state_path"]).exists())

    def test_builder_freezes_exact_config(self):
        raw = builder.render(agent.validate_config(config()))
        text = raw.decode(); self.assertNotIn(builder.MARKER, text)
        self.assertIn("controller_probe_target_sha256", text)
        compile(text, "<network-fault-agent-test>", "exec")


if __name__ == "__main__": unittest.main()
