import base64
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install_v1_capacity_telemetry.py"
SPEC = importlib.util.spec_from_file_location("install_v1_capacity_telemetry", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)


def envelope(sequence=1, transport="direct_http"):
    payload = {
        "schema_version": 1,
        "transport_profile": transport,
        "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
        "seat_set_sha256": "a" * 64,
        "formal_seat_count": 15,
        "sequence": sequence,
        "window_started_at": "2026-08-13T00:00:00Z",
        "observed_at": "2026-08-13T00:00:05Z",
        "rtt_samples_ms": [10, 11, 12, 13, 14],
        "packet_loss_percent": 0,
        "websocket_reconnects": 0,
        "key_to_frame_samples_ms": [20, 21, 22, 23, 24],
    }
    value = {
        "schema_version": 1,
        "namespace": installer.NAMESPACE,
        "signer": "hangzhou-browser-agent",
        "payload": payload,
        "signature_base64": base64.b64encode(b"s" * 64).decode(),
    }
    return installer.canonical_json(value)


class CapacityTelemetryInstallerTests(unittest.TestCase):
    def test_verify_accepts_only_signed_direct_profile(self):
        with mock.patch.object(
            installer.subprocess, "run", return_value=mock.Mock(returncode=0)
        ):
            payload, normalized = installer.verify(
                envelope(), "hangzhou-browser-agent",
                "ssh-ed25519 " + "A" * 68, Path("/usr/bin/ssh-keygen")
            )
        self.assertEqual(payload["sequence"], 1)
        self.assertEqual(normalized, envelope())

        with self.assertRaisesRegex(installer.InstallError, "payload identity"):
            installer.verify(
                envelope(transport="compat_https"), "hangzhou-browser-agent",
                "ssh-ed25519 " + "A" * 68, Path("/usr/bin/ssh-keygen")
            )

    def test_verify_rejects_noncanonical_and_bad_signature(self):
        row = json.loads(envelope())
        noncanonical = json.dumps(row, indent=2).encode()
        with self.assertRaisesRegex(installer.InstallError, "identity differs"):
            installer.verify(
                noncanonical, "hangzhou-browser-agent",
                "ssh-ed25519 " + "A" * 68, Path("/usr/bin/ssh-keygen")
            )
        with mock.patch.object(
            installer.subprocess, "run", return_value=mock.Mock(returncode=1)
        ):
            with self.assertRaisesRegex(installer.InstallError, "signature is invalid"):
                installer.verify(
                    envelope(), "hangzhou-browser-agent",
                    "ssh-ed25519 " + "A" * 68, Path("/usr/bin/ssh-keygen")
                )


if __name__ == "__main__":
    unittest.main()
