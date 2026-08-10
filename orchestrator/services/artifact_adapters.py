"""Fail-closed adapters for AI drafting and trusted local contest tools.

The artifact generator intentionally defines very small protocols.  This
module supplies production-oriented implementations without teaching the
generator about HTTP, API keys, subprocesses, or deployment configuration.

Security boundaries in this module are deliberate:

* the AI request is rebuilt from the sanitized ``AI*Context`` values and can
  never include official-test fingerprints or the raw Hydro ``source`` map;
* an OpenAI-compatible endpoint must return the exact, strict JSON draft
  schema;
* validator/oracle programs must be absolute, resolved files below an
  explicitly approved root and are always launched with ``shell=False``;
* subprocess input, combined output, wall time and (on Linux) CPU, address
  space, file, descriptor and process limits are bounded.

Callers should construct these adapters from secret-bearing runtime config.
Do not persist the API key in an artifact manifest.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

import requests

from .artifact_generation import (
    AIContestContext,
    AIPracticeInput,
    AIProblemContext,
    AIProblemDraft,
    ProblemSnapshot,
)


class ArtifactAdapterError(RuntimeError):
    """Base error for an adapter that must stop artifact generation."""


class AdapterConfigurationError(ArtifactAdapterError):
    """An adapter is missing or its trusted configuration is unsafe."""


class AIProviderError(ArtifactAdapterError):
    """The AI endpoint failed or returned a non-conforming draft."""


class AdapterExecutionError(ArtifactAdapterError):
    """A trusted local executable could not be run within its policy."""


class InputRejectedError(AdapterExecutionError):
    """A trusted validator rejected a proposed practice input."""


class OracleExecutionError(AdapterExecutionError):
    """A trusted oracle did not produce a usable answer."""


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LEVELS = frozenset({"small", "typical", "stress", "edge"})
_MAX_PROMPT_STATEMENT_BYTES = 2 * 1024 * 1024
_MAX_AI_INPUT_BYTES = 2 * 1024 * 1024


def _ai_text_bytes(
    name: str,
    value: Any,
    maximum: int,
    *,
    allow_empty: bool = False,
    forbid_nul: bool = False,
) -> bytes:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise AIProviderError(f"{name} is missing")
    try:
        payload = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AIProviderError(f"{name} is not valid Unicode text") from exc
    if len(payload) > maximum or (forbid_nul and b"\x00" in payload):
        raise AIProviderError(f"{name} is binary or too large")
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _strict_json_loads(payload: str | bytes) -> Any:
    """Parse JSON while rejecting duplicate keys and non-finite numbers."""

    def object_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        payload,
        object_pairs_hook=object_pairs,
        parse_constant=_reject_json_constant,
    )


class JSONTransport(Protocol):
    """Injectable transport so adapter tests never call a real AI service."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, Any]: ...


