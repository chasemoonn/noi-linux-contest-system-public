"""Send allowlisted student seat notifications through Hydro's native inbox."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from urllib.parse import urlsplit, urlunsplit

import requests

log = logging.getLogger("orchestrator.notify")

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{24}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _is_exact_dns_hostname(value: str) -> bool:
    labels = value.split(".")
    return (
        len(value) <= 253
        and len(labels) >= 2
        and not re.fullmatch(r"\d+(?:\.\d+){3}", value)
        and all(0 < len(label) <= 63 and _DNS_LABEL.fullmatch(label) for label in labels)
    )


class HydroNotifier:
    """Narrow client for the plugin's ``seat_ready`` notification endpoint.

    The public method deliberately accepts only a student's seat credential.
    There is no generic message body and no administrator/SSH secret field.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        allowed_exam_hosts: list[str] | tuple[str, ...] | set[str],
        request_timeout: tuple[float, float] = (2.0, 5.0),
    ):
        parsed = urlsplit(str(base_url).rstrip("/"))
        host = (parsed.hostname or "").lower().rstrip(".")
        loopback = host in {"127.0.0.1", "::1", "localhost"}
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Hydro internal_base_url must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and not loopback:
            raise ValueError("Plain HTTP is only allowed for a loopback Hydro endpoint")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Hydro internal_base_url must not contain credentials, query, or fragment")
        if len(str(token)) < 32:
            raise ValueError("Hydro orchestrator token must contain at least 32 characters")
        allowed = {
            str(value).strip().lower().rstrip(".")
            for value in allowed_exam_hosts
            if str(value).strip()
        }
        if not allowed:
            raise ValueError("At least one HTTPS exam hostname must be allowlisted")
        if any(not _is_exact_dns_hostname(value) for value in allowed):
            raise ValueError("Exam host allowlist accepts exact hostnames only")
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

        self.endpoint = f"{str(base_url).rstrip('/')}/orchestrator/submit/notify"
        self.token = str(token)
        self.allowed_exam_hosts = allowed
        self.request_timeout = tuple(float(value) for value in request_timeout)

    @staticmethod
    def notification_id(tid: str, uid: int, seat_revision: str | int) -> str:
        """Return the stable id for one student's issued seat revision."""
        if not _OBJECT_ID.fullmatch(str(tid)):
            raise ValueError("tid must be a 24-character Mongo ObjectId")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 1:
            raise ValueError("uid must identify a student user")
        revision = str(seat_revision)
        if not revision or len(revision.encode("utf-8")) > 128 or _CONTROL.search(revision):
            raise ValueError("seat_revision is invalid")
        identity = json.dumps(
            ["noi-seat-ready-v1", str(tid).lower(), uid, revision],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _text(name: str, value: str, maximum_bytes: int, *, optional: bool = False) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text")
        normalized = value if name == "student_password" else value.strip()
        if ((not optional and not normalized)
                or len(normalized.encode("utf-8")) > maximum_bytes
                or _CONTROL.search(normalized)):
            raise ValueError(f"{name} is invalid")
        return normalized

    def _desktop_url(self, value: str) -> str:
        raw = self._text("desktop_url", value, 2048)
        parsed = urlsplit(raw)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("desktop_url has an invalid port") from exc
        if (parsed.scheme.lower() != "https"
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or port not in {None, 443}
                or parsed.fragment
                or hostname not in self.allowed_exam_hosts):
            raise ValueError("desktop_url must use an allowlisted HTTPS exam hostname")
        # Normalize the host spelling and omit the default port so the Python
        # fingerprint matches the URL accepted and normalized by Node.
        netloc = hostname
        return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))

    def send_seat_ready(
        self,
        *,
        uid: int,
        notification_id: str,
        contest_title: str,
        desktop_url: str,
        candidate: str,
        student_password: str,
        available_at: str = "",
    ) -> dict:
        """Send only a student seat login notification; never a generic secret."""
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 1:
            raise ValueError("uid must identify a student user")
        if not isinstance(notification_id, str) or not _HEX_64.fullmatch(notification_id):
            raise ValueError("notification_id must be 64 lowercase hexadecimal characters")
        data = {
            "notification_id": notification_id,
            "purpose": "seat_ready",
            "uid": uid,
            "contest_title": self._text("contest_title", contest_title, 512),
            "desktop_url": self._desktop_url(desktop_url),
            "candidate": self._text("candidate", candidate, 128),
            "student_password": self._text(
                "student_password", student_password, 256
            ),
            "available_at": self._text(
                "available_at", available_at, 128, optional=True
            ),
        }
        try:
            response = requests.post(
                self.endpoint,
                json=data,
                headers={"X-Orchestrator-Token": self.token},
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            return {"ok": False, "retryable": True, "error": str(exc)}

        try:
            payload = response.json()
        except ValueError:
            payload = {"text": response.text[:300]}
        acknowledged = (
            response.ok
            and payload.get("notification_id") == notification_id
        )
        if acknowledged:
            result = {
                "ok": True,
                "retryable": False,
                "notification_id": notification_id,
                "message_id": str(payload.get("message_id") or ""),
            }
        else:
            result = {
                "ok": False,
                "retryable": (
                    response.ok
                    or response.status_code in {401, 403, 408, 425, 429}
                    or response.status_code >= 500
                ),
                "status_code": response.status_code,
                "error": payload,
            }
        # Do not log the request payload: it intentionally contains the
        # student's one-seat password.
        log.info(
            "seat notification uid=%s id=%s ok=%s status=%s",
            uid,
            notification_id[:12],
            result["ok"],
            getattr(response, "status_code", None),
        )
        return result
