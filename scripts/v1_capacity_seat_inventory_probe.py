#!/usr/bin/env python3
"""Derive the terminal 15+2 seat inventory from Docker read-only APIs."""

from __future__ import annotations

import http.client
import json
import os
import platform
import re
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote


EMBEDDED_CONFIG = None
HEX64 = re.compile(r"[a-f0-9]{64}")
TID = re.compile(r"[a-f0-9]{24}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.]\d+)?Z")


class SeatProbeError(RuntimeError):
    pass


def exact(value: Any, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise SeatProbeError(f"{label} field set differs")
    return value


def validate_config(value: Any) -> dict:
    row = exact(value, {
        "schema_version", "tid", "docker_socket", "desktop_image_id", "network_name", "network_id",
        "formal_container_ids", "spare_container_ids", "baselines", "planned_restart",
        "fault_replacement",
    }, "seat probe configuration")
    if row["schema_version"] != 1 or not isinstance(row["tid"], str) or not TID.fullmatch(row["tid"]):
        raise SeatProbeError("seat probe identity is invalid")
    if row["docker_socket"] != "/var/run/docker.sock" or \
            not isinstance(row["desktop_image_id"], str) or \
            not re.fullmatch(r"sha256:[a-f0-9]{64}", row["desktop_image_id"]) or \
            not isinstance(row["network_name"], str) or \
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", row["network_name"]) or \
            not isinstance(row["network_id"], str) or not HEX64.fullmatch(row["network_id"]):
        raise SeatProbeError("seat probe Docker identity is invalid")
    formal, spare = row["formal_container_ids"], row["spare_container_ids"]
    if not isinstance(formal, list) or len(formal) != 15 or len(set(formal)) != 15 or \
            not isinstance(spare, list) or len(spare) != 2 or len(set(spare)) != 2 or \
            set(formal) & set(spare) or any(not isinstance(item, str) or not HEX64.fullmatch(item)
                                            for item in formal + spare):
        raise SeatProbeError("seat probe must bind 15+2 unique full container IDs")
    baselines = row["baselines"]
    if not isinstance(baselines, list) or len(baselines) != 17:
        raise SeatProbeError("seat probe baseline count differs")
    seen_ids, seen_slots = set(), set()
    for item in baselines:
        baseline = exact(item, {"container_id", "slot_no", "pid", "restart_count", "started_at"},
                         "seat baseline")
        if baseline["container_id"] not in set(formal + spare) or baseline["container_id"] in seen_ids or \
                isinstance(baseline["slot_no"], bool) or not isinstance(baseline["slot_no"], int) or \
                not 1 <= baseline["slot_no"] <= 17 or baseline["slot_no"] in seen_slots or \
                isinstance(baseline["pid"], bool) or not isinstance(baseline["pid"], int) or baseline["pid"] <= 0 or \
                isinstance(baseline["restart_count"], bool) or not isinstance(baseline["restart_count"], int) or \
                baseline["restart_count"] < 0 or not isinstance(baseline["started_at"], str) or \
                not TIMESTAMP.fullmatch(baseline["started_at"]):
            raise SeatProbeError("seat probe baseline is invalid")
        seen_ids.add(baseline["container_id"]); seen_slots.add(baseline["slot_no"])
    if seen_ids != set(formal + spare) or seen_slots != set(range(1, 18)):
        raise SeatProbeError("seat probe baselines are incomplete")
    by_id = {item["container_id"]: item["slot_no"] for item in baselines}
    if {by_id[item] for item in formal} != set(range(1, 16)) or \
            {by_id[item] for item in spare} != {16, 17}:
        raise SeatProbeError("formal and spare slot roles differ")
    planned = exact(
        row["planned_restart"], {"container_id", "restart_count_delta"},
        "planned restart",
    )
    if planned["container_id"] not in set(formal) or planned["restart_count_delta"] != 1:
        raise SeatProbeError("planned restart must bind exactly one formal seat restart")
    replacement = exact(
        row["fault_replacement"], {
            "baseline_container_id", "slot_no", "replacement_spare_container_id",
            "replacement_slot_no",
        },
        "fault replacement",
    )
    replacement_id = replacement["baseline_container_id"]
    if replacement_id not in set(formal) or replacement_id == planned["container_id"] or \
            isinstance(replacement["slot_no"], bool) or \
            not isinstance(replacement["slot_no"], int) or \
            by_id.get(replacement_id) != replacement["slot_no"] or \
            replacement["replacement_spare_container_id"] not in set(spare) or \
            isinstance(replacement["replacement_slot_no"], bool) or \
            not isinstance(replacement["replacement_slot_no"], int) or \
            by_id.get(replacement["replacement_spare_container_id"]) != \
            replacement["replacement_slot_no"]:
        raise SeatProbeError("fault replacement must bind a different formal seat")
    return row


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: int = 10):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def docker_get(socket_path: str, resource: str, limit: int = 4 * 1024 * 1024) -> dict:
    connection = UnixHTTPConnection(socket_path)
    try:
        connection.request("GET", resource)
        response = connection.getresponse()
        raw = response.read(limit + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise SeatProbeError("Docker read-only query failed") from exc
    finally:
        connection.close()
    if response.status != 200 or not raw or len(raw) > limit:
        raise SeatProbeError("Docker read-only response is invalid")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SeatProbeError("Docker read-only response is not JSON") from exc
    if not isinstance(value, dict):
        raise SeatProbeError("Docker read-only response shape differs")
    return value


def probe_novnc(ip: str) -> None:
    if not re.fullmatch(r"(?:\d{1,3}[.]){3}\d{1,3}", ip):
        raise SeatProbeError("seat network address is invalid")
    connection = http.client.HTTPConnection(ip, 6080, timeout=5)
    try:
        connection.request("GET", "/vnc.html", headers={"Host": ip, "Connection": "close"})
        response = connection.getresponse()
        raw = response.read(2 * 1024 * 1024 + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise SeatProbeError("seat noVNC health query failed") from exc
    finally:
        connection.close()
    if response.status != 200 or not 0 < len(raw) <= 2 * 1024 * 1024 or b"noVNC" not in raw:
        raise SeatProbeError("seat noVNC health differs")


def collect(config: dict) -> dict:
    row = validate_config(config)
    baselines = {item["container_id"]: item for item in row["baselines"]}
    baseline_ids = row["formal_container_ids"] + row["spare_container_ids"]
    observed_ips, observed_macs = set(), set()
    planned_id = row["planned_restart"]["container_id"]
    replacement = row["fault_replacement"]
    replaced_id = replacement["baseline_container_id"]
    network = docker_get(row["docker_socket"], f"/networks/{quote(row['network_id'])}")
    options = network.get("Options") or {}
    members = network.get("Containers") or {}
    member_ids = set(members)
    stable_ids = set(baseline_ids) - {replaced_id}
    new_ids = member_ids - stable_ids
    missing_ids = set(baseline_ids) - member_ids
    if network.get("Id") != row["network_id"] or network.get("Driver") != "bridge" or \
            network.get("Internal") is not True or network.get("Attachable") is not False or \
            network.get("Name") != row["network_name"] or \
            options.get("com.docker.network.bridge.enable_icc") != "false" or \
            len(member_ids) != 17 or missing_ids != {replaced_id} or len(new_ids) != 1:
        raise SeatProbeError("seat Docker network or fault replacement set differs")
    replacement_id = next(iter(new_ids))
    if not HEX64.fullmatch(replacement_id):
        raise SeatProbeError("fault replacement container ID is invalid")
    current_ids = [
        replacement_id if item == replaced_id else item for item in baseline_ids
    ]
    for container_id in current_ids:
        value = docker_get(row["docker_socket"], f"/containers/{quote(container_id)}/json")
        state = value.get("State") or {}
        labels = (value.get("Config") or {}).get("Labels") or {}
        networks = (value.get("NetworkSettings") or {}).get("Networks") or {}
        baseline_id = replaced_id if container_id == replacement_id else container_id
        baseline = baselines[baseline_id]
        if value.get("Id") != container_id or value.get("Image") != row["desktop_image_id"] or \
                state.get("Running") is not True or state.get("Restarting") is not False or \
                labels.get("noi.contest") != row["tid"] or \
                labels.get("noi.slot") != str(baseline["slot_no"]) or set(networks) != {row["network_name"]}:
            raise SeatProbeError("seat container identity, restart, label, or network differs")
        if container_id == replacement_id:
            try:
                baseline_started = datetime.fromisoformat(
                    baseline["started_at"][:-1] + "+00:00"
                )
                current_started_raw = state.get("StartedAt")
                current_started = datetime.fromisoformat(
                    current_started_raw[:-1] + "+00:00"
                ) if isinstance(current_started_raw, str) and TIMESTAMP.fullmatch(current_started_raw) else None
            except ValueError as exc:
                raise SeatProbeError("fault replacement timestamp is invalid") from exc
            if baseline["slot_no"] != replacement["slot_no"] or \
                    isinstance(state.get("Pid"), bool) or not isinstance(state.get("Pid"), int) or \
                    state.get("Pid") <= 0 or value.get("RestartCount") != 0 or \
                    current_started is None or current_started <= baseline_started:
                raise SeatProbeError("failed seat was not rebuilt as one fresh container")
        elif container_id == planned_id:
            try:
                baseline_started = datetime.fromisoformat(
                    baseline["started_at"][:-1] + "+00:00"
                )
                current_started_raw = state.get("StartedAt")
                current_started = datetime.fromisoformat(
                    current_started_raw[:-1] + "+00:00"
                ) if isinstance(current_started_raw, str) and TIMESTAMP.fullmatch(current_started_raw) else None
            except ValueError as exc:
                raise SeatProbeError("planned restart timestamp is invalid") from exc
            if isinstance(state.get("Pid"), bool) or not isinstance(state.get("Pid"), int) or \
                    state.get("Pid") <= 0 or state.get("Pid") == baseline["pid"] or \
                    value.get("RestartCount") != baseline["restart_count"] + 1 or \
                    current_started is None or current_started <= baseline_started:
                raise SeatProbeError("planned seat restart did not recover exactly once")
        elif state.get("Pid") != baseline["pid"] or \
                value.get("RestartCount") != baseline["restart_count"] or \
                state.get("StartedAt") != baseline["started_at"]:
            raise SeatProbeError("unexpected seat restart or lifecycle drift")
        network = networks[row["network_name"]]
        if network.get("NetworkID") != row["network_id"]:
            raise SeatProbeError("seat network ID differs")
        ip, mac = network.get("IPAddress"), network.get("MacAddress")
        if not ip or not mac or ip in observed_ips or mac in observed_macs:
            raise SeatProbeError("seat network endpoint identity differs")
        observed_ips.add(ip); observed_macs.add(mac)
        probe_novnc(ip)
    promoted_id = replacement["replacement_spare_container_id"]
    formal_ids = [item for item in row["formal_container_ids"] if item != replaced_id]
    formal_ids.append(promoted_id)
    spare_ids = [item for item in row["spare_container_ids"] if item != promoted_id]
    spare_ids.append(replacement_id)
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "formal_container_ids": formal_ids,
        "spare_container_ids": spare_ids,
        "verified_container_ids": formal_ids + spare_ids,
        "unexpected_restart_events": 0,
        "planned_restart_events": 1,
        "planned_restart_recoveries": 1,
        "cross_seat_access_failures": 0,
    }


def main() -> int:
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise SeatProbeError("seat inventory probe requires Linux root")
        if EMBEDDED_CONFIG is None:
            raise SeatProbeError("seat inventory probe is not frozen")
        print(json.dumps(collect(EMBEDDED_CONFIG), sort_keys=True, separators=(",", ":")))
        return 0
    except SeatProbeError as exc:
        print(f"NO_GO: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