class RequestsJSONTransport:
    """A bounded, strict-JSON HTTP transport based on ``requests``."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, Any]:
        try:
            response = requests.post(
                url,
                headers=dict(headers),
                json=dict(payload),
                timeout=(min(10.0, timeout_seconds), timeout_seconds),
                stream=True,
                allow_redirects=False,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise AIProviderError("AI endpoint request failed") from exc

        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            response.close()
            raise AIProviderError("AI endpoint did not return application/json")
        try:
            declared = int(response.headers.get("Content-Length", "0") or 0)
        except ValueError:
            declared = 0
        if declared > max_response_bytes:
            response.close()
            raise AIProviderError("AI endpoint response exceeds configured limit")

        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > max_response_bytes:
                    raise AIProviderError(
                        "AI endpoint response exceeds configured limit"
                    )
        finally:
            response.close()
        try:
            decoded = bytes(body).decode("utf-8")
            value = _strict_json_loads(decoded)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise AIProviderError("AI endpoint response is not strict UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise AIProviderError("AI endpoint JSON envelope must be an object")
        return value


def _is_loopback_host(host: str) -> bool:
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validated_endpoint(value: str, allow_insecure_loopback: bool) -> str:
    raw = str(value).strip()
    if not raw or len(raw) > 2048:
        raise AdapterConfigurationError("AI endpoint is missing or too long")
    parsed = urlsplit(raw)
    host = parsed.hostname or ""
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise AdapterConfigurationError("AI endpoint has an invalid port") from exc
    if (
        not parsed.netloc
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed_port == 0
    ):
        raise AdapterConfigurationError(
            "AI endpoint must be an absolute URL without credentials/query/fragment"
        )
    if parsed.scheme != "https" and not (
        allow_insecure_loopback
        and parsed.scheme == "http"
        and _is_loopback_host(host)
    ):
        raise AdapterConfigurationError(
            "AI endpoint must use HTTPS (HTTP is only allowed for opted-in loopback)"
        )
    return raw


def _required_secret(name: str, value: Any, maximum_bytes: int) -> str:
    if not isinstance(value, str):
        raise AdapterConfigurationError(f"{name} is required")
    stripped = value.strip()
    if (
        not stripped
        or len(stripped.encode("utf-8")) > maximum_bytes
        or _CONTROL.search(stripped)
    ):
        raise AdapterConfigurationError(f"{name} is missing or invalid")
    return stripped


def _positive_number(
    name: str, value: Any, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdapterConfigurationError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise AdapterConfigurationError(f"{name} is outside its safe range")
    return number


def _positive_integer(
    name: str, value: Any, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdapterConfigurationError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise AdapterConfigurationError(f"{name} is outside its safe range")
    return value


def _draft_json_schema(practice_case_count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["statement_markdown", "practice_inputs"],
        "properties": {
            "statement_markdown": {"type": "string", "minLength": 1},
            "practice_inputs": {
                "type": "array",
                "minItems": practice_case_count,
                "maxItems": practice_case_count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["input_data", "level", "rationale"],
                    "properties": {
                        "input_data": {"type": "string", "minLength": 1},
                        "level": {
                            "type": "string",
                            "enum": sorted(_LEVELS),
                        },
                        "rationale": {"type": "string"},
                    },
                },
            },
        },
    }


class OpenAICompatibleArtifactProvider:
    """Strict OpenAI ``chat/completions`` provider for sanitized drafts."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 90.0,
        max_response_bytes: int = 4 * 1024 * 1024,
        allow_insecure_loopback: bool = False,
        transport: JSONTransport | None = None,
    ):
        self.endpoint = _validated_endpoint(endpoint, allow_insecure_loopback)
        self._api_key = _required_secret("AI api_key", api_key, 8192)
        # artifact_generation caps provider_id at 128 characters.  Leave room
        # for this adapter's stable prefix so a valid config cannot fail later.
        self.model = _required_secret("AI model", model, 96)
        self.timeout_seconds = _positive_number(
            "AI timeout_seconds", timeout_seconds, 1.0, 600.0
        )
        self.max_response_bytes = _positive_integer(
            "AI max_response_bytes",
            max_response_bytes,
            1024,
            32 * 1024 * 1024,
        )
        self.transport = transport or RequestsJSONTransport()

    @property
    def provider_id(self) -> str:
        # Deliberately excludes the endpoint and, of course, the API key.
        return f"openai-compatible/{self.model}"

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        transport: JSONTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "OpenAICompatibleArtifactProvider":
        if not isinstance(config, Mapping):
            raise AdapterConfigurationError("AI provider config must be an object")
        allowed = {
            "endpoint",
            "api_key",
            "api_key_env",
            "model",
            "timeout_seconds",
            "max_response_bytes",
            "allow_insecure_loopback",
        }
        unknown = set(config) - allowed
        if unknown:
            raise AdapterConfigurationError(
                "Unknown AI provider config fields: " + ", ".join(sorted(unknown))
            )
        insecure = config.get("allow_insecure_loopback", False)
        if not isinstance(insecure, bool):
            raise AdapterConfigurationError(
                "AI allow_insecure_loopback must be a boolean"
            )
        api_key = config.get("api_key", "")
        api_key_env = config.get("api_key_env", "")
        if api_key and api_key_env:
            raise AdapterConfigurationError(
                "configure either AI api_key or api_key_env, not both"
            )
        if api_key_env:
            if (
                not isinstance(api_key_env, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", api_key_env)
            ):
                raise AdapterConfigurationError("AI api_key_env name is invalid")
            source = os.environ if environ is None else environ
            api_key = source.get(api_key_env, "")
        return cls(
            endpoint=config.get("endpoint", ""),
            api_key=api_key,
            model=config.get("model", ""),
            timeout_seconds=config.get("timeout_seconds", 90.0),
            max_response_bytes=config.get(
                "max_response_bytes", 4 * 1024 * 1024
            ),
            allow_insecure_loopback=insecure,
            transport=transport,
        )

    @staticmethod
    def _safe_snapshot(
        contest: AIContestContext,
        problem: AIProblemContext,
        practice_case_count: int,
    ) -> dict[str, Any]:
        if not isinstance(contest, AIContestContext) or not isinstance(
            problem, AIProblemContext
        ):
            raise AIProviderError("AI provider accepts sanitized context objects only")
        if not 2 <= practice_case_count <= 4:
            raise AIProviderError("practice case count must be between 2 and 4")
        _ai_text_bytes(
            "problem statement",
            problem.statement_markdown,
            _MAX_PROMPT_STATEMENT_BYTES,
        )
        # List every transmitted field. Never use ``asdict`` or ``__dict__``:
        # future local-only fields must not silently cross the AI boundary.
        return {
            "contest": {
                "title": contest.title,
                "subtitle": contest.subtitle,
                "begin_at_ms": contest.begin_at_ms,
                "end_at_ms": contest.end_at_ms,
            },
            "problem": {
                "pid": problem.pid,
                "slug": problem.slug,
                "title": problem.title,
                "statement_markdown": problem.statement_markdown,
                "input_filename": problem.input_filename,
                "output_filename": problem.output_filename,
                "time_limit_ms": problem.time_limit_ms,
                "memory_limit_mb": problem.memory_limit_mb,
            },
            "practice_case_count": practice_case_count,
        }

    @staticmethod
    def _parse_draft(content: Any, practice_case_count: int) -> AIProblemDraft:
        if not isinstance(content, str):
            raise AIProviderError("AI message content must be a JSON string")
        try:
            draft = _strict_json_loads(content)
        except (ValueError, json.JSONDecodeError) as exc:
            raise AIProviderError("AI draft is not strict JSON") from exc
        if not isinstance(draft, dict) or set(draft) != {
            "statement_markdown",
            "practice_inputs",
        }:
            raise AIProviderError("AI draft has missing or unknown root fields")
        statement = draft["statement_markdown"]
        cases = draft["practice_inputs"]
        _ai_text_bytes(
            "AI statement_markdown", statement, _MAX_PROMPT_STATEMENT_BYTES
        )
        if not isinstance(cases, list) or len(cases) != practice_case_count:
            raise AIProviderError("AI practice_inputs count does not match request")

        result: list[AIPracticeInput] = []
        levels: set[str] = set()
        for index, item in enumerate(cases, start=1):
            if not isinstance(item, dict) or set(item) != {
                "input_data",
                "level",
                "rationale",
            }:
                raise AIProviderError(
                    f"AI practice input {index} has missing or unknown fields"
                )
            input_text = item["input_data"]
            level = item["level"]
            rationale = item["rationale"]
            input_data = _ai_text_bytes(
                f"AI practice input {index}",
                input_text,
                _MAX_AI_INPUT_BYTES,
                forbid_nul=True,
            )
            if not isinstance(level, str) or level not in _LEVELS:
                raise AIProviderError(f"AI practice input {index} level is invalid")
            if level in levels:
                raise AIProviderError("AI practice input levels must be distinct")
            levels.add(level)
            _ai_text_bytes(
                f"AI practice input {index} rationale",
                rationale,
                4096,
                allow_empty=True,
                forbid_nul=True,
            )
            result.append(AIPracticeInput(input_data, level, rationale))
        return AIProblemDraft(statement.strip(), tuple(result))

    def generate_problem(
        self,
        contest: AIContestContext,
        problem: AIProblemContext,
        practice_case_count: int,
    ) -> AIProblemDraft:
        snapshot = self._safe_snapshot(contest, problem, practice_case_count)
        schema = _draft_json_schema(practice_case_count)
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你只生成 CSP 风格题面草稿和互不重复的文本自测输入。"
                        "不得输出题解、参考程序、正式测试数据或测试数据摘要。"
                        "严格按 response_format JSON Schema 返回。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"task": "draft_csp_artifacts", "snapshot": snapshot},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "csp_artifact_draft",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        envelope = self.transport.post_json(
            url=self.endpoint,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            payload=payload,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
        )
        if not isinstance(envelope, Mapping):
            raise AIProviderError("AI endpoint response envelope must be an object")
        choices = envelope.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise AIProviderError("AI endpoint must return exactly one choice")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise AIProviderError("AI endpoint choice is invalid")
        message = choice.get("message")
        if not isinstance(message, Mapping) or "content" not in message:
            raise AIProviderError("AI endpoint message is missing content")
        return self._parse_draft(message["content"], practice_case_count)


