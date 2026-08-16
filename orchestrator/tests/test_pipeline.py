import io
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from services.pipeline import (
    DESKTOP_IMAGE_CONTRACT,
    NGINX_CONF,
    NGINX_LOCATION,
    Pipeline,
    _remote_readonly,
    candidate_id,
    gateway_base_url,
    novnc_path,
    probe_novnc_gateway,
    rand_password,
    safe_extract,
)
from services.seat_pool import SeatPoolState
from services.store import Store


class RemoteReadonlyRetryTests(unittest.TestCase):
    @patch("services.pipeline.time.sleep", return_value=None)
    def test_retries_only_ambiguous_minus_one(self, _sleep):
        remote = MagicMock()
        remote.run.side_effect = [
            RuntimeError("远程命令失败(-1): docker inspect"),
            "sha256:ok\n",
        ]
        self.assertEqual(_remote_readonly(remote, "docker inspect"), "sha256:ok\n")
        self.assertEqual(remote.run.call_count, 2)

    @patch("services.pipeline.time.sleep", return_value=None)
    def test_does_not_retry_real_remote_failure(self, _sleep):
        remote = MagicMock()
        remote.run.side_effect = RuntimeError("远程命令失败(1): docker inspect")
        with self.assertRaisesRegex(RuntimeError, r"失败\(1\)"):
            _remote_readonly(remote, "docker inspect")
        self.assertEqual(remote.run.call_count, 1)

    @patch("services.pipeline.time.sleep", return_value=None)
    def test_timeout_is_bounded(self, _sleep):
        remote = MagicMock()
        remote.run.side_effect = TimeoutError("read timed out")
        with self.assertRaises(TimeoutError):
            _remote_readonly(remote, "docker inspect")
        self.assertEqual(remote.run.call_count, 3)


class FakeCVM:
    def __init__(self):
        self.state = "STOPPED"
        self.start_count = 0
        self.stop_count = 0

    def status(self):
        return self.state, "198.51.100.10" if self.state == "RUNNING" else ""

    def start(self):
        self.start_count += 1
        self.state = "STARTING"

    def wait_running(self):
        self.state = "RUNNING"
        return "198.51.100.10"

    def stop(self):
        self.stop_count += 1
        self.state = "STOPPED"


class DirectFakeCVM(FakeCVM):
    desktop_access_enabled = True

    def __init__(self):
        super().__init__()
        self.direct_open = False
        self.direct_events = []
        self.direct_tid = ""
        self.direct_end_at_ms = 0

    def ensure_desktop_access(self, *, tid, end_at_ms):
        self.direct_events.append(("open", tid, int(end_at_ms)))
        self.direct_open = True
        self.direct_tid = str(tid)
        self.direct_end_at_ms = int(end_at_ms)
        return {
            "enabled": True,
            "open": True,
            "closed": False,
            "healthy": True,
            "managed_count": 1,
            "conflict_count": 0,
            "management_healthy": True,
            "instance_state": self.state,
        }

    def revoke_desktop_access(self):
        self.direct_events.append(("close",))
        self.direct_open = False
        self.direct_tid = ""
        self.direct_end_at_ms = 0
        return {
            "enabled": True,
            "open": False,
            "closed": True,
            "healthy": True,
            "managed_count": 0,
            "conflict_count": 0,
            "management_healthy": True,
        }

    def desktop_access_status(self, *, tid="", end_at_ms=0):
        expected = (
            self.direct_open
            and (not tid or tid == self.direct_tid)
            and (not end_at_ms or int(end_at_ms) == self.direct_end_at_ms)
        )
        return {
            "enabled": True,
            "open": expected,
            "closed": not self.direct_open,
            "healthy": expected if tid else not self.direct_open,
            "managed_count": int(self.direct_open),
            "conflict_count": 0,
            "management_healthy": True,
        }


class FakeRemote:
    def __init__(self, files=("apple",)):
        self.commands = []
        self.timeouts = []
        self.files = files
        self.uploads = []
        self.contents = []
        self.paper_digest = ""
        self.testdata_digest = ""
        self.image_id = "sha256:official-image"
        self.image_contract = DESKTOP_IMAGE_CONTRACT

    def wait_ssh(self, timeout=180):
        return True

    def run(self, command, timeout=300):
        self.commands.append(command)
        self.timeouts.append(timeout)
        if "org.noi.desktop.contract" in command:
            return f"{self.image_id} {self.image_contract}\n"
        if "IPAM.Config" in command:
            return "172.18.0.1\n"
        if ".IPAddress" in command:
            return "172.18.0.2\n"
        if "docker image inspect -f '{{.Id}}'" in command:
            return self.image_id + "\n"
        if "docker inspect -f '{{.Image}}'" in command:
            return self.image_id + "\n"
        if "sha256sum" in command:
            if "testdata.tar.gz" in command:
                return self.testdata_digest + "\n"
            return self.paper_digest + "\n"
        return ""

    def put_file(self, local_path, remote_path):
        self.uploads.append((local_path, remote_path))

    def put_content(self, content, remote_path):
        self.contents.append((content, remote_path))

    def get_file(self, remote_path, local_path):
        with tarfile.open(local_path, "w:gz") as archive:
            for name in self.files:
                code = (
                    '#include <cstdio>\nint main(){freopen("'
                    + name
                    + '.in","r",stdin);freopen("'
                    + name
                    + '.out","w",stdout);}\n'
                ).encode()
                info = tarfile.TarInfo(f"7/answers/alice/{name}/{name}.cpp")
                info.size = len(code)
                archive.addfile(info, io.BytesIO(code))


def pipeline_config(root: Path) -> dict:
    return {
        "contest_server": {
            "ssh_user": "root",
            "ssh_key": "/keys/contest",
            "strict_host_key": True,
            "host_key_sha256": "SHA256:" + "A" * 43,
            "seats_root": "/data/seats",
            "docker_image": "noi-linux-sim:latest",
            "docker_network": "seats",
            "memory": "1536m",
            "cpus": "1.0",
            "pids_limit": 512,
            "shm_size": "1g",
            "gateway_listen": 80,
            "submit_proxy_port": 18082,
        },
        "hydro": {"submit_enabled": False},
        "orchestrator": {
            "collected_dir": str(root / "collected"),
            "materials_dir": str(root / "materials"),
            "artifact_root": str(root / "artifacts"),
            # Unit tests exercise pipeline semantics without taking the
            # Linux-only cross-process deployment lock.  Qualification runs
            # use the real configured lock on Linux.
            "deployment_lock": "",
            "public_base_url": "https://exam.example.test",
            "auto_shutdown_after_collect": True,
            "shutdown_grace_minutes": 30,
            "shutdown_on_collect_error": False,
            "shutdown_on_prepare_error": True,
        },
    }


class DeploymentLockPlatformTests(unittest.TestCase):
    def test_configured_lock_fails_closed_without_fcntl(self):
        pipe = object.__new__(Pipeline)
        pipe.cfg = {
            "orchestrator": {"deployment_lock": "/runtime/deploy-image.lock"}
        }
        with patch("services.pipeline.fcntl", None):
            with self.assertRaisesRegex(RuntimeError, "仅支持 Linux"):
                pipe._acquire_deployment_lock()

    def test_explicitly_disabled_lock_is_a_unit_test_noop(self):
        pipe = object.__new__(Pipeline)
        pipe.cfg = {"orchestrator": {"deployment_lock": ""}}
        with patch("services.pipeline.fcntl", None):
            self.assertIsNone(pipe._acquire_deployment_lock())


def approve_v1_material_fixture(
    store: Store,
    root: Path,
    tid: str,
    paper: bytes,
    testdata: bytes,
    *,
    testdata_files: int,
    testdata_expanded_size: int,
) -> None:
    """Create the same immutable artifact/publication binding used in V1."""
    revision = "v1-fixture"
    artifact_root = root / "artifacts" / tid / revision
    student = artifact_root / "student"
    student.mkdir(parents=True, exist_ok=True)
    (student / "paper.pdf").write_bytes(paper)
    (student / "testdata.tar.gz").write_bytes(testdata)
    paper_sha256 = hashlib.sha256(paper).hexdigest()
    testdata_sha256 = hashlib.sha256(testdata).hexdigest()
    store.put_artifact_revision(
        tid,
        revision,
        state="review",
        source_sha256="1" * 64,
        root_path=str(artifact_root),
        manifest_sha256="2" * 64,
        manifest={"schema_version": 1},
        paper_name="paper.pdf",
        paper_sha256=paper_sha256,
        paper_size=len(paper),
        testdata_name="testdata.tar.gz",
        testdata_sha256=testdata_sha256,
        testdata_size=len(testdata),
        testdata_files=testdata_files,
        testdata_expanded_size=testdata_expanded_size,
    )
    receipt = {
        "publication_id": hashlib.sha256(
            f"{tid}:{revision}".encode("ascii")
        ).hexdigest(),
        "tid": tid,
        "revision": revision,
        "attachments": [
            {
                "name": "01_比赛题面.pdf",
                "sha256": paper_sha256,
                "size": len(paper),
            },
            {
                "name": "02_辅助自测数据.tar.gz",
                "sha256": testdata_sha256,
                "size": len(testdata),
            },
        ],
    }
    store.approve_artifact_with_publication(
        tid,
        revision,
        "fixture",
        {
            "ok": True,
            **receipt,
            "receipt_sha256": store._canonical_json_sha256(receipt),
        },
    )


