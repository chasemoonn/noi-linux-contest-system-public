import unittest
from unittest.mock import Mock, patch

from services.hydro_notify import HydroNotifier


TOKEN = "test-token-that-is-at-least-thirty-two-characters"


class HydroNotifierTests(unittest.TestCase):
    def notifier(self):
        return HydroNotifier(
            "http://127.0.0.1:8888",
            TOKEN,
            {"exam.example.test"},
        )

    def test_notification_id_is_stable_and_changes_with_seat_revision(self):
        tid = "0" * 24
        first = HydroNotifier.notification_id(tid, 7, "seat-r1")
        retry = HydroNotifier.notification_id(tid, 7, "seat-r1")
        changed = HydroNotifier.notification_id(tid, 7, "seat-r2")

        self.assertEqual(first, retry)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, changed)

    def test_plain_http_is_only_allowed_for_loopback(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            HydroNotifier(
                "http://hydro.internal:8888", TOKEN, {"exam.example.test"}
            )
        HydroNotifier(
            "https://hydro.internal", TOKEN, {"exam.example.test"}
        )

    @patch("services.hydro_notify.requests.post")
    def test_sends_only_the_fixed_seat_ready_schema_to_private_route(self, post):
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = {
            "notification_id": "a" * 64,
            "message_id": "1" * 24,
        }
        post.return_value = response

        result = self.notifier().send_seat_ready(
            uid=7,
            notification_id="a" * 64,
            contest_title="CSP-J 模拟赛",
            desktop_url=(
                "https://exam.example.test:443/s/personal/vnc.html"
                "?autoconnect=true"
            ),
            candidate="CSPJ-0007",
            student_password="student-seat-password",
            available_at="2026-08-08 13:55",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            post.call_args.args[0],
            "http://127.0.0.1:8888/orchestrator/submit/notify",
        )
        sent = post.call_args.kwargs["json"]
        self.assertEqual(
            set(sent),
            {
                "notification_id",
                "purpose",
                "uid",
                "contest_title",
                "desktop_url",
                "candidate",
                "student_password",
                "available_at",
            },
        )
        self.assertEqual(sent["purpose"], "seat_ready")
        self.assertEqual(
            sent["desktop_url"],
            "https://exam.example.test/s/personal/vnc.html?autoconnect=true",
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["X-Orchestrator-Token"], TOKEN
        )
        self.assertEqual(post.call_args.kwargs["timeout"], (2.0, 5.0))

    @patch("services.hydro_notify.requests.post")
    def test_request_timeout_is_locally_configurable(self, post):
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = {
            "notification_id": "f" * 64,
            "message_id": "1" * 24,
        }
        post.return_value = response
        notifier = HydroNotifier(
            "http://127.0.0.1:8888",
            TOKEN,
            {"exam.example.test"},
            request_timeout=(1, 3),
        )

        notifier.send_seat_ready(
            uid=7,
            notification_id="f" * 64,
            contest_title="Contest",
            desktop_url="https://exam.example.test/s/seat",
            candidate="CSPJ-0007",
            student_password="student-seat-password",
        )

        self.assertEqual(post.call_args.kwargs["timeout"], (1.0, 3.0))

    def test_request_timeout_must_be_a_positive_pair(self):
        for request_timeout in ((0, 5), (2, -1), (2,), [2, 5]):
            with self.subTest(request_timeout=request_timeout):
                with self.assertRaisesRegex(ValueError, "request_timeout"):
                    HydroNotifier(
                        "http://127.0.0.1:8888",
                        TOKEN,
                        {"exam.example.test"},
                        request_timeout=request_timeout,
                    )

    def test_rejects_non_https_or_non_allowlisted_student_link(self):
        notifier = self.notifier()
        invalid = (
            "http://exam.example.test/s/seat",
            "https://oj.example.test/s/seat",
            "https://exam.example.test.evil.example/s/seat",
            "https://root:secret@exam.example.test/s/seat",
            "https://exam.example.test:8443/s/seat",
            "https://exam.example.test/s/seat#secret",
        )
        for desktop_url in invalid:
            with self.subTest(desktop_url=desktop_url):
                with self.assertRaises(ValueError):
                    notifier.send_seat_ready(
                        uid=7,
                        notification_id="b" * 64,
                        contest_title="Contest",
                        desktop_url=desktop_url,
                        candidate="CSPJ-0007",
                        student_password="student-seat-password",
                    )

    def test_api_has_no_admin_or_ssh_password_parameter(self):
        with self.assertRaises(TypeError):
            self.notifier().send_seat_ready(
                uid=7,
                notification_id="c" * 64,
                contest_title="Contest",
                desktop_url="https://exam.example.test/s/seat",
                candidate="CSPJ-0007",
                student_password="student-seat-password",
                ssh_password="must-not-be-sent",
            )

    @patch("services.hydro_notify.requests.post")
    def test_token_repair_and_server_errors_are_retryable(self, post):
        response = Mock()
        response.ok = False
        response.status_code = 403
        response.json.return_value = {"error": "token mismatch"}
        post.return_value = response

        result = self.notifier().send_seat_ready(
            uid=7,
            notification_id="d" * 64,
            contest_title="Contest",
            desktop_url="https://exam.example.test/s/seat",
            candidate="CSPJ-0007",
            student_password="student-seat-password",
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])

    @patch("services.hydro_notify.requests.post")
    def test_id_conflict_is_permanent(self, post):
        response = Mock()
        response.ok = False
        response.status_code = 409
        response.json.return_value = {"error": "notification conflict"}
        post.return_value = response

        result = self.notifier().send_seat_ready(
            uid=7,
            notification_id="e" * 64,
            contest_title="Contest",
            desktop_url="https://exam.example.test/s/seat",
            candidate="CSPJ-0007",
            student_password="student-seat-password",
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["retryable"])


if __name__ == "__main__":
    unittest.main()
