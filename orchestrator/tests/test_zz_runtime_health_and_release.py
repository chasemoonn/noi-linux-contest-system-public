"""Focused guards for per-contest release timing and realtime health."""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


_runtime: Path | None = None
if "main" not in sys.modules:
    # Keep generated config/database files out of the source tree.  Besides
    # making the test work with a read-only application image, this prevents a
    # crashed test run from leaking its temporary config into release archives.
    _runtime = Path(tempfile.mkdtemp(prefix="noi-runtime-health-tests-"))
    service_root = _runtime.resolve().as_posix()
    if os.name == "nt":
        service_root = "/" + service_root.split(":/", 1)[1]
    config_path = _runtime / "config.yaml"
    config_path.write_text(
        f"""
cloud:
  provider: aliyun
  aliyun:
    access_key_id: test
    access_key_secret: test
    region_id: cn-test
    instance_id: i-test
contest_server:
  ssh_user: root
  ssh_key: /keys/test
  strict_host_key: false
  seats_root: /data/seats
  docker_image: noi-linux-official:2.0
  docker_network: seats
hydro:
  public_base_url: https://oj.example.test
  internal_base_url: http://127.0.0.1:8888
  mongo_uri: mongodb://127.0.0.1:27017/test
  domain_id: system
  submit_enabled: false
orchestrator:
  admin_password: 1234567890123456
  db: {service_root}/state.db
  collected_dir: {service_root}/collected
  materials_dir: {service_root}/materials
  public_base_url: https://exam.example.test
  prepare_before_minutes: 15
frontend_proxy:
  provider: none
""",
        encoding="utf-8",
    )
    os.environ["ORCHESTRATOR_CONFIG"] = str(config_path)

    def _cleanup_runtime() -> None:
        loaded_main = sys.modules.get("main")
        if loaded_main is not None:
            loaded_main.store.close()
        shutil.rmtree(_runtime, ignore_errors=True)

    atexit.register(_cleanup_runtime)
    with mock.patch("services.cloud.make_cvm", return_value=mock.Mock()):
        import main
else:
    import main


