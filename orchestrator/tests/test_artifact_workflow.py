import json
import tempfile
import unittest

from services.artifact_generation import (
    AIPracticeInput,
    AIProblemDraft,
    ArtifactGenerationError,
    ArtifactGenerationService,
    ArtifactRequest,
    normalized_input_digest,
)
from services.artifact_workflow import (
    build_private_clone_snapshot,
    extract_sample_input_hashes,
    strict_generation_blockers,
)


TID = "a" * 24
SESSION = "b" * 32
PREFLIGHT_ID = "c" * 64
OPERATION_ID = "d" * 64


def contest(pid="noi-private-apple"):
    return {
        "tid": TID,
        "title": "CSP-J 模拟赛",
        "files": json.dumps(["apple"]),
        "pids": json.dumps({"apple": pid}),
        "submission_session": SESSION,
        "begin_at_ms": 1_786_000_000_000,
        "end_at_ms": 1_786_018_000_000,
        "hydro_rule": "oi",
    }


def preflight(formal_hashes=None, content=None):
    return {
        "ok": True,
        "safe_to_apply": True,
        "tid": TID,
        "preflight_id": PREFLIGHT_ID,
        "contest_title": "CSP-J 模拟赛",
        "problems": [
            {
                "pid": "P1001",
                "doc_id": 101,
                "slug": "apple",
                "title": "苹果",
                "content": content
                or (
                    "## 题目描述\n\n求和。\n\n"
                    "## 输入样例 1\n\n```text\n4 5\n```\n"
                ),
                "config": {"type": "default"},
                "time_ms": {"min": 1000, "max": 2000},
                "memory_mb": {"min": 256, "max": 512},
                "formal_input_sha256": list(
                    ["1" * 64] if formal_hashes is None else formal_hashes
                ),
                "source_hash": "2" * 64,
            }
        ],
    }


def applied():
    return {
        "ok": True,
        "status": "applied",
        "tid": TID,
        "preflight_id": PREFLIGHT_ID,
        "operation_id": OPERATION_ID,
        "pids": [501],
        "mapping": [
            {
                "source_pid": "P1001",
                "source_doc_id": 101,
                "clone_pid": "noi-private-apple",
                "clone_doc_id": 501,
                "slug": "apple",
                "verified": True,
            }
        ],
    }


class NeverValidator:
    def validate(self, problem, input_data):
        raise AssertionError("formal-input duplicate must be blocked before validator")


class NeverOracle:
    def solve(self, problem, input_data):
        raise AssertionError("formal-input duplicate must be blocked before oracle")


class FormalDuplicateAI:
    provider_id = "fixture-ai/formal-duplicate"

    def generate_problem(self, contest_context, problem_context, practice_case_count):
        return AIProblemDraft(
            statement_markdown="## 题目描述\n\n求和。",
            practice_inputs=(
                AIPracticeInput(
                    b"alpha\n  beta\ngamma\n", "small", "normalized duplicate"
                ),
                AIPracticeInput(b"9 10\n", "stress", "other"),
            ),
        )


class ArtifactWorkflowTests(unittest.TestCase):
    def test_extracts_chinese_and_english_multiple_fenced_samples(self):
        markdown = """
## 输入样例 1

```text
1 2
```

### Sample Input 2

~~~
3 4
~~~

## 样例输入 三

```
5 6
```
"""
        hashes, warnings = extract_sample_input_hashes(markdown)
        self.assertEqual(
            set(hashes),
            {
                normalized_input_digest(b"1 2\n"),
                normalized_input_digest(b"3 4\n"),
                normalized_input_digest(b"5 6\n"),
            },
        )
        self.assertEqual(warnings, ())

    def test_uncertain_sample_parse_is_explicitly_warned_not_verified(self):
        hashes, warnings = extract_sample_input_hashes(
            "## Sample Input\n\nThis section has no fenced block.\n\n## Output\n1\n"
        )
        self.assertEqual(hashes, ())
        self.assertTrue(any("未完全确认" in warning for warning in warnings))

    def test_private_snapshot_binds_approved_preflight_to_verified_clone(self):
        snapshot, warnings = build_private_clone_snapshot(
            contest(), preflight(), applied()
        )
        problem = snapshot.problems[0]
        self.assertEqual(problem.pid, "noi-private-apple")
        self.assertEqual(problem.input_filename, "apple.in")
        self.assertEqual(problem.output_filename, "apple.out")
        self.assertEqual(problem.time_limit_ms, 2000)
        self.assertEqual(problem.memory_limit_mb, 512)
        self.assertEqual(problem.source["official_input_hash_count"], 1)
        self.assertIn(normalized_input_digest(b"4 5\n"), problem.forbidden_practice_input_sha256)
        self.assertEqual(strict_generation_blockers(snapshot), [])
        self.assertEqual(warnings, ())

        wrong = contest("another-clone")
        with self.assertRaisesRegex(Exception, "映射"):
            build_private_clone_snapshot(wrong, preflight(), applied())

    def test_sample_hash_cannot_substitute_for_missing_formal_hash(self):
        snapshot, _ = build_private_clone_snapshot(
            contest(), preflight(formal_hashes=[]), applied()
        )
        self.assertTrue(snapshot.problems[0].forbidden_practice_input_sha256)
        self.assertRegex(strict_generation_blockers(snapshot)[0], "正式输入")

    def test_node_preflight_normalized_hash_blocks_equivalent_ai_input(self):
        # Fixed cross-language vector shared with the Node preflight tests.
        vector = b" \r\nalpha  \r\n  beta\t \r\ngamma   \n\n"
        formal_hash = normalized_input_digest(vector)
        self.assertEqual(
            formal_hash,
            "f8ffc1d08f50fa840a132bce6f26802122df69416096b9a6d0444875e97cfc15",
        )
        snapshot, _ = build_private_clone_snapshot(
            contest(), preflight(formal_hashes=[formal_hash]), applied()
        )
        self.assertEqual(
            normalized_input_digest(b"alpha\n  beta\ngamma\n"),
            formal_hash,
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ArtifactGenerationError, "正式数据"):
                ArtifactGenerationService(directory).generate(
                    ArtifactRequest(snapshot, "r-normalized", 2),
                    ai_provider=FormalDuplicateAI(),
                    validators={"apple": NeverValidator()},
                    oracles={"apple": NeverOracle()},
                )


if __name__ == "__main__":
    unittest.main()
