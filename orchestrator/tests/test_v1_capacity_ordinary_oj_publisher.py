import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "publish_v1_capacity_ordinary_oj_telemetry.py"
spec = importlib.util.spec_from_file_location("ordinary_publisher", SCRIPT)
publisher = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(publisher)


class OrdinaryPublisherTests(unittest.TestCase):
    def test_publish_uses_only_strict_pinned_ssh_and_exact_acknowledgement(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            files = []
            for name in ("envelope", "ssh", "identity", "known_hosts"):
                path = root / name; path.write_text("x"); path.chmod(0o700 if name == "ssh" else 0o600)
                files.append(path)
            result = mock.Mock(returncode=0, stderr="", stdout="TELEMETRY_INSTALLED sequence=12\n")
            with mock.patch.object(publisher.subprocess, "run", return_value=result) as run:
                self.assertEqual(publisher.publish(*files, "telemetry@example.test"), 12)
            command = run.call_args.args[0]
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("ClearAllForwardings=yes", command)
            self.assertNotIn("PasswordAuthentication=yes", command)

    def test_publish_rejects_unexpected_acknowledgement(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); files = []
            for name in ("envelope", "ssh", "identity", "known_hosts"):
                path = root / name; path.write_text("x"); path.chmod(0o700 if name == "ssh" else 0o600)
                files.append(path)
            result = mock.Mock(returncode=0, stderr="", stdout="welcome\nTELEMETRY_INSTALLED sequence=12\n")
            with mock.patch.object(publisher.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(publisher.PublishError, "publication failed"):
                    publisher.publish(*files, "telemetry@example.test")


if __name__ == "__main__": unittest.main()
