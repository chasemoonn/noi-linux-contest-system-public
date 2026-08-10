import json
from pathlib import Path
import sys
import tempfile
import unittest

from services.artifact_adapters import (
    AIProviderError,
    AdapterConfigurationError,
    AdapterExecutionError,
    ExecutablePolicy,
    InputRejectedError,
    OpenAICompatibleArtifactProvider,
    ProcessResult,
    TrustedExecutableAdapterRegistry,
    run_trusted_executable,
)
from services.artifact_generation import (
    AIContestContext,
    AIProblemContext,
    ProblemSnapshot,
)


def draft_content(case_count=3, *, extra=False):
    levels = ["small", "typical", "stress", "edge"][:case_count]
    value = {
        "statement_markdown": (
            "## 题目描述\n\n计算两个整数的和。\n\n"
            "## 输入格式\n\n两个整数。\n\n"
            "## 输出格式\n\n一个整数。\n"
        ),
        "practice_inputs": [
            {
                "input_data": f"{index} {index + 1}\n",
                "level": level,
                "rationale": f"gradient {index}",
            }
            for index, level in enumerate(levels, start=1)
        ],
    }
    if extra:
        value["official_test_digest"] = "must-not-be-accepted"
    return json.dumps(value, ensure_ascii=False)


class FakeTransport:
    def __init__(self, content=None):
        self.content = content or draft_content()
        self.calls = []

    def post_json(self, **kwargs):
        self.calls.append(kwargs)
        return {"choices": [{"message": {"content": self.content}}]}


def contexts():
    contest = AIContestContext(
        "CSP-J 模拟赛",
        "只发题面快照",
        1_786_000_000_000,
        1_786_018_000_000,
    )
    problem = AIProblemContext(
        "P1001",
        "apple",
        "苹果",
        "## 题目描述\n\n求和。",
        "apple.in",
        "apple.out",
        1000,
        256,
    )
    # Simulate a future local-only field. The explicit snapshot allowlist must
    # still keep it out of the outbound request.
    object.__setattr__(problem, "forbidden_practice_input_sha256", "SECRET-DIGEST")
    object.__setattr__(contest, "source", {"official": "SECRET-OFFICIAL-DATA"})
    return contest, problem


