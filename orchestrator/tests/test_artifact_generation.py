from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from services.artifact_generation import (
    AIArtifactProvider,
    AIPracticeInput,
    AIProblemDraft,
    ArtifactAlreadyExistsError,
    ArtifactGenerationError,
    ArtifactGenerationService,
    ArtifactRequest,
    ContestSnapshot,
    ProblemSnapshot,
    normalized_input_digest,
    plan_file_io_changes,
    sha256_path,
    verify_student_testdata_archive,
)


FIXTURE = Path(__file__).with_name("fixtures") / "artifact_contest.json"


def contest_fixture() -> ContestSnapshot:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    problems = tuple(ProblemSnapshot(**problem) for problem in raw.pop("problems"))
    return ContestSnapshot(problems=problems, **raw)


class FakeAI(AIArtifactProvider):
    provider_id = "fixture-ai/v1"

    def __init__(self, cases=None, markdown=None):
        self.cases = cases
        self.markdown = markdown

    def generate_problem(self, contest, problem, practice_case_count):
        # External providers must never receive raw Hydro config or official
        # input fingerprints.
        assert not hasattr(contest, "source")
        assert not hasattr(contest, "problems")
        assert not hasattr(problem, "source")
        assert not hasattr(problem, "forbidden_practice_input_sha256")
        cases = self.cases
        if cases is None:
            cases = tuple(
                AIPracticeInput(
                    input_data=f"{index} {index + 1}\n".encode(),
                    level=("small", "typical", "stress", "edge")[index - 1],
                    rationale=f"gradient {index}",
                )
                for index in range(1, practice_case_count + 1)
            )
        return AIProblemDraft(
            statement_markdown=self.markdown
            or (
                "## 题目描述\n\n计算输入中两个整数的和。\n\n"
                "## 输入格式\n\n两个整数。\n\n"
                "## 输出格式\n\n输出它们的和。\n"
            ),
            practice_inputs=tuple(cases),
        )


class TwoIntegerValidator:
    def validate(self, problem, input_data):
        values = input_data.decode("ascii").split()
        if len(values) != 2:
            raise ValueError("expected two integers")
        [int(value) for value in values]


class SumOracle:
    def solve(self, problem, input_data):
        values = [int(value) for value in input_data.split()]
        multiplier = 2 if problem.slug == "banana" else 1
        return f"{sum(values) * multiplier}\n".encode()


class FlakyOracle:
    def __init__(self):
        self.counter = 0

    def solve(self, problem, input_data):
        self.counter += 1
        return f"{self.counter}\n".encode()