class ReleaseTimingTests(unittest.TestCase):
    def test_ten_minute_contest_uses_dynamic_notification_and_pending_text(self):
        contest = {
            "tid": "a" * 24,
            "title": "ten minute release",
            "state": "ready",
            "submission_mode": "both",
            "release_lead_minutes": 10,
        }
        fake_store = mock.Mock()
        fake_store.seat_pool.return_value = {
            "state": {
                "seats": [
                    {"slot_no": 1, "uid": 7, "state": "released"}
                ]
            }
        }
        fake_store.seat_pool_resource.return_value = {
            "token": "seat-token",
            "candidate": "CSP001",
            "vnc_pass": "password",
            "credential_revision": 1,
        }
        fake_store.queue_seat_notification.return_value = {"state": "pending"}
        fake_store.contests.return_value = [contest]
        fake_store.seat_by_uname.return_value = {
            "uid": 7,
            "token": "seat-token",
            "submit_token": "submit-token",
            "vnc_pass": "password",
        }
        fake_store.seat_pool_assignment.return_value = {"state": "reserved"}
        fake_notifier = mock.Mock()
        fake_notifier.notification_id.return_value = "notification-1"
        fake_notifier.send_seat_ready.return_value = {"ok": True}

        with mock.patch.object(main, "store", fake_store), mock.patch.object(
            main, "notifier", fake_notifier
        ), mock.patch.object(
            main.cvm, "status", return_value=("RUNNING", "198.51.100.10")
        ):
            main._notify_released_seats(contest)
            available_at = fake_notifier.send_seat_ready.call_args.kwargs[
                "available_at"
            ]
            self.assertEqual(available_at, "比赛开始前 10 分钟起可登录")

            fake_store.seat_pool.return_value = None
            with mock.patch.object(main.hydro, "verify_login", return_value=True):
                response = main.student_query("alice", "secret")

        self.assertIn("比赛开始前 10 分钟开放", response.body.decode("utf-8"))

    def test_corrupted_title_is_sanitized_before_notification(self):
        contest = {
            "tid": "f" * 24,
            "title": "[????????] NOI Linux ?????? 20260809T171805",
            "state": "ready",
            "submission_mode": "both",
            "release_lead_minutes": 5,
        }
        fake_store = mock.Mock()
        fake_store.seat_pool.return_value = {
            "state": {
                "seats": [{"slot_no": 1, "uid": 7, "state": "released"}]
            }
        }
        fake_store.seat_pool_resource.return_value = {
            "token": "seat-token",
            "candidate": "CSP001",
            "vnc_pass": "password",
            "credential_revision": 1,
        }
        fake_store.queue_seat_notification.return_value = {"state": "pending"}
        fake_notifier = mock.Mock()
        fake_notifier.notification_id.return_value = "notification-1"
        fake_notifier.send_seat_ready.return_value = {"ok": True}

        with mock.patch.object(main, "store", fake_store), mock.patch.object(
            main, "notifier", fake_notifier
        ), mock.patch.object(
            main.cvm, "status", return_value=("RUNNING", "198.51.100.10")
        ):
            main._notify_released_seats(contest)

        self.assertEqual(
            fake_notifier.send_seat_ready.call_args.kwargs["contest_title"],
            "本场比赛（标题显示异常，不影响登录和提交）",
        )

    def test_failed_seat_notification_is_retried_on_the_next_tick(self):
        contest = {
            "tid": "b" * 24,
            "title": "retry notification",
            "state": "ready",
            "submission_mode": "both",
            "release_lead_minutes": 5,
        }
        fake_store = mock.Mock()
        fake_store.seat_pool.return_value = {
            "state": {"seats": [{"slot_no": 1, "uid": 7, "state": "released"}]}
        }
        fake_store.seat_pool_resource.return_value = {
            "token": "seat-token",
            "candidate": "CSP001",
            "vnc_pass": "password",
            "credential_revision": 1,
        }
        fake_store.queue_seat_notification.return_value = {"state": "error"}
        fake_notifier = mock.Mock()
        fake_notifier.notification_id.return_value = "notification-1"
        fake_notifier.send_seat_ready.side_effect = [
            RuntimeError("temporary Hydro outage"),
            {"ok": True},
        ]

        with mock.patch.object(main, "store", fake_store), mock.patch.object(
            main, "notifier", fake_notifier
        ), mock.patch.object(
            main.cvm, "status", return_value=("RUNNING", "198.51.100.10")
        ):
            main._notify_released_seats(contest)
            main._notify_released_seats(contest)

        self.assertEqual(fake_notifier.send_seat_ready.call_count, 2)
        self.assertEqual(fake_store.mark_seat_notification.call_count, 2)
        first = fake_store.mark_seat_notification.call_args_list[0]
        second = fake_store.mark_seat_notification.call_args_list[1]
        self.assertFalse(first.kwargs["sent"])
        self.assertTrue(second.kwargs["sent"])

    def test_permanent_provider_rejection_is_not_retried(self):
        contest = {
            "tid": "d" * 24,
            "title": "permanent notification failure",
            "release_lead_minutes": 5,
        }
        fake_store = mock.Mock()
        fake_store.seat_pool.return_value = {
            "state": {"seats": [{"slot_no": 1, "uid": 7, "state": "released"}]}
        }
        fake_store.seat_pool_resource.return_value = {
            "token": "seat-token",
            "candidate": "CSP001",
            "vnc_pass": "password",
            "credential_revision": 1,
        }
        fake_store.queue_seat_notification.side_effect = [
            {"state": "pending", "attempts": 0},
            {"state": "permanent_failed", "attempts": 1},
        ]
        fake_notifier = mock.Mock()
        fake_notifier.notification_id.return_value = "notification-1"
        fake_notifier.send_seat_ready.return_value = {
            "ok": False,
            "retryable": False,
        }

        with mock.patch.object(main, "store", fake_store), mock.patch.object(
            main, "notifier", fake_notifier
        ), mock.patch.object(
            main.cvm, "status", return_value=("RUNNING", "198.51.100.10")
        ):
            main._notify_released_seats(contest)
            main._notify_released_seats(contest)

        fake_notifier.send_seat_ready.assert_called_once()
        self.assertFalse(
            fake_store.mark_seat_notification.call_args.kwargs["retryable"]
        )

    def test_notification_batch_is_capped_and_public_gateway_skips_cloud(self):
        contest = {
            "tid": "e" * 24,
            "title": "bounded notification batch",
            "release_lead_minutes": 5,
        }
        fake_store = mock.Mock()
        fake_store.seat_pool.return_value = {
            "state": {
                "seats": [
                    {"slot_no": slot, "uid": slot + 10, "state": "released"}
                    for slot in range(1, 6)
                ]
            }
        }
        fake_store.seat_pool_resource.side_effect = lambda _tid, slot: {
            "token": f"seat-token-{slot}",
            "candidate": f"CSP{slot:03d}",
            "vnc_pass": f"password-{slot}",
            "credential_revision": 1,
        }
        fake_store.queue_seat_notification.return_value = {
            "state": "pending",
            "attempts": 0,
        }
        fake_notifier = mock.Mock()
        fake_notifier.notification_id.side_effect = (
            lambda _tid, uid, _revision: f"notification-{uid}"
        )
        fake_notifier.send_seat_ready.return_value = {"ok": True}

        configured = {
            "contest_server": {
                "gateway_public_base_url": "https://exam.example.test"
            }
        }
        with mock.patch.object(main, "store", fake_store), mock.patch.object(
            main, "notifier", fake_notifier
        ), mock.patch.object(main, "cfg", configured), mock.patch.object(
            main.cvm, "status"
        ) as cloud_status:
            main._notify_released_seats(contest)

        self.assertEqual(fake_store.queue_seat_notification.call_count, 5)
        self.assertEqual(fake_notifier.send_seat_ready.call_count, 3)
        self.assertEqual(fake_store.mark_seat_notification.call_count, 3)
        cloud_status.assert_not_called()

    def test_one_student_failure_and_failure_write_do_not_block_the_next(self):
        contest = {
            "tid": "c" * 24,
            "title": "isolated notification failures",
            "state": "ready",
            "submission_mode": "both",
            "release_lead_minutes": 5,
        }
        fake_store = mock.Mock()
        fake_store.seat_pool.return_value = {
            "state": {
                "seats": [
                    {"slot_no": 1, "uid": 7, "state": "released"},
                    {"slot_no": 2, "uid": 8, "state": "released"},
                ]
            }
        }
        fake_store.seat_pool_resource.side_effect = lambda _tid, slot: {
            "token": f"desktop-secret-{slot}",
            "candidate": f"CSP00{slot}",
            "vnc_pass": f"password-secret-{slot}",
            "credential_revision": 1,
        }
        fake_store.queue_seat_notification.side_effect = [
            {"state": "pending"},
            {"state": "pending"},
        ]
        # Persisting the first failure also fails.  The second student must
        # still be notified and have the successful state recorded.
        fake_store.mark_seat_notification.side_effect = [
            RuntimeError("database detail that must not be logged"),
            None,
        ]
        fake_notifier = mock.Mock()
        fake_notifier.notification_id.side_effect = ["notification-1", "notification-2"]
        fake_notifier.send_seat_ready.side_effect = [
            RuntimeError("https://secret.invalid/password-secret-1"),
            {"ok": True},
        ]

        with mock.patch.object(main, "store", fake_store), mock.patch.object(
            main, "notifier", fake_notifier
        ), mock.patch.object(
            main.cvm, "status", return_value=("RUNNING", "198.51.100.10")
        ), self.assertLogs("orchestrator", level="ERROR") as captured:
            main._notify_released_seats(contest)

        self.assertEqual(fake_notifier.send_seat_ready.call_count, 2)
        self.assertEqual(fake_store.mark_seat_notification.call_count, 2)
        self.assertTrue(
            fake_store.mark_seat_notification.call_args_list[1].kwargs["sent"]
        )
        logged = "\n".join(captured.output)
        self.assertIn("error_type=RuntimeError", logged)
        self.assertNotIn("password-secret", logged)
        self.assertNotIn("secret.invalid", logged)
        self.assertNotIn("database detail", logged)


