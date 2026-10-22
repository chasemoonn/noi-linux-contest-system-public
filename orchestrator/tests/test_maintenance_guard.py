import importlib.util
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from services.maintenance import LOCK_PATHS, require_protection, settings, verify_status
from services.pipeline import Pipeline

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/maintenance/maintenance_guard.py"
spec = importlib.util.spec_from_file_location("host_maintenance_guard", SCRIPT)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)

MACHINE = "a" * 32
BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TID = "b" * 24
NONCE = "c" * 32
NOW = 1_800_000_000_000


def lease(**extra):
    return {"schema_version": 1, "dedicated_contest_host": True,
            "machine_id": MACHINE, "boot_id": BOOT, "tid": TID,
            "approved_by": "test-owner", "window_start_ms": NOW - 1000,
            "window_end_ms": NOW + 10_000_000, "maintenance_reviewed_at_ms": NOW,
            "maintenance_review_complete": True, **extra}


class LeasePolicyTests(unittest.TestCase):
    def validate(self, value):
        return host.validate_lease(value, machine_id=MACHINE, boot_id=BOOT, now=NOW)

    def test_valid_bounded_boot_bound_lease(self):
        self.assertEqual(self.validate(lease())["tid"], TID)

    def test_wrong_host_boot_missing_owner_and_missing_review(self):
        for extra in ({"machine_id": "d" * 32}, {"boot_id": "new-boot"},
                      {"approved_by": ""}, {"dedicated_contest_host": False},
                      {"maintenance_review_complete": False}, {"tid": "../old"}):
            with self.subTest(extra=extra), self.assertRaises(host.GuardError):
                self.validate(lease(**extra))

    def test_expired_future_oversized_and_boolean_time(self):
        for extra in ({"window_end_ms": NOW}, {"window_start_ms": NOW + 1},
                      {"window_end_ms": NOW + host.MAX_WINDOW_MS},
                      {"window_start_ms": True}, {"maintenance_reviewed_at_ms": NOW + 1},
                      {"maintenance_reviewed_at_ms": NOW - 2000},
                      {"window_end_ms": NOW + 179_000}):
            with self.subTest(extra=extra), self.assertRaises(host.GuardError):
                self.validate(lease(**extra))

    def test_wall_rollback_does_not_extend_lease(self):
        timer = host.Deadline(lease(), wall=NOW, boot=100)
        self.assertEqual(timer.remaining_ms(wall=NOW - 60_000, boot=110), 9_945_000)
        self.assertEqual(timer.remaining_ms(wall=NOW - 60_000, boot=10100), 0)

    def test_wall_forward_expires_early(self):
        timer = host.Deadline(lease(), wall=NOW, boot=100)
        self.assertEqual(timer.remaining_ms(wall=NOW + 10_000_000, boot=101), 0)

    def test_suspend_time_is_included(self):
        with patch.object(host.time, "clock_gettime", return_value=500, create=True) as call:
            with patch.object(host.time, "CLOCK_BOOTTIME", 7, create=True):
                self.assertEqual(host.boot_seconds(), 500)
                call.assert_called_once_with(7)


