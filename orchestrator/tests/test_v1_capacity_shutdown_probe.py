import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "v1_capacity_shutdown_probe.py"
BUILDER = ROOT / "scripts" / "build_v1_capacity_shutdown_probe.py"
spec = importlib.util.spec_from_file_location("v1_capacity_shutdown_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(probe)
build_spec = importlib.util.spec_from_file_location("build_v1_capacity_shutdown_probe", BUILDER)
builder = importlib.util.module_from_spec(build_spec); assert build_spec.loader; build_spec.loader.exec_module(builder)


def config():
    return {
        "schema_version": 1, "health_url": "https://exam.example/healthz",
        "docker_socket": "/var/run/docker.sock", "controller_container_id": "a" * 64,
        "controller_image_id": "sha256:" + "b" * 64,
        "controller_baseline": {"pid": 123, "restart_count": 0, "started_at": "2026-08-13T00:00:00Z"},
    }


def controller(row):
    return {
        "Id": row["controller_container_id"], "Image": row["controller_image_id"], "RestartCount": 0,
        "State": {"Running": True, "Restarting": False, "Pid": 123,
                  "StartedAt": "2026-08-13T00:00:00Z"},
    }


def health():
    return {
        "ok": True, "active_seats": 0,
        "desktop_access": {"enabled": True, "management_healthy": True,
                           "desired_open": False, "closed": True, "healthy": True,
                           "managed_count": 0, "conflict_count": 0, "instance_state": "STOPPED"},
        "realtime_judge": {"thread_alive": True, "running": True, "error_count": 0,
                           "queue_counts": {"pending": 0, "sending": 0, "retry": 0,
                                            "permanent_failed": 0, "ambiguous": 0, "submitted": 45}},
        "seat_notifications": {"healthy": True, "counts": {"pending": 0, "retry": 0,
            "permanent_failed": 0, "untracked": 0, "missing_resource": 0,
            "invalid_pool": 0, "sent": 15}},
    }


class ShutdownProbeTests(unittest.TestCase):
    def test_collect_derives_zero_terminal_fact(self):
        row = probe.validate_config(config())
        with mock.patch.object(probe, "docker_get", return_value=controller(row)), \
                mock.patch.object(probe, "health_get", return_value=health()):
            result = probe.collect(row)
        self.assertEqual(result["cloud_state"], "STOPPED")
        self.assertEqual(result["active_seats"], 0)
        self.assertEqual(result["delivery_queues"], 0)

    def test_collect_rejects_restart_active_seat_or_queue(self):
        row = probe.validate_config(config())
        restarted = controller(row); restarted["RestartCount"] = 1
        with mock.patch.object(probe, "docker_get", return_value=restarted):
            with self.assertRaisesRegex(probe.ShutdownProbeError, "restart state differs"):
                probe.collect(row)
        active = health(); active["active_seats"] = 1
        with mock.patch.object(probe, "docker_get", return_value=controller(row)), \
                mock.patch.object(probe, "health_get", return_value=active):
            with self.assertRaisesRegex(probe.ShutdownProbeError, "semantics differ"):
                probe.collect(row)
        queued = health(); queued["realtime_judge"]["queue_counts"]["retry"] = 1
        with mock.patch.object(probe, "docker_get", return_value=controller(row)), \
                mock.patch.object(probe, "health_get", return_value=queued):
            with self.assertRaisesRegex(probe.ShutdownProbeError, "queues are not empty"):
                probe.collect(row)

    def test_builder_freezes_exact_config(self):
        raw = builder.render(probe.validate_config(config()))
        text = raw.decode(); self.assertNotIn(builder.MARKER, text)
        self.assertIn("controller_container_id", text); compile(text, "<shutdown-probe-test>", "exec")


if __name__ == "__main__":
    unittest.main()
