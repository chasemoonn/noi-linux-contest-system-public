"""Validated, atomic storage for contest papers and practice data."""
from __future__ import annotations

import hashlib
import gzip
import io
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import secrets
import stat
import tarfile
from typing import BinaryIO
import zipfile

_TID = re.compile(r"^[0-9a-fA-F]{24}$")


class MaterialError(ValueError):
    pass


def paper_path(root: str | Path, tid: str) -> Path:
    if not _TID.fullmatch(tid):
        raise MaterialError("比赛 tid 必须是 24 位 ObjectId")
    return Path(root) / tid / "paper.pdf"


def testdata_archive_path(root: str | Path, tid: str) -> Path:
    if not _TID.fullmatch(tid):
        raise MaterialError("比赛 tid 必须是 24 位 ObjectId")
    return Path(root) / tid / "testdata.tar.gz"


def approved_material_paths(
    *,
    materials_root: str | Path,
    artifact_root: str | Path,
    contest: dict,
    artifact: dict | None,
) -> tuple[Path, Path | None]:
    """Resolve the immutable files referenced by an approved DB revision.

    Manual uploads retain the legacy ``materials/<tid>`` layout. Generated
    revisions are consumed directly from their immutable artifact directory;
    approval therefore never copies over the legacy files and cannot leave an
    approved DB row pointing at a half-written replacement.
    """
    tid = str(contest.get("tid") or "")
    if not _TID.fullmatch(tid):
        raise MaterialError("比赛 tid 必须是 24 位 ObjectId")
    active = str(contest.get("active_material_revision") or "")
    if not active or (active == "legacy-manual" and artifact is None):
        return (
            paper_path(materials_root, tid),
            testdata_archive_path(materials_root, tid)
            if contest.get("testdata_sha256")
            else None,
        )
    if (
        not isinstance(artifact, dict)
        or str(artifact.get("revision") or "") != active
        or artifact.get("state") != "approved"
    ):
        raise MaterialError("已批准材料版本记录缺失或状态不一致")

    root = Path(str(artifact.get("root_path") or "")).resolve()
    generated_root = Path(artifact_root).resolve()
    legacy_root = (Path(materials_root) / tid).resolve()
    if root == legacy_root:
        paper = (root / "paper.pdf").resolve()
        testdata = (root / "testdata.tar.gz").resolve()
    else:
        try:
            root.relative_to(generated_root)
        except ValueError as exc:
            raise MaterialError("已批准材料版本目录越过 artifact_root") from exc
        if root == generated_root:
            raise MaterialError("材料版本不能直接使用 artifact_root")
        paper = (root / "student" / "paper.pdf").resolve()
        testdata = (root / "student" / "testdata.tar.gz").resolve()
        if root not in paper.parents or root not in testdata.parents:
            raise MaterialError("已批准材料文件路径无效")
    return paper, testdata if contest.get("testdata_sha256") else None


def read_pdf_upload(
    stream: BinaryIO,
    filename: str | None,
    maximum: int,
) -> tuple[str, bytes, str]:
    raw_name = (filename or "试题.pdf").replace("\\", "/")
    name = raw_name.rsplit("/", 1)[-1].strip()
    if (
        not name
        or len(name) > 128
        or any(ord(character) < 32 for character in name)
        or not name.lower().endswith(".pdf")
    ):
        raise MaterialError("试题文件名必须是合法的 PDF 文件名")
    payload = stream.read(maximum + 1)
    if len(payload) > maximum:
        raise MaterialError(f"试题 PDF 超过 {maximum} 字节限制")
    if len(payload) < 8 or not payload.startswith(b"%PDF-"):
        raise MaterialError("上传内容不是有效的 PDF 文件")
    return name, payload, hashlib.sha256(payload).hexdigest()


