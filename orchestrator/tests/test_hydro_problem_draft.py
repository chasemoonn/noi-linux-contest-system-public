import hashlib
import unittest
from unittest.mock import Mock, patch

from services.hydro_problem_draft import HydroProblemDraftClient


TOKEN = "test-token-that-is-at-least-thirty-two-characters"
TID = "0" * 24
PROBLEMS = [
    {"pid": "P1", "slug": "books"},
    {"pid": "P2", "slug": "study"},
]


def preflight_payload():
    return {
        "safe_to_apply": True,
        "blockers": [],
        "tid": TID,
        "preflight_id": "a" * 64,
        "contest_title": "CSP-J mock",
        "begin_at": "2026-08-08T06:00:00.000Z",
        "end_at": "2026-08-08T11:00:00.000Z",
        "problems": [
            {
                "pid": "P1",
                "doc_id": 101,
                "slug": "books",
                "title": "Books",
                "content": "statement",
                "config": {"type": "default", "count": 10},
                "time_ms": {"min": 1000, "max": 1000},
                "memory_mb": {"min": 256, "max": 256},
                "formal_input_sha256": ["1" * 64],
                "source_hash": "2" * 64,
            },
            {
                "pid": "P2",
                "doc_id": 102,
                "slug": "study",
                "title": "Study",
                "content": "statement",
                "config": {"type": "default", "count": 10},
                "time_ms": {"min": 1000, "max": 2000},
                "memory_mb": {"min": 256, "max": 512},
                "formal_input_sha256": ["3" * 64],
                "source_hash": "4" * 64,
            },
        ],
    }


def apply_payload(operation_id):
    return {
        "status": "applied",
        "operation_id": operation_id,
        "preflight_id": "a" * 64,
        "tid": TID,
        "pids": [501, 502],
        "mapping": [
            {
                "source_pid": "P1",
                "source_doc_id": 101,
                "clone_pid": "noi000000-p11111111111111",
                "clone_doc_id": 501,
                "slug": "books",
                "verified": True,
            },
            {
                "source_pid": "P2",
                "source_doc_id": 102,
                "clone_pid": "noi000000-p22222222222222",
                "clone_doc_id": 502,
                "slug": "study",
                "verified": True,
            },
        ],
    }


