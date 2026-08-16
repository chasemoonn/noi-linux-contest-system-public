#!/usr/bin/env python3
"""Capture a durable, mutation-free baseline before the first NOI install."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys

from build_v1_clean_install_backup_manifest import build as seal_manifest
from build_v1_controller_install_backup import collect as collect_controller
from build_v1_hydro_install_backup import ADDON_ROOT, STATE_ROOT, collect as collect_hydro
from build_v1_install_backup import atomic, copy_file, private_output, safe_ancestors
from build_v1_ordinary_oj_install_backup import build as build_ordinary
from commit_v1_caddy_config import Admin, adapt, canonical, get_live
from verify_v1_cloud_install_backup import validate as validate_cloud
from verify_v1_install_backup import safe_file


class CollectCleanError(RuntimeError):
    pass


def require_absent(path: Path, label: str) -> None:
    requested = Path(os.path.abspath(path))
    # The target itself may not exist; validate its nearest existing ancestor.
    current = requested.parent
    while not os.path.lexists(current):
        if current.parent == current: break
        current = current.parent
    safe_ancestors(current)
    if os.path.lexists(requested):
        raise CollectCleanError(f"clean target already contains {label}")


def capture_caddy(output: Path, caddyfile: Path, snippet: Path,
                  frontend_domain: str, orchestrator_upstream: str) -> None:
    require_absent(snippet, "the NOI Caddy snippet")
    safe_ancestors(Path(os.path.abspath(caddyfile)).parent)
    disk, metadata = safe_file(caddyfile, maximum=32 * 1024 * 1024)
    try: text = disk.decode("utf-8")
    except UnicodeDecodeError as exc: raise CollectCleanError("Caddyfile is not UTF-8") from exc
    forbidden = (str(snippet), frontend_domain, orchestrator_upstream,
                 "/orchestrator/submit", "NOI_V1_HYDRO_ROUTE_HARDENING")
    if any(value in text for value in forbidden):
        raise CollectCleanError("Caddyfile already contains NOI integration")
    admin = Admin(); live, _ = get_live(admin)
    if canonical(adapt(admin, disk)) != canonical(live):
        raise CollectCleanError("Caddy disk and active config differ before clean backup")
    atomic(output / "Caddyfile", disk, stat.S_IMODE(metadata.st_mode))
    atomic(output / "caddy-active.json", canonical(live) + b"\n")


def clean_paths(args) -> dict[str, Path]:
    return {
        "install_root": args.install_root,
        "source_pointer": args.source_pointer,
        "project_config": args.project_config,
        "project_env": args.project_env,
        "database": args.database,
        "database_wal": Path(str(args.database) + "-wal"),
        "database_shm": Path(str(args.database) + "-shm"),
        "caddy_snippet": args.snippet,
        "hydro_addon_tree": ADDON_ROOT,
        "hydro_plugin_env": Path("/root/.hydro/orchestrator-plugin.env"),
        "hydro_plugin_token": Path("/root/.hydro/orchestrator-token"),
        "hydro_plugin_state": STATE_ROOT,
    }


def require_layout(args) -> None:
    root = Path(os.path.abspath(args.install_root))
    expected = {
        "source_pointer": root / "current-source",
        "project_config": root / "orchestrator" / "config.yaml",
        "project_env": root / "orchestrator" / ".env",
        "database": root / "orchestrator" / "data" / "orchestrator.db",
        "snippet": root / "orchestrator" / "runtime" / "caddy-exam.conf",
    }
    if any(Path(os.path.abspath(getattr(args, key))) != value for key, value in expected.items()):
        raise CollectCleanError("clean install target layout differs")


def collect(args) -> dict:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", args.frontend_domain) \
            or "." not in args.frontend_domain or ".." in args.frontend_domain:
        raise CollectCleanError("frontend domain differs")
    if args.orchestrator_upstream != "http://127.0.0.1:8600":
        raise CollectCleanError("orchestrator upstream differs")
    require_layout(args); paths = clean_paths(args)
    for key, path in paths.items(): require_absent(path, key.replace("_", " "))
    root = private_output(args.output_directory)
    capture_caddy(root, args.caddyfile, args.snippet,
                  args.frontend_domain, args.orchestrator_upstream)
    copy_file(Path("/root/.hydro/addon.json"), root / "hydro-addon.json")
    copy_file(Path("/root/.pm2/dump.pm2"), root / "pm2-dump.json")
    copy_file(Path("/root/.pm2/dump.pm2.bak"), root / "pm2-dump.backup.json", optional=True)
    collect_hydro(root, args.pm2_bin)
    controller = collect_controller(root, args.docker_socket, "noi-orchestrator")
    if controller["controller_present"]:
        raise CollectCleanError("clean target contains an NOI controller")
    build_ordinary(root, args.oj_origin, args.pm2_bin)
    safe_ancestors(Path(os.path.abspath(args.cloud_snapshot)).parent)
    cloud_raw, _ = safe_file(args.cloud_snapshot, maximum=4 * 1024 * 1024)
    try: validate_cloud(json.loads(cloud_raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CollectCleanError("clean cloud snapshot differs") from exc
    atomic(root / "cloud-before.json", cloud_raw)
    target = {"schema_version": 1, "operation": "clean-install",
              "paths": {key: {"path": str(Path(os.path.abspath(path))), "present": False}
                        for key, path in sorted(paths.items())}}
    atomic(root / "clean-target.json",
           (json.dumps(target, sort_keys=True, separators=(",", ":")) + "\n").encode())
    manifest = seal_manifest(root, args.plan_id, args.source_revision,
                             args.candidate_manifest_sha256)
    raw, _ = safe_file(root / "backup-manifest.json", maximum=4 * 1024 * 1024)
    return {"status": "sealed", "operation": "clean-install", "plan_id": args.plan_id,
            "backup_manifest_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "artifacts": len(manifest["artifacts"]), "service_mutations": 0}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--plan-id", required=True); parser.add_argument("--source-revision", required=True)
    parser.add_argument("--candidate-manifest-sha256", required=True)
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--source-pointer", required=True, type=Path)
    parser.add_argument("--project-config", required=True, type=Path)
    parser.add_argument("--project-env", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--caddyfile", required=True, type=Path)
    parser.add_argument("--snippet", required=True, type=Path)
    parser.add_argument("--frontend-domain", required=True)
    parser.add_argument("--orchestrator-upstream", default="http://127.0.0.1:8600")
    parser.add_argument("--oj-origin", required=True); parser.add_argument("--pm2-bin", required=True, type=Path)
    parser.add_argument("--cloud-snapshot", required=True, type=Path)
    parser.add_argument("--docker-socket", default=Path("/var/run/docker.sock"), type=Path)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise CollectCleanError("clean install backup collection requires Linux root")
        print(json.dumps(collect(args), sort_keys=True)); return 0
    except (CollectCleanError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
