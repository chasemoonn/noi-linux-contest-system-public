"""Submit collected code to the Hydro orchestrator plugin."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat

import requests

log = logging.getLogger("orchestrator.submit")


class HydroSubmitter:
    def __init__(
        self,
        base_url: str,
        token: str,
        lang: str = "",
        *,
        qualification_failure_marker_path: str = "",
        qualification_marker: str = "",
    ):
        self.endpoint = f"{base_url.rstrip('/')}/orchestrator/submit"
        self.status_endpoint = f"{self.endpoint}/status"
        self.token = token
        self.lang = lang
        self.qualification_failure_marker_path = str(
            qualification_failure_marker_path or ""
        )
        self.qualification_marker = str(qualification_marker or "")
        if self.qualification_failure_marker_path:
            if os.name == "posix" and not re.fullmatch(
                r"/app/data/qualification/[A-Za-z0-9_.-]{1,128}[.]json",
                self.qualification_failure_marker_path,
            ):
                raise ValueError(
                    "qualification failure marker path is outside the qualification directory"
                )
            if os.name != "posix" and not Path(
                self.qualification_failure_marker_path
            ).is_absolute():
                raise ValueError("qualification failure marker path must be absolute")
            if not re.fullmatch(
                r"NOI-V1-QUAL-[A-Z0-9]{16,64}", self.qualification_marker
            ):
                raise ValueError("qualification marker is invalid")

    def _qualification_failure_active(self, submission_id: str) -> bool:
        """Recognize one root-only qualification failure marker before OJ I/O.

        Production configurations omit this path. It exists solely so an
        independent qualification run can prove collection retry semantics
        without mutating the host network namespace or the ordinary OJ.
        """
        if not self.qualification_failure_marker_path:
            return False
        path = Path(self.qualification_failure_marker_path)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return False
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (
                    os.name == "posix"
                    and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077)
                )
                or not 0 < info.st_size <= 4096
            ):
                raise RuntimeError("qualification failure marker metadata is unsafe")
            raw = os.read(descriptor, info.st_size + 1)
            if len(raw) != info.st_size:
                raise RuntimeError("qualification failure marker changed while reading")
        finally:
            os.close(descriptor)
        try:
            marker = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("qualification failure marker is invalid") from exc
        expected = {
            "schema_version": 1,
            "qualification_marker": self.qualification_marker,
            "scenario": "collection_retry",
            "submission_id": str(submission_id),
            "failure": "block_until_removed",
        }
        if marker != expected:
            raise RuntimeError("qualification failure marker identity differs")
        return True

    @staticmethod
    def submission_id(session: str, tid: str, uid: int, pid: str) -> str:
        """Return a stable id for one logical contest submission slot."""
        identity = json.dumps(
            [str(session), str(tid), int(uid), str(pid)],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def realtime_submission_id(
        session: str,
        tid: str,
        uid: int,
        pid: str,
        client_nonce: str,
    ) -> str:
        """Return a stable id for one explicit real-time submit action.

        The legacy :meth:`submission_id` deliberately represents one final
        problem slot. Including a browser nonce here lets later source versions
        create distinct Hydro records while a transport replay gets the same
        record.
        """
        identity = json.dumps(
            [
                "web-v1",
                str(session),
                str(tid),
                int(uid),
                str(pid),
                str(client_nonce),
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def submit_one(
        self,
        tid: str,
        uid: int,
        pid: str,
        code: str,
        submission_id: str,
        *,
        lang: str | None = None,
        submission_kind: str = "final",
        accepted_at_ms: int | None = None,
    ) -> dict:
        if submission_kind not in {"final", "realtime"}:
            raise ValueError("submission_kind must be final or realtime")
        if self._qualification_failure_active(submission_id):
            return {
                "ok": False,
                "retryable": False,
                "ambiguous": False,
                "error": "qualification collection retry failure",
            }
        data = {
            "tid": tid,
            "uid": uid,
            "pid": pid,
            "code": code,
            "submission_id": submission_id,
            "submission_kind": submission_kind,
        }
        selected_lang = self.lang if lang is None else str(lang)
        if selected_lang:
            data["lang"] = selected_lang
        if accepted_at_ms is not None:
            data["accepted_at_ms"] = int(accepted_at_ms)
        try:
            response = requests.post(
                self.endpoint,
                data=data,
                headers={"X-Orchestrator-Token": self.token},
                timeout=(5, 30),
            )
        except requests.RequestException as exc:
            return {"ok": False, "retryable": True, "error": str(exc)}
        try:
            payload = response.json()
        except ValueError:
            payload = {"text": response.text[:300]}
        if response.ok and payload.get("rid"):
            result = {
                "ok": True,
                "retryable": False,
                "rid": str(payload["rid"]),
            }
        else:
            # A nominally successful response without a rid is an incomplete
            # acknowledgement. Retrying is safe because submission_id is
            # stable and the plugin returns the original record on replay.
            error_body = payload.get("error") if isinstance(payload, dict) else None
            error_name = (
                str(error_body.get("name") or "")
                if isinstance(error_body, dict)
                else ""
            )
            ambiguous = (
                response.status_code == 409
                and error_name == "OrchestratorSubmissionAmbiguousError"
            )
            retryable = not ambiguous and (
                response.ok
                # A rolling deployment can briefly expose a mismatched
                # orchestrator token. Keep the durable outbox item retryable
                # so fixing the shared secret recovers it automatically.
                or response.status_code in {401, 403, 408, 425, 429}
                or response.status_code >= 500
            )
            result = {
                "ok": False,
                "retryable": retryable,
                "ambiguous": ambiguous,
                "status_code": response.status_code,
                "error": payload,
            }
        log.info("submit uid=%s pid=%s result=%s", uid, pid, result)
        return result

    def resolve_submission(self, submission_id: str) -> dict:
        """Read the plugin's exact correlation result without resubmitting."""
        if not isinstance(submission_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}", submission_id
        ):
            raise ValueError("submission_id must be 64 lowercase hexadecimal characters")
        try:
            response = requests.post(
                self.status_endpoint,
                data={"submission_id": submission_id},
                headers={"X-Orchestrator-Token": self.token},
                timeout=(5, 30),
            )
        except requests.RequestException as exc:
            return {"ok": False, "status": "unavailable", "error": str(exc)}
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        status = str(payload.get("status") or "") if isinstance(payload, dict) else ""
        rid = str(payload.get("rid") or "") if isinstance(payload, dict) else ""
        if response.ok and status == "resolved" and re.fullmatch(
            r"[0-9a-f]{24}", rid
        ):
            return {"ok": True, "status": status, "rid": rid}
        if response.ok and status in {
            "missing", "multiple", "pending", "unknown", "unsupported"
        }:
            return {"ok": False, "status": status}
        return {
            "ok": False,
            "status": "unavailable",
            "status_code": int(response.status_code),
        }