class ArtifactGenerationTests(unittest.TestCase):
    def _dependencies(self, contest):
        validators = {problem.slug: TwoIntegerValidator() for problem in contest.problems}
        oracles = {problem.slug: SumOracle() for problem in contest.problems}
        return validators, oracles

    def test_atomic_release_separates_teacher_and_student_artifacts(self):
        contest = contest_fixture()
        validators, oracles = self._dependencies(contest)
        with tempfile.TemporaryDirectory() as directory:
            service = ArtifactGenerationService(directory)
            release = service.generate(
                ArtifactRequest(
                    contest,
                    "r1",
                    3,
                    ("apple: 样例抽取未完全确认，请教师检查",),
                ),
                ai_provider=FakeAI(),
                validators=validators,
                oracles=oracles,
            )

            self.assertTrue(release.paper_path.is_file())
            self.assertTrue(release.testdata_path.is_file())
            self.assertTrue((release.directory / "teacher/snapshot.json").is_file())
            self.assertTrue((release.directory / "teacher/validation-report.json").is_file())
            self.assertFalse((release.directory / "student/snapshot.json").exists())
            self.assertEqual(release.manifest["status"], "awaiting_teacher_approval")
            self.assertTrue(release.manifest["validation"]["student_archive_in_out_only"])
            self.assertEqual(
                release.manifest["warnings"],
                ["apple: 样例抽取未完全确认，请教师检查"],
            )
            validation = json.loads(
                (release.directory / "teacher/validation-report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(validation["warnings"], release.manifest["warnings"])
            self.assertEqual(release.pdf.page_count, 3)

            members = verify_student_testdata_archive(
                release.testdata_path, [problem.slug for problem in contest.problems]
            )
            self.assertEqual(len(members), 12)
            self.assertTrue(all(name.endswith((".in", ".out")) for name in members))
            with tarfile.open(release.testdata_path, "r:gz") as archive:
                self.assertNotIn("snapshot.json", archive.getnames())
                self.assertNotIn("validation-report.json", archive.getnames())
                self.assertEqual(archive.extractfile("apple/1.out").read(), b"3\n")
                self.assertEqual(archive.extractfile("banana/1.out").read(), b"6\n")

            manifest = json.loads(release.manifest_path.read_text(encoding="utf-8"))
            for item in manifest["files"]:
                actual = release.directory / item["path"]
                self.assertEqual(actual.stat().st_size, item["size"])
                self.assertEqual(sha256_path(actual), item["sha256"])
                self.assertEqual(
                    item["audience"],
                    "student" if item["path"].startswith("student/") else "teacher",
                )

    def test_missing_ai_fails_closed_and_leaves_no_revision(self):
        contest = contest_fixture()
        validators, oracles = self._dependencies(contest)
        with tempfile.TemporaryDirectory() as directory:
            service = ArtifactGenerationService(directory)
            with self.assertRaisesRegex(ArtifactGenerationError, "AI provider"):
                service.generate(
                    ArtifactRequest(contest, "r1"),
                    ai_provider=None,
                    validators=validators,
                    oracles=oracles,
                )
            self.assertFalse((Path(directory) / contest.tid / "r1").exists())

    def test_missing_validator_or_oracle_fails_before_ai_generation(self):
        contest = contest_fixture()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ArtifactGenerationError, "validator"):
                ArtifactGenerationService(directory).generate(
                    ArtifactRequest(contest, "r1"),
                    ai_provider=FakeAI(),
                    validators={},
                    oracles={},
                )
            validators, _ = self._dependencies(contest)
            with self.assertRaisesRegex(ArtifactGenerationError, "oracle"):
                ArtifactGenerationService(directory).generate(
                    ArtifactRequest(contest, "r2"),
                    ai_provider=FakeAI(),
                    validators=validators,
                    oracles={},
                )

    def test_file_io_change_is_planned_and_blocks_until_hydro_is_reread(self):
        contest = contest_fixture()
        broken_problem = replace(
            contest.problems[0], input_filename=None, output_filename=None
        )
        broken = replace(contest, problems=(broken_problem, *contest.problems[1:]))
        changes = plan_file_io_changes(broken)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].required_input_filename, "apple.in")
        validators, oracles = self._dependencies(broken)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ArtifactGenerationError, "尚未应用"):
                ArtifactGenerationService(directory).generate(
                    ArtifactRequest(broken, "r1"),
                    ai_provider=FakeAI(),
                    validators=validators,
                    oracles=oracles,
                )

    def test_duplicate_or_official_input_is_blocked_and_temp_is_removed(self):
        contest = contest_fixture()
        duplicate = AIPracticeInput(b"1 2\n", "small")
        ai = FakeAI(cases=(duplicate, AIPracticeInput(b"1 2\r\n", "stress")))
        validators, oracles = self._dependencies(contest)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ArtifactGenerationError, "重复"):
                ArtifactGenerationService(directory).generate(
                    ArtifactRequest(contest, "r1", 2),
                    ai_provider=ai,
                    validators=validators,
                    oracles=oracles,
                )
            contest_dir = Path(directory) / contest.tid
            self.assertFalse((contest_dir / "r1").exists())
            self.assertEqual(list(contest_dir.glob("*.generating")), [])

        forbidden_problem = replace(
            contest.problems[0],
            forbidden_practice_input_sha256=(normalized_input_digest(b"1 2\n"),),
        )
        forbidden_contest = replace(
            contest, problems=(forbidden_problem, *contest.problems[1:])
        )
        validators, oracles = self._dependencies(forbidden_contest)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ArtifactGenerationError, "正式数据"):
                ArtifactGenerationService(directory).generate(
                    ArtifactRequest(forbidden_contest, "r1", 2),
                    ai_provider=FakeAI(
                        cases=(
                            AIPracticeInput(b"1 2\n", "small"),
                            AIPracticeInput(b"2 3\n", "stress"),
                        )
                    ),
                    validators=validators,
                    oracles=oracles,
                )

    def test_nondeterministic_oracle_and_solution_leak_are_blocked(self):
        contest = contest_fixture()
        validators, oracles = self._dependencies(contest)
        oracles["apple"] = FlakyOracle()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ArtifactGenerationError, "不确定"):
                ArtifactGenerationService(directory).generate(
                    ArtifactRequest(contest, "r1", 2),
                    ai_provider=FakeAI(
                        cases=(
                            AIPracticeInput(b"1 2\n", "small"),
                            AIPracticeInput(b"2 3\n", "stress"),
                        )
                    ),
                    validators=validators,
                    oracles=oracles,
                )

        validators, oracles = self._dependencies(contest)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ArtifactGenerationError, "题解"):
                ArtifactGenerationService(directory).generate(
                    ArtifactRequest(contest, "r1", 2),
                    ai_provider=FakeAI(
                        cases=(
                            AIPracticeInput(b"1 2\n", "small"),
                            AIPracticeInput(b"2 3\n", "stress"),
                        ),
                        markdown="## 题解\n\n这是不应发给学生的内容。",
                    ),
                    validators=validators,
                    oracles=oracles,
                )

    def test_successful_revision_is_immutable(self):
        contest = contest_fixture()
        validators, oracles = self._dependencies(contest)
        with tempfile.TemporaryDirectory() as directory:
            service = ArtifactGenerationService(directory)
            request = ArtifactRequest(contest, "r1", 2)
            service.generate(
                request,
                ai_provider=FakeAI(),
                validators=validators,
                oracles=oracles,
            )
            with self.assertRaisesRegex(ArtifactAlreadyExistsError, "不可覆盖"):
                service.generate(
                    request,
                    ai_provider=FakeAI(),
                    validators=validators,
                    oracles=oracles,
                )

    def test_slug_supports_registration_limit_of_64_characters(self):
        contest = contest_fixture()
        slug = "a" + "b" * 63
        only = replace(
            contest.problems[0],
            slug=slug,
            input_filename=f"{slug}.in",
            output_filename=f"{slug}.out",
        )
        adjusted = replace(contest, problems=(only,))
        with tempfile.TemporaryDirectory() as directory:
            release = ArtifactGenerationService(directory).generate(
                ArtifactRequest(adjusted, "r-long", 2),
                ai_provider=FakeAI(
                    cases=(
                        AIPracticeInput(b"1 2\n", "small"),
                        AIPracticeInput(b"2 3\n", "stress"),
                    )
                ),
                validators={slug: TwoIntegerValidator()},
                oracles={slug: SumOracle()},
            )
            members = verify_student_testdata_archive(
                release.testdata_path, [slug]
            )
            self.assertIn(f"{slug}/1.in", members)

        too_long = "a" + "b" * 64
        invalid = replace(
            adjusted,
            problems=(
                replace(
                    only,
                    slug=too_long,
                    input_filename=f"{too_long}.in",
                    output_filename=f"{too_long}.out",
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ArtifactGenerationError, "不安全"):
                ArtifactGenerationService(directory).generate(
                    ArtifactRequest(invalid, "r-too-long", 2),
                    ai_provider=FakeAI(),
                    validators={},
                    oracles={},
                )


if __name__ == "__main__":
    unittest.main()
