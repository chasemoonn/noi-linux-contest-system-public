"""SSH operations on the contest server."""
from __future__ import annotations

import base64
import hashlib
import hmac
import posixpath
import socket
import time

import paramiko


def host_key_sha256(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class PinnedFingerprintPolicy(paramiko.MissingHostKeyPolicy):
    """Accept a new IP only when its host key matches the pinned fingerprint."""

    def __init__(self, expected_sha256: str):
        self.expected_sha256 = expected_sha256.strip()

    def missing_host_key(self, client, hostname, key):
        actual = host_key_sha256(key)
        if not hmac.compare_digest(actual, self.expected_sha256):
            raise paramiko.SSHException(
                f"SSH 主机指纹不匹配: {hostname} expected={self.expected_sha256} actual={actual}"
            )


class Remote:
    def __init__(
        self,
        host: str,
        user: str,
        key_path: str,
        known_hosts: str | None = None,
        strict_host_key: bool = True,
        host_key_sha256: str | None = None,
    ):
        self.host = host
        self.user = user
        self.key_path = key_path
        self.known_hosts = known_hosts
        self.strict_host_key = strict_host_key
        self.host_key_sha256 = (host_key_sha256 or "").strip()

    def _client(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        if self.host_key_sha256:
            # StopCharging may assign a different public IP. In fingerprint mode
            # the instance key is the trust anchor, not an IP-keyed known_hosts row.
            client.set_missing_host_key_policy(
                PinnedFingerprintPolicy(self.host_key_sha256)
            )
        else:
            client.load_system_host_keys()
            if self.known_hosts:
                client.load_host_keys(self.known_hosts)
            if self.strict_host_key:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.host,
            username=self.user,
            key_filename=self.key_path,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        return client

    def run(self, cmd: str, timeout: int = 300) -> str:
        client = self._client()
        try:
            _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            if code != 0:
                raise RuntimeError(f"远程命令失败({code}): {cmd}\n{err}")
            return out
        finally:
            client.close()

    def put_content(self, content: str, remote_path: str) -> None:
        client = self._client()
        sftp = None
        try:
            sftp = client.open_sftp()
            current = ""
            for part in posixpath.dirname(remote_path).split("/"):
                if not part:
                    continue
                current += "/" + part
                try:
                    sftp.mkdir(current)
                except OSError:
                    pass
            with sftp.open(remote_path, "wb") as handle:
                handle.write(content.encode("utf-8"))
        finally:
            if sftp:
                sftp.close()
            client.close()

    def put_file(self, local_path: str, remote_path: str) -> None:
        client = self._client()
        sftp = None
        try:
            sftp = client.open_sftp()
            current = ""
            for part in posixpath.dirname(remote_path).split("/"):
                if not part:
                    continue
                current += "/" + part
                try:
                    sftp.mkdir(current)
                except OSError:
                    pass
            sftp.put(local_path, remote_path)
        finally:
            if sftp:
                sftp.close()
            client.close()

    def get_file(self, remote_path: str, local_path: str) -> None:
        client = self._client()
        sftp = None
        try:
            sftp = client.open_sftp()
            sftp.get(remote_path, local_path)
        finally:
            if sftp:
                sftp.close()
            client.close()

    def wait_ssh(self, timeout: int = 180) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((self.host, 22), timeout=5):
                    return True
            except OSError:
                time.sleep(5)
        return False
