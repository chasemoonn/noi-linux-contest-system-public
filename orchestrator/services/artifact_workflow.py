"""Build immutable artifact snapshots from read-only Hydro documents."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

import yaml

from .artifact_generation import (
    ContestSnapshot,
    ProblemSnapshot,
    normalized_input_digest,
)


class ArtifactSnapshotError(RuntimeError):
    pass


_HEX_24 = re.compile(r"^[0-9a-fA-F]{24}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SAMPLE_INPUT_HEADING = re.compile(
    r"^(?:输入样例|样例输入|sample\s+input|input\s+sample)"
    r"(?:\s*(?:[#№]?\s*\d+|[一二三四五六七八九十]+))?\s*[:：]?$",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})[^`~]*$")


def extract_sample_input_hashes(markdown: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract common fenced sample inputs without pretending all Markdown is parsed.

    Only explicit ``输入样例``/``样例输入``/``Sample Input`` headings are
    accepted. Ambiguous or unclosed sections produce teacher-visible warnings
    and are never reported as verified samples.
    """
    if not isinstance(markdown, str):
        return (), ("题面不是文本，未能抽取样例输入",)
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    hashes: set[str] = set()
    warnings: list[str] = []
    found_heading = False
    for index, line in enumerate(lines):
        heading = _HEADING.match(line.strip())
        if not heading:
            continue
        title = re.sub(r"[*_`]", "", heading.group(2)).strip()
        if not _SAMPLE_INPUT_HEADING.fullmatch(title):
            continue
        found_heading = True
        cursor = index + 1
        skipped_content = False
        fence_match = None
        while cursor < len(lines):
            if _HEADING.match(lines[cursor].strip()):
                break
            candidate = _FENCE.match(lines[cursor])
            if candidate:
                fence_match = candidate
                break
            if lines[cursor].strip():
                skipped_content = True
            cursor += 1
        label = title or "样例输入"
        if fence_match is None:
            warnings.append(f"{label}: 标题后未找到 fenced code block，样例抽取未完全确认")
            continue
        marker = fence_match.group(1)
        closing = re.compile(rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$")
        end = cursor + 1
        while end < len(lines) and not closing.match(lines[end]):
            end += 1
        if end >= len(lines):
            warnings.append(f"{label}: fenced code block 未闭合，样例抽取未完全确认")
            continue
        sample = "\n".join(lines[cursor + 1 : end]).encode("utf-8")
        if not sample.strip():
            warnings.append(f"{label}: fenced code block 为空，样例抽取未完全确认")
            continue
        hashes.add(normalized_input_digest(sample))
        if skipped_content:
            warnings.append(
                f"{label}: 标题与代码块之间含额外内容，已抽取但请教师确认"
            )
    if not found_heading:
        warnings.append("未识别到输入样例 / Sample Input 标题，样例抽取未完全确认")
    return tuple(sorted(hashes)), tuple(warnings)


def _statement(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactSnapshotError("Hydro 题面为空")
    text = value.strip()
    if text.startswith("{"):
        try:
            languages = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(languages, dict):
            for key in ("zh_CN", "zh", "zh-Hans", "en"):
                candidate = languages.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for candidate in languages.values():
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
    return text


def _config(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        result = yaml.safe_load(value)
    except yaml.YAMLError as exc:
        raise ArtifactSnapshotError(f"Hydro 题目配置 YAML 无效: {exc}") from exc
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ArtifactSnapshotError("Hydro 题目配置必须是映射")
    return result


def _number(value: Any, default: int) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(1, int(value))
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        if match:
            number = float(match.group())
            lowered = value.lower()
            if "s" in lowered and "ms" not in lowered:
                number *= 1000
            return max(1, int(number))
    return default


def _memory_mb(value: Any, default: int = 256) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return max(1, int(number / 1024 if number > 4096 else number))
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        if match:
            number = float(match.group())
            lowered = value.lower()
            if "g" in lowered:
                number *= 1024
            elif "k" in lowered:
                number /= 1024
            return max(1, int(number))
    return default


def build_contest_snapshot(
    hydro,
    contest: dict,
    official_hashes_by_pid: Mapping[str, list[str] | tuple[str, ...]] | None = None,
) -> ContestSnapshot:
    try:
        files = json.loads(contest["files"])
        pids = json.loads(contest["pids"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ArtifactSnapshotError("登记题目映射损坏") from exc
    problems = []
    for slug in files:
        pid = str(pids.get(slug) or "")
        if not pid:
            raise ArtifactSnapshotError(f"题目 {slug} 没有 Hydro pid")
        document = hydro.get_problem(pid)
        if not document:
            raise ArtifactSnapshotError(f"Hydro 题目不存在: {pid}")
        config = _config(document.get("config"))
        basename = str(config.get("filename") or "").strip()
        hash_values = (official_hashes_by_pid or {}).get(pid, ())
        if not hash_values:
            hash_values = document.get("official_input_sha256", ())
        official_hashes = tuple(
            sorted(
                {
                    str(value).lower()
                    for value in hash_values
                    if re.fullmatch(r"[0-9a-fA-F]{64}", str(value))
                }
            )
        )
        sample_hashes, sample_warnings = extract_sample_input_hashes(
            _statement(document.get("content"))
        )
        forbidden_hashes = tuple(sorted(set(official_hashes) | set(sample_hashes)))
        source = {
            "doc_id": int(document["docId"]),
            "pid": str(document.get("pid") or pid),
            "config_sha256": hashlib.sha256(
                json.dumps(
                    config,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "official_input_hash_count": len(official_hashes),
            "sample_input_hash_count": len(sample_hashes),
            "sample_extraction_warnings": list(sample_warnings),
        }
        problems.append(
            ProblemSnapshot(
                pid=pid,
                slug=str(slug),
                title=str(document.get("title") or slug),
                statement_markdown=_statement(document.get("content")),
                input_filename=f"{basename}.in" if basename else None,
                output_filename=f"{basename}.out" if basename else None,
                time_limit_ms=_number(config.get("time"), 1000),
                memory_limit_mb=_memory_mb(
                    config.get("memory", config.get("memoryMax")), 256
                ),
                source=source,
                forbidden_practice_input_sha256=forbidden_hashes,
            )
        )
    return ContestSnapshot(
        tid=str(contest["tid"]),
        title=str(contest.get("title") or contest["tid"]),
        subtitle="CSP 模拟赛 · NOI Linux 环境",
        begin_at_ms=int(contest.get("begin_at_ms") or 0),
        end_at_ms=int(contest.get("end_at_ms") or 0),
        problems=tuple(problems),
        source={
            "submission_session": str(contest.get("submission_session") or ""),
            "hydro_rule": str(contest.get("hydro_rule") or ""),
        },
    )


def build_preflight_gate_snapshot(
    contest: Mapping[str, Any],
    preflight: Mapping[str, Any],
) -> tuple[ContestSnapshot, tuple[str, ...]]:
    """Build a non-publishable snapshot for gates that must precede cloning.

    This snapshot uses the original Hydro pids but the teacher-approved target
    filenames (``slug.in``/``slug.out``).  It is only used to validate formal
    input fingerprints, request limits, the AI provider, and trusted local tool
    mappings before the first mutating Hydro call.
    """
    tid = str(contest.get("tid") or "").lower()
    preflight_id = str(preflight.get("preflight_id") or "")
    if (
        not _HEX_24.fullmatch(tid)
        or preflight.get("tid") != tid
        or preflight.get("safe_to_apply") is not True
        or not _HEX_64.fullmatch(preflight_id)
    ):
        raise ArtifactSnapshotError("Hydro 预检不能建立本场安全门快照")
    try:
        files = json.loads(str(contest["files"]))
        local_pids = json.loads(str(contest["pids"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ArtifactSnapshotError("本场题目文件或 pid 映射损坏") from exc
    if (
        not isinstance(files, list)
        or not files
        or not isinstance(local_pids, dict)
        or any(not isinstance(slug, str) or not _SLUG.fullmatch(slug) for slug in files)
        or set(files) != set(local_pids)
        or len(set(files)) != len(files)
    ):
        raise ArtifactSnapshotError("本场题目文件列表无效")
    items = preflight.get("problems")
    if not isinstance(items, list):
        raise ArtifactSnapshotError("Hydro 预检题目列表缺失")
    originals = {
        str(item.get("slug")): item for item in items if isinstance(item, Mapping)
    }
    if len(originals) != len(items) or set(originals) != set(files):
        raise ArtifactSnapshotError("Hydro 预检与登记题目集合不一致")

    problems: list[ProblemSnapshot] = []
    warnings: list[str] = []
    seen_pids: set[str] = set()
    for slug in files:
        original = originals[slug]
        pid = str(original.get("pid") or "")
        if not pid or pid != str(local_pids.get(slug) or "") or pid in seen_pids:
            raise ArtifactSnapshotError(f"{slug}: Hydro 预检原题映射不一致")
        seen_pids.add(pid)
        statement = _statement(original.get("content"))
        raw_formal = original.get("formal_input_sha256")
        if not isinstance(raw_formal, list) or any(
            not isinstance(value, str) or not _HEX_64.fullmatch(value)
            for value in raw_formal
        ):
            raise ArtifactSnapshotError(f"{slug}: 正式输入数据指纹格式无效")
        formal = tuple(sorted(set(raw_formal)))
        sample_hashes, sample_warnings = extract_sample_input_hashes(statement)
        warnings.extend(f"{slug}: {warning}" for warning in sample_warnings)
        time_ms = original.get("time_ms")
        memory_mb = original.get("memory_mb")
        if not isinstance(time_ms, Mapping) or not isinstance(memory_mb, Mapping):
            raise ArtifactSnapshotError(f"{slug}: 时限或内存摘要缺失")
        try:
            time_limit = int(time_ms["max"])
            memory_limit = int(memory_mb["max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactSnapshotError(f"{slug}: 时限或内存摘要无效") from exc
        problems.append(
            ProblemSnapshot(
                pid=pid,
                slug=slug,
                title=str(original.get("title") or slug),
                statement_markdown=statement,
                input_filename=f"{slug}.in",
                output_filename=f"{slug}.out",
                time_limit_ms=time_limit,
                memory_limit_mb=memory_limit,
                source={
                    "source_pid": pid,
                    "source_doc_id": original.get("doc_id"),
                    "source_hash": str(original.get("source_hash") or ""),
                    "official_input_hash_count": len(formal),
                    "sample_input_hash_count": len(sample_hashes),
                    "sample_extraction_warnings": list(sample_warnings),
                    "preflight_gate_only": True,
                },
                forbidden_practice_input_sha256=tuple(
                    sorted(set(formal) | set(sample_hashes))
                ),
            )
        )
    return (
        ContestSnapshot(
            tid=tid,
            title=str(contest.get("title") or preflight.get("contest_title") or tid),
            subtitle="CSP 模拟赛 · NOI Linux 环境",
            begin_at_ms=int(contest.get("begin_at_ms") or 0),
            end_at_ms=int(contest.get("end_at_ms") or 0),
            problems=tuple(problems),
            source={
                "submission_session": str(contest.get("submission_session") or ""),
                "hydro_rule": str(contest.get("hydro_rule") or ""),
                "preflight_id": preflight_id,
                "preflight_gate_only": True,
            },
        ),
        tuple(warnings),
    )


def build_private_clone_snapshot(
    contest: Mapping[str, Any],
    preflight: Mapping[str, Any],
    applied: Mapping[str, Any],
) -> tuple[ContestSnapshot, tuple[str, ...]]:
    """Build a snapshot solely from the approved preflight and verified clones.

    The post-apply endpoint intentionally does not return statements or formal
    hashes again. This function therefore binds the persisted, teacher-approved
    preflight to the exact verified clone mapping and to the local contest's
    already atomically replaced pid map.
    """
    tid = str(contest.get("tid") or "").lower()
    if not _HEX_24.fullmatch(tid):
        raise ArtifactSnapshotError("比赛 tid 无效")
    if preflight.get("tid") != tid or applied.get("tid") != tid:
        raise ArtifactSnapshotError("Hydro 预检或克隆结果不属于本场比赛")
    preflight_id = str(preflight.get("preflight_id") or "")
    operation_id = str(applied.get("operation_id") or "")
    if (
        not _HEX_64.fullmatch(preflight_id)
        or applied.get("preflight_id") != preflight_id
        or not _HEX_64.fullmatch(operation_id)
        or applied.get("status") != "applied"
    ):
        raise ArtifactSnapshotError("Hydro 预检与克隆结果无法建立不可变关联")
    if preflight.get("safe_to_apply") is not True:
        raise ArtifactSnapshotError("Hydro 预检没有明确允许克隆")
    try:
        files = json.loads(str(contest["files"]))
        local_pids = json.loads(str(contest["pids"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ArtifactSnapshotError("本场题目文件或 pid 映射损坏") from exc
    if (
        not isinstance(files, list)
        or not files
        or not isinstance(local_pids, dict)
        or any(not isinstance(slug, str) or not _SLUG.fullmatch(slug) for slug in files)
        or len(set(files)) != len(files)
    ):
        raise ArtifactSnapshotError("本场题目文件列表无效")

    preflight_items = preflight.get("problems")
    clone_items = applied.get("mapping")
    if not isinstance(preflight_items, list) or not isinstance(clone_items, list):
        raise ArtifactSnapshotError("Hydro 预检或克隆题目列表缺失")
    before = {
        str(item.get("slug")): item
        for item in preflight_items
        if isinstance(item, Mapping)
    }
    clones = {
        str(item.get("slug")): item
        for item in clone_items
        if isinstance(item, Mapping)
    }
    if set(before) != set(files) or set(clones) != set(files):
        raise ArtifactSnapshotError("Hydro 预检、克隆与登记题目集合不一致")

    problems: list[ProblemSnapshot] = []
    warnings: list[str] = []
    seen_clone_pids: set[str] = set()
    for slug in files:
        original = before[slug]
        clone = clones[slug]
        clone_pid = str(clone.get("clone_pid") or "")
        if (
            clone.get("verified") is not True
            or not clone_pid
            or clone_pid in seen_clone_pids
            or str(local_pids.get(slug) or "") != clone_pid
            or str(clone.get("source_pid") or "") != str(original.get("pid") or "")
            or clone.get("source_doc_id") != original.get("doc_id")
        ):
            raise ArtifactSnapshotError(f"{slug}: 克隆映射未完全验证")
        seen_clone_pids.add(clone_pid)
        statement = _statement(original.get("content"))
        formal = tuple(
            sorted(
                {
                    str(value).lower()
                    for value in original.get("formal_input_sha256", [])
                    if isinstance(value, str) and _HEX_64.fullmatch(value)
                }
            )
        )
        sample_hashes, sample_warnings = extract_sample_input_hashes(statement)
        warnings.extend(f"{slug}: {warning}" for warning in sample_warnings)
        time_ms = original.get("time_ms")
        memory_mb = original.get("memory_mb")
        if not isinstance(time_ms, Mapping) or not isinstance(memory_mb, Mapping):
            raise ArtifactSnapshotError(f"{slug}: 时限或内存摘要缺失")
        try:
            time_limit = int(time_ms["max"])
            memory_limit = int(memory_mb["max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactSnapshotError(f"{slug}: 时限或内存摘要无效") from exc
        problems.append(
            ProblemSnapshot(
                pid=clone_pid,
                slug=slug,
                title=str(original.get("title") or slug),
                statement_markdown=statement,
                input_filename=f"{slug}.in",
                output_filename=f"{slug}.out",
                time_limit_ms=time_limit,
                memory_limit_mb=memory_limit,
                source={
                    "source_pid": str(original.get("pid") or ""),
                    "source_doc_id": original.get("doc_id"),
                    "clone_pid": clone_pid,
                    "clone_doc_id": clone.get("clone_doc_id"),
                    "source_hash": str(original.get("source_hash") or ""),
                    "official_input_hash_count": len(formal),
                    "sample_input_hash_count": len(sample_hashes),
                    "sample_extraction_warnings": list(sample_warnings),
                },
                forbidden_practice_input_sha256=tuple(
                    sorted(set(formal) | set(sample_hashes))
                ),
            )
        )
    return (
        ContestSnapshot(
            tid=tid,
            title=str(contest.get("title") or preflight.get("contest_title") or tid),
            subtitle="CSP 模拟赛 · NOI Linux 环境",
            begin_at_ms=int(contest.get("begin_at_ms") or 0),
            end_at_ms=int(contest.get("end_at_ms") or 0),
            problems=tuple(problems),
            source={
                "submission_session": str(contest.get("submission_session") or ""),
                "hydro_rule": str(contest.get("hydro_rule") or ""),
                "preflight_id": preflight_id,
                "operation_id": operation_id,
            },
        ),
        tuple(warnings),
    )


def strict_generation_blockers(snapshot: ContestSnapshot) -> list[str]:
    """Return hard blockers required by the teacher-facing promise."""
    blockers = []
    for problem in snapshot.problems:
        if int(problem.source.get("official_input_hash_count") or 0) <= 0:
            blockers.append(
                f"{problem.slug}: 尚未从 Hydro 本机取得正式输入数据指纹"
            )
    return blockers