class StateMachineTests(unittest.TestCase):
    def make(self, close=None):
        self.locks = MagicMock()
        self.events = []
        self.locks.release.side_effect = lambda: self.events.append("unlock")
        return host.Guard(lease(), self.locks, wall=lambda: NOW, boot=lambda: 100,
                          close=close or (lambda: self.events.append("close_gateway")))

    def test_close_before_unlock_no_student_container_action(self):
        guard = self.make()
        self.assertEqual(guard.status(NONCE)["locks"], LOCK_PATHS)
        guard.finish()
        self.assertEqual(self.events, ["close_gateway", "unlock"])
        with self.assertRaises(host.GuardError):
            guard.status(NONCE)

    def test_gateway_failure_does_not_suspend_updates_forever(self):
        def fail():
            raise RuntimeError("gateway failure")
        guard = self.make(fail)
        with self.assertRaises(RuntimeError):
            guard.finish()
        self.assertEqual(self.events, ["unlock"])

    def test_lock_replacement_fails_status(self):
        guard = self.make()
        self.locks.verify.side_effect = host.GuardError("replaced")
        with self.assertRaises(host.GuardError):
            guard.status(NONCE)

    def test_expired_guard_never_returns_protected(self):
        guard = self.make()
        guard.boot = lambda: 10100
        with self.assertRaises(host.GuardError):
            guard.status(NONCE)

    def test_malformed_or_mutating_requests_cannot_release_or_extend(self):
        guard = self.make()
        for data in (b"[]", b"null", b"0", b"{", b"x" * 1025,
                     b'{"nonce":42,"command":"status"}',
                     json.dumps({"command": "release", "nonce": NONCE}).encode(),
                     json.dumps({"command": "renew", "nonce": NONCE}).encode()):
            with self.subTest(data=data[:40]), self.assertRaises((ValueError, host.GuardError)):
                host.status_request(data, guard)
            self.locks.release.assert_not_called()
        result = host.status_request(json.dumps({"command": "status", "nonce": NONCE}).encode(), guard)
        self.assertEqual(result["nonce"], NONCE)

    def test_supervisor_requires_watchdog_correct_pid_and_no_auto_restart(self):
        baseline = f"MainPID={os.getpid()}\nType=notify\nWatchdogUSec=15s\nRuntimeMaxUSec=4h\nRestart=no\nInvocationID={'f' * 32}"
        with patch.dict(host.os.environ, {"INVOCATION_ID": "f" * 32}):
            with patch.object(host, "command", return_value=baseline):
                host.verify_supervisor()
            for text in (baseline.replace("15s", "0"), baseline.replace("Restart=no", "Restart=always"),
                         baseline.replace(f"MainPID={os.getpid()}", "MainPID=1"), baseline.replace("4h", "infinity")):
                with patch.object(host, "command", return_value=text), self.assertRaises(host.GuardError):
                    host.verify_supervisor()

    def test_unit_is_nonpersistent_and_has_independent_crash_cleanup(self):
        unit = SCRIPT.with_name("noi-maintenance-guard.service").read_text()
        self.assertIn("RuntimeMaxSec=4h", unit)
        self.assertIn("Restart=no", unit)
        self.assertIn("ExecStopPost=", unit)
        self.assertIn("WatchdogSec=15s", unit)
        self.assertIn("Type=notify", unit)
        self.assertNotIn("\n[Install]", unit)
        self.assertNotIn("WantedBy=", unit)
        self.assertIn("Before=nginx.service apt-daily.service apt-daily-upgrade.service", unit)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.states = {unit: {"LoadState": "loaded", "ActiveState": "inactive",
                              "UnitFileState": "enabled", "ControlPID": "0"} for unit in host.UNITS}
        self.states.update({unit: {"LoadState": "loaded", "ActiveState": "active", "ControlPID": "0"}
                            for unit in ("nginx.service", "docker.service")})
        self.apt = "ROOT='/'\nSTATUS='/var/lib/dpkg/status'\nLISTS='/var/lib/apt/lists/'\nARCHIVES='/var/cache/apt/archives/'"

    def run_preflight(self, audit="", reboot=False, release='ID=ubuntu\nVERSION_ID="22.04"'):
        def fake_command(*args, **kwargs):
            return audit if args[0].endswith("/dpkg") else self.apt
        with patch.object(host.Path, "read_text", return_value=release), \
                patch.object(host.Path, "exists", return_value=reboot), \
                patch.object(host, "unit_state", side_effect=lambda name: self.states[name]), \
                patch.object(host, "command", side_effect=fake_command) as call:
            result = host.preflight()
        self.assertTrue(all(args[0][0] in ("/usr/bin/dpkg", "/usr/bin/apt-config") for args in call.call_args_list))
        return result

    def test_disabled_masked_and_absent_custom_config_unchanged(self):
        for unit in host.UNITS:
            self.states[unit]["UnitFileState"] = "masked"
            self.states[unit]["LoadState"] = "masked"
        self.assertEqual(self.run_preflight()["units"], {name: self.states[name] for name in host.UNITS})

    def test_active_update_boot_catchup_and_manual_service_start_refused(self):
        for active in ("active", "activating", "deactivating", "failed", "unknown"):
            self.states["apt-daily-upgrade.service"]["ActiveState"] = active
            with self.subTest(active=active), self.assertRaises(host.GuardError):
                self.run_preflight()

    def test_pending_reboot_dirty_dpkg_and_wrong_distribution_refused(self):
        for kwargs in ({"reboot": True}, {"audit": "unconfigured package"},
                       {"release": 'ID=ubuntu\nVERSION_ID="24.04"'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(host.GuardError):
                self.run_preflight(**kwargs)

    def test_custom_apt_paths_and_no_locking_refused(self):
        baseline = self.apt
        for apt in (baseline.replace("/var/lib/dpkg/status", "/custom/status"), baseline + "\nNOLOCK='true'"):
            self.apt = apt
            with self.assertRaises(host.GuardError):
                self.run_preflight()

    def test_unknown_unit_and_service_reload_refused(self):
        self.states["apt-daily.timer"]["LoadState"] = "not-found"
        with self.assertRaises(host.GuardError):
            self.run_preflight()
        self.states["apt-daily.timer"]["LoadState"] = "loaded"
        self.states["nginx.service"]["ControlPID"] = "321"
        with self.assertRaises(host.GuardError):
            self.run_preflight()


@unittest.skipUnless(sys.platform == "linux", "real POSIX locks require Linux CI")
class RealLinuxLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = [Path(self.temp.name) / str(i) for i in range(4)]
        for path in self.paths:
            path.write_bytes(b"original lock content\n")
        self.locks = host.PackageLocks(self.paths)
        self.addCleanup(self.locks.release)

    def contender(self, path):
        return subprocess.run([sys.executable, "-c",
            "import fcntl,sys; f=open(sys.argv[1],'r+'); fcntl.lockf(f,fcntl.LOCK_EX|fcntl.LOCK_NB)", str(path)],
            capture_output=True, timeout=5).returncode

    def test_all_locks_exclude_separate_apt_style_process_then_restore(self):
        self.locks.acquire()
        for path in self.paths:
            self.assertNotEqual(self.contender(path), 0)
        self.locks.release()
        for path in self.paths:
            self.assertEqual(self.contender(path), 0)
            self.assertEqual(path.read_bytes(), b"original lock content\n")

    def test_inflight_transaction_not_killed_and_partial_acquire_rolls_back(self):
        child = subprocess.Popen([sys.executable, "-u", "-c",
            "import fcntl,sys; f=open(sys.argv[1],'r+'); fcntl.lockf(f,fcntl.LOCK_EX); print('held',flush=True); sys.stdin.read()",
            str(self.paths[2])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            with self.assertRaises(OSError):
                self.locks.acquire()
            self.assertIsNone(child.poll())
            self.assertEqual(self.contender(self.paths[0]), 0)
        finally:
            child.communicate("", timeout=5)
        self.locks.acquire()

    def test_missing_lock_never_created(self):
        self.paths[2].unlink()
        with self.assertRaises(FileNotFoundError):
            self.locks.acquire()
        self.assertFalse(self.paths[2].exists())
        self.assertEqual(self.contender(self.paths[0]), 0)

    def test_symlink_and_hardlink_refused(self):
        self.paths[1].unlink()
        self.paths[1].symlink_to(self.paths[0])
        with self.assertRaises(OSError):
            self.locks.acquire()
        self.paths[1].unlink()
        os.link(self.paths[0], self.paths[1])
        with self.assertRaises(host.GuardError):
            self.locks.acquire()

    def test_inode_replacement_refused(self):
        self.locks.acquire()
        self.paths[0].unlink()
        self.paths[0].write_bytes(b"replacement")
        with self.assertRaises(host.GuardError):
            self.locks.verify()

    def test_process_death_releases_without_permanent_policy_changes(self):
        child = subprocess.Popen([sys.executable, "-u", "-c",
            "import fcntl,sys; f=open(sys.argv[1],'r+'); fcntl.lockf(f,fcntl.LOCK_EX); print('held',flush=True); sys.stdin.read()",
            str(self.paths[0])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            self.assertNotEqual(self.contender(self.paths[0]), 0)
            child.kill()
            child.communicate(timeout=5)
            self.assertEqual(self.contender(self.paths[0]), 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


class ControllerGateTests(unittest.TestCase):
    def status(self, **extra):
        return {"schema_version": 1, "state": "protected", "nonce": NONCE,
                "machine_id": MACHINE, "boot_id": BOOT, "tid": TID,
                "pid": 234, "locks": LOCK_PATHS, "checked_at_ms": NOW,
                "expires_at_ms": NOW + 10_000_000, "remaining_ms": 10_000_000, **extra}

    def verify(self, value):
        return verify_status(value, nonce=NONCE, machine_id=MACHINE, tid=TID,
                             now_ms=NOW, required_until_ms=NOW + 5_400_000)

    def test_accepts_only_fresh_complete_lease(self):
        self.assertEqual(self.verify(self.status())["tid"], TID)
        for extra in ({"nonce": "d" * 32}, {"machine_id": "d" * 32}, {"tid": "e" * 24},
                      {"state": "released"}, {"locks": LOCK_PATHS[:3]}, {"checked_at_ms": NOW - 5001},
                      {"remaining_ms": 5_400_000}, {"expires_at_ms": NOW + 5_400_000},
                      {"pid": True}, {"boot_id": "stale"}):
            with self.subTest(extra=extra), self.assertRaises(RuntimeError):
                self.verify(self.status(**extra))

    def test_feature_off_performs_no_remote_action(self):
        remote = MagicMock()
        self.assertIsNone(require_protection(remote, {}))
        remote.run.assert_not_called()

    def test_enabled_requires_pinned_host_boolean(self):
        for value in ({"enabled": "true"}, {"enabled": True}, None):
            with self.assertRaises(ValueError):
                settings({"maintenance_guard": value})

    def test_nonce_and_archive_grace_checked_on_remote_status(self):
        remote = MagicMock()
        remote.run.return_value = json.dumps(self.status())
        with patch("services.maintenance.secrets.token_hex", return_value=NONCE), \
                patch("services.maintenance.time.time", return_value=NOW / 1000):
            server = {"maintenance_guard": {"enabled": True, "machine_id": MACHINE}}
            require_protection(remote, server, tid=TID, end_at_ms=NOW + 3_600_000)
            with self.assertRaises(RuntimeError):
                require_protection(remote, server, tid=TID, end_at_ms=NOW + 3_600_000, grace_minutes=0)
        self.assertIn("status --nonce " + NONCE, remote.run.call_args.args[0])

    def test_shutdown_refuses_without_lease_before_cloud_stop(self):
        cvm = MagicMock()
        cvm.status.return_value = ("RUNNING", "198.51.100.2")
        pipe = Pipeline({}, cvm, MagicMock(), MagicMock(), MagicMock())
        with patch.object(pipe, "_check_maintenance", side_effect=RuntimeError("no lease")):
            with self.assertRaises(RuntimeError):
                pipe._stop_server_best_effort()
        cvm.stop.assert_not_called()

    def test_stopped_host_never_needs_boot_or_lease(self):
        cvm = MagicMock()
        cvm.status.return_value = ("STOPPED", "")
        pipe = Pipeline({}, cvm, MagicMock(), MagicMock(), MagicMock())
        with patch.object(pipe, "_check_maintenance") as gate:
            pipe._stop_server_best_effort()
        gate.assert_not_called()
        cvm.start.assert_not_called()
        cvm.stop.assert_not_called()

    def test_enabled_maintenance_never_automatically_boots_unprepared_vm(self):
        cvm = MagicMock()
        cvm.status.return_value = ("STOPPED", "")
        pipe = Pipeline({"contest_server": {"maintenance_guard": {"enabled": True, "machine_id": MACHINE}}},
                        cvm, MagicMock(), MagicMock(), MagicMock())
        with self.assertRaises(RuntimeError):
            pipe._ensure_server()
        cvm.start.assert_not_called()
        cvm.wait_running.assert_not_called()

    def test_admission_failure_never_opens_desktop_access(self):
        cvm = MagicMock()
        cvm.desktop_access_enabled = True
        pipe = Pipeline({}, cvm, MagicMock(), MagicMock(), MagicMock())
        with patch.object(pipe, "_check_maintenance", side_effect=RuntimeError("missing")):
            with self.assertRaises(RuntimeError):
                pipe._ensure_desktop_access({"tid": TID, "end_at_ms": NOW})
        cvm.ensure_desktop_access.assert_not_called()

    def test_ready_reconciler_latches_lease_failure_and_revokes_access(self):
        cvm = MagicMock()
        cvm.desktop_access_enabled = True
        cvm.revoke_desktop_access.return_value = {"closed": True}
        store = MagicMock()
        pipe = Pipeline({}, cvm, MagicMock(), store, MagicMock())
        with patch.object(pipe, "_desired_desktop_access", return_value=({"tid": TID, "end_at_ms": NOW}, "ready")), \
                patch.object(pipe, "_validate_ready_gateway"), \
                patch.object(pipe, "_check_maintenance", side_effect=RuntimeError("expired")):
            with self.assertRaises(RuntimeError):
                pipe.reconcile_desktop_access()
        self.assertEqual(store.set_state.call_args.args[:2], (TID, "error"))
        cvm.ensure_desktop_access.assert_not_called()
        cvm.revoke_desktop_access.assert_called_once()


def host_fixture(directory, mode):
    """Real daemon/socket/locks; OS probes, systemd and gateway are test doubles.

    Every path is a disposable root-owned test directory. Never run package
    commands or change any host services, including on a root CI runner.
    """
    root = Path(directory)
    paths = tuple(str(root / f"package-{i}") for i in range(4))
    actual_locks, actual_guard = host.PackageLocks, host.Guard
    host.RUNTIME, host.CONFIG = root, root / "manifest.json"
    host.LOCK_PATHS = paths
    host.PackageLocks = lambda: actual_locks(paths)
    host.secure_regular = lambda path: None
    host.verify_supervisor = lambda: None
    host.preflight = lambda: {"fixture": True}
    host.notify = lambda message: None
    host.read_lease = lambda: lease(window_end_ms=host.now_ms() + 45_000 + (1800 if mode == "expiry" else 60_000))
    def closed():
        if mode != "fallback":
            result = subprocess.run([sys.executable, "-c",
                "import fcntl,sys; f=open(sys.argv[1],'r+'); fcntl.lockf(f,fcntl.LOCK_EX|fcntl.LOCK_NB)", paths[0]],
                capture_output=True, timeout=5)
            if result.returncode == 0:
                raise AssertionError("gateway closed after package unlock")
        (root / "closed").write_text("confirmed")
    host.Guard = lambda value, locks: actual_guard(value, locks, close=closed)
    if mode == "fallback":
        host.close_gateway = closed
        sys.argv = ["guard", "close-gateway"]
        host.main()
    else:
        host.serve()


@unittest.skipUnless(sys.platform == "linux" and getattr(os, "geteuid", lambda: -1)() == 0,
                     "daemon fixtures run as root in isolated Linux CI temporary paths")
class LinuxDaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="noi-guard-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        (self.root / "manifest.json").write_text("{}")
        for index in range(4):
            (self.root / f"package-{index}").write_bytes(b"unchanged")
        self.child = None
        self.addCleanup(self.cleanup_child)

    def cleanup_child(self):
        if self.child is not None and self.child.poll() is None:
            self.child.kill()
            self.child.communicate(timeout=5)

    def launch(self, mode):
        env = dict(os.environ, INVOCATION_ID="f" * 32,
                   PYTHONPATH=str(SCRIPT.parents[2] / "orchestrator"))
        return subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()),
                                 "--host-fixture", str(self.root), mode],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def ready(self, mode):
        self.child = self.launch(mode)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if self.child.poll() is not None:
                self.fail(str(self.child.communicate()))
            try:
                with patch.object(host, "RUNTIME", self.root):
                    result = host.query(NONCE)
                self.assertEqual(result["state"], "protected")
                return
            except (OSError, ValueError):
                time.sleep(0.03)
        self.fail("fixture did not become ready")

    def assert_released(self):
        locks = host.PackageLocks(tuple(self.root / f"package-{i}" for i in range(4)))
        locks.acquire()
        locks.release()
        for i in range(4):
            self.assertEqual((self.root / f"package-{i}").read_bytes(), b"unchanged")

    def test_transport_disconnect_and_sigterm_preserve_locks_until_close(self):
        self.ready("normal")
        for data in (b"", b"[]", b"{", json.dumps({"command": "status", "nonce": NONCE}).encode()):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(self.root / "guard.sock"))
                connection.sendall(data)
        with patch.object(host, "RUNTIME", self.root):
            self.assertEqual(host.query(NONCE)["nonce"], NONCE)
        self.child.terminate()
        stdout, stderr = self.child.communicate(timeout=10)
        self.assertEqual(self.child.returncode, 0, stderr)
        self.assertIn('"event": "released"', stdout)
        self.assertTrue((self.root / "closed").exists())
        self.assert_released()

    def test_deadline_recovers_without_controller(self):
        self.ready("expiry")
        stdout, _ = self.child.communicate(timeout=10)
        self.assertIn('"event": "released"', stdout)
        self.assertTrue((self.root / "closed").exists())
        self.assert_released()

    def test_sigkill_leaves_marker_for_independent_stop_post(self):
        self.ready("normal")
        self.child.kill()
        self.child.communicate(timeout=5)
        self.assert_released()
        self.assertFalse((self.root / "closed").exists())
        self.child = self.launch("fallback")
        stdout, stderr = self.child.communicate(timeout=5)
        self.assertEqual(self.child.returncode, 0, stderr)
        self.assertTrue((self.root / "closed").exists())
        self.assertIn('"event": "systemd_close_confirmed"', stdout)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--host-fixture":
        host_fixture(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