class FrontendReconciliationTests(unittest.TestCase):
    @staticmethod
    def _calling_thread_monotonic(values):
        """Keep a patched clock from being consumed by unrelated workers."""
        caller = threading.get_ident()
        sequence = iter(values)
        real_monotonic = time.monotonic

        def monotonic():
            if threading.get_ident() == caller:
                return next(sequence)
            return real_monotonic()

        return monotonic

    @staticmethod
    def _pipeline(root: Path, cvm) -> Pipeline:
        pipe = Pipeline(
            pipeline_config(root), cvm, MagicMock(), MagicMock(), MagicMock()
        )
        pipe.frontend = MagicMock()
        return pipe

    def test_manual_shutdown_closes_frontend_before_cloud_stop(self):
        events = []
        states = ["RUNNING", "STOPPED"]
        cvm = MagicMock()
        cvm.status.side_effect = lambda: (
            events.append("status")
            or (states.pop(0), "198.51.100.10")
        )
        cvm.stop.side_effect = lambda: events.append("stop")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)
            pipe.frontend.disable.side_effect = lambda: events.append("disable")

            pipe.shutdown_server()

        self.assertEqual(events, ["disable", "status", "stop", "status"])

    def test_manual_shutdown_still_closes_frontend_when_already_stopped(self):
        cvm = MagicMock()
        cvm.status.return_value = ("Stopped", "")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)

            pipe.shutdown_server()

        pipe.frontend.disable.assert_called_once_with()
        cvm.stop.assert_not_called()

    def test_manual_boot_revokes_stale_direct_rule_before_start(self):
        events = []
        cvm = MagicMock()
        cvm.desktop_access_enabled = True
        cvm.status.return_value = ("STOPPED", "")
        cvm.revoke_desktop_access.side_effect = (
            lambda: events.append("revoke") or {"closed": True}
        )
        cvm.start.side_effect = lambda: events.append("start")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)

            self.assertTrue(pipe.boot_server())

        self.assertEqual(events, ["revoke", "start"])

    def test_manual_boot_refuses_start_when_stale_rule_cannot_close(self):
        cvm = MagicMock()
        cvm.desktop_access_enabled = True
        cvm.status.return_value = ("STOPPED", "")
        cvm.revoke_desktop_access.side_effect = RuntimeError("revoke failed")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)

            with self.assertRaisesRegex(RuntimeError, "revoke failed"):
                pipe.boot_server()

        cvm.start.assert_not_called()

    def test_manual_shutdown_stops_vm_if_fallback_route_cannot_close(self):
        cvm = MagicMock()
        cvm.status.side_effect = [
            ("Running", "198.51.100.10"),
            ("Stopped", ""),
        ]
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)
            pipe.frontend.disable.side_effect = RuntimeError("reload failed")

            with self.assertRaisesRegex(RuntimeError, "reload failed"):
                pipe.shutdown_server()

        self.assertEqual(cvm.status.call_count, 2)
        cvm.stop.assert_called_once_with()

    def test_manual_shutdown_revokes_direct_rule_before_fallback_and_vm(self):
        events = []
        cvm = MagicMock()
        cvm.desktop_access_enabled = True
        cvm.revoke_desktop_access.side_effect = lambda: events.append("revoke") or {}
        states = ["RUNNING", "STOPPED"]
        cvm.status.side_effect = lambda: events.append("status") or (
            states.pop(0), "198.51.100.10"
        )
        cvm.stop.side_effect = lambda: events.append("stop")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)
            pipe.frontend.disable.side_effect = lambda: events.append("disable")

            pipe.shutdown_server()

        self.assertEqual(
            events, ["revoke", "disable", "status", "stop", "status"]
        )

    def test_service_cleanup_stops_vm_when_direct_revoke_fails(self):
        cvm = MagicMock()
        cvm.desktop_access_enabled = True
        cvm.revoke_desktop_access.side_effect = RuntimeError("cloud revoke failed")
        cvm.status.side_effect = [
            ("RUNNING", "198.51.100.10"),
            ("STOPPED", ""),
        ]
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)

            with self.assertRaisesRegex(RuntimeError, "停机兜底"):
                pipe.fail_closed_desktop_cleanup()

        cvm.revoke_desktop_access.assert_called_once_with()
        cvm.stop.assert_called_once_with()

    def test_stop_server_rejects_unknown_cloud_state(self):
        cvm = MagicMock()
        cvm.status.return_value = ("REBOOTING", "198.51.100.10")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)

            with self.assertRaisesRegex(RuntimeError, "不可安全停机"):
                pipe._stop_server_best_effort()

        cvm.stop.assert_not_called()

    def test_stop_server_times_out_while_starting(self):
        cvm = MagicMock()
        cvm.status.return_value = ("STARTING", "")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)
            with patch(
                "services.pipeline.time.monotonic",
                side_effect=self._calling_thread_monotonic([0, 0, 121]),
            ), patch("services.pipeline.time.sleep"):
                with self.assertRaisesRegex(TimeoutError, "STARTING"):
                    pipe._stop_server_best_effort()

        cvm.stop.assert_not_called()

    def test_stop_server_requires_transition_after_stop_request(self):
        cvm = MagicMock()
        cvm.status.return_value = ("RUNNING", "198.51.100.10")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)
            with patch(
                "services.pipeline.time.monotonic",
                side_effect=self._calling_thread_monotonic([0, 0, 121]),
            ), patch("services.pipeline.time.sleep"):
                with self.assertRaisesRegex(TimeoutError, "RUNNING"):
                    pipe._stop_server_best_effort()

        cvm.stop.assert_called_once_with()

    def test_stop_server_waits_through_stopping_until_stopped(self):
        cvm = MagicMock()
        cvm.status.side_effect = [
            ("RUNNING", "198.51.100.10"),
            ("STOPPING", "198.51.100.10"),
            ("STOPPED", ""),
        ]
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)
            with patch("services.pipeline.time.sleep") as sleep:
                pipe._stop_server_best_effort()

        cvm.stop.assert_called_once_with()
        sleep.assert_called_once_with(3)

    def test_stop_server_times_out_while_stopping(self):
        cvm = MagicMock()
        cvm.status.return_value = ("STOPPING", "198.51.100.10")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)
            with patch(
                "services.pipeline.time.monotonic",
                side_effect=self._calling_thread_monotonic([0, 0, 121]),
            ), patch("services.pipeline.time.sleep"):
                with self.assertRaisesRegex(TimeoutError, "STOPPING"):
                    pipe._stop_server_best_effort()

        cvm.stop.assert_not_called()

    def test_manual_shutdown_persists_error_so_reconciler_cannot_reopen(self):
        tid = "3" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                end_at_ms = int(
                    (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
                    * 1000
                )
                store.upsert_contest(
                    tid,
                    "manual shutdown",
                    ["apple"],
                    {"apple": "P1"},
                    end_at_ms=end_at_ms,
                )
                store.set_state(tid, "ready")
                cvm = DirectFakeCVM()
                cvm.state = "RUNNING"
                pipe = Pipeline(
                    pipeline_config(root), cvm, MagicMock(), store, MagicMock()
                )
                pipe.frontend = MagicMock()
                pipe.reconcile_desktop_access()
                self.assertTrue(cvm.direct_open)

                pipe.shutdown_server()
                pipe.reconcile_desktop_access()

                self.assertEqual(store.get_contest(tid)["state"], "error")
                self.assertFalse(cvm.direct_open)
            finally:
                store.close()

    def test_stopped_reconciliation_is_latched_until_cloud_runs(self):
        cvm = MagicMock()
        cvm.status.return_value = ("Stopped", "")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)

            self.assertTrue(pipe.reconcile_frontend())
            self.assertFalse(pipe.reconcile_frontend())
            self.assertEqual(pipe.frontend.disable.call_count, 1)

            cvm.status.return_value = ("Running", "198.51.100.10")
            self.assertFalse(pipe.reconcile_frontend())
            cvm.status.return_value = ("Stopped", "")
            self.assertTrue(pipe.reconcile_frontend())

        self.assertEqual(pipe.frontend.disable.call_count, 2)

    def test_frontend_enable_clears_stopped_reconciliation_latch(self):
        cvm = MagicMock()
        cvm.status.return_value = ("Stopped", "")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)
            self.assertTrue(pipe.reconcile_frontend())

            pipe._enable_frontend("198.51.100.10", 80)

            self.assertTrue(pipe.reconcile_frontend())

        pipe.frontend.enable.assert_called_once_with("198.51.100.10", 80)
        self.assertEqual(pipe.frontend.disable.call_count, 2)

    def test_direct_mode_keeps_https_fallback_for_one_ready_contest(self):
        cvm = MagicMock()
        cvm.desktop_access_enabled = True
        cvm.status.return_value = ("RUNNING", "198.51.100.10")
        with tempfile.TemporaryDirectory() as directory:
            pipe = self._pipeline(Path(directory), cvm)
            pipe.store.contests.return_value = [
                {
                    "tid": "a" * 24,
                    "state": "ready",
                    "end_at_ms": int(time.time() * 1000) + 60_000,
                }
            ]

            self.assertTrue(pipe.reconcile_frontend(force=True))

        pipe.frontend.enable.assert_called_once_with("198.51.100.10", 80)
        pipe.frontend.disable.assert_not_called()


