"""Publish one immutable contest material release through the Hydro addon."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import requests


_TID = re.compile(r"^[0-9a-fA-F]{24}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ATTACHMENTS = (
    ("01_比赛题面.pdf", "paper"),
    ("02_辅助自测数据.tar.gz", "testdata"),
)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class HydroMaterialPublisher:
    """Narrow client for publishing the exact V1 material attachment set."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        maximum_bytes: int = 128 * 1024 * 1024,
        request_timeout: tuple[float, float] = (5.0, 180.0),
    ):
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
        if (
            isinstance(maximum_bytes, bool)
            or not isinstance(maximum_bytes, int)
            or not 1024 * 1024 <= maximum_bytes <= 512 * 1024 * 1024
        ):
            raise ValueError("maximum_bytes must be between 1 MiB and 512 MiB")
        if (
            not isinstance(request_timeout, tuple)
            or len(request_timeout) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or float(value) <= 0
                for value in request_timeout
            )
        ):
            raise ValueError("request_timeout must contain positive connect/read seconds")
        self.endpoint = f"{str(base_url).rstrip('/')}/orchestrator/submit/materials"
        self.token = str(token)
        self.maximum_bytes = maximum_bytes
        self.request_timeout = tuple(float(value) for value in request_timeout)
        self.session = requests.Session()
        self.session.trust_env = False

    @staticmethod
    def publication_id(tid: str, revision: str, attachments: list[dict]) -> str:
        normalized_tid = str(tid).lower()
        if not _TID.fullmatch(normalized_tid):
            raise ValueError("tid must be a 24-character Mongo ObjectId")
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise ValueError("revision is invalid")
        identity = [
            "noi-material-publication-v1",
            normalized_tid,
            revision,
            [
                {
                    "name": item["name"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                }
                for item in attachments
            ],
        ]
        return hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _attachment(self, name: str, path: Path, expected_sha256: str) -> dict:
        if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(
            expected_sha256
        ):
            raise ValueError(f"{name} expected SHA-256 is invalid")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"{name} is not readable") from exc
        actual = hashlib.sha256(content).hexdigest()
        if not content or actual != expected_sha256:
            raise ValueError(f"{name} bytes do not match the approved SHA-256")
        return {
            "name": name,
            "sha256": actual,
            "size": len(content),
            "content_base64": base64.b64encode(content).decode("ascii"),
        }

    def publish(
        self,
        *,
        tid: str,
        revision: str,
        paper_path: str | Path,
        paper_sha256: str,
        testdata_path: str | Path,
        testdata_sha256: str,
    ) -> dict:
        normalized_tid = str(tid).lower()
        if not _TID.fullmatch(normalized_tid):
            raise ValueError("tid must be a 24-character Mongo ObjectId")
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise ValueError("revision is invalid")
        paths = {
            "paper": (Path(paper_path), str(paper_sha256)),
            "testdata": (Path(testdata_path), str(testdata_sha256)),
        }
        attachments = [
            self._attachment(name, *paths[kind]) for name, kind in _ATTACHMENTS
        ]
        if sum(item["size"] for item in attachments) > self.maximum_bytes:
            raise ValueError("approved material attachment set exceeds maximum_bytes")
        publication_id = self.publication_id(
            normalized_tid,
            revision,
            attachments,
        )
        payload = {
            "publication_id": publication_id,
            "tid": normalized_tid,
            "revision": revision,
            "attachments": [
                {
                    "name": item["name"],
                    "sha256": item["sha256"],
                    "content_base64": item["content_base64"],
                }
                for item in attachments
            ],
        }
        try:
            response = self.session.post(
                self.endpoint,
                json=payload,
                headers={"X-Orchestrator-Token": self.token},
                timeout=self.request_timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            return {"ok": False, "retryable": True, "error": str(exc)}
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text[:300]}
        expected_receipt = [
            {"name": item["name"], "sha256": item["sha256"], "size": item["size"]}
            for item in attachments
        ]
        valid = (
            response.ok
            and isinstance(body, dict)
            and body.get("status") == "published"
            and body.get("publication_id") == publication_id
            and body.get("tid") == normalized_tid
            and body.get("revision") == revision
            and body.get("attachments") == expected_receipt
        )
        if not valid:
            status = int(response.status_code or 0)
            return {
                "ok": False,
                "retryable": status in {408, 425, 429} or status >= 500,
                "status_code": status,
                "error": body,
            }
        receipt = {
            "publication_id": publication_id,
            "tid": normalized_tid,
            "revision": revision,
            "attachments": expected_receipt,
        }
        return {
            "ok": True,
            **receipt,
            "receipt_sha256": _canonical_sha256(receipt),
        }
