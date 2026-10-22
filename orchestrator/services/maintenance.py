"""Opt-in admission and shutdown gate for the independent host maintenance lease."""
from __future__ import annotations

import json
import re
import secrets
import time

LOCK_PATHS = ["/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock",
              "/var/lib/apt/lists/lock", "/var/cache/apt/archives/lock"]
SHUTDOWN_RESERVE_MS = 150_000  # Covers the existing bounded 120-second cloud stop.


def settings(server):
    value = server.get("maintenance_guard", {"enabled": False})
    if not isinstance(value, dict) or type(value.get("enabled", False)) is not bool:
        raise ValueError("maintenance_guard.enabled must be boolean")
    if not value.get("enabled", False):
        return None
    if not re.fullmatch(r"[0-9a-f]{32}", str(value.get("machine_id", ""))):
        raise ValueError("maintenance_guard requires the dedicated host machine_id")
    return value


def verify_status(result, *, nonce, machine_id, tid, now_ms, required_until_ms):
    """Never accept a file snapshot, another contest, or a wall-clock-only TTL."""
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        raise RuntimeError("invalid maintenance status schema")
    for key in ("checked_at_ms", "expires_at_ms", "remaining_ms", "pid"):
        if type(result.get(key)) is not int:
            raise RuntimeError("invalid maintenance status timestamp/pid")
    if (result.get("state") != "protected" or result.get("nonce") != nonce
            or result.get("machine_id") != machine_id or result.get("locks") != LOCK_PATHS
            or result["pid"] <= 1
            or not re.fullmatch(r"[0-9a-f-]{36}", str(result.get("boot_id", "")))
            or not re.fullmatch(r"[0-9a-f]{24}", str(result.get("tid", "")))):
        raise RuntimeError("maintenance host/lease/lock proof mismatch")
    if tid is not None and result.get("tid") != tid:
        raise RuntimeError("maintenance lease belongs to another contest")
    if abs(result["checked_at_ms"] - now_ms) > 5000:
        raise RuntimeError("maintenance status stale or host clock skew")
    # Reserve clock skew and query latency, and check BOOTTIME remaining as
    # well as absolute expiry. Neither clock adjustment extends this proof.
    available_until = min(result["expires_at_ms"],
                          result["checked_at_ms"] + result["remaining_ms"]) - 5000
    if required_until_ms < now_ms or available_until < required_until_ms:
        raise RuntimeError("maintenance lease cannot cover contest/archive/shutdown")
    return result


def require_protection(remote, server, *, tid=None, end_at_ms=None, grace_minutes=30):
    config = settings(server)
    if config is None:
        return None
    nonce = secrets.token_hex(16)
    raw = remote.run("sudo -n /usr/bin/python3 /opt/noi-maintenance/maintenance_guard.py "
                     f"status --nonce {nonce}", timeout=10)
    if len(raw) > 8192:
        raise RuntimeError("oversized maintenance status")
    value = json.loads(raw)
    now = int(time.time() * 1000)
    until = now + SHUTDOWN_RESERVE_MS
    if end_at_ms is not None:
        if type(end_at_ms) is not int or type(grace_minutes) is not int or grace_minutes < 30:
            raise RuntimeError("invalid protected contest/archive period")
        until = max(until, end_at_ms + grace_minutes * 60_000 + SHUTDOWN_RESERVE_MS)
    return verify_status(value, nonce=nonce, machine_id=config["machine_id"],
                         tid=tid, now_ms=now, required_until_ms=until)