class OpenAICompatibleAdapterTests(unittest.TestCase):
    def provider(self, transport):
        return OpenAICompatibleArtifactProvider(
            endpoint="https://ai.example.test/v1/chat/completions",
            api_key="test-only-api-key",
            model="fixture-model",
            transport=transport,
        )

    def test_missing_secret_or_unsafe_endpoint_fails_closed(self):
        with self.assertRaisesRegex(AdapterConfigurationError, "api_key"):
            OpenAICompatibleArtifactProvider(
                endpoint="https://ai.example.test/v1/chat/completions",
                api_key="",
                model="fixture-model",
            )
        with self.assertRaisesRegex(AdapterConfigurationError, "HTTPS"):
            OpenAICompatibleArtifactProvider(
                endpoint="http://ai.example.test/v1/chat/completions",
                api_key="secret",
                model="fixture-model",
            )
        with self.assertRaises(AdapterConfigurationError):
            OpenAICompatibleArtifactProvider.from_config(
                {
                    "endpoint": "https://ai.example.test/v1/chat/completions",
                    "api_key": "secret",
                    "model": "fixture-model",
                    "surprise": True,
                }
            )
        with self.assertRaisesRegex(AdapterConfigurationError, "api_key"):
            OpenAICompatibleArtifactProvider.from_config(
                {
                    "endpoint": "https://ai.example.test/v1/chat/completions",
                    "api_key_env": "ARTIFACT_AI_API_KEY",
                    "model": "fixture-model",
                },
                environ={},
            )

        configured = OpenAICompatibleArtifactProvider.from_config(
            {
                "endpoint": "https://ai.example.test/v1/chat/completions",
                "api_key_env": "ARTIFACT_AI_API_KEY",
                "model": "fixture-model",
            },
            environ={"ARTIFACT_AI_API_KEY": "runtime-only-secret"},
            transport=FakeTransport(),
        )
        self.assertEqual(configured.provider_id, "openai-compatible/fixture-model")

    def test_only_explicit_sanitized_snapshot_fields_cross_ai_boundary(self):
        transport = FakeTransport()
        contest, problem = contexts()
        draft = self.provider(transport).generate_problem(contest, problem, 3)

        self.assertEqual(len(draft.practice_inputs), 3)
        call = transport.calls[0]
        body_text = json.dumps(call["payload"], ensure_ascii=False)
        self.assertNotIn("SECRET-DIGEST", body_text)
        self.assertNotIn("SECRET-OFFICIAL-DATA", body_text)
        self.assertNotIn("forbidden_practice_input_sha256", body_text)
        self.assertNotIn("source", body_text)
        self.assertNotIn("test-only-api-key", body_text)

        user_payload = json.loads(call["payload"]["messages"][1]["content"])
        snapshot = user_payload["snapshot"]
        self.assertEqual(
            set(snapshot), {"contest", "problem", "practice_case_count"}
        )
        self.assertEqual(
            set(snapshot["contest"]),
            {"title", "subtitle", "begin_at_ms", "end_at_ms"},
        )
        self.assertEqual(
            set(snapshot["problem"]),
            {
                "pid",
                "slug",
                "title",
                "statement_markdown",
                "input_filename",
                "output_filename",
                "time_limit_ms",
                "memory_limit_mb",
            },
        )
        response_format = call["payload"]["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertIs(response_format["json_schema"]["strict"], True)
        self.assertFalse(
            response_format["json_schema"]["schema"]["additionalProperties"]
        )
        self.assertEqual(
            call["headers"]["Authorization"], "Bearer test-only-api-key"
        )

    def test_draft_parser_rejects_extra_duplicate_and_wrong_count(self):
        contest, problem = contexts()
        with self.assertRaisesRegex(AIProviderError, "unknown root"):
            self.provider(FakeTransport(draft_content(extra=True))).generate_problem(
                contest, problem, 3
            )

        duplicate = (
            '{"statement_markdown":"one","statement_markdown":"two",'
            '"practice_inputs":[]}'
        )
        with self.assertRaisesRegex(AIProviderError, "strict JSON"):
            self.provider(FakeTransport(duplicate)).generate_problem(
                contest, problem, 3
            )

        with self.assertRaisesRegex(AIProviderError, "count"):
            self.provider(FakeTransport(draft_content(2))).generate_problem(
                contest, problem, 3
            )

        invalid_unicode = json.loads(draft_content())
        invalid_unicode["practice_inputs"][0]["input_data"] = "\ud800"
        with self.assertRaisesRegex(AIProviderError, "Unicode"):
            self.provider(
                FakeTransport(json.dumps(invalid_unicode))
            ).generate_problem(contest, problem, 3)

    def test_duplicate_gradient_is_rejected(self):
        value = json.loads(draft_content())
        value["practice_inputs"][1]["level"] = "small"
        contest, problem = contexts()
        with self.assertRaisesRegex(AIProviderError, "distinct"):
            self.provider(
                FakeTransport(json.dumps(value, ensure_ascii=False))
            ).generate_problem(contest, problem, 3)


def problem(slug="apple"):
    return ProblemSnapshot(
        pid="P1001",
        slug=slug,
        title="苹果",
        statement_markdown="statement",
        input_filename=f"{slug}.in",
        output_filename=f"{slug}.out",
    )


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, policy, input_data):
        self.calls.append((policy, input_data))
        return self.result


