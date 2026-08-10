"""Fail-closed generation of teacher-reviewed CSP contest artifacts.

This module has no web, database, Hydro or deployment dependencies.  It turns
an immutable contest snapshot into one atomic, versioned release.  A caller is
expected to persist the release metadata and obtain teacher approval before
making the student files available.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import tarfile
from typing import Any, Mapping, Protocol, Sequence

from .csp_pdf import MarkdownDocument, PdfInspection, inspect_pdf, render_csp_pdf


class ArtifactGenerationError(RuntimeError):
    """A release is unsafe or incomplete and must not be published."""


class ArtifactAlreadyExistsError(ArtifactGenerationError):
    """A version is immutable once successfully generated."""


@dataclass(frozen=True)
class ProblemSnapshot:
    pid: str
    slug: str
    title: str
    statement_markdown: str
    input_filename: str | None
    output_filename: str | None
    time_limit_ms: int = 1000
    memory_limit_mb: int = 256
    source: Mapping[str, Any] = field(default_factory=dict)
    # Digests supplied by the trusted local caller. This permits comparison
    # against official tests without ever sending those tests to an AI service.
    forbidden_practice_input_sha256: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContestSnapshot:
    tid: str
    title: str
    subtitle: str
    begin_at_ms: int
    end_at_ms: int
    problems: tuple[ProblemSnapshot, ...]
    source: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactRequest:
    contest: ContestSnapshot
    revision: str
    practice_cases_per_problem: int = 3
    generation_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AIPracticeInput:
    input_data: bytes
    level: str
    rationale: str = ""


@dataclass(frozen=True)
class AIProblemDraft:
    statement_markdown: str
    practice_inputs: tuple[AIPracticeInput, ...]


@dataclass(frozen=True)
class AIContestContext:
    """The minimum contest metadata allowed to cross the AI boundary."""

    title: str
    subtitle: str
    begin_at_ms: int
    end_at_ms: int


@dataclass(frozen=True)
class AIProblemContext:
    """Sanitized statement data; never contains official tests or raw config."""

    pid: str
    slug: str
    title: str
    statement_markdown: str
    input_filename: str
    output_filename: str
    time_limit_ms: int
    memory_limit_mb: int


class AIArtifactProvider(Protocol):
    """AI boundary. Provider implementations must not receive official tests."""

    @property
    def provider_id(self) -> str: ...

    def generate_problem(
        self,
        contest: AIContestContext,
        problem: AIProblemContext,
        practice_case_count: int,
    ) -> AIProblemDraft: ...


class InputValidator(Protocol):
    def validate(self, problem: ProblemSnapshot, input_data: bytes) -> None: ...


class OutputOracle(Protocol):
    def solve(self, problem: ProblemSnapshot, input_data: bytes) -> bytes: ...


class PdfRenderer(Protocol):
    def __call__(
        self,
        contest_title: str,
        subtitle: str,
        documents: Sequence[MarkdownDocument],
        destination: str | Path,
    ) -> PdfInspection: ...


@dataclass(frozen=True)
class FileIOChange:
    pid: str
    slug: str
    old_input_filename: str | None
    old_output_filename: str | None
    required_input_filename: str
    required_output_filename: str


@dataclass(frozen=True)
class ArtifactRelease:
    tid: str
    revision: str
    directory: Path
    manifest_path: Path
    paper_path: Path
    testdata_path: Path
    manifest: Mapping[str, Any]
    pdf: PdfInspection


_TID = re.compile(r"^[0-9a-fA-F]{24}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LEAK_HEADING = re.compile(
    r"(?im)^#{1,6}\s*(?:题解|解法|参考程序|标准程序|solution)\s*$"
)
_SOURCE_IN_CODE = re.compile(
    r"(?is)```[^\n]*\n.*?(?:#\s*include\s*[<\"]|\bint\s+main\s*\().*?```"
)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"无法序列化 {type(value).__name__}")


def _canonical_json(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=_json_default,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArtifactGenerationError(f"题目快照不能安全序列化: {exc}") from exc


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_input_digest(payload: bytes) -> str:
    """Stable local-only fingerprint for sample/official data comparison."""
    normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    normalized = b"\n".join(line.rstrip() for line in normalized.split(b"\n")).strip()
    return sha256_bytes(normalized)


def plan_file_io_changes(contest: ContestSnapshot) -> tuple[FileIOChange, ...]:
    """Return the Hydro changes that must be applied and re-read first."""
    changes: list[FileIOChange] = []
    for problem in contest.problems:
        required_input = f"{problem.slug}.in"
        required_output = f"{problem.slug}.out"
        if (
            problem.input_filename != required_input
            or problem.output_filename != required_output
        ):
            changes.append(
                FileIOChange(
                    problem.pid,
                    problem.slug,
                    problem.input_filename,
                    problem.output_filename,
                    required_input,
                    required_output,
                )
            )
    return tuple(changes)


def _validate_request(request: ArtifactRequest) -> None:
    contest = request.contest
    if not _TID.fullmatch(contest.tid):
        raise ArtifactGenerationError("比赛 tid 必须是 24 位 ObjectId")
    if not _REVISION.fullmatch(request.revision):
        raise ArtifactGenerationError("材料版本号只能包含安全字符且最长 64 字符")
    if not contest.title.strip():
        raise ArtifactGenerationError("比赛标题不能为空")
    if len(contest.title) > 200 or len(contest.subtitle) > 500:
        raise ArtifactGenerationError("比赛标题或副标题过长")
    if contest.end_at_ms <= contest.begin_at_ms:
        raise ArtifactGenerationError("比赛结束时间必须晚于开始时间")
    if not contest.problems:
        raise ArtifactGenerationError("比赛没有题目")
    if not 2 <= request.practice_cases_per_problem <= 4:
        raise ArtifactGenerationError("每题自测数据必须设置为 2 到 4 组")
    if len(request.generation_warnings) > 200:
        raise ArtifactGenerationError("材料生成警告数量过多")
    for warning in request.generation_warnings:
        if (
            not isinstance(warning, str)
            or not warning.strip()
            or len(warning.encode("utf-8")) > 4096
        ):
            raise ArtifactGenerationError("材料生成警告格式无效")
    seen_slugs: set[str] = set()
    seen_pids: set[str] = set()
    for problem in contest.problems:
        if not _SLUG.fullmatch(problem.slug):
            raise ArtifactGenerationError(
                f"题目 {problem.pid} 的英文目录名不安全: {problem.slug}"
            )
        if problem.slug in seen_slugs or problem.pid in seen_pids:
            raise ArtifactGenerationError("比赛题目 pid 或英文目录名重复")
        seen_slugs.add(problem.slug)
        seen_pids.add(problem.pid)
        if not problem.pid.strip() or not problem.title.strip():
            raise ArtifactGenerationError("题目 pid 和标题不能为空")
        if len(problem.pid) > 128 or len(problem.title) > 200:
            raise ArtifactGenerationError("题目 pid 或标题过长")
        if len(problem.statement_markdown.encode("utf-8")) > 2 * 1024 * 1024:
            raise ArtifactGenerationError(
                f"题目 {problem.slug} 的原始题面超过 2 MiB"
            )
        if problem.time_limit_ms <= 0 or problem.memory_limit_mb <= 0:
            raise ArtifactGenerationError(f"题目 {problem.slug} 的时限或内存无效")
        for digest in problem.forbidden_practice_input_sha256:
            if not _DIGEST.fullmatch(digest):
                raise ArtifactGenerationError(
                    f"题目 {problem.slug} 的禁止数据摘要格式无效"
                )


def _assert_no_student_leak(markdown: str, slug: str) -> None:
    if _LEAK_HEADING.search(markdown) or _SOURCE_IN_CODE.search(markdown):
        raise ArtifactGenerationError(
            f"题目 {slug} 的 AI 题面疑似包含题解或参考程序，已阻断"
        )


def _problem_markdown(problem: ProblemSnapshot, generated: str) -> str:
    generated = generated.strip()
    if not generated:
        raise ArtifactGenerationError(f"题目 {problem.slug} 的 AI 题面为空")
    if len(generated.encode("utf-8")) > 2 * 1024 * 1024:
        raise ArtifactGenerationError(
            f"题目 {problem.slug} 的 AI 题面超过 2 MiB"
        )
    _assert_no_student_leak(generated, problem.slug)
    # File I/O is inserted by trusted code rather than left to model wording.
    return (
        f"# {problem.title}（{problem.slug}）\n\n"
        "## 文件读写\n\n"
        f"程序从 `{problem.input_filename}` 读入数据，并将答案写入 "
        f"`{problem.output_filename}`。\n\n"
        f"{generated}\n"
    )


def _contest_markdown(contest: ContestSnapshot) -> str:
    begin = datetime.fromtimestamp(contest.begin_at_ms / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(contest.end_at_ms / 1000, tz=timezone.utc)
    lines = [
        f"# {contest.title}",
        "",
        contest.subtitle,
        "",
        f"- 开始时间（UTC）：{begin.isoformat()}",
        f"- 结束时间（UTC）：{end.isoformat()}",
        "",
        "| 题目 | 目录 | 输入文件 | 输出文件 | 时间限制 | 内存限制 |",
        "|---|---|---|---|---|---|",
    ]
    for problem in contest.problems:
        lines.append(
            f"| {problem.title} | {problem.slug} | {problem.input_filename} | "
            f"{problem.output_filename} | {problem.time_limit_ms} ms | "
            f"{problem.memory_limit_mb} MiB |"
        )
    return "\n".join(lines) + "\n"


def _safe_provider_id(provider: AIArtifactProvider) -> str:
    try:
        value = str(provider.provider_id).strip()
    except Exception as exc:
        raise ArtifactGenerationError("无法读取 AI provider 标识") from exc
    if not value or len(value) > 128 or any(ord(ch) < 32 for ch in value):
        raise ArtifactGenerationError("AI provider 标识为空或不安全")
    return value


def validate_artifact_preconditions(
    request: ArtifactRequest,
    *,
    ai_provider: AIArtifactProvider | None,
    validators: Mapping[str, InputValidator],
    oracles: Mapping[str, OutputOracle],
) -> str:
    """Validate every generation prerequisite that needs no AI/tool execution.

    The orchestration layer calls this before it performs an irreversible
    Hydro private-problem clone.  ``generate`` calls it again at the release
    boundary, so a caller cannot accidentally bypass the same fail-closed
    checks.  It intentionally does not contact the AI provider or execute any
    local tool.
    """
    _validate_request(request)
    if ai_provider is None:
        raise ArtifactGenerationError("未配置 AI provider，不能生成备赛材料")
    provider_id = _safe_provider_id(ai_provider)
    changes = plan_file_io_changes(request.contest)
    if changes:
        details = ", ".join(
            f"{item.slug}->{item.required_input_filename}/{item.required_output_filename}"
            for item in changes
        )
        raise ArtifactGenerationError(
            "题目文件读写配置尚未应用并重新读取，不能发布: " + details
        )
    if not isinstance(validators, Mapping) or not isinstance(oracles, Mapping):
        raise ArtifactGenerationError("可信 validator/oracle 注册表格式无效")
    for problem in request.contest.problems:
        if problem.slug not in validators:
            raise ArtifactGenerationError(
                f"题目 {problem.slug} 缺少可信 input validator"
            )
        if problem.slug not in oracles:
            raise ArtifactGenerationError(
                f"题目 {problem.slug} 缺少可信 output oracle"
            )
    return provider_id


def _ai_context(
    contest: ContestSnapshot, problem: ProblemSnapshot
) -> tuple[AIContestContext, AIProblemContext]:
    """Create the only objects handed to an external AI implementation."""
    return (
        AIContestContext(
            contest.title,
            contest.subtitle,
            contest.begin_at_ms,
            contest.end_at_ms,
        ),
        AIProblemContext(
            problem.pid,
            problem.slug,
            problem.title,
            problem.statement_markdown,
            str(problem.input_filename),
            str(problem.output_filename),
            problem.time_limit_ms,
            problem.memory_limit_mb,
        ),
    )


def _call_validator(
    validator: InputValidator, problem: ProblemSnapshot, input_data: bytes
) -> None:
    try:
        result = validator.validate(problem, input_data)
    except Exception as exc:
        raise ArtifactGenerationError(
            f"题目 {problem.slug} 的输入没有通过 validator: {exc}"
        ) from exc
    if result is False:
        raise ArtifactGenerationError(
            f"题目 {problem.slug} 的 validator 明确拒绝了输入"
        )


def _call_oracle_twice(
    oracle: OutputOracle, problem: ProblemSnapshot, input_data: bytes
) -> bytes:
    try:
        first = oracle.solve(problem, input_data)
        second = oracle.solve(problem, input_data)
    except Exception as exc:
        raise ArtifactGenerationError(
            f"题目 {problem.slug} 的可信 oracle 运行失败: {exc}"
        ) from exc
    if not isinstance(first, bytes) or not isinstance(second, bytes):
        raise ArtifactGenerationError(
            f"题目 {problem.slug} 的 oracle 必须返回 bytes"
        )
    if first != second:
        raise ArtifactGenerationError(
            f"题目 {problem.slug} 的 oracle 输出不确定，已阻断"
        )
    if len(first) > 16 * 1024 * 1024:
        raise ArtifactGenerationError(
            f"题目 {problem.slug} 的自测输出超过 16 MiB"
        )
    return first


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _deterministic_testdata_archive(
    output_path: Path, files: Mapping[str, bytes], allowed_slugs: set[str]
) -> None:
    """Write a gzip tar whose members can only be ``slug/N.in|N.out``."""
    expected = re.compile(r"^([a-z][a-z0-9_]{0,63})/([1-4])\.(in|out)$")
    for name in files:
        match = expected.fullmatch(name)
        path = PurePosixPath(name)
        if (
            not match
            or match.group(1) not in allowed_slugs
            or path.is_absolute()
            or ".." in path.parts
        ):
            raise ArtifactGenerationError(
                f"学生测试数据包出现不允许的文件: {name}"
            )
    with output_path.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for slug in sorted(allowed_slugs):
                    directory = tarfile.TarInfo(slug)
                    directory.type = tarfile.DIRTYPE
                    directory.mode = 0o555
                    directory.mtime = 0
                    archive.addfile(directory)
                for name in sorted(files):
                    payload = files[name]
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    member.mode = 0o444
                    member.mtime = 0
                    archive.addfile(member, io.BytesIO(payload))


def verify_student_testdata_archive(
    archive_path: str | Path, allowed_slugs: Sequence[str]
) -> tuple[str, ...]:
    """Re-open a student archive and enforce the final distribution whitelist."""
    slugs = set(allowed_slugs)
    pattern = re.compile(r"^([a-z][a-z0-9_]{0,63})/([1-4])\.(in|out)$")
    names: list[str] = []
    pairs: dict[tuple[str, str], set[str]] = {}
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.isdir():
                    if member.name not in slugs:
                        raise ArtifactGenerationError(
                            f"学生测试数据包包含未知目录: {member.name}"
                        )
                    continue
                if not member.isfile():
                    raise ArtifactGenerationError(
                        f"学生测试数据包包含特殊文件: {member.name}"
                    )
                match = pattern.fullmatch(member.name)
                if not match or match.group(1) not in slugs:
                    raise ArtifactGenerationError(
                        f"学生测试数据包包含非 .in/.out 文件: {member.name}"
                    )
                names.append(member.name)
                pairs.setdefault((match.group(1), match.group(2)), set()).add(
                    match.group(3)
                )
    except (tarfile.TarError, OSError) as exc:
        raise ArtifactGenerationError(f"学生测试数据包无法复核: {exc}") from exc
    incomplete = [f"{slug}/{number}" for (slug, number), ext in pairs.items() if ext != {"in", "out"}]
    if incomplete:
        raise ArtifactGenerationError(
            "学生测试数据存在不完整的输入输出对: " + ", ".join(incomplete)
        )
    for slug in slugs:
        if not any(key[0] == slug for key in pairs):
            raise ArtifactGenerationError(f"学生测试数据缺少题目目录: {slug}")
    return tuple(sorted(names))


def _manifest_files(root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(value for value in root.rglob("*") if value.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        audience = "student" if relative.startswith("student/") else "teacher"
        files.append(
            {
                "path": relative,
                "audience": audience,
                "size": path.stat().st_size,
                "sha256": sha256_path(path),
            }
        )
    return files


class ArtifactGenerationService:
    """Generate one immutable release or leave no publishable directory."""

    def __init__(
        self,
        root: str | Path,
        *,
        pdf_renderer: PdfRenderer = render_csp_pdf,
    ):
        self.root = Path(root)
        self.pdf_renderer = pdf_renderer

    def generate(
        self,
        request: ArtifactRequest,
        *,
        ai_provider: AIArtifactProvider | None,
        validators: Mapping[str, InputValidator],
        oracles: Mapping[str, OutputOracle],
    ) -> ArtifactRelease:
        provider_id = validate_artifact_preconditions(
            request,
            ai_provider=ai_provider,
            validators=validators,
            oracles=oracles,
        )

        contest_root = self.root / request.contest.tid
        destination = contest_root / request.revision
        if destination.exists():
            raise ArtifactAlreadyExistsError(
                f"材料版本 {request.revision} 已存在，版本不可覆盖"
            )
        contest_root.mkdir(parents=True, exist_ok=True)
        temporary = contest_root / (
            f".{request.revision}-{secrets.token_hex(8)}.generating"
        )
        temporary.mkdir(mode=0o700)

        pdf_inspection: PdfInspection | None = None
        try:
            teacher = temporary / "teacher"
            student = temporary / "student"
            markdown_root = teacher / "markdown"
            practice_root = teacher / "practice"
            markdown_root.mkdir(parents=True)
            practice_root.mkdir(parents=True)
            student.mkdir(parents=True)

            snapshot_payload = _canonical_json(asdict(request.contest))
            _write(teacher / "snapshot.json", snapshot_payload)
            contest_markdown = _contest_markdown(request.contest)
            _write(markdown_root / "contest.md", contest_markdown.encode("utf-8"))

            pdf_documents: list[MarkdownDocument] = []
            student_files: dict[str, bytes] = {}
            validation_problems: list[dict[str, Any]] = []
            for problem in request.contest.problems:
                ai_contest, ai_problem = _ai_context(request.contest, problem)
                try:
                    draft = ai_provider.generate_problem(
                        ai_contest,
                        ai_problem,
                        request.practice_cases_per_problem,
                    )
                except Exception as exc:
                    raise ArtifactGenerationError(
                        f"题目 {problem.slug} 的 AI 生成失败: {exc}"
                    ) from exc
                if not isinstance(draft, AIProblemDraft):
                    raise ArtifactGenerationError(
                        f"题目 {problem.slug} 的 AI provider 返回类型无效"
                    )
                if len(draft.practice_inputs) != request.practice_cases_per_problem:
                    raise ArtifactGenerationError(
                        f"题目 {problem.slug} 应生成 {request.practice_cases_per_problem} 组自测数据，"
                        f"实际为 {len(draft.practice_inputs)} 组"
                    )
                if any(
                    not isinstance(case, AIPracticeInput)
                    or not isinstance(case.input_data, bytes)
                    or not isinstance(case.level, str)
                    or not isinstance(case.rationale, str)
                    for case in draft.practice_inputs
                ):
                    raise ArtifactGenerationError(
                        f"题目 {problem.slug} 的 AI 自测数据类型无效"
                    )
                levels = [case.level.strip() for case in draft.practice_inputs]
                if any(not level for level in levels) or len(set(levels)) != len(levels):
                    raise ArtifactGenerationError(
                        f"题目 {problem.slug} 的自测梯度必须非空且互不重复"
                    )
                if any(
                    len(level) > 64 or any(ord(character) < 32 for character in level)
                    for level in levels
                ):
                    raise ArtifactGenerationError(
                        f"题目 {problem.slug} 的自测梯度标签不安全"
                    )

                problem_markdown = _problem_markdown(
                    problem, draft.statement_markdown
                )
                problem_md_path = markdown_root / "problems" / f"{problem.slug}.md"
                _write(problem_md_path, problem_markdown.encode("utf-8"))
                pdf_documents.append(
                    MarkdownDocument(
                        problem.slug,
                        problem.title,
                        problem_markdown,
                        str(problem.input_filename),
                        str(problem.output_filename),
                        problem.time_limit_ms,
                        problem.memory_limit_mb,
                    )
                )

                seen_inputs: set[str] = set()
                forbidden = set(problem.forbidden_practice_input_sha256)
                case_reports: list[dict[str, Any]] = []
                for case_index, case in enumerate(draft.practice_inputs, 1):
                    if not isinstance(case, AIPracticeInput) or not isinstance(
                        case.input_data, bytes
                    ):
                        raise ArtifactGenerationError(
                            f"题目 {problem.slug} 第 {case_index} 组输入类型无效"
                        )
                    if not case.input_data:
                        raise ArtifactGenerationError(
                            f"题目 {problem.slug} 第 {case_index} 组输入为空"
                        )
                    if len(case.input_data) > 16 * 1024 * 1024:
                        raise ArtifactGenerationError(
                            f"题目 {problem.slug} 第 {case_index} 组输入超过 16 MiB"
                        )
                    if len(case.rationale) > 4096:
                        raise ArtifactGenerationError(
                            f"题目 {problem.slug} 第 {case_index} 组数据说明过长"
                        )
                    normalized_digest = normalized_input_digest(case.input_data)
                    if normalized_digest in seen_inputs:
                        raise ArtifactGenerationError(
                            f"题目 {problem.slug} 的自测输入重复"
                        )
                    if normalized_digest in forbidden:
                        raise ArtifactGenerationError(
                            f"题目 {problem.slug} 的自测输入与样例或正式数据重复"
                        )
                    seen_inputs.add(normalized_digest)
                    _call_validator(
                        validators[problem.slug], problem, case.input_data
                    )
                    output_data = _call_oracle_twice(
                        oracles[problem.slug], problem, case.input_data
                    )
                    input_name = f"{problem.slug}/{case_index}.in"
                    output_name = f"{problem.slug}/{case_index}.out"
                    student_files[input_name] = case.input_data
                    student_files[output_name] = output_data
                    _write(practice_root / input_name, case.input_data)
                    _write(practice_root / output_name, output_data)
                    case_reports.append(
                        {
                            "case": case_index,
                            "level": case.level.strip(),
                            "rationale": case.rationale.strip(),
                            "input_size": len(case.input_data),
                            "input_sha256": sha256_bytes(case.input_data),
                            "normalized_input_sha256": normalized_digest,
                            "output_size": len(output_data),
                            "output_sha256": sha256_bytes(output_data),
                            "validator": "passed",
                            "oracle_deterministic": True,
                        }
                    )
                validation_problems.append(
                    {
                        "pid": problem.pid,
                        "slug": problem.slug,
                        "status": "passed",
                        "cases": case_reports,
                    }
                )

            validation_report = {
                "schema_version": 1,
                "status": "passed",
                "provider_id": provider_id,
                "warnings": list(request.generation_warnings),
                "problems": validation_problems,
            }
            _write(
                teacher / "validation-report.json",
                _canonical_json(validation_report),
            )

            paper_path = student / "paper.pdf"
            pdf_inspection = self.pdf_renderer(
                request.contest.title,
                request.contest.subtitle,
                pdf_documents,
                paper_path,
            )
            # Renderer injection is for fonts/layout variants, not a bypass of
            # the parser and required-text gate.
            pdf_inspection = inspect_pdf(
                paper_path,
                required_text=(request.contest.title,),
                minimum_pages=len(pdf_documents) + 1,
            )
            testdata_path = student / "testdata.tar.gz"
            _deterministic_testdata_archive(
                testdata_path,
                student_files,
                {problem.slug for problem in request.contest.problems},
            )
            archive_names = verify_student_testdata_archive(
                testdata_path,
                [problem.slug for problem in request.contest.problems],
            )
            expected_files = len(request.contest.problems) * request.practice_cases_per_problem * 2
            if len(archive_names) != expected_files:
                raise ArtifactGenerationError(
                    f"学生测试数据文件数异常: {len(archive_names)} != {expected_files}"
                )

            snapshot_digest = sha256_bytes(snapshot_payload)
            manifest = {
                "schema_version": 1,
                "tid": request.contest.tid,
                "revision": request.revision,
                "status": "awaiting_teacher_approval",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "provider_id": provider_id,
                "source_snapshot_sha256": snapshot_digest,
                "practice_cases_per_problem": request.practice_cases_per_problem,
                "warnings": list(request.generation_warnings),
                "student_archive_members": list(archive_names),
                "pdf": {
                    "pages": pdf_inspection.page_count,
                    "size": pdf_inspection.byte_size,
                },
                "validation": {
                    "status": "passed",
                    "all_inputs_validated": True,
                    "all_outputs_oracle_verified_twice": True,
                    "student_archive_in_out_only": True,
                },
                "files": _manifest_files(temporary),
            }
            _write(temporary / "manifest.json", _canonical_json(manifest))

            # Verify all hashes immediately before the atomic publication step.
            for item in manifest["files"]:
                path = temporary / item["path"]
                if path.stat().st_size != item["size"] or sha256_path(path) != item["sha256"]:
                    raise ArtifactGenerationError(
                        f"材料在生成过程中发生变化: {item['path']}"
                    )
            if destination.exists():
                raise ArtifactAlreadyExistsError(
                    f"材料版本 {request.revision} 已被并发创建"
                )
            temporary.rename(destination)
            final_manifest = destination / "manifest.json"
            final_paper = destination / "student" / "paper.pdf"
            final_testdata = destination / "student" / "testdata.tar.gz"
            return ArtifactRelease(
                request.contest.tid,
                request.revision,
                destination,
                final_manifest,
                final_paper,
                final_testdata,
                manifest,
                PdfInspection(
                    final_paper,
                    pdf_inspection.page_count,
                    pdf_inspection.byte_size,
                    pdf_inspection.extracted_text,
                ),
            )
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