@dataclass(frozen=True)
class ExecutablePolicy:
    """One resolved trusted executable and its fixed launch policy."""

    executable: Path
    args: tuple[str, ...] = ()
    timeout_seconds: float = 3.0
    max_input_bytes: int = 16 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    cpu_seconds: int = 3
    memory_limit_bytes: int = 512 * 1024 * 1024
    open_files: int = 64
    processes: int = 8


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessRunner(Protocol):
    def __call__(self, policy: ExecutablePolicy, input_data: bytes) -> ProcessResult: ...


def _minimal_environment() -> dict[str, str]:
    if os.name == "nt":
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
        }
        return {key: value for key, value in environment.items() if value}
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _linux_resource_limiter(policy: ExecutablePolicy):
    if os.name != "posix":
        return None

    def apply_limits() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(
            resource.RLIMIT_CPU, (policy.cpu_seconds, policy.cpu_seconds)
        )
        resource.setrlimit(
            resource.RLIMIT_AS,
            (policy.memory_limit_bytes, policy.memory_limit_bytes),
        )
        # This covers files opened by the tool. stdout/stderr are separately
        # bounded while they are drained from pipes by the parent.
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (policy.max_output_bytes, policy.max_output_bytes),
        )
        resource.setrlimit(
            resource.RLIMIT_NOFILE, (policy.open_files, policy.open_files)
        )
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(
                resource.RLIMIT_NPROC, (policy.processes, policy.processes)
            )

    return apply_limits


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass


