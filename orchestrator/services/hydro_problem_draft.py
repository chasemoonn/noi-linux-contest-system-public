"""Safely preflight and apply contest-private Hydro file-I/O problem clones."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

import requests

log = logging.getLogger("orchestrator.problem_draft")

_HEX_24 = re.compile(r"^[0-9a-fA-F]{24}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_PID = re.compile(
    r"^(?:(?:[a-z0-9]{1,10}-)?[a-z][a-z0-9]*|[1-9][0-9]{0,9})$",
    re.IGNORECASE,
)
_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class HydroProblemDraftClient:
    """Narrow client for the private Hydro contest problem-clone endpoint.

    The endpoint never accepts testdata bytes. Preflight returns statement text,
    a safe config summary, and normalized SHA-256 values computed inside Hydro.
    ``apply`` is accepted only after the caller supplies a teacher approval id.
    """

    def __init__(self, base_url: str, token: str):
        parsed = urlsplit(str(base_url).rstrip("/"))
        host = (parsed.hostname or "").lower().rstrip(".")
        loopback = host in {"127.0.0.1", "::1", "localhost"}
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Hydro internal_base_url must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and not loopback:
            raise ValueError("Plain HTTP is only allowed for a loopback Hydro endpoint")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Hydro internal_base_url must not contain credentials, query, or fragment"
            )
        if len(str(token)) < 32:
            raise ValueError("Hydro orchestrator token must contain at least 32 characters")
        self.endpoint = (
            f"{str(base_url).rstrip('/')}/orchestrator/submit/problem-fileio"
        )
        self.token = str(token)

    @staticmethod
    def operation_id(tid: str, preflight_id: str, approval_id: str) -> str:
        """Return one stable id for one approved immutable preflight."""
        normalized_tid = HydroProblemDraftClient._tid(tid)
        for name, value in (
            ("preflight_id", preflight_id),
            ("approval_id", approval_id),
        ):
            if not isinstance(value, str) or not _HEX_64.fullmatch(value):
                raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
        identity = json.dumps(
            ["noi-fileio-apply-v1", normalized_tid, preflight_id, approval_id],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _tid(value: str) -> str:
        if not isinstance(value, str) or not _HEX_24.fullmatch(value):
            raise ValueError("tid must be a 24-character Mongo ObjectId")
        return value.lower()

    @staticmethod
    def _problems(values: Iterable[Mapping[str, object]]) -> list[dict[str, str]]:
        if isinstance(values, (str, bytes, Mapping)):
            raise ValueError("problems must be a list of pid/slug mappings")
        try:
            items = list(values)
        except TypeError as exc:
            raise ValueError("problems must be iterable") from exc
        if not 1 <= len(items) <= 100:
            raise ValueError("problems must contain between 1 and 100 entries")
        result: list[dict[str, str]] = []
        seen_pids: set[str] = set()
        seen_slugs: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping) or set(item) != {"pid", "slug"}:
                raise ValueError("each problem must contain only pid and slug")
            pid = item["pid"]
            slug = item["slug"]
            if (
                not isinstance(pid, str)
                or len(pid) > 64
                or not _PID.fullmatch(pid)
                or not isinstance(slug, str)
                or not _SLUG.fullmatch(slug)
            ):
                raise ValueError("problem pid or slug is invalid")
            if pid.lower() in seen_pids or slug in seen_slugs:
                raise ValueError("problem pid and slug values must be unique")
            seen_pids.add(pid.lower())
            seen_slugs.add(slug)
            result.append({"pid": pid, "slug": slug})
        return result

    def _post(self, payload: dict, timeout: tuple[int, int]) -> tuple[object, dict]:
        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                headers={"X-Orchestrator-Token": self.token},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            return None, {"ok": False, "retryable": True, "error": str(exc)}
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text[:300]}
        if not isinstance(body, dict):
            body = {"payload": body}
        return response, body

    @staticmethod
    def _failure(response: object, body: dict) -> dict:
        if response is None:
            return body
        status = int(getattr(response, "status_code", 0) or 0)
        return {
            "ok": False,
            "retryable": (
                bool(getattr(response, "ok", False))
                or status in {408, 425, 429}
                or status >= 500
            ),
            "status_code": status,
            "error": body,
        }

    @staticmethod
    def _valid_preflight(body: dict, tid: str, problems: list[dict[str, str]]) -> bool:
        if body.get("tid") != tid or not isinstance(body.get("safe_to_apply"), bool):
            return False
        if body["safe_to_apply"] is False:
            return (
                body.get("preflight_id", "") == ""
                and isinstance(body.get("blockers"), list)
                and all(isinstance(value, str) for value in body["blockers"])
            )
        if not isinstance(body.get("preflight_id"), str) or not _HEX_64.fullmatch(
            body["preflight_id"]
        ):
            return False
        returned = body.get("problems")
        if not isinstance(returned, list) or len(returned) != len(problems):
            return False
        expected_slugs = {item["slug"] for item in problems}
        actual_slugs: set[str] = set()
        for item in returned:
            if not isinstance(item, dict):
                return False
            slug = item.get("slug")
            hashes = item.get("formal_input_sha256")
            if (
                slug not in expected_slugs
                or slug in actual_slugs
                or not isinstance(item.get("doc_id"), int)
                or isinstance(item.get("doc_id"), bool)
                or item["doc_id"] <= 0
                or not isinstance(item.get("content"), str)
                or not isinstance(item.get("title"), str)
                or not isinstance(item.get("config"), dict)
                or not isinstance(item.get("time_ms"), dict)
                or not isinstance(item.get("memory_mb"), dict)
                or not isinstance(hashes, list)
                or not all(isinstance(value, str) and _HEX_64.fullmatch(value) for value in hashes)
            ):
                return False
            actual_slugs.add(slug)
        return actual_slugs == expected_slugs

    @staticmethod
    def _valid_apply(
        body: dict,
        tid: str,
        operation_id: str,
        preflight_id: str,
        problems: list[dict[str, str]],
    ) -> bool:
        mapping = body.get("mapping")
        pids = body.get("pids")
        if (
            body.get("status") != "applied"
            or body.get("operation_id") != operation_id
            or body.get("tid") != tid
            or body.get("preflight_id") != preflight_id
            or not isinstance(mapping, list)
            or len(mapping) != len(problems)
            or not isinstance(pids, list)
        ):
            return False
        expected_slugs = {item["slug"] for item in problems}
        actual_slugs: set[str] = set()
        clone_ids: list[int] = []
        for item in mapping:
            if not isinstance(item, dict):
                return False
            slug = item.get("slug")
            source_doc_id = item.get("source_doc_id")
            clone_doc_id = item.get("clone_doc_id")
            if (
                slug not in expected_slugs
                or slug in actual_slugs
                or item.get("verified") is not True
                or not isinstance(source_doc_id, int)
                or isinstance(source_doc_id, bool)
                or source_doc_id <= 0
                or not isinstance(clone_doc_id, int)
                or isinstance(clone_doc_id, bool)
                or clone_doc_id <= 0
                or not isinstance(item.get("source_pid"), str)
                or not isinstance(item.get("clone_pid"), str)
                or not _PID.fullmatch(item["clone_pid"])
            ):
                return False
            actual_slugs.add(slug)
            clone_ids.append(clone_doc_id)
        return actual_slugs == expected_slugs and pids == clone_ids

    def preflight(
        self,
        *,
        tid: str,
        problems: Iterable[Mapping[str, object]],
    ) -> dict:
        normalized_tid = self._tid(tid)
        normalized_problems = self._problems(problems)
        payload = {
            "action": "preflight",
            "tid": normalized_tid,
            "problems": normalized_problems,
        }
        response, body = self._post(payload, (5, 180))
        if response is None:
            return body
        if not getattr(response, "ok", False) or not self._valid_preflight(
            body, normalized_tid, normalized_problems
        ):
            return self._failure(response, body)
        # Do not log or copy statement content into logs.
        log.info(
            "problem preflight tid=%s safe=%s count=%s",
            normalized_tid,
            body["safe_to_apply"],
            len(body.get("problems", [])),
        )
        return {"ok": True, "retryable": False, **body}

    def apply(
        self,
        *,
        tid: str,
        problems: Iterable[Mapping[str, object]],
        operation_id: str,
        approval_id: str,
        preflight_id: str,
    ) -> dict:
        normalized_tid = self._tid(tid)
        normalized_problems = self._problems(problems)
        for name, value in (
            ("operation_id", operation_id),
            ("approval_id", approval_id),
            ("preflight_id", preflight_id),
        ):
            if not isinstance(value, str) or not _HEX_64.fullmatch(value):
                raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
        payload = {
            "action": "apply",
            "tid": normalized_tid,
            "problems": normalized_problems,
            "operation_id": operation_id,
            "approval_id": approval_id,
            "preflight_id": preflight_id,
        }
        response, body = self._post(payload, (5, 600))
        if response is None:
            return body
        if not getattr(response, "ok", False) or not self._valid_apply(
            body, normalized_tid, operation_id, preflight_id, normalized_problems
        ):
            return self._failure(response, body)
        log.info(
            "problem draft applied tid=%s operation=%s count=%s",
            normalized_tid,
            operation_id[:12],
            len(body["mapping"]),
        )
        return {"ok": True, "retryable": False, **body}
