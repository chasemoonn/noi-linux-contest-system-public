import unittest
from unittest.mock import Mock, patch
import json
from pathlib import Path
import re
import tempfile

from services.hydro_submit import HydroSubmitter

REAL_FULLMATCH = re.fullmatch


class HydroSubmitterTests(unittest.TestCase):
    @staticmethod
    def qualification_fullmatch(pattern, value):
        if pattern.startswith("/app/data/qualification/"):
            return REAL_FULLMATCH(r".+", value)
        return REAL_FULLMATCH(pattern, value)

    def test_qualification_failure_marker_blocks_only_until_removed(self):
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / "failure.json"
            identity = "a" * 64
            marker.write_text(json.dumps({
                "schema_version": 1,
                "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
                "scenario": "collection_retry",
                "submission_id": identity,
                "failure": "block_until_removed",
            }), encoding="utf-8")
            marker.chmod(0o600)
            with patch("services.hydro_submit.re.fullmatch", side_effect=self.qualification_fullmatch):
                submitter = HydroSubmitter(
                    "http://127.0.0.1:8888", "secret", "cc",
                    qualification_failure_marker_path=marker.as_posix(),
                    qualification_marker="NOI-V1-QUAL-1234567890ABCDEF",
                )
            first = submitter.submit_one("b" * 24, 7, "P1", "code", identity)
            self.assertFalse(first["ok"])
            self.assertFalse(first["retryable"])
            self.assertTrue(marker.exists())
            again = submitter.submit_one("b" * 24, 7, "P1", "code", identity)
            self.assertFalse(again["ok"])
            marker.unlink()
            with patch("services.hydro_submit.requests.post") as post:
                post.return_value = Mock(ok=True, json=lambda: {"rid": "c" * 24})
                second = submitter.submit_one("b" * 24, 7, "P1", "code", identity)
            self.assertTrue(second["ok"])
            post.assert_called_once()

    def test_qualification_failure_marker_never_matches_another_submission(self):
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / "failure.json"
            marker.write_text(json.dumps({
                "schema_version": 1,
                "qualification_marker": "NOI-V1-QUAL-1234567890ABCDEF",
                "scenario": "collection_retry",
                "submission_id": "a" * 64,
                "failure": "block_until_removed",
            }), encoding="utf-8")
            marker.chmod(0o600)
            with patch("services.hydro_submit.re.fullmatch", side_effect=self.qualification_fullmatch):
                submitter = HydroSubmitter(
                    "http://127.0.0.1:8888", "secret", "cc",
                    qualification_failure_marker_path=marker.as_posix(),
                    qualification_marker="NOI-V1-QUAL-1234567890ABCDEF",
                )
            with self.assertRaisesRegex(RuntimeError, "identity differs"), \
                    patch("services.hydro_submit.requests.post") as post:
                submitter.submit_one("b" * 24, 7, "P1", "code", "d" * 64)
            post.assert_not_called()
            self.assertTrue(marker.exists())
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

    @patch("services.hydro_submit.requests.post")
    def test_ambiguous_plugin_response_is_never_retryable(self, post):
        response = Mock()
        response.ok = False
        response.status_code = 409
        response.json.return_value = {
            "error": {
                "name": "OrchestratorSubmissionAmbiguousError",
                "code": 409,
            }
        }
        post.return_value = response
        submitter = HydroSubmitter("http://127.0.0.1:8888", "secret", "cc")

        result = submitter.submit_one(
            "0" * 24, 7, "P1", "int main() {}", "e" * 64
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["retryable"])
        self.assertTrue(result["ambiguous"])

    @patch("services.hydro_submit.requests.post")
    def test_read_only_resolution_accepts_only_a_resolved_rid(self, post):
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = {"status": "resolved", "rid": "f" * 24}
        post.return_value = response
        submitter = HydroSubmitter("http://127.0.0.1:8888", "secret", "cc")

        result = submitter.resolve_submission("a" * 64)

        self.assertEqual(result, {"ok": True, "status": "resolved", "rid": "f" * 24})
        self.assertTrue(post.call_args.args[0].endswith("/orchestrator/submit/status"))

    @patch("services.hydro_submit.requests.post")
    def test_read_only_resolution_preserves_non_unique_result(self, post):
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = {"status": "multiple"}
        post.return_value = response
        submitter = HydroSubmitter("http://127.0.0.1:8888", "secret", "cc")

        self.assertEqual(
            submitter.resolve_submission("b" * 64),
            {"ok": False, "status": "multiple"},
        )


if __name__ == "__main__":
    unittest.main()
