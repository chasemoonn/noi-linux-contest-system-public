"""Deterministic student filenames for AI-managed Hydro contests."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
import re
from typing import Any

import yaml


class ProblemMappingError(ValueError):
    pass


_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _configured_filename(value: Any) -> str:
    if isinstance(value, Mapping):
        config = dict(value)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = yaml.safe_load(value)
        except yaml.YAMLError:
            return ""
        config = dict(parsed) if isinstance(parsed, Mapping) else {}
    else:
        config = {}
    filename = config.get("filename")
    if not isinstance(filename, str):
        return ""
    candidate = filename.strip().lower()
    candidate = re.sub(r"\.(?:in|out)$", "", candidate, flags=re.IGNORECASE)
    return candidate if _SLUG.fullmatch(candidate) else ""


def auto_problem_mapping(
    contest: Mapping[str, Any],
    get_problem: Callable[[int], Mapping[str, Any] | None],
) -> tuple[list[str], dict[str, str], list[dict[str, Any]]]:
    """Map contest pids in Hydro order without guessing from Chinese titles.

    Globally unique safe ``config.filename`` values win. Public pids are the
    second choice. Remaining problems receive stable ``problemN`` names while
    avoiding every higher-priority name.
    """
    raw_pids = contest.get("pids")
    if not isinstance(raw_pids, (list, tuple)) or not raw_pids:
        raise ProblemMappingError("Hydro 比赛没有题目，无法自动生成文件名")
    doc_ids: list[int] = []
    for value in raw_pids:
        if isinstance(value, bool):
            raise ProblemMappingError("Hydro 比赛题目编号无效")
        try:
            doc_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ProblemMappingError("Hydro 比赛题目编号无效") from exc
        if doc_id <= 0 or doc_id in doc_ids:
            raise ProblemMappingError("Hydro 比赛题目编号无效或重复")
        doc_ids.append(doc_id)
    if len(doc_ids) > 100:
        raise ProblemMappingError("Hydro 比赛题目数量超过 100")

    problems: list[dict[str, Any]] = []
    for doc_id in doc_ids:
        problem = get_problem(doc_id)
        if not isinstance(problem, Mapping):
            raise ProblemMappingError(f"无法读取本场 Hydro 题目 docId={doc_id}")
        actual = problem.get("docId")
        if isinstance(actual, bool):
            raise ProblemMappingError(f"Hydro 题目 docId={doc_id} 返回编号无效")
        try:
            actual_id = int(actual)
        except (TypeError, ValueError) as exc:
            raise ProblemMappingError(
                f"Hydro 题目 docId={doc_id} 返回编号无效"
            ) from exc
        if actual_id != doc_id:
            raise ProblemMappingError(f"Hydro 题目 docId={doc_id} 不属于本场比赛")
        public_pid = str(problem.get("pid") or doc_id).strip()
        safe_pid = public_pid.lower() if _SLUG.fullmatch(public_pid.lower()) else ""
        problems.append(
            {
                "doc_id": doc_id,
                "pid": public_pid,
                "filename": _configured_filename(problem.get("config")),
                "safe_pid": safe_pid,
            }
        )

    filename_counts = Counter(item["filename"] for item in problems if item["filename"])
    assigned: list[str | None] = [None] * len(problems)
    sources: list[str | None] = [None] * len(problems)
    used: set[str] = set()
    for index, item in enumerate(problems):
        candidate = item["filename"]
        if candidate and filename_counts[candidate] == 1 and candidate not in used:
            assigned[index] = candidate
            sources[index] = "config.filename"
            used.add(candidate)

    pid_counts = Counter(
        item["safe_pid"]
        for index, item in enumerate(problems)
        if assigned[index] is None and item["safe_pid"]
    )
    for index, item in enumerate(problems):
        candidate = item["safe_pid"]
        if (
            assigned[index] is None
            and candidate
            and pid_counts[candidate] == 1
            and candidate not in used
        ):
            assigned[index] = candidate
            sources[index] = "pid"
            used.add(candidate)

    fallback = 1
    for index in range(len(problems)):
        if assigned[index] is not None:
            continue
        while f"problem{fallback}" in used:
            fallback += 1
        assigned[index] = f"problem{fallback}"
        sources[index] = "fallback"
        used.add(assigned[index])
        fallback += 1

    slugs = [str(value) for value in assigned]
    pid_map = {slug: str(item["pid"]) for slug, item in zip(slugs, problems)}
    details = [
        {
            "position": index,
            "doc_id": int(item["doc_id"]),
            "pid": str(item["pid"]),
            "slug": slug,
            "source": str(sources[index - 1]),
            "input_filename": f"{slug}.in",
            "output_filename": f"{slug}.out",
        }
        for index, (slug, item) in enumerate(zip(slugs, problems), start=1)
    ]
    return slugs, pid_map, details
