import unittest
from unittest.mock import MagicMock, patch

import paramiko

from services.remote import (
    PinnedFingerprintPolicy,
    Remote,
    host_key_sha256,
)


class RemoteHostKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = paramiko.RSAKey.generate(1024)

    def test_matching_pinned_fingerprint_is_accepted(self):
        policy = PinnedFingerprintPolicy(host_key_sha256(self.key))
        policy.missing_host_key(None, "203.0.113.10", self.key)

    def test_mismatched_pinned_fingerprint_is_rejected(self):
        policy = PinnedFingerprintPolicy("SHA256:" + "A" * 43)
        with self.assertRaises(paramiko.SSHException):
            policy.missing_host_key(None, "203.0.113.10", self.key)

    @patch("services.remote.paramiko.SSHClient")
    def test_fingerprint_mode_ignores_ip_keyed_known_hosts(self, ssh_client):
        client = MagicMock()
        ssh_client.return_value = client
        remote = Remote(
            "203.0.113.10",
            "root",
            "/keys/contest",
            "/keys/stale-known-hosts",
            True,
            host_key_sha256(self.key),
        )

        self.assertIs(remote._client(), client)
        client.load_system_host_keys.assert_not_called()
        client.load_host_keys.assert_not_called()
        policy = client.set_missing_host_key_policy.call_args.args[0]
        self.assertIsInstance(policy, PinnedFingerprintPolicy)

    @patch("services.remote.paramiko.SSHClient")
    def test_put_file_creates_remote_directories_and_uploads(self, ssh_client):
        client = MagicMock()
        sftp = MagicMock()
        client.open_sftp.return_value = sftp
        ssh_client.return_value = client
        remote = Remote(
            "203.0.113.10",
            "root",
            "/keys/contest",
            host_key_sha256=host_key_sha256(self.key),
        )

        remote.put_file("C:/tmp/paper.pdf", "/data/contest/materials/paper.pdf")

        self.assertEqual(sftp.mkdir.call_count, 3)
        sftp.put.assert_called_once_with(
            "C:/tmp/paper.pdf", "/data/contest/materials/paper.pdf"
        )
        sftp.close.assert_called_once()
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
