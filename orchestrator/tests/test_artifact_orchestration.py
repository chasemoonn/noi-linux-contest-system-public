import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.artifact_generation import (
    AIPracticeInput,
    AIProblemDraft,
    ArtifactGenerationService,
)
from services.artifact_orchestration import ArtifactJobRunner
from services.hydro_problem_draft import HydroProblemDraftClient
from services.store import Store, SubmissionConflictError


TID = "8" * 24


class FakeProblemClient:
    def __init__(self, *, formal_hashes=None):
        self.formal_hashes = ["f" * 64] if formal_hashes is None else formal_hashes
        self.preflight_calls = 0
        self.apply_calls = 0

    operation_id = staticmethod(HydroProblemDraftClient.operation_id)

    def preflight(self, *, tid, problems):
        self.preflight_calls += 1
        self.problems = list(problems)
        return {
            "ok": True,
            "retryable": False,
            "safe_to_apply": True,
            "blockers": [],
            "tid": tid,
            "preflight_id": "a" * 64,
            "contest_title": "CSP-J 模拟赛",
            "problems": [
                {
                    "pid": problems[0]["pid"],
                    "doc_id": 101,
                    "slug": "apple",
                    "title": "苹果",
                    "content": (
                        "## 题目描述\n\n计算两个整数之和。\n\n"
                        "## 输入样例 1\n\n```text\n4 5\n```\n"
                    ),
                    "config": {"type": "default", "count": 10},
                    "time_ms": {"min": 1000, "max": 1000},
                    "memory_mb": {"min": 256, "max": 256},
                    "formal_input_sha256": list(self.formal_hashes),
                    "source_hash": "b" * 64,
                }
            ],
        }

    def apply(
        self,
        *,
        tid,
        problems,
        operation_id,
        approval_id,
        preflight_id,
    ):
        self.apply_calls += 1
        return {
            "ok": True,
            "retryable": False,
            "status": "applied",
            "operation_id": operation_id,
            "preflight_id": preflight_id,
            "tid": tid,
            "pids": [501],
            "mapping": [
                {
                    "source_pid": problems[0]["pid"],
                    "source_doc_id": 101,
                    "clone_pid": "noi-private-apple",
                    "clone_doc_id": 501,
                    "slug": "apple",
                    "verified": True,
                }
            ],
        }


class FakeAI:
    provider_id = "fixture-ai/orchestration"

    def generate_problem(self, contest, problem, practice_case_count):
        levels = ("small", "typical", "stress", "edge")
        return AIProblemDraft(
            statement_markdown=(
                "## 题目描述\n\n计算两个整数之和。\n\n"
                "## 输入格式\n\n两个整数。\n\n"
                "## 输出格式\n\n一个整数。\n"
            ),
            practice_inputs=tuple(
                AIPracticeInput(
                    f"{index} {index + 1}\n".encode(),
                    levels[index - 1],
                    f"gradient {index}",
                )
                for index in range(1, practice_case_count + 1)
            ),
        )


class FailingAI(FakeAI):
    def generate_problem(self, contest, problem, practice_case_count):
        raise RuntimeError("fixture AI unavailable after clone")


class TwoIntegerValidator:
    def validate(self, problem, input_data):
        values = input_data.split()
        if len(values) != 2:
            raise ValueError("expected two integers")
        [int(value) for value in values]


class SumOracle:
    def solve(self, problem, input_data):
        return f"{sum(int(value) for value in input_data.split())}\n".encode()


class GoodRegistry:
    def adapters_for(self, slugs):
        return (
            {slug: TwoIntegerValidator() for slug in slugs},
            {slug: SumOracle() for slug in slugs},
        )


class MissingRegistry:
    def adapters_for(self, slugs):
        raise RuntimeError("missing trusted oracle mapping for apple")


class ArtifactOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(str(root / "state.db"))
        self.artifact_root = root / "artifacts"
        self.store.upsert_contest(
            TID,
            "CSP-J 模拟赛",
            ["apple"],
            {"apple": "P1001"},
            materials_mode="ai",
            material_state="pending",
            begin_at_ms=1_786_000_000_000,
            end_at_ms=1_786_018_000_000,
            hydro_rule="oi",
            practice_groups=2,
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def runner(self, client, registry):
        return ArtifactJobRunner(
            store=self.store,
            problem_client=client,
            ai_provider=FakeAI(),
            tool_registry=registry,
            generation_service=ArtifactGenerationService(self.artifact_root),
        )

    def runner_with_ai(self, client, registry, ai_provider):
        return ArtifactJobRunner(
            store=self.store,
            problem_client=client,
            ai_provider=ai_provider,
            tool_registry=registry,
            generation_service=ArtifactGenerationService(self.artifact_root),
        )

    def test_end_to_end_job_lands_review_and_never_auto_approves(self):
        client = FakeProblemClient()
        runner = self.runner(client, GoodRegistry())
        job = runner.start(TID, "teacher")
        self.assertEqual(job["state"], "queued")
        self.assertTrue(runner.run(job["job_id"]))

        finished = self.store.artifact_job(job["job_id"])
        contest = self.store.get_contest(TID)
        revisions = self.store.artifact_revisions(TID)
        self.assertEqual(finished["state"], "done")
        self.assertEqual(revisions[0]["state"], "review")
        self.assertEqual(contest["material_state"], "review")
        self.assertEqual(contest["active_material_revision"], "")
        self.assertEqual(json.loads(contest["pids"]), {"apple": "noi-private-apple"})
        self.assertEqual(revisions[0]["file_io_plan"][0]["input_filename"], "apple.in")
        self.assertTrue((Path(revisions[0]["root_path"]) / "teacher/validation-report.json").is_file())
        self.assertEqual(client.preflight_calls, 1)
        self.assertEqual(client.apply_calls, 1)
        with self.assertRaisesRegex(SubmissionConflictError, "陈旧题面"):
            runner.start(TID, "teacher")
        self.assertEqual(client.preflight_calls, 1)
        self.assertEqual(client.apply_calls, 1)

    def test_failure_after_clone_publishes_no_revision_and_retry_resumes(self):
        client = FakeProblemClient()
        failing = self.runner_with_ai(client, GoodRegistry(), FailingAI())
        first = failing.start(TID, "teacher")
        self.assertFalse(failing.run(first["job_id"]))
        self.assertEqual(self.store.artifact_job(first["job_id"])["state"], "error")
        self.assertEqual(self.store.artifact_revisions(TID), [])
        self.assertEqual(json.loads(self.store.get_contest(TID)["pids"])["apple"], "noi-private-apple")

        working = self.runner(client, GoodRegistry())
        second = working.start(TID, "teacher")
        self.assertEqual(second["details"]["resumed_from"], first["job_id"])
        self.assertTrue(working.run(second["job_id"]))
        # Persisted approved preflight/apply are reused, so retry cannot create
        # another private clone after an external partial success.
        self.assertEqual(client.preflight_calls, 1)
        self.assertEqual(client.apply_calls, 1)
        self.assertEqual(len(self.store.artifact_revisions(TID)), 1)

    def test_missing_formal_hash_blocks_before_clone_or_local_pid_replace(self):
        client = FakeProblemClient(formal_hashes=[])
        runner = self.runner(client, GoodRegistry())
        job = runner.start(TID, "teacher")
        with patch.object(
            self.store,
            "replace_contest_pid_map",
            wraps=self.store.replace_contest_pid_map,
        ) as replace:
            self.assertFalse(runner.run(job["job_id"]))
        failed = self.store.artifact_job(job["job_id"])
        self.assertEqual(failed["state"], "error")
        self.assertIn("正式输入数据指纹", failed["error"])
        self.assertEqual(client.apply_calls, 0)
        replace.assert_not_called()
        self.assertEqual(json.loads(self.store.get_contest(TID)["pids"]), {"apple": "P1001"})
        self.assertEqual(self.store.artifact_revisions(TID), [])

    def test_missing_trusted_tool_blocks_before_clone_or_local_pid_replace(self):
        client = FakeProblemClient()
        runner = self.runner(client, MissingRegistry())
        job = runner.start(TID, "teacher")
        with patch.object(
            self.store,
            "replace_contest_pid_map",
            wraps=self.store.replace_contest_pid_map,
        ) as replace:
            self.assertFalse(runner.run(job["job_id"]))
        failed = self.store.artifact_job(job["job_id"])
        self.assertIn("missing trusted oracle", failed["error"])
        self.assertEqual(client.apply_calls, 0)
        replace.assert_not_called()
        self.assertEqual(json.loads(self.store.get_contest(TID)["pids"]), {"apple": "P1001"})
        self.assertEqual(self.store.artifact_revisions(TID), [])

    def test_missing_ai_provider_blocks_before_clone(self):
        client = FakeProblemClient()
        runner = self.runner_with_ai(client, GoodRegistry(), None)
        job = runner.start(TID, "teacher")
        with patch.object(
            self.store,
            "replace_contest_pid_map",
            wraps=self.store.replace_contest_pid_map,
        ) as replace:
            self.assertFalse(runner.run(job["job_id"]))
        self.assertIn("AI provider", self.store.artifact_job(job["job_id"])["error"])
        self.assertEqual(client.apply_calls, 0)
        replace.assert_not_called()

    def test_invalid_practice_group_count_blocks_before_clone(self):
        contest = self.store.get_contest(TID)
        self.store.upsert_contest(
            TID,
            contest["title"],
            ["apple"],
            {"apple": "P1001"},
            materials_mode="ai",
            material_state="pending",
            begin_at_ms=int(contest["begin_at_ms"]),
            end_at_ms=int(contest["end_at_ms"]),
            hydro_rule="oi",
            practice_groups=1,
        )
        client = FakeProblemClient()
        runner = self.runner(client, GoodRegistry())
        job = runner.start(TID, "teacher")
        with patch.object(
            self.store,
            "replace_contest_pid_map",
            wraps=self.store.replace_contest_pid_map,
        ) as replace:
            self.assertFalse(runner.run(job["job_id"]))
        self.assertIn("2 到 4", self.store.artifact_job(job["job_id"])["error"])
        self.assertEqual(client.apply_calls, 0)
        replace.assert_not_called()

    def test_two_clicks_cannot_create_concurrent_active_jobs(self):
        runner = self.runner(FakeProblemClient(), GoodRegistry())
        runner.start(TID, "teacher")
        with self.assertRaisesRegex(SubmissionConflictError, "正在执行"):
            runner.start(TID, "teacher")


if __name__ == "__main__":
    unittest.main()