class DisabledRealtimeHealthTests(unittest.TestCase):
    def test_active_web_or_both_contest_fails_closed_without_worker(self):
        for mode, state in (("web", "registered"), ("both", "collecting")):
            with self.subTest(mode=mode, state=state):
                fake_store = mock.Mock()
                fake_store.seat_notification_health.return_value = {
                    "counts": {"pending": 0, "retry": 0, "sent": 0},
                    "max_retry_attempts": 0,
                    "oldest_retry_at": "",
                }
                fake_store.contests.return_value = [
                    {
                        "tid": mode * 12,
                        "state": state,
                        "submission_mode": mode,
                    }
                ]
                with mock.patch.object(main, "store", fake_store), mock.patch.object(
                    main, "realtime_judge", None
                ):
                    response = main.healthz()
                payload = json.loads(response.body)
                self.assertEqual(response.status_code, 503)
                self.assertFalse(payload["ok"])
                self.assertEqual(
                    payload["active_realtime_contests"][0]["submission_mode"],
                    mode,
                )

    def test_folder_only_active_contests_stay_healthy_without_worker(self):
        fake_store = mock.Mock()
        fake_store.seat_notification_health.return_value = {
            "counts": {
                "pending": 1,
                "retry": 1,
                "permanent_failed": 1,
                "sent": 0,
                "untracked": 1,
                "missing_resource": 1,
                "invalid_pool": 1,
            },
            "max_retry_attempts": 1,
            "oldest_retry_at": "2026-08-08 13:00:00",
        }
        fake_store.contests.return_value = [
            {
                "tid": "f" * 24,
                "state": "ready",
                "submission_mode": "folder",
            },
            {
                "tid": "w" * 24,
                "state": "done",
                "submission_mode": "web",
            },
        ]
        with mock.patch.object(main, "store", fake_store), mock.patch.object(
            main, "realtime_judge", None
        ):
            payload = main.healthz()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["realtime_judge"], "disabled")
        self.assertEqual(payload["active_realtime_contests"], [])

    def test_active_outstanding_notification_makes_health_unhealthy(self):
        for state in (
            "pending",
            "retry",
            "permanent_failed",
            "untracked",
            "missing_resource",
            "invalid_pool",
        ):
            with self.subTest(state=state):
                fake_store = mock.Mock()
                fake_store.contests.return_value = [
                    {
                        "tid": "n" * 24,
                        "state": "ready",
                        "submission_mode": "folder",
                    }
                ]
                counts = {"pending": 0, "retry": 0, "sent": 2}
                counts[state] = 1
                fake_store.seat_notification_health.return_value = {
                    "counts": counts,
                    "max_retry_attempts": 3 if state == "retry" else 0,
                    "oldest_retry_at": (
                        "2026-08-08 13:00:00" if state == "retry" else ""
                    ),
                }

                with mock.patch.object(main, "store", fake_store), mock.patch.object(
                    main, "notifier", mock.Mock()
                ), mock.patch.object(main, "realtime_judge", None):
                    response = main.healthz()

                payload = json.loads(response.body)
                self.assertEqual(response.status_code, 503)
                self.assertFalse(payload["ok"])
                self.assertFalse(payload["seat_notifications"]["healthy"])
                self.assertEqual(
                    payload["seat_notifications"]["counts"][state], 1
                )
                self.assertNotIn("last_error", payload["seat_notifications"])


if __name__ == "__main__":
    unittest.main()
