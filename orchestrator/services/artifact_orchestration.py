"""Crash-aware orchestration for one teacher-approved AI material job."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import secrets
import tarfile
from typing import Any, Mapping

from .artifact_generation import (
    ArtifactGenerationService,
    ArtifactRequest,
    sha256_path,
    validate_artifact_preconditions,
)
from .artifact_workflow import (
    ArtifactSnapshotError,
    build_preflight_gate_snapshot,
    build_private_clone_snapshot,
    strict_generation_blockers,
)
from .store import SubmissionConflictError


class ArtifactOrchestrationError(RuntimeError):
    pass


_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


def _registered_problems(contest: Mapping[str, Any]) -> list[dict[str, str]]:
    try:
        files = json.loads(str(contest["files"]))
        pid_map = json.loads(str(contest["pids"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ArtifactOrchestrationError("登记题目映射损坏") from exc
    if (
        not isinstance(files, list)
        or not files
        or not isinstance(pid_map, dict)
        or len(set(files)) != len(files)
        or set(files) != set(pid_map)
    ):
        raise ArtifactOrchestrationError("登记题目文件与 pid 映射不完整")
    result: list[dict[str, str]] = []
    seen_pids: set[str] = set()
    for slug in files:
        pid = pid_map.get(slug)
        if (
            not isinstance(slug, str)
            or not _SLUG.fullmatch(slug)
            or not isinstance(pid, str)
            or not pid.strip()
            or len(pid) > 128
            or pid in seen_pids
        ):
            raise ArtifactOrchestrationError("登记题目 slug 或 pid 无效")
        seen_pids.add(pid)
        result.append({"pid": pid, "slug": slug})
    return result


def _failure_message(result: Any, fallback: str) -> str:
    if isinstance(result, Mapping):
        if result.get("safe_to_apply") is False and isinstance(
            result.get("blockers"), list
        ):
            return "; ".join(str(value) for value in result["blockers"])[:4000]
        error = result.get("error")
        if isinstance(error, str):
            return error[:4000]
        if isinstance(error, Mapping):
            try:
                return json.dumps(error, ensure_ascii=False, sort_keys=True)[:4000]
            except (TypeError, ValueError):
                pass
    return fallback


def _validate_preflight(
    tid: str,
    requested: list[dict[str, str]],
    preflight: Mapping[str, Any],
) -> None:
    if (
        preflight.get("ok") is not True
        or preflight.get("safe_to_apply") is not True
        or preflight.get("tid") != tid
        or not isinstance(preflight.get("preflight_id"), str)
        or not _HEX_64.fullmatch(preflight["preflight_id"])
    ):
        raise ArtifactOrchestrationError("Hydro 题目预检没有通过安全门")
    items = preflight.get("problems")
    if not isinstance(items, list) or len(items) != len(requested):
        raise ArtifactOrchestrationError("Hydro 题目预检数量不一致")
    expected = {item["slug"]: item["pid"] for item in requested}
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise ArtifactOrchestrationError("Hydro 题目预检结构无效")
        slug = item.get("slug")
        hashes = item.get("formal_input_sha256")
        if (
            slug not in expected
            or slug in seen
            or item.get("pid") != expected[slug]
            or not isinstance(item.get("doc_id"), int)
            or isinstance(item.get("doc_id"), bool)
            or not isinstance(item.get("content"), str)
            or not isinstance(item.get("title"), str)
            or not isinstance(item.get("time_ms"), Mapping)
            or not isinstance(item.get("memory_mb"), Mapping)
            or not isinstance(hashes, list)
            or not all(
                isinstance(value, str) and _HEX_64.fullmatch(value)
                for value in hashes
            )
        ):
            raise ArtifactOrchestrationError("Hydro 题目预检内容无效")
        seen.add(str(slug))
    if seen != set(expected):
        raise ArtifactOrchestrationError("Hydro 题目预检集合不一致")


def _validate_apply(
    tid: str,
    requested: list[dict[str, str]],
    preflight_id: str,
    operation_id: str,
    applied: Mapping[str, Any],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    if (
        applied.get("ok") is not True
        or applied.get("status") != "applied"
        or applied.get("tid") != tid
        or applied.get("preflight_id") != preflight_id
        or applied.get("operation_id") != operation_id
    ):
        raise ArtifactOrchestrationError("Hydro 私有题克隆没有通过响应校验")
    mapping = applied.get("mapping")
    if not isinstance(mapping, list) or len(mapping) != len(requested):
        raise ArtifactOrchestrationError("Hydro 私有题克隆数量不一致")
    expected = {item["slug"]: item["pid"] for item in requested}
    clone_map: dict[str, str] = {}
    plan: list[dict[str, Any]] = []
    for item in mapping:
        if not isinstance(item, Mapping):
            raise ArtifactOrchestrationError("Hydro 私有题克隆结构无效")
        slug = item.get("slug")
        clone_pid = item.get("clone_pid")
        if (
            slug not in expected
            or slug in clone_map
            or item.get("source_pid") != expected[slug]
            or item.get("verified") is not True
            or not isinstance(clone_pid, str)
            or not clone_pid
        ):
            raise ArtifactOrchestrationError("Hydro 私有题克隆映射未完全验证")
        clone_map[str(slug)] = clone_pid
        plan.append(
            {
                "slug": str(slug),
                "source_pid": str(item["source_pid"]),
                "source_doc_id": int(item["source_doc_id"]),
                "clone_pid": clone_pid,
                "clone_doc_id": int(item["clone_doc_id"]),
                "input_filename": f"{slug}.in",
                "output_filename": f"{slug}.out",
                "verified": True,
            }
        )
    if set(clone_map) != set(expected) or len(set(clone_map.values())) != len(
        clone_map
    ):
        raise ArtifactOrchestrationError("Hydro 私有题克隆 pid 不完整或重复")
    return clone_map, plan


def _archive_stats(path: Path) -> tuple[int, int]:
    count = 0
    expanded = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                if member.isfile():
                    count += 1
                    expanded += int(member.size)
    except (tarfile.TarError, OSError) as exc:
        raise ArtifactOrchestrationError("生成后的学生自测数据无法复核") from exc
    return count, expanded


class ArtifactJobRunner:
    """Prepare and run material jobs; the caller owns the background thread."""

    _RESUME_KEYS = (
        "requested_problems",
        "approval_id",
        "preflight",
        "operation_id",
        "apply",
    )

    def __init__(
        self,
        *,
        store,
        problem_client,
        ai_provider,
        tool_registry,
        generation_service: ArtifactGenerationService,
        logger: logging.Logger | None = None,
    ):
        self.store = store
        self.problem_client = problem_client
        self.ai_provider = ai_provider
        self.tool_registry = tool_registry
        self.generation_service = generation_service
        self.log = logger or logging.getLogger("orchestrator.artifacts")

    def start(self, tid: str, approved_by: str) -> dict:
        contest = self.store.get_contest(tid)
        if not contest:
            raise KeyError(f"比赛不存在: {tid}")
        if contest.get("materials_mode") != "ai":
            raise SubmissionConflictError("本场不是 AI 材料模式")
        requested = _registered_problems(contest)
        session = str(contest.get("submission_session") or "")
        if not _HEX_32.fullmatch(session):
            raise ArtifactOrchestrationError("比赛提交会话无效")
        teacher = str(approved_by).strip()
        if not teacher or len(teacher.encode("utf-8")) > 256:
            raise ArtifactOrchestrationError("教师账号无效")

        job_id = secrets.token_hex(16)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        revision = f"ai-{timestamp}-{job_id[:12]}"
        approval_id = hashlib.sha256(
            json.dumps(
                ["teacher-fileio-approval-v1", tid, session, job_id, teacher],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        details: dict[str, Any] = {
            "schema_version": 1,
            "submission_session": session,
            "requested_problems": requested,
            "approved_by": teacher,
            "approval_id": approval_id,
            "stage": "queued",
        }
        previous = self.store.latest_artifact_job(tid)
        if previous and previous.get("state") == "done":
            raise SubmissionConflictError(
                "本场材料已成功生成；为避免复用陈旧题面或正式数据指纹，"
                "不能直接重新生成，请在 Hydro 新建比赛后重新登记"
            )
        if previous and previous.get("state") in {"error", "interrupted"}:
            old = previous.get("details") or {}
            old_requested = old.get("requested_problems")
            if (
                old.get("submission_session") == session
                and isinstance(old_requested, list)
                and {item.get("slug") for item in old_requested if isinstance(item, dict)}
                == {item["slug"] for item in requested}
            ):
                for key in self._RESUME_KEYS:
                    if key in old:
                        details[key] = old[key]
                details["resumed_from"] = str(previous["job_id"])
                details["approved_by"] = teacher
        return self.store.start_artifact_job(
            job_id,
            tid,
            revision,
            details=details,
            message="教师已批准克隆为本场私有题；任务已排队",
        )

    def _save(
        self,
        job_id: str,
        details: dict[str, Any],
        *,
        progress: int,
        message: str,
    ) -> None:
        self.store.update_artifact_job(
            job_id,
            "running",
            progress=progress,
            message=message,
            details=details,
        )

    def run(self, job_id: str) -> bool:
        job = self.store.artifact_job(job_id)
        if not job or job.get("state") not in {"queued", "running"}:
            return False
        details = dict(job.get("details") or {})
        tid = str(job["tid"])
        try:
            contest = self.store.get_contest(tid)
            if not contest:
                raise ArtifactOrchestrationError("比赛不存在")
            if contest.get("state") not in {"registered", "error"}:
                raise ArtifactOrchestrationError("比赛已进入备赛或运行阶段")
            session = str(details.get("submission_session") or "")
            if session != str(contest.get("submission_session") or ""):
                raise ArtifactOrchestrationError("比赛已重新登记，材料任务已过期")
            requested = details.get("requested_problems")
            if not isinstance(requested, list) or not requested:
                raise ArtifactOrchestrationError("任务缺少原始题目映射")

            details["stage"] = "preflight"
            self._save(job_id, details, progress=5, message="正在预检原题与正式数据指纹")
            preflight = details.get("preflight")
            if preflight is None:
                preflight = self.problem_client.preflight(
                    tid=tid, problems=requested
                )
                if not isinstance(preflight, Mapping) or preflight.get("ok") is not True:
                    raise ArtifactOrchestrationError(
                        _failure_message(preflight, "Hydro 题目预检失败")
                    )
                details["preflight"] = dict(preflight)
            _validate_preflight(tid, requested, preflight)

            # Everything that can be checked without mutating Hydro must pass
            # before the private-clone apply call.  In particular, a missing
            # formal-data digest, invalid practice count, unavailable AI
            # provider, or absent trusted executable mapping must never alter
            # the contest problem ids.
            source_pid_map = {
                str(item["slug"]): str(item["pid"]) for item in requested
            }
            gate_contest = dict(contest)
            gate_contest["pids"] = json.dumps(
                source_pid_map, ensure_ascii=False, sort_keys=True
            )
            gate_snapshot, gate_warnings = build_preflight_gate_snapshot(
                gate_contest, preflight
            )
            blockers = strict_generation_blockers(gate_snapshot)
            if blockers:
                raise ArtifactOrchestrationError("; ".join(blockers))
            practice_groups = int(contest.get("practice_groups") or 0)
            validators, oracles = self.tool_registry.adapters_for(
                [problem.slug for problem in gate_snapshot.problems]
            )
            validate_artifact_preconditions(
                ArtifactRequest(
                    gate_snapshot,
                    str(job["revision"]),
                    practice_groups,
                    tuple(gate_warnings),
                ),
                ai_provider=self.ai_provider,
                validators=validators,
                oracles=oracles,
            )
            details["warnings"] = list(gate_warnings)
            details["stage"] = "preclone_gates_passed"
            self._save(
                job_id,
                details,
                progress=20,
                message="正式数据指纹、AI 与可信校验工具已预检；尚未改动 Hydro",
            )

            approval_id = str(details.get("approval_id") or "")
            if not _HEX_64.fullmatch(approval_id):
                raise ArtifactOrchestrationError("任务缺少有效的教师克隆批准号")
            expected_operation = self.problem_client.operation_id(
                tid, str(preflight["preflight_id"]), approval_id
            )
            stored_operation = str(details.get("operation_id") or expected_operation)
            if stored_operation != expected_operation:
                raise ArtifactOrchestrationError("已保存的克隆操作号与教师批准不一致")
            details["operation_id"] = expected_operation
            details["stage"] = "applying_private_clones"
            self._save(
                job_id,
                details,
                progress=25,
                message="预检已保存；正在克隆本场私有题并设置 slug.in/out",
            )

            applied = details.get("apply")
            if applied is None:
                current_pid_map = {
                    item["slug"]: item["pid"]
                    for item in _registered_problems(contest)
                }
                if current_pid_map != source_pid_map:
                    raise ArtifactOrchestrationError(
                        "本场题目映射在预检后已改变，拒绝克隆"
                    )
                applied = self.problem_client.apply(
                    tid=tid,
                    problems=requested,
                    operation_id=expected_operation,
                    approval_id=approval_id,
                    preflight_id=str(preflight["preflight_id"]),
                )
                if not isinstance(applied, Mapping) or applied.get("ok") is not True:
                    raise ArtifactOrchestrationError(
                        _failure_message(applied, "Hydro 私有题克隆失败")
                    )
                details["apply"] = dict(applied)
            clone_map, file_io_plan = _validate_apply(
                tid,
                requested,
                str(preflight["preflight_id"]),
                expected_operation,
                applied,
            )
            current_pid_map = {
                item["slug"]: item["pid"] for item in _registered_problems(contest)
            }
            if current_pid_map not in (source_pid_map, clone_map):
                raise ArtifactOrchestrationError(
                    "本场题目映射既不是批准的原题，也不是已验证的私有克隆"
                )
            details["file_io_plan"] = file_io_plan
            details["stage"] = "binding_private_clones"
            self._save(
                job_id,
                details,
                progress=40,
                message="私有题全部验证通过；正在原子更新本场题目映射",
            )
            contest = self.store.replace_contest_pid_map(
                tid,
                expected_submission_session=session,
                pid_map=clone_map,
            )

            snapshot, warnings = build_private_clone_snapshot(
                contest, preflight, applied
            )
            blockers = strict_generation_blockers(snapshot)
            if blockers:
                raise ArtifactOrchestrationError("; ".join(blockers))
            details["warnings"] = list(warnings)
            details["stage"] = "generating"
            self._save(
                job_id,
                details,
                progress=55,
                message="严格安全门已通过；正在生成 CSP 题面 PDF 与梯度自测",
            )
            release = self.generation_service.generate(
                ArtifactRequest(
                    snapshot,
                    str(job["revision"]),
                    practice_groups,
                    tuple(warnings),
                ),
                ai_provider=self.ai_provider,
                validators=validators,
                oracles=oracles,
            )
            testdata_files, expanded_size = _archive_stats(release.testdata_path)
            details["stage"] = "review"
            details["release"] = {
                "revision": release.revision,
                "root_path": str(release.directory),
                "manifest_sha256": sha256_path(release.manifest_path),
                "paper_sha256": sha256_path(release.paper_path),
                "testdata_sha256": sha256_path(release.testdata_path),
            }
            self.store.put_artifact_revision(
                tid,
                release.revision,
                state="review",
                source_sha256=str(release.manifest["source_snapshot_sha256"]),
                root_path=str(release.directory),
                manifest_sha256=details["release"]["manifest_sha256"],
                manifest=dict(release.manifest),
                file_io_plan=file_io_plan,
                warnings=list(warnings),
                paper_name="paper.pdf",
                paper_sha256=details["release"]["paper_sha256"],
                paper_size=release.paper_path.stat().st_size,
                testdata_name="testdata.tar.gz",
                testdata_sha256=details["release"]["testdata_sha256"],
                testdata_size=release.testdata_path.stat().st_size,
                testdata_files=testdata_files,
                testdata_expanded_size=expanded_size,
                complete_job_id=job_id,
                complete_job_details=details,
            )
            self.log.info(
                "artifact job completed tid=%s revision=%s", tid, release.revision
            )
            return True
        except Exception as exc:
            self.log.exception("artifact job failed tid=%s job=%s", tid, job_id)
            try:
                current = self.store.artifact_job(job_id)
                if current and current.get("state") in {"queued", "running"}:
                    details["stage"] = "error"
                    self.store.update_artifact_job(
                        job_id,
                        "error",
                        progress=int(current.get("progress") or 0),
                        message="材料生成失败，未发布；修复后可再次点击安全重试",
                        error=str(exc),
                        details=details,
                    )
            except Exception:
                self.log.exception("cannot persist artifact job failure %s", job_id)
            return False
