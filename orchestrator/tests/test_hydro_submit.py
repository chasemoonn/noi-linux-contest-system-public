import unittest
from unittest.mock import Mock, patch

from services.hydro_submit import HydroSubmitter


class HydroSubmitterTests(unittest.TestCase):
    def test_submission_id_is_stable_and_scoped_to_run(self):
        first = HydroSubmitter.submission_id("run-a", "0" * 24, 7, "P1")
        retry = HydroSubmitter.submission_id("run-a", "0" * 24, 7, "P1")
        next_run = HydroSubmitter.submission_id("run-b", "0" * 24, 7, "P1")

        self.assertEqual(first, retry)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, next_run)

    @patch("services.hydro_submit.requests.post")
    def test_submit_sends_idempotency_key(self, post):
        response = Mock()
        response.ok = True
        response.json.return_value = {"rid": "1" * 24}
        post.return_value = response
        submitter = HydroSubmitter("http://127.0.0.1:8888", "secret", "cc")
        submission_id = "a" * 64

        result = submitter.submit_one(
            "0" * 24, 7, "P1", "int main() {}", submission_id
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            post.call_args.kwargs["data"]["submission_id"], submission_id
        )
        self.assertEqual(post.call_args.kwargs["data"]["submission_kind"], "final")

    @patch("services.hydro_submit.requests.post")
    def test_submit_can_mark_realtime_and_use_exact_language(self, post):
        response = Mock()
        response.ok = True
        response.json.return_value = {"rid": "2" * 24}
        post.return_value = response
        submitter = HydroSubmitter("http://127.0.0.1:8888", "secret", "cc")

        submitter.submit_one(
            "0" * 24,
            7,
            "P1",
            "int main() {}",
            "b" * 64,
            lang="cc.cc14o2",
            submission_kind="realtime",
            accepted_at_ms=1_786_080_000_000,
        )

        data = post.call_args.kwargs["data"]
        self.assertEqual(data["submission_kind"], "realtime")
        self.assertEqual(data["lang"], "cc.cc14o2")
        self.assertEqual(data["accepted_at_ms"], 1_786_080_000_000)

    @patch("services.hydro_submit.requests.post")
    def test_success_without_rid_is_retryable(self, post):
        response = Mock()
        response.ok = True
        response.status_code = 202
        response.json.return_value = {"ok": True}
        post.return_value = response
        submitter = HydroSubmitter("http://127.0.0.1:8888", "secret", "cc")

        result = submitter.submit_one(
            "0" * 24, 7, "P1", "int main() {}", "c" * 64
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])

    @patch("services.hydro_submit.requests.post")
    def test_authentication_failure_is_retryable_after_token_repair(self, post):
        response = Mock()
        response.ok = False
        response.status_code = 401
        response.json.return_value = {"error": "token mismatch"}
        post.return_value = response
        submitter = HydroSubmitter("http://127.0.0.1:8888", "secret", "cc")

        result = submitter.submit_one(
            "0" * 24, 7, "P1", "int main() {}", "d" * 64
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])


if __name__ == "__main__":
    unittest.main()
