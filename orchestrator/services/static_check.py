"""Static checks for NOI-style filenames, freopen calls, and Windows-only APIs."""
from __future__ import annotations

from pathlib import Path
import re

BANNED_PATTERNS = [
    (r"#\s*include\s*[<\"]windows\.h", "使用了 windows.h（Linux 下编译错误）"),
    (r"#\s*include\s*[<\"]conio\.h", "使用了 conio.h（Linux 下不存在）"),
    (r"system\s*\(\s*[\"']pause[\"']", "使用了 system(\"pause\")（Windows 专属）"),
    (r"\bgetch\s*\(", "使用了 getch()（Windows 专属）"),
]


def strip_cpp_comments(code: str) -> str:
    """Remove C/C++ comments while preserving string and character literals."""
    out: list[str] = []
    i = 0
    state = "code"
    quote = ""
    while i < len(code):
        char = code[i]
        nxt = code[i + 1] if i + 1 < len(code) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                state = "line"
                i += 2
                continue
            if char == "/" and nxt == "*":
                state = "block"
                i += 2
                continue
            if char in ('"', "'"):
                state, quote = "string", char
            out.append(char)
            i += 1
            continue
        if state == "line":
            if char == "\n":
                out.append(char)
                state = "code"
            i += 1
            continue
        if state == "block":
            if char == "*" and nxt == "/":
                state = "code"
                i += 2
            else:
                if char == "\n":
                    out.append(char)
                i += 1
            continue
        out.append(char)
        if char == "\\" and nxt:
            out.append(nxt)
            i += 2
            continue
        if char == quote:
            state = "code"
        i += 1
    return "".join(out)


def check_code(code: str, name: str) -> list[str]:
    source = strip_cpp_comments(code)
    issues = []
    for pattern, message in BANNED_PATTERNS:
        if re.search(pattern, source, re.IGNORECASE):
            issues.append(message)
    calls = re.findall(
        r'(?:std::)?freopen\s*\(\s*"([^"]+)"\s*,\s*"([rw][^"]*)"',
        source,
    )
    inputs = [filename for filename, mode in calls if mode.startswith("r")]
    outputs = [filename for filename, mode in calls if mode.startswith("w")]
    if not inputs or not outputs:
        issues.append("未检测到完整的 freopen 文件读写（复赛中该题 0 分）")
    else:
        if f"{name}.in" not in inputs:
            issues.append(f"输入文件名错误：期望 {name}.in，实际 {inputs}")
        if f"{name}.out" not in outputs:
            issues.append(f"输出文件名错误：期望 {name}.out，实际 {outputs}")
    return issues


def check_submission(submit_dir: str, files: list[str]) -> dict:
    directory = Path(submit_dir)
    present = (
        {path.name: path for path in directory.iterdir() if path.is_file()}
        if directory.exists()
        else {}
    )
    lower_map = {filename.casefold(): filename for filename in present}
    report = {}
    for name in files:
        expected = f"{name}.cpp"
        if expected in present:
            code = present[expected].read_text(encoding="utf-8-sig", errors="replace")
            issues = check_code(code, name)
            report[name] = {
                "status": "ok" if not issues else "rule_violation",
                "file": expected,
                "issues": issues,
            }
        elif expected.casefold() in lower_map:
            actual = lower_map[expected.casefold()]
            report[name] = {
                "status": "invalid_filename",
                "file": actual,
                "issues": [f"文件名错误：期望 {expected}，实际 {actual}（0 分）"],
            }
        else:
            report[name] = {
                "status": "missing",
                "file": "",
                "issues": [f"未提交 {expected}（0 分）"],
            }
    return report


def check_answer_tree(answer_root: str, candidate: str, files: list[str]) -> dict:
    """Check the official candidate/problem/problem.cpp directory layout."""
    root = Path(answer_root)
    candidate_dir = root / candidate
    report = {}
    for name in files:
        problem_dir = candidate_dir / name
        expected = problem_dir / f"{name}.cpp"
        if expected.is_file():
            code = expected.read_text(encoding="utf-8-sig", errors="replace")
            issues = check_code(code, name)
            extras = [
                path.name
                for path in problem_dir.iterdir()
                if path.name != expected.name
            ]
            if extras:
                issues.append(
                    f"题目目录含额外文件或目录：{sorted(extras)}（正式规则只放源程序）"
                )
            report[name] = {
                "status": "ok" if not issues else "rule_violation",
                "file": f"{candidate}/{name}/{name}.cpp",
                "issues": issues,
            }
            continue

        actual = ""
        if candidate_dir.is_dir():
            problem_case = next(
                (
                    path
                    for path in candidate_dir.iterdir()
                    if path.name.casefold() == name.casefold()
                ),
                None,
            )
            if problem_case and problem_case.is_dir():
                source_case = next(
                    (
                        path
                        for path in problem_case.iterdir()
                        if path.name.casefold() == f"{name}.cpp".casefold()
                    ),
                    None,
                )
                if source_case:
                    actual = f"{candidate}/{problem_case.name}/{source_case.name}"
        if actual:
            report[name] = {
                "status": "invalid_filename",
                "file": actual,
                "issues": [
                    f"目录或文件名大小写错误：期望 {candidate}/{name}/{name}.cpp，实际 {actual}（0 分）"
                ],
            }
        else:
            report[name] = {
                "status": "missing",
                "file": "",
                "issues": [
                    f"未收到 {candidate}/{name}/{name}.cpp（目录结构不符或未提交，0 分）"
                ],
            }
    return report


def force_zero_code(code: str, issues: list[str]) -> str:
    reason = "；".join(issues).replace("\n", " ")
    return f'#error "NOI environment rule violation"\n// {reason}\n{code}'
