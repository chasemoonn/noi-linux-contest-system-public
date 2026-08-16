import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "v1_capacity_ordinary_oj_agent.py"
BUILDER = ROOT / "scripts" / "build_v1_capacity_ordinary_oj_agent.py"

spec = importlib.util.spec_from_file_location("v1_capacity_ordinary_oj_agent", SCRIPT)
agent = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(agent)
build_spec = importlib.util.spec_from_file_location("build_v1_capacity_ordinary_oj_agent", BUILDER)
builder = importlib.util.module_from_spec(build_spec); assert build_spec.loader; build_spec.loader.exec_module(builder)


def config():
    return {
        "schema_version": 1, "oj_origin": "https://oj.example", "public_paths": ["/", "/login"],
        "prep_health_path": "/prep/health", "pm2_bin": "/root/bin/pm2",
        "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
        "pm2_baseline": [
            {"name": name, "pid": index + 10, "restart_time": 0, "status": "online"}
            for index, name in enumerate(agent.PROCESS_NAMES)
        ],
        "credential_canary": "NOI-V1-CREDENTIAL-" + "A" * 32,
        "result_canary": "NOI-V1-RESULT-" + "B" * 32,
        "signer": "ordinary-oj-agent", "signing_key_path": "/root/agent/signing-key",
        "ssh_keygen_path": "/usr/bin/ssh-keygen", "lock_path": "/root/agent/observer.lock",
        "state_path": "/root/agent/state.json",
        "output_path": "/root/agent/envelope.json",
    }


class OrdinaryOjAgentTests(unittest.TestCase):
    def test_config_binds_four_exact_pm2_processes_and_two_canaries(self):
        row = agent.validate_config(config())
        self.assertEqual([item["name"] for item in row["pm2_baseline"]], sorted(agent.PROCESS_NAMES))
        wrong = config(); wrong["pm2_baseline"][0]["name"] = "hydrooj"
        with self.assertRaisesRegex(agent.AgentError, "process set differs"):
            agent.validate_config(wrong)
        wrong_canary = config(); wrong_canary["result_canary"] = wrong_canary["credential_canary"]
        with self.assertRaisesRegex(agent.AgentError, "result_canary"):
            agent.validate_config(wrong_canary)

    def test_collect_fails_on_restart_or_public_canary(self):
        value = agent.validate_config(config())
        health = {"ok": True, "database": "ok", "initialization": "ready"}
        responses = [(200, b"home", None), (200, b"login", None), (200, b"{}", health)]
        with mock.patch.object(agent, "request", side_effect=responses), \
                mock.patch.object(agent, "pm2_rows", return_value=value["pm2_baseline"]), \
                mock.patch.object(agent, "next_sequence", return_value=8):
            row = agent.collect(value)
        self.assertEqual(row["sequence"], 8)
        self.assertEqual(row["ordinary_oj_restarts"], 0)

        changed = [dict(item) for item in value["pm2_baseline"]]; changed[0]["restart_time"] = 1
        with mock.patch.object(agent, "request", side_effect=responses), \
                mock.patch.object(agent, "pm2_rows", return_value=changed):
            with self.assertRaisesRegex(agent.AgentError, "PM2 identity"):
                agent.collect(value)

        leaked = [(200, value["credential_canary"].encode(), None), (200, b"login", None),
                  (200, b"{}", health)]
        with mock.patch.object(agent, "request", side_effect=leaked), \
                mock.patch.object(agent, "pm2_rows", return_value=value["pm2_baseline"]):
            with self.assertRaisesRegex(agent.AgentError, "canary"):
                agent.collect(value)

    def test_envelope_payload_contains_no_canary_or_process_identity(self):
        value = agent.validate_config(config())
        health = {"ok": True, "database": "ok", "initialization": "ready"}
        responses = [(200, b"home", None), (200, b"login", None), (200, b"{}", health)]
        with mock.patch.object(agent, "request", side_effect=responses), \
                mock.patch.object(agent, "pm2_rows", return_value=value["pm2_baseline"]), \
                mock.patch.object(agent, "next_sequence", return_value=1):
            row = agent.collect(value)
        raw = agent.canonical(row)
        self.assertNotIn(value["credential_canary"].encode(), raw)
        self.assertNotIn(b"hydrooj", raw)
        self.assertRegex(row["pm2_fingerprint_sha256"], r"^[a-f0-9]{64}$")

    def test_builder_freezes_configuration_and_removes_marker(self):
        raw = builder.render(agent.validate_config(config()))
        text = raw.decode()
        self.assertNotIn(builder.MARKER, text)
        self.assertIn("ordinary-oj-agent", text)
        compile(text, "<ordinary-agent-test>", "exec")

    def test_lock_contention_fails_before_observation(self):
        value = agent.validate_config(config())
        with mock.patch.object(agent.platform, "system", return_value="Linux"), \
                mock.patch.object(agent.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(agent.os, "umask", side_effect=[0o022, 0o077]) as umask, \
                mock.patch.object(agent, "EMBEDDED_CONFIG", value), \
                mock.patch.object(agent, "acquire_run_lock", side_effect=agent.AgentError(
                    "ordinary OJ observer is already running"
                )), mock.patch.object(agent, "collect") as collect:
            self.assertEqual(agent.main(), 2)
        collect.assert_not_called()
        self.assertEqual(umask.call_args_list, [mock.call(0o077), mock.call(0o022)])


if __name__ == "__main__":
    unittest.main()