def save_paper(root: str | Path, tid: str, payload: bytes) -> Path:
    destination = paper_path(root, tid)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"paper-{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def read_testdata_upload(
    stream: BinaryIO,
    filename: str | None,
    maximum: int,
    expanded_maximum: int,
    maximum_files: int,
    problems: list[str],
    groups_per_problem: int | None = None,
) -> tuple[str, bytes, str, int, int]:
    """Validate a teacher ZIP and convert it to a safe, normalized tarball.

    The ZIP may either contain problem folders at its root, or one harmless
    wrapper folder containing those problem folders.  Only regular files are
    repacked, with read-only permissions and paths rooted by a registered
    problem name.
    """
    raw_name = (filename or "测试数据.zip").replace("\\", "/")
    name = raw_name.rsplit("/", 1)[-1].strip()
    if (
        not name
        or len(name) > 128
        or any(ord(character) < 32 for character in name)
        or not name.lower().endswith(".zip")
    ):
        raise MaterialError("测试数据文件名必须是合法的 ZIP 文件名")
    payload = stream.read(maximum + 1)
    if len(payload) > maximum:
        raise MaterialError(f"测试数据 ZIP 超过 {maximum} 字节限制")

    problem_set = set(problems)
    if not problem_set:
        raise MaterialError("登记题目为空，无法校验测试数据")
    if groups_per_problem is not None and not 2 <= int(groups_per_problem) <= 4:
        raise MaterialError("每题辅助数据组数必须在 2 到 4 之间")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise MaterialError("上传内容不是有效的 ZIP 文件") from exc

    candidates: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    expanded_size = 0

    def canonical_problem_folder(value: str) -> str:
        if value in problem_set:
            return value
        for problem in problems:
            if re.fullmatch(
                rf"(?:t?\d+[_. -]+)?{re.escape(problem)}",
                value,
                flags=re.IGNORECASE,
            ):
                return problem
        return value

    with archive:
        for info in archive.infolist():
            original_path = info.filename
            if "\x00" in original_path:
                raise MaterialError(f"测试数据包含非法路径: {original_path!r}")
            raw_path = original_path.replace("\\", "/")
            path = PurePosixPath(raw_path)
            parts = path.parts
            if path.is_absolute() or any(
                part in {"", ".", ".."} or any(ord(ch) < 32 for ch in part)
                for part in parts
            ):
                raise MaterialError(f"测试数据包含非法路径: {original_path!r}")
            if not parts or parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
                continue
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise MaterialError(f"测试数据包含不允许的特殊文件: {original_path}")
            if info.flag_bits & 0x1:
                raise MaterialError(f"测试数据不能使用加密 ZIP 条目: {original_path}")
            if info.is_dir():
                continue
            if len(raw_path) > 240:
                raise MaterialError(f"测试数据路径过长: {raw_path}")
            candidates.append((info, parts))
            expanded_size += int(info.file_size)
            if len(candidates) > maximum_files:
                raise MaterialError(f"测试数据文件数超过 {maximum_files} 个限制")
            if expanded_size > expanded_maximum:
                raise MaterialError(
                    f"测试数据解压后超过 {expanded_maximum} 字节限制"
                )

        if not candidates:
            raise MaterialError("测试数据 ZIP 中没有文件")
        first_parts = {parts[0] for _, parts in candidates}
        canonical_first_parts = {
            canonical_problem_folder(value) for value in first_parts
        }
        strip_wrapper = (
            len(first_parts) == 1
            and not canonical_first_parts.issubset(problem_set)
        )

        normalized: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        seen: set[str] = set()
        present: set[str] = set()
        input_groups: dict[str, set[str]] = {
            problem: set() for problem in problems
        }
        output_groups: dict[str, set[str]] = {
            problem: set() for problem in problems
        }
        for info, parts in candidates:
            target_parts = parts[1:] if strip_wrapper else parts
            if target_parts:
                target_parts = (
                    canonical_problem_folder(target_parts[0]),
                    *target_parts[1:],
                )
            if len(target_parts) != 2 or target_parts[0] not in problem_set:
                raise MaterialError(
                    "测试数据必须按 题目名/N.in 与 题目名/N.out 的目录结构存放；"
                    f"发现: {'/'.join(target_parts)}"
                )
            filename = target_parts[-1]
            suffix = PurePosixPath(filename).suffix.lower()
            stem = PurePosixPath(filename).stem.casefold()
            if suffix not in {".in", ".out"} or not stem:
                raise MaterialError(
                    "辅助数据只允许成对的 .in/.out 文件；"
                    f"发现: {'/'.join(target_parts)}"
                )
            target = "/".join(target_parts)
            if target in seen:
                raise MaterialError(f"测试数据包含重复路径: {target}")
            seen.add(target)
            problem = target_parts[0]
            present.add(problem)
            if suffix == ".in":
                input_groups[problem].add(stem)
            else:
                output_groups[problem].add(stem)
            normalized.append((info, target_parts))

        missing = problem_set - present
        if missing:
            raise MaterialError("测试数据缺少题目目录: " + ",".join(sorted(missing)))
        for problem in problems:
            inputs = input_groups[problem]
            outputs = output_groups[problem]
            if inputs != outputs:
                missing_outputs = sorted(inputs - outputs)
                missing_inputs = sorted(outputs - inputs)
                detail = []
                if missing_outputs:
                    detail.append("缺少 .out: " + ",".join(missing_outputs))
                if missing_inputs:
                    detail.append("缺少 .in: " + ",".join(missing_inputs))
                raise MaterialError(
                    f"题目 {problem} 的辅助数据没有成对: " + "；".join(detail)
                )
            group_count = len(inputs)
            expected = (
                int(groups_per_problem)
                if groups_per_problem is not None
                else None
            )
            if expected is not None and group_count != expected:
                raise MaterialError(
                    f"题目 {problem} 必须恰好包含 {expected} 组 .in/.out，"
                    f"当前为 {group_count} 组"
                )
            if expected is None and not 2 <= group_count <= 4:
                raise MaterialError(
                    f"题目 {problem} 必须包含 2 到 4 组 .in/.out，"
                    f"当前为 {group_count} 组"
                )

        output = io.BytesIO()
        # tarfile's w:gz mode inherits the wall-clock gzip timestamp.  That
        # made an identical teacher ZIP produce a different approved digest
        # when it was registered again.  Freeze both archive layers so the
        # normalized student material is content-addressed and reproducible.
        with gzip.GzipFile(
            filename="", fileobj=output, mode="wb", mtime=0
        ) as compressed, tarfile.open(
            fileobj=compressed, mode="w"
        ) as target_archive:
            directories: set[str] = set()
            for _, parts in normalized:
                for index in range(1, len(parts)):
                    directories.add("/".join(parts[:index]))
            for directory in sorted(directories):
                member = tarfile.TarInfo(directory)
                member.type = tarfile.DIRTYPE
                member.mode = 0o555
                member.mtime = 0
                target_archive.addfile(member)
            for info, parts in sorted(normalized, key=lambda item: item[1]):
                with archive.open(info) as source:
                    data = source.read(info.file_size + 1)
                if len(data) != info.file_size:
                    raise MaterialError(f"测试数据条目长度异常: {info.filename}")
                member = tarfile.TarInfo("/".join(parts))
                member.size = len(data)
                member.mode = 0o444
                member.mtime = 0
                target_archive.addfile(member, io.BytesIO(data))

    normalized_payload = output.getvalue()
    digest = hashlib.sha256(normalized_payload).hexdigest()
    return name, normalized_payload, digest, len(candidates), expanded_size


def save_testdata_archive(root: str | Path, tid: str, payload: bytes) -> Path:
    destination = testdata_archive_path(root, tid)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"testdata-{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