class ExecutableAdapterTests(unittest.TestCase):
    @staticmethod
    def config(runner_code="pass", oracle_code="print(3)"):
        executable = str(Path(sys.executable).resolve())
        return {
            "approved_roots": [str(Path(sys.executable).resolve().parent)],
            "defaults": {
                "timeout_seconds": 2.0,
                "max_input_bytes": 4096,
                "max_output_bytes": 4096,
                "cpu_seconds": 2,
                "memory_limit_bytes": 512 * 1024 * 1024,
            },
            "validators": {
                "apple": {"executable": executable, "args": ["-c", runner_code]}
            },
            "oracles": {
                "apple": {"executable": executable, "args": ["-c", oracle_code]}
            },
        }

    def test_missing_mapping_and_path_outside_root_fail_closed(self):
        registry = TrustedExecutableAdapterRegistry.from_config(
            self.config(), runner=FakeRunner(ProcessResult(0, b"", b""))
        )
        with self.assertRaisesRegex(AdapterConfigurationError, "validator"):
            registry.validator_for("banana")
        with self.assertRaisesRegex(AdapterConfigurationError, "validator"):
            registry.adapters_for(["apple", "banana"])

        with tempfile.TemporaryDirectory() as directory:
            unsafe = self.config()
            unsafe["approved_roots"] = [directory]
            with self.assertRaisesRegex(AdapterConfigurationError, "approved_roots"):
                TrustedExecutableAdapterRegistry.from_config(unsafe)

        relative = self.config()
        relative["validators"]["apple"]["executable"] = "python"
        with self.assertRaisesRegex(AdapterConfigurationError, "absolute"):
            TrustedExecutableAdapterRegistry.from_config(relative)

    def test_registry_accepts_the_64_character_registration_slug_limit(self):
        slug = "a" + "b" * 63
        configured = self.config()
        configured["validators"] = {slug: configured["validators"]["apple"]}
        configured["oracles"] = {slug: configured["oracles"]["apple"]}
        registry = TrustedExecutableAdapterRegistry.from_config(
            configured, runner=FakeRunner(ProcessResult(0, b"", b""))
        )
        validators, oracles = registry.adapters_for([slug])
        self.assertEqual(set(validators), {slug})
        self.assertEqual(set(oracles), {slug})

    def test_validator_and_oracle_are_bound_to_problem_and_propagate_bytes(self):
        validator_runner = FakeRunner(ProcessResult(0, b"diagnostic", b""))
        validator_registry = TrustedExecutableAdapterRegistry.from_config(
            self.config(), runner=validator_runner
        )
        validator = validator_registry.validator_for("apple")
        validator.validate(problem(), b"1 2\n")
        self.assertEqual(validator_runner.calls[0][1], b"1 2\n")
        with self.assertRaisesRegex(AdapterExecutionError, "wrong problem"):
            validator.validate(problem("banana"), b"1 2\n")

        oracle_runner = FakeRunner(ProcessResult(0, b"3\n", b""))
        oracle_registry = TrustedExecutableAdapterRegistry.from_config(
            self.config(), runner=oracle_runner
        )
        self.assertEqual(oracle_registry.oracle_for("apple").solve(problem(), b"1 2\n"), b"3\n")

        reject_runner = FakeRunner(ProcessResult(9, b"", b"invalid input"))
        rejected = TrustedExecutableAdapterRegistry.from_config(
            self.config(), runner=reject_runner
        ).validator_for("apple")
        with self.assertRaisesRegex(InputRejectedError, "invalid input"):
            rejected.validate(problem(), b"bad\n")

    def test_real_runner_has_timeout_and_combined_output_cap(self):
        executable = Path(sys.executable).resolve()
        timeout_policy = ExecutablePolicy(
            executable=executable,
            args=("-c", "import time; time.sleep(2)"),
            timeout_seconds=0.05,
            max_input_bytes=128,
            max_output_bytes=1024,
            cpu_seconds=1,
            memory_limit_bytes=512 * 1024 * 1024,
        )
        with self.assertRaisesRegex(AdapterExecutionError, "wall-time"):
            run_trusted_executable(timeout_policy, b"")

        output_policy = ExecutablePolicy(
            executable=executable,
            args=("-c", "import sys; sys.stdout.write('x' * 100000)"),
            timeout_seconds=2,
            max_input_bytes=128,
            max_output_bytes=1024,
            cpu_seconds=1,
            memory_limit_bytes=512 * 1024 * 1024,
        )
        with self.assertRaisesRegex(AdapterExecutionError, "output limit"):
            run_trusted_executable(output_policy, b"")

    def test_real_validator_and_oracle_execute_without_a_shell(self):
        config = self.config(
            runner_code=(
                "import sys; data=sys.stdin.buffer.read(); "
                "sys.exit(0 if data == b'1 2\\n' else 7)"
            ),
            oracle_code=(
                "import sys; a,b=map(int,sys.stdin.buffer.read().split()); "
                "sys.stdout.buffer.write((str(a+b)+'\\n').encode())"
            ),
        )
        registry = TrustedExecutableAdapterRegistry.from_config(config)
        validators, oracles = registry.adapters_for(["apple"])
        validators["apple"].validate(problem(), b"1 2\n")
        self.assertEqual(oracles["apple"].solve(problem(), b"1 2\n"), b"3\n")
        with self.assertRaises(InputRejectedError):
            validators["apple"].validate(problem(), b"bad\n")


if __name__ == "__main__":
    unittest.main()
