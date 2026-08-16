import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install_v1_capacity_ordinary_oj_telemetry.py"
spec = importlib.util.spec_from_file_location("ordinary_installer", SCRIPT)
installer = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(installer)


def envelope(sequence=1):
    payload = {
        "schema_version": 1, "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
        "sequence": sequence, "observed_at": "2026-08-13T00:00:00Z",
        "homepage_status": 200, "login_status": 200, "prep_health_ok": True,
        "prep_database_ok": True, "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0,
        "ordinary_oj_pid_changes": 0, "credential_leaks": 0, "result_leaks": 0,
        "pm2_fingerprint_sha256": "a" * 64,
    }
    return installer.canonical({"schema_version": 1, "namespace": installer.NAMESPACE,
        "signer": "ordinary-agent", "payload": payload,
        "signature_base64": base64.b64encode(b"s" * 64).decode()})


class OrdinaryInstallerTests(unittest.TestCase):
    def test_verify_accepts_exact_zero_failure_payload(self):
        with mock.patch.object(installer.subprocess, "run", return_value=mock.Mock(returncode=0)):
            payload, normalized = installer.verify(
                envelope(), "ordinary-agent", "ssh-ed25519 " + "A" * 68, Path("/ssh-keygen")
            )
        self.assertEqual(payload["sequence"], 1); self.assertEqual(normalized, envelope())

    def test_verify_rejects_nonzero_failure_and_wrong_namespace(self):
        value = json.loads(envelope()); value["payload"]["credential_leaks"] = 1
        with self.assertRaisesRegex(installer.InstallError, "non-zero"):
            installer.verify(installer.canonical(value), "ordinary-agent", "ssh-ed25519 " + "A" * 68,
                             Path("/ssh-keygen"))
        value = json.loads(envelope()); value["namespace"] = "noi-v1-capacity-telemetry"
        with self.assertRaisesRegex(installer.InstallError, "identity differs"):
            installer.verify(installer.canonical(value), "ordinary-agent", "ssh-ed25519 " + "A" * 68,
                             Path("/ssh-keygen"))

    def test_existing_sequence_is_monotonic(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "envelope.json"; path.write_bytes(envelope(7)); path.chmod(0o600)
            self.assertEqual(installer.prior_sequence(path), 7)


if __name__ == "__main__": unittest.main()