class PipelineHelpersTests(unittest.TestCase):
    def test_remote_material_upload_atomically_replaces_readonly_retry_target(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "paper.pdf"
            payload = b"%PDF-1.7\nretry-safe"
            local.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            remote = FakeRemote()
            remote.paper_digest = digest

            Pipeline._put_remote_verified_file(
                remote,
                local,
                "/data/seats/test/materials/paper.pdf",
                digest,
            )

            self.assertEqual(len(remote.uploads), 1)
            self.assertRegex(
                remote.uploads[0][1],
                r"/paper\.pdf\.upload-[a-f0-9]{24}$",
            )
            self.assertNotEqual(
                remote.uploads[0][1],
                "/data/seats/test/materials/paper.pdf",
            )
            commit = next(command for command in remote.commands if "mv -f --" in command)
            self.assertIn("chmod 0444", commit)
            self.assertIn("/data/seats/test/materials/paper.pdf", commit)

    def test_gateway_base_supports_http_eip_and_rejects_stale_ip(self):
        server = {
            "gateway_public_base_url": "http://198.51.100.10",
            "gateway_listen": 80,
        }
        self.assertEqual(
            gateway_base_url(server, "198.51.100.10"),
            "http://198.51.100.10",
        )
        self.assertEqual(
            gateway_base_url(server, ""),
            "http://198.51.100.10",
        )
        with self.assertRaisesRegex(RuntimeError, "不一致"):
            gateway_base_url(server, "198.51.100.11")
        self.assertEqual(
            gateway_base_url(
                {"gateway_scheme": "http", "gateway_listen": 8080},
                "198.51.100.10",
            ),
            "http://198.51.100.10:8080",
        )

    @patch("services.pipeline.http.client.HTTPConnection")
    @patch("services.pipeline.os.urandom", return_value=b"0" * 16)
    def test_direct_probe_requires_page_200_and_websocket_101(
        self, _random, connection
    ):
        page_connection = MagicMock()
        page_response = MagicMock(status=200)
        page_connection.getresponse.return_value = page_response
        websocket_connection = MagicMock()
        websocket_response = MagicMock(status=101)
        key = base64.b64encode(b"0" * 16).decode("ascii")
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(
                    "ascii"
                )
            ).digest()
        ).decode("ascii")
        websocket_response.getheader.return_value = expected
        websocket_connection.getresponse.return_value = websocket_response
        connection.side_effect = [page_connection, websocket_connection]

        result = probe_novnc_gateway(
            "198.51.100.10",
            80,
            "seat-token",
            quality=9,
            compression=2,
        )

        self.assertEqual(result, {"page_status": 200, "websocket_status": 101})
        page_request = page_connection.request.call_args
        self.assertEqual(page_request.args[0], "GET")
        self.assertIn("quality=9&compression=2", page_request.args[1])
        websocket_request = websocket_connection.request.call_args
        self.assertEqual(websocket_request.args[1], "/s/seat-token/websockify")
        self.assertEqual(websocket_request.kwargs["headers"]["Upgrade"], "websocket")

    def test_direct_rule_reconciles_from_persisted_ready_state_and_deadline(self):
        tid = "4" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                end_at_ms = int(
                    (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
                    * 1000
                )
                store.upsert_contest(
                    tid,
                    "direct",
                    ["apple"],
                    {"apple": "P1"},
                    end_at_ms=end_at_ms,
                )
                store.set_state(tid, "ready")
                cvm = DirectFakeCVM()
                cvm.state = "RUNNING"
                pipe = Pipeline(
                    pipeline_config(root), cvm, MagicMock(), store, MagicMock()
                )

                opened = pipe.reconcile_desktop_access()
                self.assertTrue(opened["desired_open"])
                self.assertTrue(pipe.desktop_access_health()["healthy"])

                store.set_state(tid, "collecting")
                closed = pipe.reconcile_desktop_access()
                self.assertFalse(closed["desired_open"])
                self.assertTrue(closed["closed"])
                self.assertFalse(cvm.direct_open)
            finally:
                store.close()

    def test_closed_reconcile_stops_vm_when_rule_cannot_be_revoked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                cvm = DirectFakeCVM()
                cvm.state = "RUNNING"
                cvm.revoke_desktop_access = MagicMock(
                    side_effect=RuntimeError("cloud revoke failed")
                )
                pipe = Pipeline(
                    pipeline_config(root), cvm, MagicMock(), store, MagicMock()
                )

                with self.assertRaisesRegex(RuntimeError, "cloud revoke failed"):
                    pipe.reconcile_desktop_access()

                self.assertEqual(cvm.stop_count, 1)
                self.assertEqual(cvm.state, "STOPPED")
            finally:
                store.close()

    def test_open_reconcile_revokes_existing_rule_after_topology_failure(self):
        tid = "4" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                end_at_ms = int(
                    (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
                    * 1000
                )
                store.upsert_contest(
                    tid,
                    "direct topology drift",
                    ["apple"],
                    {"apple": "P1"},
                    end_at_ms=end_at_ms,
                )
                store.set_state(tid, "ready")
                cvm = DirectFakeCVM()
                cvm.state = "RUNNING"
                cvm.direct_open = True
                cvm.direct_tid = tid
                cvm.direct_end_at_ms = end_at_ms
                cvm.ensure_desktop_access = MagicMock(
                    side_effect=RuntimeError("security-group topology drift")
                )
                pipe = Pipeline(
                    pipeline_config(root), cvm, MagicMock(), store, MagicMock()
                )

                with self.assertRaisesRegex(RuntimeError, "topology drift"):
                    pipe.reconcile_desktop_access()

                self.assertFalse(cvm.direct_open)
                self.assertEqual(cvm.stop_count, 0)
            finally:
                store.close()

    def test_reconcile_revokes_existing_rule_when_store_state_is_unreadable(self):
        cvm = DirectFakeCVM()
        cvm.state = "RUNNING"
        cvm.direct_open = True
        store = MagicMock()
        store.contests.side_effect = RuntimeError("state database unavailable")
        with tempfile.TemporaryDirectory() as directory:
            pipe = Pipeline(
                pipeline_config(Path(directory)),
                cvm,
                MagicMock(),
                store,
                MagicMock(),
            )

            with self.assertRaisesRegex(RuntimeError, "state database unavailable"):
                pipe.reconcile_desktop_access()

        self.assertFalse(cvm.direct_open)
        self.assertEqual(cvm.stop_count, 0)

    def test_queued_reconcile_cannot_reopen_after_shutdown_latch(self):
        tid = "5" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                end_at_ms = int(
                    (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
                    * 1000
                )
                store.upsert_contest(
                    tid,
                    "shutdown race",
                    ["apple"],
                    {"apple": "P1"},
                    end_at_ms=end_at_ms,
                )
                store.set_state(tid, "ready")
                cvm = DirectFakeCVM()
                cvm.state = "RUNNING"
                cvm.direct_open = True
                cvm.direct_tid = tid
                cvm.direct_end_at_ms = end_at_ms
                pipe = Pipeline(
                    pipeline_config(root), cvm, MagicMock(), store, MagicMock()
                )
                started = threading.Event()
                failures = []

                def queued_reconcile():
                    started.set()
                    try:
                        pipe.reconcile_desktop_access()
                    except Exception as exc:  # pragma: no cover - assertion below
                        failures.append(exc)

                with pipe._desktop_access_guard:
                    worker = threading.Thread(target=queued_reconcile)
                    worker.start()
                    self.assertTrue(started.wait(1))
                    pipe.begin_shutdown()
                worker.join(2)

                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                self.assertFalse(cvm.direct_open)
                self.assertFalse(
                    any(event[0] == "open" for event in cvm.direct_events)
                )
            finally:
                store.close()

    def test_desktop_image_contract_resolves_exact_image_id_and_fails_closed(self):
        remote = FakeRemote()
        image_id = Pipeline._inspect_desktop_image_contract(
            remote, "noi-linux-official:2.0"
        )
        self.assertEqual(image_id, remote.image_id)
        self.assertIn("org.noi.desktop.contract", remote.commands[-1])

        remote.image_contract = "legacy"
        with self.assertRaisesRegex(RuntimeError, "契约不匹配"):
            Pipeline._inspect_desktop_image_contract(
                remote, "noi-linux-official:2.0"
            )

    def test_password_uses_vnc_safe_length(self):
        value = rand_password()
        self.assertEqual(len(value), 8)
        self.assertNotIn("0", value)

    def test_safe_extract_rejects_traversal(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo("../outside.txt")
            payload = b"bad"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        stream.seek(0)
        with tempfile.TemporaryDirectory() as directory:
            with tarfile.open(fileobj=stream, mode="r") as archive:
                with self.assertRaises(ValueError):
                    safe_extract(archive, Path(directory))

    def test_gateway_config_is_scoped_to_contest(self):
        tid = "a" * 24
        config = NGINX_CONF.format(
            tid=tid, listen="192.0.2.2:80", locations=""
        )
        self.assertTrue(config.startswith(f"# noi-contest: {tid}\n"))
        self.assertIn("listen 192.0.2.2:80 default_server", config)

    def test_gateway_redirect_includes_seat_scoped_websocket_path(self):
        config = NGINX_LOCATION.format(
            token="seat-token",
            cip="172.18.0.2",
            novnc_path=novnc_path("seat-token", 3, 2),
        )
        self.assertIn(
            "/s/seat-token/vnc.html?path=s/seat-token/websockify", config
        )
        self.assertIn("reconnect=true&reconnect_delay=5000", config)
        self.assertIn("quality=3&compression=2", config)
        self.assertIn("proxy_buffering off", config)
        self.assertIn("tcp_nodelay on", config)
        self.assertIn("location /s/seat-token/", config)

    def test_candidate_id_falls_back_for_unsafe_username(self):
        self.assertEqual(candidate_id("BJ-001", 7), "BJ-001")
        self.assertEqual(candidate_id("bad user", 7), "U7")

    def test_end_at_watchdog_is_scheduled_on_contest_host(self):
        tid = "7" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = FakeRemote()
            pipe = Pipeline(
                pipeline_config(root), MagicMock(), MagicMock(), MagicMock(), MagicMock()
            )
            end_at_ms = int(
                (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
                * 1000
            )

            pipe._install_freeze_watchdog(
                remote, {"tid": tid, "end_at_ms": end_at_ms}
            )

            command = next(c for c in remote.commands if "systemctl restart" in c)
            self.assertIn(f"noi-contest-freeze-{tid}", command)
            payloads = "\n".join(content for content, _ in remote.contents)
            self.assertIn("AccuracySec=100ms", payloads)
            self.assertIn("Persistent=true", payloads)
            calendar_line = next(
                line for line in payloads.splitlines() if line.startswith("OnCalendar=")
            )
            self.assertRegex(calendar_line, r"^OnCalendar=@\d+$")
            self.assertIn(f"label=noi.contest={tid}", payloads)
            self.assertIn("docker pause", payloads)
            self.assertIn("docker ps -aq", payloads)
            self.assertIn("docker update --restart=no", payloads)
            self.assertIn("systemctl stop nginx", payloads)
            self.assertIn("systemctl is-active --quiet nginx", payloads)
            self.assertLess(
                payloads.index("docker pause"), payloads.index("systemctl stop nginx")
            )
            self.assertIn("Restart=on-failure", payloads)
            self.assertIn("set -eu", payloads)

    def test_schedule_extension_updates_watchdog_before_atomic_store_commit(self):
        tid = "8" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                begin_ms = now_ms + 1_800_000
                end_ms = now_ms + 7_200_000
                store.upsert_contest(
                    tid,
                    "schedule sync",
                    ["apple"],
                    {"apple": "P1"},
                    begin_at_ms=begin_ms,
                    end_at_ms=end_ms,
                    hydro_rule="oi",
                )
                store.set_state(tid, "ready")
                pool = SeatPoolState.create(
                    tid,
                    max_participants=1,
                    spare_count=2,
                    begin_at_ms=begin_ms,
                )
                store.put_seat_pool(tid, None, pool.to_dict())
                cvm = DirectFakeCVM()
                cvm.state = "RUNNING"
                remote = FakeRemote()
                pipe = Pipeline(
                    pipeline_config(root), cvm, MagicMock(), store, MagicMock()
                )
                pipe._remote = MagicMock(return_value=remote)
                events = []
                pipe._install_freeze_watchdog = MagicMock(
                    side_effect=lambda *_: events.append("watchdog")
                )
                original_commit = store.commit_schedule_sync

                def commit_after_watchdog(*args, **kwargs):
                    events.append("commit")
                    return original_commit(*args, **kwargs)

                store.commit_schedule_sync = MagicMock(
                    side_effect=commit_after_watchdog
                )

                changed = pipe.sync_contest_schedule(
                    tid,
                    begin_at_ms=begin_ms + 600_000,
                    end_at_ms=end_ms + 600_000,
                    hydro_rule="oi",
                    observed_at_ms=now_ms,
                )

                self.assertEqual(events, ["watchdog", "commit"])
                self.assertFalse(changed["deadline_reached"])
                self.assertEqual(
                    store.get_contest(tid)["end_at_ms"], end_ms + 600_000
                )
                self.assertEqual(
                    store.seat_pool(tid)["state"]["begin_at_ms"],
                    begin_ms + 600_000,
                )
            finally:
                store.close()

    def test_schedule_extension_does_not_commit_when_watchdog_update_fails(self):
        tid = "9" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                begin_ms = now_ms + 1_800_000
                end_ms = now_ms + 7_200_000
                store.upsert_contest(
                    tid,
                    "schedule sync failure",
                    ["apple"],
                    {"apple": "P1"},
                    begin_at_ms=begin_ms,
                    end_at_ms=end_ms,
                    hydro_rule="oi",
                )
                store.set_state(tid, "ready")
                pool = SeatPoolState.create(
                    tid,
                    max_participants=1,
                    spare_count=2,
                    begin_at_ms=begin_ms,
                )
                store.put_seat_pool(tid, None, pool.to_dict())
                cvm = DirectFakeCVM()
                cvm.state = "RUNNING"
                pipe = Pipeline(
                    pipeline_config(root), cvm, MagicMock(), store, MagicMock()
                )
                pipe._remote = MagicMock(return_value=FakeRemote())
                pipe._install_freeze_watchdog = MagicMock(
                    side_effect=RuntimeError("timer install failed")
                )

                with self.assertRaisesRegex(RuntimeError, "timer install failed"):
                    pipe.sync_contest_schedule(
                        tid,
                        begin_at_ms=begin_ms + 600_000,
                        end_at_ms=end_ms + 600_000,
                        hydro_rule="oi",
                        observed_at_ms=now_ms,
                    )

                self.assertEqual(store.get_contest(tid)["end_at_ms"], end_ms)
                self.assertEqual(
                    store.seat_pool(tid)["state"]["begin_at_ms"], begin_ms
                )
            finally:
                store.close()

    def test_collect_freezes_before_removing_deadline_watchdog(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            pipe = Pipeline(
                pipeline_config(Path(directory)),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
            )
            pipe._freeze_containers = MagicMock(
                side_effect=lambda *_: events.append("freeze")
            )
            pipe._remove_freeze_watchdog = MagicMock(
                side_effect=lambda *_: events.append("remove")
            )

            pipe._freeze_for_collection(MagicMock(), "7" * 24, "seat-1")

        self.assertEqual(events, ["freeze", "remove"])

    def test_collect_keeps_deadline_watchdog_when_freeze_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            pipe = Pipeline(
                pipeline_config(Path(directory)),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
            )
            pipe._freeze_containers = MagicMock(
                side_effect=RuntimeError("freeze failed")
            )
            pipe._remove_freeze_watchdog = MagicMock()

            with self.assertRaisesRegex(RuntimeError, "freeze failed"):
                pipe._freeze_for_collection(MagicMock(), "7" * 24, "seat-1")

        pipe._remove_freeze_watchdog.assert_not_called()

    def test_collect_retries_from_error_and_restarts_stopped_server(self):
        tid = "b" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
                store.add_seat(
                    tid,
                    7,
                    "alice",
                    "token",
                    "pass1234",
                    "submit-token",
                    "alice",
                    "seat-b-7",
                    "172.18.0.2",
                )
                store.set_state(tid, "error", "previous collection failed")
                cvm = FakeCVM()
                remote = FakeRemote()
                pipe = Pipeline(
                    pipeline_config(root), cvm, MagicMock(), store, MagicMock()
                )
                pipe._remote = lambda _: remote

                report = pipe.collect(tid)

                self.assertEqual(report["alice"]["apple"]["status"], "ok")
                self.assertEqual(store.get_contest(tid)["state"], "safe_wait")
                self.assertEqual(cvm.start_count, 1)
                self.assertEqual(cvm.stop_count, 0)
                self.assertTrue(any("docker pause" in command for command in remote.commands))
            finally:
                store.close()

    def test_prepare_blocks_unapproved_material_before_cloud_start(self):
        tid = "c" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
                cvm = FakeCVM()
                remote = FakeRemote()
                hydro = MagicMock()
                pipe = Pipeline(
                    pipeline_config(root), cvm, hydro, store, MagicMock()
                )
                pipe._remote = lambda _: remote

                with self.assertRaisesRegex(RuntimeError, "材料尚未"):
                    pipe.prepare(tid)

                self.assertEqual(store.get_contest(tid)["state"], "error")
                self.assertEqual(cvm.start_count, 0)
                self.assertEqual(cvm.stop_count, 0)
                self.assertEqual(remote.commands, [])
            finally:
                store.close()

    def test_prepare_blocks_missing_oj_material_receipt_before_cloud_mutation(self):
        tid = "1" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                begin = datetime.now(timezone.utc) + timedelta(hours=1)
                end = begin + timedelta(hours=5)
                store.upsert_contest(
                    tid,
                    "receipt required",
                    ["apple"],
                    {"apple": "P1"},
                    submission_mode="both",
                    materials_mode="manual",
                    begin_at_ms=int(begin.timestamp() * 1000),
                    end_at_ms=int(end.timestamp() * 1000),
                    hydro_rule="oi",
                    practice_groups=2,
                )
                store.set_paper(tid, "paper.pdf", "3" * 64, 10)
                store.set_testdata(tid, "data.tar.gz", "4" * 64, 20, 4, 60)
                hydro = MagicMock()
                hydro.roster.return_value = [{"uid": 7, "uname": "alice"}]
                hydro.get_contest.return_value = {
                    "beginAt": begin,
                    "endAt": end,
                    "rule": "oi",
                }
                cvm = DirectFakeCVM()
                pipe = Pipeline(
                    pipeline_config(root), cvm, hydro, store, MagicMock()
                )
                pipe.frontend = MagicMock()

                with self.assertRaisesRegex(RuntimeError, "同字节发布"):
                    pipe.prepare(tid)

                self.assertEqual(cvm.start_count, 0)
                self.assertEqual(cvm.direct_events, [])
                pipe.frontend.disable.assert_not_called()
            finally:
                store.close()

    def test_prepare_rejects_hydro_time_drift_before_cloud_start(self):
        tid = "8" * 24
        begin = datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc)
        end = begin + timedelta(hours=5)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(
                    tid,
                    "test",
                    ["apple"],
                    {"apple": "P1"},
                    "web",
                    begin_at_ms=int(begin.timestamp() * 1000),
                    end_at_ms=int(end.timestamp() * 1000),
                    hydro_rule="oi",
                )
                cvm = FakeCVM()
                hydro = MagicMock()
                hydro.get_contest.return_value = {
                    "beginAt": begin,
                    "endAt": end + timedelta(minutes=1),
                    "rule": "oi",
                }
                pipe = Pipeline(
                    pipeline_config(root), cvm, hydro, store, MagicMock()
                )

                with self.assertRaisesRegex(RuntimeError, "重新登记"):
                    pipe.prepare(tid)

                self.assertEqual(cvm.start_count, 0)
                self.assertEqual(store.get_contest(tid)["state"], "error")
            finally:
                store.close()

    def test_prepare_error_retry_cannot_delete_current_web_submissions(self):
        tid = "6" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(
                    tid, "test", ["apple"], {"apple": "P1"}, "web"
                )
                store.set_state(tid, "ready")
                contest = store.get_contest(tid)
                row = store.enqueue_web_submission(
                    tid,
                    7,
                    "apple",
                    "int main(){}",
                    client_nonce="a" * 32,
                    submission_id="b" * 64,
                    submission_session=contest["submission_session"],
                    judge_pid="P1",
                    judge_lang="cc",
                    judge_source="int main(){}",
                    issues=[],
                    accepted_at_ms=1_786_080_000_000,
                )
                store.set_state(tid, "error", "collect failed")
                cvm = FakeCVM()
                pipe = Pipeline(
                    pipeline_config(root), cvm, MagicMock(), store, MagicMock()
                )

                with self.assertRaisesRegex(RuntimeError, "已有座位或学生递交"):
                    pipe.prepare(tid)

                self.assertEqual(cvm.start_count, 0)
                self.assertIsNotNone(store.get_web_submission(row["id"]))
            finally:
                store.close()

    def test_prepare_error_retry_cannot_destroy_uncollected_folder_seat(self):
        tid = "5" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(
                    tid, "folder", ["apple"], {"apple": "P1"}, "folder"
                )
                store.add_seat(
                    tid,
                    7,
                    "alice",
                    "desktop-token",
                    "pass1234",
                    "submit-token",
                    "alice",
                    "seat-container",
                    "172.18.0.2",
                )
                store.set_state(tid, "error", "tar download failed")
                cvm = FakeCVM()
                pipe = Pipeline(
                    pipeline_config(root), cvm, MagicMock(), store, MagicMock()
                )

                with self.assertRaisesRegex(RuntimeError, "已有座位或学生递交"):
                    pipe.prepare(tid)

                self.assertEqual(cvm.start_count, 0)
                self.assertEqual(len(store.seats(tid)), 1)
            finally:
                store.close()

    def test_unapproved_material_does_not_touch_running_manual_server(self):
        tid = "9" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
                cvm = FakeCVM()
                cvm.state = "RUNNING"
                remote = FakeRemote()
                hydro = MagicMock()
                frontend = MagicMock()
                pipe = Pipeline(
                    pipeline_config(root), cvm, hydro, store, MagicMock()
                )
                pipe.frontend = frontend
                pipe._remote = lambda _: remote

                with self.assertRaisesRegex(RuntimeError, "材料尚未"):
                    pipe.prepare(tid)

                self.assertEqual(cvm.start_count, 0)
                self.assertEqual(cvm.stop_count, 0)
                self.assertEqual(remote.commands, [])
                frontend.disable.assert_not_called()
            finally:
                store.close()

    def test_prepare_uploads_verified_v1_material_bundle(self):
        tid = "f" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                payload = b"%PDF-1.7\ncontest-paper"
                digest = hashlib.sha256(payload).hexdigest()
                local_paper = root / "materials" / tid / "paper.pdf"
                local_paper.parent.mkdir(parents=True)
                local_paper.write_bytes(payload)
                testdata = b"normalized-testdata-tarball"
                testdata_digest = hashlib.sha256(testdata).hexdigest()
                (local_paper.parent / "testdata.tar.gz").write_bytes(testdata)
                begin = datetime.now(timezone.utc) + timedelta(hours=1)
                end = begin + timedelta(hours=5)
                store.upsert_contest(
                    tid,
                    "test",
                    ["apple"],
                    {"apple": "P1"},
                    submission_mode="both",
                    begin_at_ms=int(begin.timestamp() * 1000),
                    end_at_ms=int(end.timestamp() * 1000),
                    hydro_rule="oi",
                    max_participants=1,
                    spare_seats=0,
                    practice_groups=2,
                )
                store.set_paper(tid, "题面.pdf", digest, len(payload))
                store.set_testdata(
                    tid,
                    "data.zip",
                    testdata_digest,
                    len(testdata),
                    4,
                    20,
                )
                approve_v1_material_fixture(
                    store,
                    root,
                    tid,
                    payload,
                    testdata,
                    testdata_files=4,
                    testdata_expanded_size=20,
                )
                hydro = MagicMock()
                hydro.roster.return_value = [{"uid": 7, "uname": "alice"}]
                hydro.get_contest.return_value = {
                    "beginAt": begin,
                    "endAt": end,
                    "rule": "oi",
                }
                cvm = FakeCVM()
                remote = FakeRemote()
                remote.paper_digest = digest
                remote.testdata_digest = testdata_digest
                pipe = Pipeline(pipeline_config(root), cvm, hydro, store, MagicMock())
                pipe._remote = lambda _: remote

                ip = pipe.prepare(tid)

                self.assertEqual(ip, "198.51.100.10")
                self.assertEqual(store.get_contest(tid)["state"], "ready")
                self.assertEqual(len(remote.uploads), 2)
                self.assertRegex(
                    remote.uploads[0][1],
                    r"/materials/paper\.pdf\.upload-[a-f0-9]{24}$",
                )
                self.assertTrue(
                    any(
                        "mv -f --" in command
                        and "/materials/paper.pdf" in command
                        for command in remote.commands
                    )
                )
                docker_run = next(c for c in remote.commands if c.startswith("docker run"))
                self.assertIn("/materials:/home/student/试题:ro", docker_run)
                self.assertIn("/testdata:/home/student/测试数据:ro", docker_run)
                self.assertIn("SUBMISSION_MODE=both", docker_run)
                self.assertIn("HAS_TEST_DATA=1", docker_run)
                self.assertIn("RESOLUTION=1366x768", docker_run)
                self.assertIn("FRAME_RATE=30", docker_run)
                self.assertTrue(docker_run.endswith(remote.image_id))
                self.assertNotIn("--memory-swap", docker_run)
                desktop_check = next(
                    c for c in remote.commands if "pgrep -x systemd-logind" in c
                )
                self.assertIn("pgrep -x gnome-shell", desktop_check)
                self.assertIn("/usr/libexec/gnome-session-binary", desktop_check)
                self.assertIn("[g]nome-initial-setup", desktop_check)
                self.assertNotIn(
                    "pgrep -f gnome-initial-setup", desktop_check
                )
                self.assertIn(".contest-finalizer-status", desktop_check)
                self.assertIn("比赛资料（从这里开始）", desktop_check)
                self.assertIn("/run/contest-materials/.manifest", desktop_check)
                self.assertIn("schema=3", desktop_check)
                for entry in (
                    "01_比赛题面.pdf",
                    "02_辅助自测数据",
                    "03_答案文件夹",
                    "04_CSP程序回收系统.html",
                    "05_使用说明.txt",
                ):
                    self.assertIn(entry, desktop_check)
                self.assertIn("sha256sum /home/student/试题/paper.pdf", desktop_check)
                self.assertIn("curl -fsS --max-time 3", desktop_check)
                self.assertEqual(desktop_check.count("docker exec "), 2)
                self.assertTrue(remote.contents)
                port_gate = next(
                    c for c in remote.commands
                    if "网关端口 80 已被非 nginx 进程占用" in c
                )
                self.assertIn("ss -H -ltnp4 'sport = :80'", port_gate)
                self.assertLess(
                    remote.commands.index(port_gate),
                    remote.commands.index(docker_run),
                )
                gateway_commit = next(
                    c for c in remote.commands
                    if "nginx 网关未在限定时间内监听" in c
                )
                self.assertIn("reload-or-restart nginx", gateway_commit)
                self.assertIn("grep -F '\"nginx\"'", gateway_commit)
                self.assertIn("curl -fsS --max-time 3", gateway_commit)
                self.assertIn("/s/", gateway_commit)
            finally:
                store.close()

    def test_gateway_port_gate_rejects_invalid_port_before_remote_command(self):
        remote = FakeRemote()
        with self.assertRaisesRegex(ValueError, "网关监听端口无效"):
            Pipeline._assert_gateway_port_available(remote, 0)
        self.assertEqual(remote.commands, [])

    def test_gateway_activation_rejects_unscoped_readiness_path(self):
        remote = FakeRemote()
        with self.assertRaisesRegex(ValueError, "网关验收路径无效"):
            Pipeline._activate_pool_gateway(
                remote, port=80, readiness_path="/not-a-seat"
            )
        self.assertEqual(remote.commands, [])

    def test_gateway_listener_scope_allows_unrelated_interface_listener(self):
        remote = FakeRemote()
        Pipeline._assert_gateway_port_available(
            remote, 80, bind_address="192.0.2.2"
        )
        command = remote.commands[-1]
        self.assertIn('$4 == "192.0.2.2:80"', command)
        self.assertIn('$4 == "0.0.0.0:80"', command)
        self.assertNotIn('$4 == "10.0.2.15:80"', command)

        Pipeline._activate_pool_gateway(
            remote,
            port=80,
            bind_address="192.0.2.2",
            readiness_path="/s/seat-token/vnc.html",
        )
        activation = remote.commands[-1]
        self.assertIn("http://192.0.2.2:80/s/seat-token/vnc.html", activation)
        self.assertIn('$4 == "192.0.2.2:80"', activation)

    def test_direct_prepare_reaches_ready_with_https_fallback_enabled(self):
        tid = "8" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                payload = b"%PDF-1.7\ndirect-contest-paper"
                digest = hashlib.sha256(payload).hexdigest()
                local_paper = root / "materials" / tid / "paper.pdf"
                local_paper.parent.mkdir(parents=True)
                local_paper.write_bytes(payload)
                testdata = b"direct-testdata-tarball"
                testdata_digest = hashlib.sha256(testdata).hexdigest()
                (local_paper.parent / "testdata.tar.gz").write_bytes(testdata)
                begin = datetime.now(timezone.utc) + timedelta(hours=1)
                end = begin + timedelta(hours=5)
                store.upsert_contest(
                    tid,
                    "direct prepare",
                    ["apple"],
                    {"apple": "P1"},
                    submission_mode="both",
                    begin_at_ms=int(begin.timestamp() * 1000),
                    end_at_ms=int(end.timestamp() * 1000),
                    hydro_rule="oi",
                    max_participants=1,
                    spare_seats=0,
                    practice_groups=2,
                )
                store.set_paper(tid, "题面.pdf", digest, len(payload))
                store.set_testdata(
                    tid,
                    "data.zip",
                    testdata_digest,
                    len(testdata),
                    4,
                    20,
                )
                approve_v1_material_fixture(
                    store,
                    root,
                    tid,
                    payload,
                    testdata,
                    testdata_files=4,
                    testdata_expanded_size=20,
                )
                hydro = MagicMock()
                hydro.roster.return_value = [{"uid": 7, "uname": "alice"}]
                hydro.get_contest.return_value = {
                    "beginAt": begin,
                    "endAt": end,
                    "rule": "oi",
                }
                cvm = DirectFakeCVM()
                remote = FakeRemote()
                remote.paper_digest = digest
                remote.testdata_digest = testdata_digest
                pipe = Pipeline(
                    pipeline_config(root), cvm, hydro, store, MagicMock()
                )
                pipe.frontend = MagicMock()
                pipe._remote = lambda _: remote
                pipe._probe_direct_gateway = MagicMock(
                    return_value={"page_status": 200, "websocket_status": 101}
                )

                ip = pipe.prepare(tid)

                self.assertEqual(ip, "198.51.100.10")
                self.assertEqual(store.get_contest(tid)["state"], "ready")
                self.assertTrue(cvm.direct_open)
                pipe.frontend.enable.assert_called_once_with("198.51.100.10", 80)
                pipe.frontend.disable.assert_not_called()
                pipe._probe_direct_gateway.assert_called_once()
            finally:
                store.close()

    def test_direct_prepare_does_not_publish_sg_when_fallback_enable_fails(self):
        tid = "6" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                payload = b"%PDF-1.7\ndirect-close-failure"
                digest = hashlib.sha256(payload).hexdigest()
                local_paper = root / "materials" / tid / "paper.pdf"
                local_paper.parent.mkdir(parents=True)
                local_paper.write_bytes(payload)
                testdata = b"direct-failure-testdata"
                testdata_digest = hashlib.sha256(testdata).hexdigest()
                (local_paper.parent / "testdata.tar.gz").write_bytes(testdata)
                begin = datetime.now(timezone.utc) + timedelta(hours=1)
                end = begin + timedelta(hours=5)
                store.upsert_contest(
                    tid,
                    "direct close failure",
                    ["apple"],
                    {"apple": "P1"},
                    submission_mode="both",
                    begin_at_ms=int(begin.timestamp() * 1000),
                    end_at_ms=int(end.timestamp() * 1000),
                    hydro_rule="oi",
                    max_participants=1,
                    spare_seats=0,
                    practice_groups=2,
                )
                store.set_paper(tid, "题面.pdf", digest, len(payload))
                store.set_testdata(
                    tid,
                    "data.zip",
                    testdata_digest,
                    len(testdata),
                    4,
                    20,
                )
                approve_v1_material_fixture(
                    store,
                    root,
                    tid,
                    payload,
                    testdata,
                    testdata_files=4,
                    testdata_expanded_size=20,
                )
                hydro = MagicMock()
                hydro.roster.return_value = [{"uid": 7, "uname": "alice"}]
                hydro.get_contest.return_value = {
                    "beginAt": begin,
                    "endAt": end,
                    "rule": "oi",
                }
                cvm = DirectFakeCVM()
                cvm.state = "RUNNING"
                remote = FakeRemote()
                remote.paper_digest = digest
                remote.testdata_digest = testdata_digest
                config = pipeline_config(root)
                # The HTTPS compatibility route is part of readiness. Failure
                # must prevent the direct SG publication even if the VM was
                # already running before this prepare attempt.
                config["orchestrator"]["shutdown_on_prepare_error"] = False
                pipe = Pipeline(config, cvm, hydro, store, MagicMock())
                pipe.frontend = MagicMock()
                pipe.frontend.enable.side_effect = RuntimeError("reload failed")
                pipe._remote = lambda _: remote

                with self.assertRaisesRegex(RuntimeError, "reload failed"):
                    pipe.prepare(tid)

                self.assertEqual(cvm.start_count, 0)
                self.assertEqual(cvm.stop_count, 0)
                self.assertEqual(cvm.state, "RUNNING")
                self.assertFalse(cvm.direct_open)
                self.assertEqual(store.get_contest(tid)["state"], "error")
                pipe.frontend.enable.assert_called_once_with("198.51.100.10", 80)
                pipe.frontend.disable.assert_called_once_with()
            finally:
                store.close()

    def test_prepare_uploads_and_mounts_required_testdata_read_only(self):
        tid = "e" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                paper = b"%PDF-1.7\ncontest-paper"
                paper_digest = hashlib.sha256(paper).hexdigest()
                paper_path = root / "materials" / tid / "paper.pdf"
                paper_path.parent.mkdir(parents=True)
                paper_path.write_bytes(paper)
                testdata = b"normalized-testdata-tarball"
                testdata_digest = hashlib.sha256(testdata).hexdigest()
                testdata_path = root / "materials" / tid / "testdata.tar.gz"
                testdata_path.write_bytes(testdata)
                begin = datetime.now(timezone.utc) + timedelta(hours=1)
                end = begin + timedelta(hours=5)
                store.upsert_contest(
                    tid,
                    "test",
                    ["apple"],
                    {"apple": "P1"},
                    submission_mode="both",
                    begin_at_ms=int(begin.timestamp() * 1000),
                    end_at_ms=int(end.timestamp() * 1000),
                    hydro_rule="oi",
                    max_participants=1,
                    spare_seats=0,
                    practice_groups=2,
                )
                store.set_paper(tid, "题面.pdf", paper_digest, len(paper))
                store.set_testdata(
                    tid, "data.zip", testdata_digest, len(testdata), 4, 20
                )
                approve_v1_material_fixture(
                    store,
                    root,
                    tid,
                    paper,
                    testdata,
                    testdata_files=4,
                    testdata_expanded_size=20,
                )
                hydro = MagicMock()
                hydro.roster.return_value = [{"uid": 7, "uname": "alice"}]
                hydro.get_contest.return_value = {
                    "beginAt": begin,
                    "endAt": end,
                    "rule": "oi",
                }
                cvm = FakeCVM()
                remote = FakeRemote()
                remote.paper_digest = paper_digest
                remote.testdata_digest = testdata_digest
                config = pipeline_config(root)
                config["contest_server"]["memory_swap"] = "1536m"
                pipe = Pipeline(config, cvm, hydro, store, MagicMock())
                pipe._remote = lambda _: remote

                pipe.prepare(tid)

                self.assertEqual(len(remote.uploads), 2)
                self.assertRegex(
                    remote.uploads[1][1],
                    r"/materials/testdata\.tar\.gz\.upload-[a-f0-9]{24}$",
                )
                self.assertTrue(
                    any(
                        "mv -f --" in command
                        and "/materials/testdata.tar.gz" in command
                        for command in remote.commands
                    )
                )
                docker_run = next(c for c in remote.commands if c.startswith("docker run"))
                self.assertIn("/testdata:/home/student/测试数据:ro", docker_run)
                self.assertIn("HAS_TEST_DATA=1", docker_run)
                self.assertIn("--memory-swap 1536m", docker_run)
                self.assertTrue(
                    any(
                        "tar --delay-directory-restore -xzf" in c
                        for c in remote.commands
                    )
                )
                rebuild_testdata = next(
                    c
                    for c in remote.commands
                    if "tar --delay-directory-restore -xzf" in c
                )
                self.assertIn("--delay-directory-restore", rebuild_testdata)
                self.assertIn("chmod -R u+w --", rebuild_testdata)
                self.assertIn("rm -rf --", rebuild_testdata)
                desktop_check = next(
                    c for c in remote.commands if ".contest-finalizer-status" in c
                )
                self.assertEqual(
                    remote.timeouts[remote.commands.index(desktop_check)], 300
                )
                self.assertIn("find /home/student/测试数据 -type f", desktop_check)
                self.assertIn("test ! -w", desktop_check)
                self.assertEqual(desktop_check.count("docker exec "), 2)
            finally:
                store.close()

    def test_collect_always_uses_frozen_formal_answer_directory(self):
        tid = "d" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(
                    tid,
                    "dual",
                    ["apple", "banana"],
                    submission_mode="both",
                )
                store.add_seat(
                    tid,
                    7,
                    "alice",
                    "token",
                    "pass1234",
                    "submit-token",
                    "alice",
                    "seat-d-7",
                    "172.18.0.2",
                )
                store.add_web_submission(
                    tid,
                    7,
                    "apple",
                    '#include <cstdio>\nint main(){freopen("apple.in","r",stdin);'
                    'freopen("apple.out","w",stdout);return 7;}\n',
                )
                store.set_state(tid, "ready")
                cvm = FakeCVM()
                remote = FakeRemote(("apple", "banana"))
                pipe = Pipeline(
                    pipeline_config(root), cvm, MagicMock(), store, MagicMock()
                )
                pipe._remote = lambda _: remote

                report = pipe.collect(tid)

                self.assertEqual(
                    report["alice"]["apple"]["submission_source"],
                    "deadline_snapshot",
                )
                self.assertEqual(
                    report["alice"]["banana"]["submission_source"],
                    "deadline_snapshot",
                )
                self.assertEqual(report["alice"]["apple"]["status"], "ok")
                self.assertEqual(report["alice"]["banana"]["status"], "ok")
            finally:
                store.close()

    def test_failed_hydro_submit_enters_retryable_error_with_stable_id(self):
        tid = "e" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
                contest = store.get_contest(tid)
                store.add_seat(
                    tid,
                    7,
                    "alice",
                    "token",
                    "pass1234",
                    "submit-token",
                    "alice",
                    "seat-e-7",
                    "172.18.0.2",
                )
                store.set_state(tid, "ready")
                config = pipeline_config(root)
                config["hydro"] = {
                    "submit_enabled": True,
                    "internal_base_url": "http://127.0.0.1:8888",
                    "orchestrator_token": "secret",
                    "submit_lang": "cc",
                }
                remote = FakeRemote()
                pipe = Pipeline(config, FakeCVM(), MagicMock(), store, MagicMock())
                pipe._remote = lambda _: remote

                with patch("services.pipeline.HydroSubmitter") as factory:
                    submitter = factory.return_value
                    submitter.submission_id.return_value = "a" * 64
                    submitter.submit_one.side_effect = [
                        {"ok": False, "error": "temporary failure"},
                        {"ok": True, "rid": "b" * 24},
                    ]
                    with self.assertRaisesRegex(RuntimeError, "回传失败 1 项"):
                        pipe.collect(tid)

                    self.assertFalse(
                        any(
                            "docker rm -f seat-e-7" in command
                            for command in remote.commands
                        ),
                        "回传失败时必须保留冻结容器，供重试收卷",
                    )
                    self.assertEqual(store.get_contest(tid)["state"], "error")

                    # Keep the retry setup explicit after the first confirmed
                    # shutdown before exercising the operator's retry button.
                    pipe.cvm.state = "STOPPED"
                    pipe.collect(tid)

                    self.assertEqual(submitter.submission_id.call_count, 2)
                    self.assertEqual(submitter.submit_one.call_count, 2)
                    self.assertEqual(
                        [call.args[-1] for call in submitter.submit_one.call_args_list],
                        ["a" * 64, "a" * 64],
                    )
                    self.assertFalse(
                        any(
                            "docker rm -f seat-e-7" in command
                            for command in remote.commands
                        ),
                        "回传成功后仍须保留冻结容器直到安全等待结束",
                    )

                self.assertEqual(store.get_contest(tid)["state"], "safe_wait")
            finally:
                store.close()

    def test_collect_reuses_realtime_web_rid_without_duplicate_hydro_submit(self):
        tid = "1" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(
                    tid,
                    "realtime web",
                    ["apple"],
                    {"apple": "P1"},
                    submission_mode="both",
                )
                contest = store.get_contest(tid)
                store.add_seat(
                    tid,
                    7,
                    "alice",
                    "token",
                    "pass1234",
                    "submit-token",
                    "alice",
                    "seat-1-7",
                    "172.18.0.2",
                )
                source = (
                    '#include <cstdio>\nint main(){freopen("apple.in","r",stdin);'
                    'freopen("apple.out","w",stdout);}\n'
                )
                store.set_state(tid, "ready")
                web_row = store.enqueue_web_submission(
                    tid,
                    7,
                    "apple",
                    source,
                    client_nonce="a" * 32,
                    submission_id="b" * 64,
                    submission_session=contest["submission_session"],
                    judge_pid="P1",
                    judge_lang="cc",
                    judge_source=source,
                    issues=[],
                    accepted_at_ms=1_786_080_000_000,
                )
                config = pipeline_config(root)
                config["hydro"] = {
                    "submit_enabled": True,
                    "internal_base_url": "http://127.0.0.1:8888",
                    "orchestrator_token": "secret",
                    "submit_lang": "cc",
                }
                remote = FakeRemote(("apple",))
                realtime_judge = MagicMock()

                def ensure_after_freeze(_row_id):
                    self.assertTrue(
                        any(
                            "docker pause" in command
                            for command in remote.commands
                        )
                    )
                    return {
                        **web_row,
                        "rid": "9" * 24,
                        "judge_state": "submitted",
                        "judge_issues": "[]",
                    }

                realtime_judge.ensure.side_effect = ensure_after_freeze
                pipe = Pipeline(
                    config,
                    FakeCVM(),
                    MagicMock(),
                    store,
                    MagicMock(),
                    realtime_judge=realtime_judge,
                )
                pipe._remote = lambda _: remote

                with patch("services.pipeline.HydroSubmitter") as factory:
                    report = pipe.collect(tid)

                self.assertEqual(report["alice"]["apple"]["status"], "ok")
                realtime_judge.ensure.assert_called_once_with(web_row["id"])
                factory.return_value.submit_one.assert_not_called()
                run_dir = next((root / "collected" / tid).iterdir())
                submit_log = json.loads(
                    (run_dir / "submit_log.json").read_text(encoding="utf-8")
                )
                logged = submit_log["alice"]["apple"]
                self.assertEqual(logged["rid"], "9" * 24)
                self.assertTrue(logged["reused_realtime"])
                self.assertFalse(logged["enforced_zero"])
            finally:
                store.close()

    def test_collect_missing_formal_file_creates_auditable_zero_final_record(self):
        tid = "2" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(
                    tid,
                    "web missing",
                    ["apple"],
                    {"apple": "P1"},
                    submission_mode="web",
                )
                store.add_seat(
                    tid,
                    7,
                    "alice",
                    "token",
                    "pass1234",
                    "submit-token",
                    "alice",
                    "seat-2-7",
                    "172.18.0.2",
                )
                store.set_state(tid, "ready")
                config = pipeline_config(root)
                config["hydro"] = {
                    "submit_enabled": True,
                    "internal_base_url": "http://127.0.0.1:8888",
                    "orchestrator_token": "secret",
                    "submit_lang": "cc",
                }
                remote = FakeRemote(())
                pipe = Pipeline(
                    config, FakeCVM(), MagicMock(), store, MagicMock()
                )
                pipe._remote = lambda _: remote

                with patch("services.pipeline.HydroSubmitter") as factory:
                    factory.return_value.submission_id.return_value = "c" * 64
                    factory.return_value.submit_one.return_value = {
                        "ok": True,
                        "rid": "d" * 24,
                    }
                    report = pipe.collect(tid)

                self.assertEqual(report["alice"]["apple"]["status"], "missing")
                factory.return_value.submit_one.assert_called_once()
                run_dir = next((root / "collected" / tid).iterdir())
                submit_log = json.loads(
                    (run_dir / "submit_log.json").read_text(encoding="utf-8")
                )
                logged = submit_log["alice"]["apple"]
                self.assertTrue(logged["ok"])
                self.assertTrue(logged["enforced_zero"])
                self.assertEqual(logged["rid"], "d" * 24)
            finally:
                store.close()

    def test_safe_wait_preserves_frozen_seat_until_grace_then_stops_vm(self):
        tid = "3" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(
                    tid,
                    "delayed shutdown",
                    ["apple"],
                    {"apple": "P1"},
                    begin_at_ms=500_000,
                    end_at_ms=1_000_000,
                    hydro_rule="oi",
                )
                store.add_seat(
                    tid,
                    7,
                    "alice",
                    "token",
                    "pass1234",
                    "submit-token",
                    "alice",
                    "seat-3-7",
                    "172.18.0.2",
                )
                store.set_state(tid, "ready")
                config = pipeline_config(root)
                config["orchestrator"]["shutdown_grace_minutes"] = 1
                cvm = FakeCVM()
                remote = FakeRemote(("apple",))
                pipe = Pipeline(config, cvm, MagicMock(), store, MagicMock())
                pipe._remote = lambda _: remote

                with patch("services.pipeline.time.time", return_value=1000):
                    pipe.collect(tid)

                waiting = store.get_contest(tid)
                self.assertEqual(waiting["state"], "safe_wait")
                self.assertEqual(waiting["shutdown_after_ms"], 1_060_000)
                self.assertFalse(
                    any("docker rm -f seat-3-7" in c for c in remote.commands)
                )
                with patch("services.pipeline.time.time", return_value=1059):
                    result = pipe.finish_safe_wait(tid)
                self.assertFalse(result["ended"])
                self.assertEqual(cvm.state, "RUNNING")

                with patch("services.pipeline.time.time", return_value=1061):
                    result = pipe.finish_safe_wait(tid)

                self.assertTrue(result["ended"])
                self.assertEqual(store.get_contest(tid)["state"], "safe_ended")
                self.assertEqual(cvm.state, "STOPPED")
                self.assertTrue(
                    any("docker rm -f seat-3-7" in c for c in remote.commands)
                )
            finally:
                store.close()

    def test_safe_wait_refuses_shutdown_when_collection_evidence_is_tampered(self):
        tid = "4" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(str(root / "state.db"))
            try:
                store.upsert_contest(
                    tid,
                    "tamper evidence",
                    ["apple"],
                    {"apple": "P1"},
                    begin_at_ms=500_000,
                    end_at_ms=1_000_000,
                    hydro_rule="oi",
                )
                store.add_seat(
                    tid,
                    7,
                    "alice",
                    "token",
                    "pass1234",
                    "submit-token",
                    "alice",
                    "seat-4-7",
                    "172.18.0.2",
                )
                store.set_state(tid, "ready")
                config = pipeline_config(root)
                config["orchestrator"]["shutdown_grace_minutes"] = 1
                cvm = FakeCVM()
                pipe = Pipeline(config, cvm, MagicMock(), store, MagicMock())
                pipe._remote = lambda _: FakeRemote(("apple",))
                with patch("services.pipeline.time.time", return_value=1000):
                    pipe.collect(tid)
                contest = store.get_contest(tid)
                report_path = Path(contest["collection_dir"]) / "report.json"
                report_path.write_text("{}", encoding="utf-8")

                with patch("services.pipeline.time.time", return_value=1061):
                    with self.assertRaisesRegex(RuntimeError, "回收证据文件校验失败"):
                        pipe.finish_safe_wait(tid)

                self.assertEqual(store.get_contest(tid)["state"], "safe_wait")
                self.assertEqual(cvm.state, "RUNNING")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
