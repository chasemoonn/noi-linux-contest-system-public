#!/usr/bin/env python3
"""Bounded real-systemd tests ONLY on an ephemeral GitHub-hosted Jammy runner.

No production connection, package install/upgrade, reboot, timer policy edit,
global WSL action, port or credential. All locks and service jobs are fixtures.
Real dpkg --audit / apt-config are read-only; python-apt competes on temp locks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

SOURCE_ROOT = Path(__file__).resolve().parents[1]
GUARD = SOURCE_ROOT / "deploy/maintenance/maintenance_guard.py"
UNIT = GUARD.with_name("noi-maintenance-guard.service")
FIXTURE = Path(__file__).with_name("maintenance_systemd_ci_fixture.py")
UPDATE_UNITS = ("apt-daily.service", "apt-daily-upgrade.service",
                "apt-daily.timer", "apt-daily-upgrade.timer")
SCENARIOS = ("sigterm", "systemctl-stop", "stop-transaction", "sigkill", "watchdog-sigstop", "expiry")


def run(*args, timeout=15, check=True):
    value = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})
    if check and value.returncode:
        raise RuntimeError(f"{args[0]} failed ({value.returncode}): {value.stderr[:2000]}")
    return value


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def state(unit):
    return dict(line.split("=", 1) for line in run(
        "/usr/bin/systemctl", "show", unit,
        "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID,ControlPID,Result,NRestarts,Type,WatchdogUSec,RuntimeMaxUSec,Restart"
    ).stdout.splitlines() if "=" in line)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def policy_snapshot():
    digest = hashlib.sha256()
    for path in sorted(Path("/etc/apt").rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to("/etc/apt")).encode())
            digest.update(path.read_bytes())
    return {"apt_configuration_sha256": digest.hexdigest(),
            "update_unit_policy": {unit: {key: state(unit).get(key) for key in ("LoadState", "UnitFileState")}
                                   for unit in UPDATE_UNITS}}


def admission():
    require(sys.platform == "linux" and os.geteuid() == 0, "Linux root required")
    require(os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("ImageOS") == "ubuntu22", "only ephemeral GitHub-hosted ubuntu22 is allowed")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    require(release.get("ID") == "ubuntu" and release.get("VERSION_ID", "").strip('"') == "22.04", "Jammy required")
    require(Path("/proc/1/comm").read_text().strip() == "systemd", "real systemd PID 1 required")
    run("/usr/bin/python3", "-c", "import apt_pkg")
    return {"os": "ubuntu-22.04", "pid1": "systemd",
            "systemd": run("/usr/bin/systemctl", "--version").stdout.splitlines()[0],
            "apt": run("/usr/bin/apt-get", "--version").stdout.splitlines()[0],
            "dpkg": run("/usr/bin/dpkg", "--version").stdout.splitlines()[0]}


def apt_lock_check(paths, expected_busy):
    # Use the installed APT library, not another implementation of lockf.
    result = run("/usr/bin/python3", "-c", """
import apt_pkg,json,os,sys
apt_pkg.init()
busy=[]
for path in json.loads(sys.argv[1]):
    try:
        fd=apt_pkg.get_lock(path)
    except SystemError:
        busy.append(True)
    else:
        busy.append(fd < 0)
        if fd >= 0: os.close(fd)
