#!/usr/bin/env python3
"""Derive the terminal capacity shutdown fact from HTTPS and Docker read-only APIs."""
from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
import os
import platform
import re
import socket
import ssl
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit


EMBEDDED_CONFIG = None
HEX64 = re.compile(r"[a-f0-9]{64}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.]\d+)?Z")


class ShutdownProbeError(RuntimeError):
    pass


def exact(value: Any, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ShutdownProbeError(f"{label} field set differs")
    return value


def validate_config(value: Any) -> dict:
    row = exact(value, {
        "schema_version", "health_url", "docker_socket", "controller_container_id",
        "controller_image_id", "controller_baseline",
    }, "shutdown probe configuration")
    try:
        parsed = urlsplit(row["health_url"])
        health_port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ShutdownProbeError("shutdown probe health URL is invalid") from exc
    if row["schema_version"] != 1 or parsed.scheme != "https" or not parsed.hostname or \
            parsed.username or parsed.password or parsed.query or parsed.fragment or \
            parsed.path != "/healthz" or row["docker_socket"] != "/var/run/docker.sock" or \
            health_port not in (None, 443) or \
            not isinstance(row["controller_container_id"], str) or \
            not HEX64.fullmatch(row["controller_container_id"]) or \
            not isinstance(row["controller_image_id"], str) or not IMAGE.fullmatch(row["controller_image_id"]):
        raise ShutdownProbeError("shutdown probe identity is invalid")
    baseline = exact(
        row["controller_baseline"], {"pid", "restart_count", "started_at"},
        "shutdown controller baseline",
    )
    if isinstance(baseline["pid"], bool) or not isinstance(baseline["pid"], int) or baseline["pid"] <= 0 or \
            isinstance(baseline["restart_count"], bool) or not isinstance(baseline["restart_count"], int) or \
            baseline["restart_count"] < 0 or not isinstance(baseline["started_at"], str) or \
            not TIMESTAMP.fullmatch(baseline["started_at"]):
        raise ShutdownProbeError("shutdown controller baseline is invalid")
    return row


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: int = 10):
        super().__init__("localhost", timeout=timeout); self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout); self.sock.connect(self.socket_path)


def docker_get(socket_path: str, resource: str, limit: int = 4 * 1024 * 1024) -> dict:
    connection = UnixHTTPConnection(socket_path)
    try:
        connection.request("GET", resource)
        response = connection.getresponse(); raw = response.read(limit + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise ShutdownProbeError("shutdown Docker read-only query failed") from exc
    finally:
        connection.close()
    if response.status != 200 or not raw or len(raw) > limit:
        raise ShutdownProbeError("shutdown Docker response is invalid")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShutdownProbeError("shutdown Docker response is not JSON") from exc
    if not isinstance(value, dict):
        raise ShutdownProbeError("shutdown Docker response shape differs")
    return value


def health_get(url: str) -> dict:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        response = opener.open(request, timeout=10)
    except (OSError, urllib.error.URLError, ssl.SSLError) as exc:
        raise ShutdownProbeError("shutdown health query failed") from exc
    with response:
        raw = response.read(1024 * 1024 + 1)
        status = int(response.status)
        final_url = response.geturl()
        content_type = response.headers.get_content_type()
    if status != 200 or final_url != url or content_type != "application/json" or \
            not raw or len(raw) > 1024 * 1024:
        raise ShutdownProbeError("shutdown health response differs")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShutdownProbeError("shutdown health response is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ShutdownProbeError("shutdown health response shape differs")
    return value


def count_states(value: Any, states: tuple[str, ...], label: str) -> int:
    if not isinstance(value, dict):
        raise ShutdownProbeError(f"{label} counts differ")
    total = 0
    for state in states:
        item = value.get(state, 0)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ShutdownProbeError(f"{label} counts differ")
        total += item
    return total


def collect(config: dict) -> dict:
    row = validate_config(config)
    container = docker_get(
        row["docker_socket"], f"/containers/{quote(row['controller_container_id'])}/json"
    )
    state = container.get("State") or {}; baseline = row["controller_baseline"]
    if container.get("Id") != row["controller_container_id"] or \
            container.get("Image") != row["controller_image_id"] or state.get("Running") is not True or \
            state.get("Restarting") is not False or state.get("Pid") != baseline["pid"] or \
            container.get("RestartCount") != baseline["restart_count"] or \
            state.get("StartedAt") != baseline["started_at"]:
        raise ShutdownProbeError("shutdown controller identity, PID, or restart state differs")
    health = health_get(row["health_url"])
    desktop = health.get("desktop_access")
    realtime = health.get("realtime_judge")
    notifications = health.get("seat_notifications")
    if health.get("ok") is not True or health.get("active_seats") != 0 or \
            not isinstance(desktop, dict) or desktop.get("desired_open") is not False or \
            desktop.get("closed") is not True or desktop.get("healthy") is not True or \
            desktop.get("enabled") is not True or desktop.get("management_healthy") is not True or \
            desktop.get("managed_count") != 0 or desktop.get("conflict_count") != 0 or \
            desktop.get("instance_state") != "STOPPED" or not isinstance(realtime, dict) or \
            realtime.get("thread_alive") is not True or realtime.get("running") is not True or \
            realtime.get("error_count") != 0 or not isinstance(notifications, dict) or \
            notifications.get("healthy") is not True:
        raise ShutdownProbeError("shutdown health semantics differ")
    delivery = count_states(
        realtime.get("queue_counts"),
        ("pending", "sending", "retry", "permanent_failed", "ambiguous"),
        "delivery queue",
    )
    notification = count_states(
        notifications.get("counts"),
        ("pending", "retry", "permanent_failed", "untracked", "missing_resource", "invalid_pool"),
        "notification queue",
    )
    if delivery or notification:
        raise ShutdownProbeError("shutdown queues are not empty")
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "active_seats": 0, "managed_rules": 0, "conflict_rules": 0,
        "cloud_state": "STOPPED", "delivery_queues": 0, "notification_queues": 0,
    }


def main() -> int:
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise ShutdownProbeError("shutdown observation probe requires Linux root")
        if EMBEDDED_CONFIG is None:
            raise ShutdownProbeError("shutdown observation probe is not frozen")
        print(json.dumps(collect(EMBEDDED_CONFIG), sort_keys=True, separators=(",", ":")))
        return 0
    except ShutdownProbeError as exc:
        print(f"NO_GO: {exc}", file=__import__("sys").stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