class _CombinedCapture:
    def __init__(self, maximum: int, process: subprocess.Popen[bytes]):
        self.maximum = maximum
        self.process = process
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.total = 0
        self.exceeded = threading.Event()
        self.lock = threading.Lock()

    def drain(self, stream, destination: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                with self.lock:
                    remaining = self.maximum - self.total
                    if remaining > 0:
                        destination.extend(chunk[:remaining])
                        self.total += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        self.exceeded.set()
                        _terminate_process(self.process)
        finally:
            stream.close()


def run_trusted_executable(
    policy: ExecutablePolicy, input_data: bytes
) -> ProcessResult:
    """Run one approved executable without a shell and with hard bounds."""
    if not isinstance(input_data, bytes):
        raise AdapterExecutionError("trusted executable input must be bytes")
    if len(input_data) > policy.max_input_bytes:
        raise AdapterExecutionError("trusted executable input exceeds configured limit")

    argv = [str(policy.executable), *policy.args]
    popen_kwargs: dict[str, Any] = {
        "args": argv,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": str(policy.executable.parent),
        "env": _minimal_environment(),
        "shell": False,
        "close_fds": True,
    }
    limiter = _linux_resource_limiter(policy)
    if limiter is not None:
        popen_kwargs["preexec_fn"] = limiter
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(**popen_kwargs)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise AdapterExecutionError("failed to start trusted executable") from exc
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    capture = _CombinedCapture(policy.max_output_bytes, process)
    output_thread = threading.Thread(
        target=capture.drain,
        args=(process.stdout, capture.stdout),
        daemon=True,
    )
    error_thread = threading.Thread(
        target=capture.drain,
        args=(process.stderr, capture.stderr),
        daemon=True,
    )
    writer_error: list[BaseException] = []

    def write_input() -> None:
        try:
            process.stdin.write(input_data)
            process.stdin.flush()
        except BrokenPipeError:
            pass
        except BaseException as exc:  # saved and re-raised on the caller thread
            writer_error.append(exc)
        finally:
            process.stdin.close()

    input_thread = threading.Thread(target=write_input, daemon=True)
    output_thread.start()
    error_thread.start()
    input_thread.start()
    timed_out = False
    try:
        process.wait(timeout=policy.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process(process)
        process.wait(timeout=2)
    input_thread.join(timeout=2)
    output_thread.join(timeout=2)
    error_thread.join(timeout=2)

    if timed_out:
        raise AdapterExecutionError("trusted executable exceeded wall-time limit")
    if capture.exceeded.is_set():
        raise AdapterExecutionError("trusted executable exceeded output limit")
    if writer_error:
        raise AdapterExecutionError("failed to send input to trusted executable") from writer_error[0]
    return ProcessResult(
        int(process.returncode), bytes(capture.stdout), bytes(capture.stderr)
    )


def _diagnostic(stderr: bytes) -> str:
    text = stderr.decode("utf-8", errors="replace").strip()
    text = " ".join(text.split())
    return text[:500]


class LocalExecutableValidator:
    """Validator protocol adapter: stdin=input, exit 0 means accepted."""

    def __init__(
        self,
        slug: str,
        policy: ExecutablePolicy,
        *,
        runner: ProcessRunner = run_trusted_executable,
    ):
        self.slug = slug
        self.policy = policy
        self.runner = runner

    def validate(self, problem: ProblemSnapshot, input_data: bytes) -> None:
        if problem.slug != self.slug:
            raise AdapterExecutionError("validator used for the wrong problem")
        result = self.runner(self.policy, input_data)
        if result.returncode != 0:
            diagnostic = _diagnostic(result.stderr)
            suffix = f": {diagnostic}" if diagnostic else ""
            raise InputRejectedError(
                f"validator rejected {self.slug} with exit {result.returncode}{suffix}"
            )


class LocalExecutableOracle:
    """Oracle protocol adapter: stdin=input, stdout=trusted expected output."""

    def __init__(
        self,
        slug: str,
        policy: ExecutablePolicy,
        *,
        runner: ProcessRunner = run_trusted_executable,
    ):
        self.slug = slug
        self.policy = policy
        self.runner = runner

    def solve(self, problem: ProblemSnapshot, input_data: bytes) -> bytes:
        if problem.slug != self.slug:
            raise AdapterExecutionError("oracle used for the wrong problem")
        result = self.runner(self.policy, input_data)
        if result.returncode != 0:
            diagnostic = _diagnostic(result.stderr)
            suffix = f": {diagnostic}" if diagnostic else ""
            raise OracleExecutionError(
                f"oracle failed for {self.slug} with exit {result.returncode}{suffix}"
            )
        return result.stdout


def _approved_roots(raw: Any) -> tuple[Path, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise AdapterConfigurationError("approved_roots must be a non-empty list")
    roots: list[Path] = []
    for value in raw:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise AdapterConfigurationError("approved roots must be absolute paths")
        try:
            root = Path(value).resolve(strict=True)
        except OSError as exc:
            raise AdapterConfigurationError("approved root does not exist") from exc
        if not root.is_dir():
            raise AdapterConfigurationError("approved root must be a directory")
        roots.append(root)
    return tuple(dict.fromkeys(roots))


def _inside_root(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _policy_from_config(
    raw: Any,
    *,
    roots: Sequence[Path],
    defaults: Mapping[str, Any],
) -> ExecutablePolicy:
    if not isinstance(raw, Mapping):
        raise AdapterConfigurationError("executable mapping must be an object")
    allowed = {
        "executable",
        "args",
        "timeout_seconds",
        "max_input_bytes",
        "max_output_bytes",
        "cpu_seconds",
        "memory_limit_bytes",
        "open_files",
        "processes",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise AdapterConfigurationError(
            "unknown executable fields: " + ", ".join(sorted(unknown))
        )
    executable_value = raw.get("executable")
    if not isinstance(executable_value, str) or not Path(executable_value).is_absolute():
        raise AdapterConfigurationError("trusted executable must be an absolute path")
    try:
        executable = Path(executable_value).resolve(strict=True)
    except OSError as exc:
        raise AdapterConfigurationError("trusted executable does not exist") from exc
    if not executable.is_file() or not _inside_root(executable, roots):
        raise AdapterConfigurationError(
            "trusted executable must resolve to a file inside approved_roots"
        )
    if os.name == "posix" and not os.access(executable, os.X_OK):
        raise AdapterConfigurationError("trusted executable is not executable")

    args_raw = raw.get("args", ())
    if not isinstance(args_raw, (list, tuple)):
        raise AdapterConfigurationError("trusted executable args must be a list")
    args: list[str] = []
    for value in args_raw:
        if (
            not isinstance(value, str)
            or "\x00" in value
            or len(value.encode("utf-8")) > 4096
        ):
            raise AdapterConfigurationError("trusted executable has an invalid argument")
        args.append(value)
    if sum(len(value.encode("utf-8")) for value in args) > 16 * 1024:
        raise AdapterConfigurationError("trusted executable argument list is too large")

    def setting(name: str, fallback: Any) -> Any:
        return raw[name] if name in raw else defaults.get(name, fallback)

    timeout = _positive_number(
        "executable timeout_seconds", setting("timeout_seconds", 3.0), 0.05, 60.0
    )
    max_input = _positive_integer(
        "executable max_input_bytes",
        setting("max_input_bytes", 16 * 1024 * 1024),
        1,
        64 * 1024 * 1024,
    )
    max_output = _positive_integer(
        "executable max_output_bytes",
        setting("max_output_bytes", 16 * 1024 * 1024),
        1,
        64 * 1024 * 1024,
    )
    cpu = _positive_integer(
        "executable cpu_seconds", setting("cpu_seconds", 3), 1, 60
    )
    memory = _positive_integer(
        "executable memory_limit_bytes",
        setting("memory_limit_bytes", 512 * 1024 * 1024),
        64 * 1024 * 1024,
        8 * 1024 * 1024 * 1024,
    )
    open_files = _positive_integer(
        "executable open_files", setting("open_files", 64), 16, 1024
    )
    processes = _positive_integer(
        "executable processes", setting("processes", 8), 1, 128
    )
    return ExecutablePolicy(
        executable,
        tuple(args),
        timeout,
        max_input,
        max_output,
        cpu,
        memory,
        open_files,
        processes,
    )


class TrustedExecutableAdapterRegistry:
    """Fail-closed per-problem validator and oracle registry.

    Expected configuration::

        approved_roots: [/opt/noi-artifact-tools]
        defaults: {timeout_seconds: 3, memory_limit_bytes: 536870912}
        validators:
          apple: {executable: /opt/noi-artifact-tools/apple-validator}
        oracles:
          apple: {executable: /opt/noi-artifact-tools/apple-oracle}
    """

    def __init__(
        self,
        validators: Mapping[str, LocalExecutableValidator],
        oracles: Mapping[str, LocalExecutableOracle],
    ):
        self._validators = dict(validators)
        self._oracles = dict(oracles)

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        runner: ProcessRunner = run_trusted_executable,
    ) -> "TrustedExecutableAdapterRegistry":
        if not isinstance(config, Mapping):
            raise AdapterConfigurationError("trusted tool config must be an object")
        allowed = {"approved_roots", "defaults", "validators", "oracles"}
        unknown = set(config) - allowed
        if unknown:
            raise AdapterConfigurationError(
                "unknown trusted tool config fields: "
                + ", ".join(sorted(unknown))
            )
        roots = _approved_roots(config.get("approved_roots"))
        defaults = config.get("defaults", {})
        if not isinstance(defaults, Mapping):
            raise AdapterConfigurationError("trusted tool defaults must be an object")
        allowed_defaults = {
            "timeout_seconds",
            "max_input_bytes",
            "max_output_bytes",
            "cpu_seconds",
            "memory_limit_bytes",
            "open_files",
            "processes",
        }
        unknown_defaults = set(defaults) - allowed_defaults
        if unknown_defaults:
            raise AdapterConfigurationError(
                "unknown trusted tool defaults: "
                + ", ".join(sorted(unknown_defaults))
            )
        validators_raw = config.get("validators", {})
        oracles_raw = config.get("oracles", {})
        if not isinstance(validators_raw, Mapping) or not isinstance(
            oracles_raw, Mapping
        ):
            raise AdapterConfigurationError("validators and oracles must be objects")

        validators: dict[str, LocalExecutableValidator] = {}
        oracles: dict[str, LocalExecutableOracle] = {}
        for slug, raw in validators_raw.items():
            if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
                raise AdapterConfigurationError("validator problem slug is invalid")
            validators[slug] = LocalExecutableValidator(
                slug,
                _policy_from_config(raw, roots=roots, defaults=defaults),
                runner=runner,
            )
        for slug, raw in oracles_raw.items():
            if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
                raise AdapterConfigurationError("oracle problem slug is invalid")
            oracles[slug] = LocalExecutableOracle(
                slug,
                _policy_from_config(raw, roots=roots, defaults=defaults),
                runner=runner,
            )
        return cls(validators, oracles)

    def validator_for(self, slug: str) -> LocalExecutableValidator:
        try:
            return self._validators[slug]
        except KeyError as exc:
            raise AdapterConfigurationError(
                f"missing trusted validator mapping for {slug}"
            ) from exc

    def oracle_for(self, slug: str) -> LocalExecutableOracle:
        try:
            return self._oracles[slug]
        except KeyError as exc:
            raise AdapterConfigurationError(
                f"missing trusted oracle mapping for {slug}"
            ) from exc

    def adapters_for(
        self, slugs: Sequence[str]
    ) -> tuple[dict[str, LocalExecutableValidator], dict[str, LocalExecutableOracle]]:
        validators: dict[str, LocalExecutableValidator] = {}
        oracles: dict[str, LocalExecutableOracle] = {}
        for slug in slugs:
            validators[slug] = self.validator_for(slug)
            oracles[slug] = self.oracle_for(slug)
        return validators, oracles