class HydroProblemDraftClientTests(unittest.TestCase):
    def client(self):
        return HydroProblemDraftClient("http://127.0.0.1:8888", TOKEN)

    def test_plain_http_is_only_allowed_for_loopback(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            HydroProblemDraftClient("http://hydro.internal:8888", TOKEN)
        HydroProblemDraftClient("https://hydro.internal", TOKEN)

    def test_operation_id_is_stable_and_bound_to_approval(self):
        first = HydroProblemDraftClient.operation_id(TID, "a" * 64, "b" * 64)
        retry = HydroProblemDraftClient.operation_id(TID, "a" * 64, "b" * 64)
        changed = HydroProblemDraftClient.operation_id(TID, "a" * 64, "c" * 64)
        self.assertEqual(first, retry)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, changed)

    def test_normalized_input_cross_language_fixed_vector(self):
        payload = b" \r\nalpha  \r\n  beta\t \r\ngamma   \n\n"
        normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        normalized = b"\n".join(
            line.rstrip() for line in normalized.split(b"\n")
        ).strip()
        self.assertEqual(normalized, b"alpha\n  beta\ngamma")
        self.assertEqual(
            hashlib.sha256(normalized).hexdigest(),
            "f8ffc1d08f50fa840a132bce6f26802122df69416096b9a6d0444875e97cfc15",
        )

    @patch("services.hydro_problem_draft.requests.post")
    def test_preflight_sends_only_tid_and_pid_slug_mapping(self, post):
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = preflight_payload()
        post.return_value = response

        result = self.client().preflight(tid=TID, problems=PROBLEMS)

        self.assertTrue(result["ok"])
        self.assertEqual(
            post.call_args.args[0],
            "http://127.0.0.1:8888/orchestrator/submit/problem-fileio",
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"action": "preflight", "tid": TID, "problems": PROBLEMS},
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["X-Orchestrator-Token"], TOKEN
        )
        self.assertEqual(result["problems"][0]["formal_input_sha256"], ["1" * 64])

    @patch("services.hydro_problem_draft.requests.post")
    def test_malformed_hash_response_is_not_accepted(self, post):
        payload = preflight_payload()
        payload["problems"][0]["formal_input_sha256"] = ["not-a-hash"]
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = payload
        post.return_value = response

        result = self.client().preflight(tid=TID, problems=PROBLEMS)

        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])

    @patch("services.hydro_problem_draft.requests.post")
    def test_apply_requires_and_validates_verified_clone_mapping(self, post):
        operation_id = "d" * 64
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = apply_payload(operation_id)
        post.return_value = response

        result = self.client().apply(
            tid=TID,
            problems=PROBLEMS,
            operation_id=operation_id,
            approval_id="b" * 64,
            preflight_id="a" * 64,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["pids"], [501, 502])
        self.assertTrue(all(item["verified"] for item in result["mapping"]))
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["action"], "apply")
        self.assertEqual(sent["approval_id"], "b" * 64)
        self.assertEqual(post.call_args.kwargs["timeout"], (5, 600))

    @patch("services.hydro_problem_draft.requests.post")
    def test_unverified_or_reordered_mapping_cannot_update_local_pid_map(self, post):
        operation_id = "e" * 64
        payload = apply_payload(operation_id)
        payload["mapping"][1]["verified"] = False
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = payload
        post.return_value = response

        result = self.client().apply(
            tid=TID,
            problems=PROBLEMS,
            operation_id=operation_id,
            approval_id="b" * 64,
            preflight_id="a" * 64,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])

    @patch("services.hydro_problem_draft.requests.post")
    def test_conflict_and_safety_block_are_permanent(self, post):
        for status in (409, 422):
            with self.subTest(status=status):
                response = Mock()
                response.ok = False
                response.status_code = status
                response.json.return_value = {"error": "blocked"}
                post.return_value = response
                result = self.client().preflight(tid=TID, problems=PROBLEMS)
                self.assertFalse(result["ok"])
                self.assertFalse(result["retryable"])

    @patch("services.hydro_problem_draft.requests.post")
    def test_authentication_failure_is_permanent(self, post):
        response = Mock()
        response.ok = False
        response.status_code = 403
        response.json.return_value = {"error": "token mismatch"}
        post.return_value = response

        result = self.client().preflight(tid=TID, problems=PROBLEMS)

        self.assertFalse(result["ok"])
        self.assertFalse(result["retryable"])

    @patch("services.hydro_problem_draft.requests.post")
    def test_apply_rejects_a_response_for_a_different_preflight(self, post):
        operation_id = "f" * 64
        payload = apply_payload(operation_id)
        payload["preflight_id"] = "9" * 64
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = payload
        post.return_value = response

        result = self.client().apply(
            tid=TID,
            problems=PROBLEMS,
            operation_id=operation_id,
            approval_id="b" * 64,
            preflight_id="a" * 64,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])

    def test_rejects_unsafe_pid_slug_and_extra_fields_locally(self):
        invalid = (
            [{"pid": "../P1", "slug": "books"}],
            [{"pid": "P1", "slug": "../books"}],
            [{"pid": "P1", "slug": "books", "config": "secret"}],
            [{"pid": "P1", "slug": "same"}, {"pid": "P2", "slug": "same"}],
        )
        for problems in invalid:
            with self.subTest(problems=problems):
                with self.assertRaises(ValueError):
                    self.client().preflight(tid=TID, problems=problems)

    @patch("services.hydro_problem_draft.requests.post")
    def test_accepts_a_64_character_file_slug(self, post):
        slug = "a" + "b" * 63
        payload = preflight_payload()
        payload["problems"] = [
            {**payload["problems"][0], "slug": slug},
        ]
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = payload
        post.return_value = response

        result = self.client().preflight(
            tid=TID,
            problems=[{"pid": "P1", "slug": slug}],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(post.call_args.kwargs["json"]["problems"][0]["slug"], slug)


if __name__ == "__main__":
    unittest.main()
