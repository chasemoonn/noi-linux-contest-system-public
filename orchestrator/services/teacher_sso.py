"""Small, dependency-free ticket helpers for Hydro teacher SSO."""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import re
import time


_TID = re.compile(r"^[0-9a-f]{24}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,4096}$")


@dataclass(frozen=True)
class TeacherIdentity:
    uid: int
    uname: str
    tid: str | None
    expires_at: int
    technical: bool = False

    @property
    def actor(self) -> str:
        return f"oj:{self.uid}:{self.uname}" if not self.technical else self.uname


def _b64decode(value: str) -> bytes:
    if not value or not _TOKEN.fullmatch(value):
        raise ValueError("invalid SSO token encoding")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(key: bytes, purpose: str, payload: str) -> str:
    digest = hmac.new(
        key,
        f"noi-teacher-{purpose}-v1:{payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_hydro_ticket(
    ticket: str,
    key: str,
    *,
    now: int | None = None,
    maximum_lifetime: int = 120,
) -> TeacherIdentity:
    """Verify the short-lived ticket emitted by the Hydro addon."""
    try:
        payload_text, signature = ticket.split(".", 1)
    except ValueError as exc:
        raise ValueError("malformed SSO ticket") from exc
    expected = _sign(key.encode("utf-8"), "ticket", payload_text)
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid SSO ticket signature")
    try:
        payload = json.loads(_b64decode(payload_text))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid SSO ticket payload") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "v", "uid", "uname", "tid", "iat", "exp", "nonce"
    }:
        raise ValueError("invalid SSO ticket fields")
    current = int(time.time() if now is None else now)
    try:
        version = int(payload["v"])
        uid = int(payload["uid"])
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid SSO ticket values") from exc
    uname = str(payload["uname"])
    tid = str(payload["tid"]).lower()
    nonce = str(payload["nonce"])
    if (
        version != 1
        or uid <= 0
        or not uname
        or len(uname) > 128
        or not _TID.fullmatch(tid)
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce)
        or issued_at > current + 30
        or expires_at < current
        or expires_at <= issued_at
        or expires_at - issued_at > maximum_lifetime
    ):
        raise ValueError("expired or invalid SSO ticket")
    return TeacherIdentity(uid, uname, tid, expires_at)


def issue_session(
    identity: TeacherIdentity,
    key: str,
    *,
    now: int | None = None,
    lifetime: int = 12 * 60 * 60,
) -> str:
    current = int(time.time() if now is None else now)
    payload = {
        "v": 1,
        "uid": identity.uid,
        "uname": identity.uname,
        "tid": identity.tid,
        "iat": current,
        "exp": current + lifetime,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"{encoded}.{_sign(key.encode('utf-8'), 'session', encoded)}"


def verify_session(token: str, key: str, *, now: int | None = None) -> TeacherIdentity:
    try:
        payload_text, signature = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("malformed teacher session") from exc
    expected = _sign(key.encode("utf-8"), "session", payload_text)
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid teacher session signature")
    try:
        payload = json.loads(_b64decode(payload_text))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid teacher session payload") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "v", "uid", "uname", "tid", "iat", "exp"
    }:
        raise ValueError("invalid teacher session fields")
    current = int(time.time() if now is None else now)
    try:
        version = int(payload["v"])
        uid = int(payload["uid"])
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid teacher session values") from exc
    uname = str(payload["uname"])
    tid = str(payload["tid"]).lower()
    if (
        version != 1
        or uid <= 0
        or not uname
        or len(uname) > 128
        or not _TID.fullmatch(tid)
        or issued_at > current + 30
        or expires_at < current
        or expires_at <= issued_at
        or expires_at - issued_at > 24 * 60 * 60
    ):
        raise ValueError("expired or invalid teacher session")
    return TeacherIdentity(uid, uname, tid, expires_at)