print(json.dumps(busy))
""", json.dumps(paths))
    value = json.loads(result.stdout)
    require(value == [expected_busy] * 4, f"real APT lock exclusion mismatch: {value}")
    return value


def query(runtime):
    nonce = secrets.token_hex(16)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(3)
        channel.connect(str(runtime / "guard.sock"))
        channel.sendall(json.dumps({"command": "status", "nonce": nonce}).encode())
        data = bytearray()
        while True:
            part = channel.recv(4096)
            if not part:
                break
            data.extend(part)
            require(len(data) <= 8192, "oversized guard response")
    result = json.loads(data)
    require(result["nonce"] == nonce and result["state"] == "protected", "live proof mismatch")
    return result


def wait_ended(unit, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = state(unit)
        if value["ActiveState"] in ("inactive", "failed") and value["ControlPID"] == "0":
            return value
        time.sleep(0.25)
    raise RuntimeError("fixture service did not end within bound")


def scenario(root, mode, evidence_dir, record):
    base = f"noi-guard-ci-{secrets.token_hex(6)}"
    folder = root / mode
    folder.mkdir(mode=0o700)
    runtime = Path("/run") / base
    units = {name: base + "-" + name for name in (*UPDATE_UNITS, "nginx.service", "docker.service", "noi-maintenance-guard.service")}
    guard_unit, gateway = units["noi-maintenance-guard.service"], units["nginx.service"]
    unit_paths = []
    locks = [folder / f"lock-{index}" for index in range(4)]
    for path in locks:
        path.write_bytes(b"unchanged CI lock inode\n")
    inodes = [path.stat().st_ino for path in locks]
    paths = [str(path) for path in locks]
    config_path = folder / "fixture.json"
    closure = folder / "gateway-closed.json"
    config = {"guard_source": str(root / "maintenance_guard.py"), "runtime": str(runtime),
              "lease": str(folder / "lease.json"), "closure": str(closure), "locks": paths, "units": units}
    config_path.write_text(json.dumps(config))
    fixture = root / "maintenance_systemd_ci_fixture.py"
    fixture_command = f"/usr/bin/python3 {fixture} {config_path}"
    template = UNIT.read_text()
    for original, mapped in units.items():
        template = template.replace(original, mapped)
    template = template.replace("RuntimeDirectory=noi-maintenance", f"RuntimeDirectory={base}")
    template = template.replace("/usr/bin/python3 /opt/noi-maintenance/maintenance_guard.py", fixture_command)
    record["unit_adapter_sha256"] = hashlib.sha256(template.encode()).hexdigest()
    record["fixture_unit"] = guard_unit
    try:
        for original, mapped in units.items():
            path = Path("/run/systemd/system") / mapped
            require(not path.exists() and not path.is_symlink(), "fixture unit already exists")
            if original == "noi-maintenance-guard.service":
                content = template
            elif original.endswith(".timer"):
                content = f"[Timer]\nOnActiveSec=1h\nUnit={units[original.replace('.timer', '.service')]}\n"
            else:
                content = "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/true\n"
                if original == "nginx.service":
                    content += f"ExecStop={fixture_command} gateway-stopped\n"
            with path.open("x") as handle:
                handle.write(content)
            unit_paths.append(path)
        run("/usr/bin/systemctl", "daemon-reload")
        run("/usr/bin/systemctl", "start", gateway, units["docker.service"])
        now = int(time.time() * 1000)
        lease = {"schema_version": 1, "dedicated_contest_host": True,
                 "machine_id": Path("/etc/machine-id").read_text().strip(),
                 "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                 "tid": secrets.token_hex(12), "approved_by": "disposable-ci-fixture",
                 "window_start_ms": now - 1000, "window_end_ms": now + (190_000 if mode == "expiry" else 600_000),
                 "maintenance_reviewed_at_ms": now, "maintenance_review_complete": True}
        # Synthetic approval is exclusively for temporary fixture locks/services.
        (folder / "lease.json").write_text(json.dumps(lease))
        os.chmod(folder / "lease.json", 0o600)
        started = time.monotonic()
        run("/usr/bin/systemctl", "start", guard_unit, timeout=160)
        ready = state(guard_unit)
        record["ready_state"] = ready
        require(ready["ActiveState"] == "active", "real READY not accepted")
        proof = query(runtime)
        require(proof["pid"] == int(ready["MainPID"]) and proof["tid"] == lease["tid"], "wrong live daemon")
        record["apt_busy_while_protected"] = apt_lock_check(paths, True)
        acted = time.monotonic()
        if mode == "systemctl-stop":
            run("/usr/bin/systemctl", "stop", guard_unit, timeout=45)
        elif mode == "stop-transaction":
            run("/usr/bin/systemctl", "stop", guard_unit, gateway, timeout=45)
        elif mode != "expiry":
            os.kill(int(ready["MainPID"]), {"sigterm": signal.SIGTERM, "sigkill": signal.SIGKILL,
                                          "watchdog-sigstop": signal.SIGSTOP}[mode])
        ended = wait_ended(guard_unit, 165 if mode == "expiry" else 45)
        record["end_state"] = ended
        record["end_after_action_seconds"] = round(time.monotonic() - acted, 3)
        record["total_seconds"] = round(time.monotonic() - started, 3)
        require(ended["NRestarts"] == "0", "unexpected auto restart")
        require(state(gateway)["ActiveState"] == "inactive", "gateway fixture remains open")
        require(state(units["docker.service"])["ActiveState"] == "active", "Docker fixture changed")
        require(closure.exists(), "gateway closure evidence missing")
        record["gateway_closure"] = json.loads(closure.read_text())
        if mode in ("sigterm", "systemctl-stop", "stop-transaction", "expiry"):
            require(record["gateway_closure"]["locks_busy"] == [True] * 4, "normal closure happened after unlock")
        if mode in ("sigterm", "systemctl-stop", "stop-transaction"):
            require(ended["Result"] == "success", "normal stop failed")
            require(record["end_after_action_seconds"] < 10, "dummy gateway stop stalled")
        if mode == "watchdog-sigstop":
            require(ended["Result"] == "watchdog", "real watchdog did not cause termination")
        if mode == "sigkill":
            require(ended["Result"] == "signal", "SIGKILL not recorded as failure")
        if mode == "expiry":
            require(record["total_seconds"] >= 140, "expiry skipped the actual wall-time wait")
        record["apt_busy_after_release"] = apt_lock_check(paths, False)
        require([path.stat().st_ino for path in locks] == inodes, "lock inodes replaced")
        require(all(path.read_bytes() == b"unchanged CI lock inode\n" for path in locks), "lock content changed")
        require(not runtime.exists(), "systemd RuntimeDirectory cleanup missing")
        record["passed"] = True
    finally:
        # Evidence is taken before cleanup; a failure is never reset into pass.
        journal = run("/usr/bin/journalctl", "-u", guard_unit, "--no-pager", "-o", "short-monotonic", check=False)
        (evidence_dir / f"{mode}.journal.txt").write_text(journal.stdout)
        record["journal_sha256"] = sha(evidence_dir / f"{mode}.journal.txt")
        # All targets were exclusively created by this case; never kill updates.
        if unit_paths:
            run("/usr/bin/systemctl", "kill", "--signal=SIGCONT", guard_unit, check=False)
            run("/usr/bin/systemctl", "stop", *units.values(), timeout=90, check=False)
            require(all(state(name)["ActiveState"] in ("inactive", "failed") for name in units.values()), "CI fixture cleanup incomplete")
            for path in unit_paths:
                path.unlink()
            run("/usr/bin/systemctl", "daemon-reload")
            run("/usr/bin/systemctl", "reset-failed", *units.values(), check=False)
        record["fixture_units_removed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-github-runner", action="store_true", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    environment = admission()  # No state changes before the dedicated CI gate.
    args.output_directory.mkdir(parents=True, exist_ok=False)
    before = policy_snapshot()
    evidence = {"schema_version": 1, "passed": False, "environment": environment,
                "source_revision": os.environ["GITHUB_SHA"],
                "sources": {str(p.relative_to(SOURCE_ROOT)): sha(p) for p in (GUARD, UNIT, FIXTURE, Path(__file__))},
                "policy_before": before, "scenarios": [],
                "limitations": ["gateway/docker/update units are fixtures", "all package locks are temporary files",
                                "no real nginx upgrade, boot catchup, host reboot, or capacity test",
                                "no production qualification or deployment"]}
    failure = None
    # Do not recursively remove a fixture tree if systemd cleanup failed.
    # The ephemeral runner owns its disposal; preserve this run for diagnosis.
    temporary = tempfile.mkdtemp(prefix="noi-systemd-ci-", dir="/root")
    if temporary:
        root = Path(temporary)
        for source in (GUARD, FIXTURE):
            shutil.copyfile(source, root / source.name)
        try:
            for mode in SCENARIOS:
                record = {"name": mode, "passed": False}
                evidence["scenarios"].append(record)
                print("START " + mode, flush=True)
                scenario(root, mode, args.output_directory, record)
                print("PASS " + mode, flush=True)
            evidence["passed"] = True
        except Exception as error:
            failure = error
            evidence["failure"] = {"type": type(error).__name__, "message": str(error)[:4000]}
        finally:
            evidence["policy_after"] = policy_snapshot()
            evidence["policy_unchanged"] = before == evidence["policy_after"]
            evidence["passed"] = evidence["passed"] and evidence["policy_unchanged"]
            (args.output_directory / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    if failure:
        raise failure
    require(evidence["passed"], "policy snapshot changed")


if __name__ == "__main__":
    main()
