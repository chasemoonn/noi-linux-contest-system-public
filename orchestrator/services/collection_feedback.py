"""Bounded, read-only feedback from durable collection reports; never resubmit."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time


def _read_verified(directory: Path, name: str, digest: str) -> dict:
    path = directory / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory:
        raise ValueError("untrusted report path")
    with path.open("rb") as handle:
        raw = handle.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("report digest or size mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("report must be an object")
    return value


def directory_receipts(root: Path, contest: dict, seat: dict,
                       problems: list[str], latest: dict) -> dict:
    """Only return the current candidate's source-free, score-free summaries."""
    missing_web = [problem for problem in problems if problem not in latest]
    if not missing_web:
        return {}
    state = contest.get("state")
    if state not in {"collecting", "safe_wait", "safe_ended", "done", "error"}:
        end = int(contest.get("end_at_ms") or 0)
        if not end or time.time() * 1000 < end:
            return {}
    fallback = "unverified" if state in {"safe_wait", "safe_ended", "done", "error"} else "pending"
    rows = {problem: {"state": fallback} for problem in missing_web}
    if state not in {"safe_wait", "safe_ended", "done"}:
        return rows
    if not contest.get("collection_receipt_sha256") or not contest.get("collection_dir"):
        return rows
    try:
        tid = str(contest.get("tid") or "")
        if not re.fullmatch(r"[0-9a-f]{24}", tid) or seat.get("tid") != tid:
            raise ValueError("contest identity mismatch")
        contest_root = Path(root).resolve() / tid
        if contest_root.is_symlink():
            raise ValueError("untrusted contest root")
        configured = Path(contest["collection_dir"])
        if configured.is_symlink():
            raise ValueError("untrusted collection path")
        directory = configured.resolve()
        directory.relative_to(contest_root)
        receipt = _read_verified(directory, "collection_receipt.json",
                                 str(contest["collection_receipt_sha256"]))
        if (receipt.get("schema_version") != 1 or receipt.get("tid") != tid
                or not contest.get("collection_run_id")
                or receipt.get("run_id") != contest["collection_run_id"]
                or receipt.get("submit_failures") != 0):
            raise ValueError("collection receipt mismatch")
        files = receipt["files"]
        report = _read_verified(directory, "report.json", files["report.json"])
        deliveries = _read_verified(directory, "submit_log.json", files["submit_log.json"])
        uname = str(seat.get("uname") or "")
        selected = report.get(uname, {})
        delivered = deliveries.get(uname, {})
        completed = int(receipt["completed_at_ms"])
        if completed <= 0:
            raise ValueError("invalid collection timestamp")
        created = datetime.fromtimestamp(completed / 1000, timezone.utc).isoformat()
        for problem in missing_web:
            item = selected.get(problem, {})
            result = delivered.get(problem, {})
            if item.get("submission_source") != "deadline_snapshot":
                rows[problem] = {"state": "unverified"}
                continue
            if result.get("skipped") is True and result.get("submitted") is False:
                rows[problem] = {"state": "missing", "created_at": created}
            elif (result.get("ok") is True
                  and re.fullmatch(r"[0-9a-f]{24}", str(result.get("rid") or ""))):
                size = int(item["size"])
                if size <= 0:
                    raise ValueError("invalid collected size")
                rows[problem] = {"state": "submitted", "size": size, "created_at": created}
            elif deliveries.get("_status") == "Hydro 回传已禁用":
                rows[problem] = {"state": "stored" if int(item.get("size") or 0) > 0 else "missing",
                                 "created_at": created}
            else:
                rows[problem] = {"state": "unverified"}
        return rows
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        # Missing/expired/corrupt evidence must not break the submission page,
        # expose internal paths, or manufacture a successful OJ delivery.
        return {problem: {"state": "unverified"} for problem in missing_web}
