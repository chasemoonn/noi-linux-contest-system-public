#!/usr/bin/env python3
"""NOI Linux V1 diagnostic, planning, and transaction entry point.

This module intentionally imports no cloud, HTTP, SSH, database, Docker, or
service-management client.  Production install planning may invoke the frozen
read-only candidate verifier, which in turn uses ssh-keygen for signatures.
The install apply command delegates only to the root-owned, SHA-pinned V1
transaction executor; no service mutation is implemented directly here.
"""
from __future__ import annotations

import sys

# Read-only commands must not create __pycache__ beside the repository files.
sys.dont_write_bytecode = True

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_ROOT = REPO_ROOT / "orchestrator"
if str(ORCHESTRATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATOR_ROOT))

import yaml

from services.config import validate_config


SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 4 * 1024 * 1024
SUPPORTED_RUNTIME_SYSTEMS = {"Linux", "Windows"}
SUPPORTED_PROFILE = "aliyun-hydro5-pm2-direct-v1"
REDACTIONS = [
    "secrets",
    "access_keys",
    "cookies",
    "private_keys",
    "student_identity",
    "desktop_entries",
    "network_topology",
    "filesystem_paths",
]
_ENV = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")
_DYNAMIC_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_ENV_NAMES = {
    "ALIYUN_REGION_ID",
    "CLOUD_PROVIDER",
    "FRONTEND_PROXY_PROVIDER",
    "TENCENT_REGION",
}

_CONTAINER_KEYS = {
    "ai",
    "aliyun",
    "artifact_generation",
    "cloud",
    "contest_server",
    "defaults",
    "desktop_access",
    "frontend_proxy",
    "hydro",
    "oracles",
    "orchestrator",
    "tencent",
    "tools",
    "validators",
}
_SAFE_LEAF_KEYS = {
    "admin_username",
    "allow_insecure_loopback",
    "approved_roots",
    "artifact_root",
    "auto_shutdown_after_collect",
    "caddyfile_path",
    "collected_dir",
    "cpus",
    "db",
    "default_max_participants",
    "default_spare_seats",
    "deployment_lock",
    "description_prefix",
    "docker_image",
    "docker_network",
    "domain_id",
    "enabled",
    "executable",
    "frame_rate",
    "gateway_listen",
    "gateway_bind_address",
    "gateway_scheme",
    "host_key_sha256",
    "max_input_bytes",
    "max_output_bytes",
    "max_response_bytes",
    "materials_dir",
    "memory",
    "memory_limit_bytes",
    "memory_swap",
    "model",
    "no_vnc_compression",
    "no_vnc_quality",
    "notify_enabled",
    "open_files",
    "paper_max_bytes",
    "pids_limit",
    "port",
    "practice_groups_per_problem",
    "prepare_before_minutes",
    "prepare_late_grace_minutes",
    "priority",
    "processes",
    "provider",
    "realtime_judge_idle_seconds",
    "realtime_judge_lease_seconds",
    "reconcile_seconds",
    "region",
    "region_id",
    "release_lead_minutes",
    "resolution",
    "seat_pool_maximum",
    "seat_pool_total_maximum",
    "seats_root",
    "shm_size",
    "shutdown_on_collect_error",
    "shutdown_grace_minutes",
    "shutdown_on_prepare_error",
    "snippet_path",
    "strict_host_key",
    "submit_enabled",
    "submit_lang",
    "submit_proxy_port",
    "testdata_expanded_max_bytes",
    "testdata_max_bytes",
    "testdata_max_files",
    "timeout_seconds",
    "web_submit_max_bytes",
}
_PATH_KEYS = {
    "approved_roots",
    "artifact_root",
    "caddyfile_path",
    "collected_dir",
    "db",
    "deployment_lock",
    "executable",
    "materials_dir",
    "seats_root",
    "snippet_path",
}
_TOPOLOGY_KEYS = {
    "description_prefix",
    "docker_image",
    "docker_network",
    "gateway_listen",
    "gateway_bind_address",
    "host_key_sha256",
    "port",
    "region",
    "region_id",
}
_SECRET_KEYS = {
    "access_key_id",
    "access_key_secret",
    "admin_password",
    "api_key",
    "api_key_env",
    "orchestrator_token",
    "secret_id",
    "secret_key",
    "ssh_key",
}
_ENTRY_KEYS = {
    "admin_url",
    "domain",
    "endpoint",
    "gateway_public_base_url",
    "instance_id",
    "internal_base_url",
    "known_hosts",
    "management_source_cidrs",
    "mongo_uri",
    "notify_allowed_https_hosts",
    "orchestrator_upstream",
    "public_base_url",
    "security_group_id",
    "source_cidr",
    "student_notice_url",
}
_IDENTITY_KEYS = {"admin_username", "ssh_user"}
_KNOWN_KEYS = (
    _CONTAINER_KEYS
    | _SAFE_LEAF_KEYS
    | _SECRET_KEYS
    | _ENTRY_KEYS
    | _IDENTITY_KEYS
    | _PATH_KEYS
    | _TOPOLOGY_KEYS
)


class ConfigReadError(Exception):
    """A configuration could not be read or validated safely."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class CliArgumentError(Exception):
    """An argparse failure whose original text must not be echoed."""


class NoictlArgumentParser(argparse.ArgumentParser):
    """Keep argument errors inside the stable/redacted output contract."""

    def error(self, message: str) -> None:
        del message
        raise CliArgumentError


class UnsafeOutputError(Exception):
    """A payload still appears to contain a value that must be redacted."""


class InstallPlanError(Exception):
    """A candidate cannot safely produce an installation plan."""


class InstallQualificationError(InstallPlanError):
    """A candidate failed complete production-qualification reverification."""


@dataclass
class ConfigState:
    raw: dict[str, Any]
    effective: dict[str, Any]
    sources: dict[tuple[Any, ...], str]
    raw_bytes: bytes
    secret_candidates: set[str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _runtime_system() -> str:
    if sys.platform.startswith("linux"):
        return "Linux"
    if sys.platform in {"win32", "cygwin"}:
        return "Windows"
    return sys.platform or "unknown"


def _python_version() -> str:
    return ".".join(str(value) for value in sys.version_info[:3])


def _configure_output_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass


def _print_text(value: str) -> None:
    try:
        print(value)
    except UnicodeEncodeError:
        print(value.encode("ascii", "backslashreplace").decode("ascii"))


def _base_result(command: str, status: str, summary: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": status,
        "changed": False,
        "plan_id": None,
        "summary": summary,
        "checks": [],
        "actions": [],
        "warnings": [],
        "redactions": list(REDACTIONS),
    }


def _check(
    code: str,
    status_value: str,
    scope: str,
    message: str,
    evidence: dict[str, Any] | None = None,
    remediation: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "status": status_value,
        "scope": scope,
        "message": message,
        "evidence": evidence or {},
        "remediation": remediation,
    }


def _default_config_path() -> Path:
    configured = os.environ.get("ORCHESTRATOR_CONFIG")
    if configured:
        return Path(configured).expanduser()
    cwd_config = Path.cwd() / "config.yaml"
    try:
        # lstat never follows a final symlink/reparse point.  If any directory
        # entry exists, let the single-descriptor loader accept or reject it;
        # do not probe its target merely while choosing a default.
        os.lstat(cwd_config)
    except FileNotFoundError:
        return ORCHESTRATOR_ROOT / "config.yaml"
    except OSError:
        # Preserve the local precedence and let the loader return a generic,
        # redacted read error rather than silently selecting another config.
        return cwd_config
    return cwd_config


def _is_obvious_network_path(path: Path) -> bool:
    text = str(path)
    return text.startswith("\\\\") or text.startswith("//")


def _safe_support_filename(value: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,122}\.json", value):
        return False
    windows_stem = value.split(".", 1)[0].upper()
    return windows_stem not in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }


def _path_text(path: tuple[Any, ...]) -> str:
    output = ""
    for part in path:
        if isinstance(part, int):
            output += f"[{part}]"
        else:
            output += ("." if output else "") + str(part)
    return output


def _sensitive_env_name(name: str) -> bool:
    lowered = name.lower()
    return any(
        marker in lowered
        for marker in (
            "access_key",
            "cookie",
            "credential",
            "password",
            "private_key",
            "secret",
            "session",
            "token",
        )
    )


def _flatten_strings(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _flatten_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _flatten_strings(item)
    elif value is not None:
        yield str(value)


def _expand(
    value: Any,
    path: tuple[Any, ...],
    sources: dict[tuple[Any, ...], str],
    secret_candidates: set[str],
) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfigReadError("non_string_key")
            output[key] = _expand(
                item, path + (key,), sources, secret_candidates
            )
        return output
    if isinstance(value, list):
        return [
            _expand(item, path + (index,), sources, secret_candidates)
            for index, item in enumerate(value)
        ]
    if not isinstance(value, str):
        sources[path] = "file"
        return value

    source_parts: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            replacement = os.environ[name]
            source_parts.append(f"environment:{name}")
            if _sensitive_env_name(name):
                secret_candidates.add(replacement)
            return replacement
        if default is not None:
            source_parts.append(f"default:{name}")
            return default
        raise ConfigReadError("missing_environment")

    try:
        expanded = _ENV.sub(replace, value)
    except ConfigReadError:
        raise
    sources[path] = ",".join(dict.fromkeys(source_parts)) if source_parts else "file"
    return expanded


def _load_config_state(path: Path) -> ConfigState:
    if _is_obvious_network_path(path):
        raise ConfigReadError("network_path_refused")
    expected_windows_identity: tuple[int, int] | None = None
    if os.name == "nt":
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError as exc:
            raise ConfigReadError("missing_file") from exc
        except OSError as exc:
            raise ConfigReadError("unreadable_file") from exc
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(path_stat.st_mode) or (
            getattr(path_stat, "st_file_attributes", 0) & reparse_flag
        ):
            raise ConfigReadError("symlink_refused")
        if path_stat.st_ino <= 0:
            raise ConfigReadError("file_identity_unavailable")
        expected_windows_identity = (path_stat.st_dev, path_stat.st_ino)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ConfigReadError("missing_file") from exc
    except OSError as exc:
        raise ConfigReadError("unreadable_file") from exc
    try:
        file_stat = os.fstat(descriptor)
        if expected_windows_identity is not None and (
            file_stat.st_ino <= 0
            or (file_stat.st_dev, file_stat.st_ino)
            != expected_windows_identity
        ):
            raise ConfigReadError("file_changed_during_open")
        if not stat.S_ISREG(file_stat.st_mode):
            raise ConfigReadError("not_regular_file")
        if file_stat.st_size > MAX_CONFIG_BYTES:
            raise ConfigReadError("file_too_large")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw_bytes = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw_bytes) > MAX_CONFIG_BYTES:
            raise ConfigReadError("file_too_large")
        text = raw_bytes.decode("utf-8")
        raw = yaml.safe_load(text)
    except UnicodeDecodeError as exc:
        raise ConfigReadError("invalid_utf8") from exc
    except yaml.YAMLError as exc:
        raise ConfigReadError("invalid_yaml") from exc
    except OSError as exc:
        raise ConfigReadError("unreadable_file") from exc
    except ConfigReadError:
        raise
    except MemoryError:
        raise
    except Exception as exc:
        # PyYAML constructors may raise data-dependent ValueError or
        # OverflowError (for example an impossible implicit timestamp).
        raise ConfigReadError("invalid_yaml") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(raw, dict):
        raise ConfigReadError("invalid_root")

    sources: dict[tuple[Any, ...], str] = {}
    secret_candidates: set[str] = set()
    try:
        effective = _expand(raw, (), sources, secret_candidates)
        validate_config(effective)
    except ConfigReadError:
        raise
    except MemoryError:
        raise
    except Exception as exc:
        # Malformed YAML shapes can make the shared validator raise a
        # data-dependent AttributeError, KeyError or OverflowError.  Those are
        # schema failures, not runtime failures.  BaseException is deliberately
        # excluded so interrupts and process exits still propagate.
        raise ConfigReadError("validation_failed") from exc
    return ConfigState(raw, effective, sources, raw_bytes, secret_candidates)


def _classify_key(key: str) -> str:
    if key in _SECRET_KEYS:
        return "secret"
    if key in _ENTRY_KEYS:
        return "entry"
    if key in _IDENTITY_KEYS:
        return "identity"
    if key in _PATH_KEYS:
        return "path"
    if key in _TOPOLOGY_KEYS:
        return "topology"
    if key in _CONTAINER_KEYS:
        return "container"
    if key in _SAFE_LEAF_KEYS:
        return "safe"
    return "unknown"


def _source_is_safe(source: str) -> bool:
    if source == "file":
        return True
    for part in source.split(","):
        _, _, name = part.partition(":")
        if not name or _sensitive_env_name(name):
            return False
        if part.startswith("environment:") and name not in _SAFE_ENV_NAMES:
            return False
    return True


def _looks_sensitive_string(value: str) -> bool:
    lowered = value.lower()
    if "-----begin " in lowered or "://" in lowered:
        return True
    if len(value) >= 12 and re.search(
        r"(?i)(?:password|token|secret|access[_-]?key|cookie|session)", value
    ):
        return True
    if re.fullmatch(r"(?:AKIA|ASIA|LTAI)[A-Z0-9]{12,}", value):
        return True
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}",
            value,
        )
    )


def _redact_config(state: ConfigState) -> tuple[Any, dict[str, str], set[str]]:
    output_sources: dict[str, str] = {}
    secret_candidates = set(state.secret_candidates)

    def redact(
        value: Any,
        original_path: tuple[Any, ...],
        display_path: tuple[Any, ...],
    ) -> Any:
        nearest_key = next(
            (part for part in reversed(original_path) if isinstance(part, str)),
            "",
        )
        classification = _classify_key(nearest_key) if nearest_key else "container"
        display_name = _path_text(display_path)

        if classification in {"secret", "entry", "identity", "path", "topology"}:
            secret_candidates.update(_flatten_strings(value))
            output_sources[display_name] = "redacted"
            return f"<redacted:{classification}>"

        if isinstance(value, dict):
            result: dict[str, Any] = {}
            hidden_index = 0
            dynamic_parent = nearest_key in {"validators", "oracles"}
            for key, item in value.items():
                known = key in _KNOWN_KEYS or (
                    dynamic_parent and bool(_DYNAMIC_SLUG.fullmatch(key))
                )
                if not known:
                    hidden_index += 1
                    safe_key = f"unclassified_{hidden_index}"
                    secret_candidates.update(_flatten_strings(key))
                    secret_candidates.update(_flatten_strings(item))
                    result[safe_key] = "<redacted:unclassified>"
                    output_sources[
                        _path_text(display_path + (safe_key,))
                    ] = "redacted"
                    continue
                result[key] = redact(
                    item,
                    original_path + (key,),
                    display_path + (key,),
                )
            return result

        if isinstance(value, list):
            return [
                redact(
                    item,
                    original_path + (index,),
                    display_path + (index,),
                )
                for index, item in enumerate(value)
            ]

        source = state.sources.get(original_path, "file")
        if not isinstance(value, (str, int, float, bool, type(None))) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            secret_candidates.update(_flatten_strings(value))
            output_sources[display_name] = "redacted"
            return "<redacted:unclassified>"
        if classification != "safe" or not _source_is_safe(source):
            secret_candidates.update(_flatten_strings(value))
            output_sources[display_name] = "redacted"
            return "<redacted:unclassified>"
        if isinstance(value, str) and _looks_sensitive_string(value):
            secret_candidates.add(value)
            output_sources[display_name] = "redacted"
            return "<redacted:unclassified>"
        output_sources[display_name] = source
        return value

    redacted = redact(state.effective, (), ())
    return redacted, output_sources, secret_candidates


def _assert_no_secret_leak(payload: Any, candidates: Iterable[str]) -> None:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    lowered = serialized.lower()
    if "-----begin private key-----" in lowered:
        raise UnsafeOutputError
    output_values: list[str] = []

    def collect_values(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                collect_values(item)
        elif isinstance(value, list):
            for item in value:
                collect_values(item)
        elif value is not None:
            output_values.append(str(value))

    collect_values(payload)
    for candidate in candidates:
        candidate_text = str(candidate)
        if len(candidate_text) < 4:
            continue
        for output_value in output_values:
            if candidate_text == output_value or (
                len(candidate_text) >= 12 and candidate_text in output_value
            ):
                raise UnsafeOutputError


def _profile_static_evidence(config: dict[str, Any]) -> dict[str, Any]:
    provider = str((config.get("cloud") or {}).get("provider") or "").lower()
    server = config.get("contest_server") or {}
    admin = config.get("orchestrator") or {}
    image_match = str(server.get("docker_image") or "") == "noi-linux-official:2.0"
    participants_match = int(admin.get("default_max_participants", 15)) == 15
    spares_match = int(admin.get("default_spare_seats", 2)) == 2
    return {
        "profile": SUPPORTED_PROFILE,
        "provider_match": provider == "aliyun",
        "desktop_image_match": image_match,
        "participants_15_match": participants_match,
        "spare_seats_2_match": spares_match,
        "static_match": provider == "aliyun"
        and image_match
        and participants_match
        and spares_match,
    }


def _doctor(config_path: Path) -> tuple[dict[str, Any], int, ConfigState | None]:
    checks: list[dict[str, Any]] = []
    warnings = [
        "第一批 doctor 只做本地静态检查；未探测 Hydro、Docker、云 API、SSH、端口、进程或比赛状态。",
        "静态通过不等于生产 profile 验收或 15+2 容量签字。",
    ]
    system_name = _runtime_system()
    runtime_supported = system_name in SUPPORTED_RUNTIME_SYSTEMS
    checks.append(
        _check(
            "NOICTL_RUNTIME_PLATFORM",
            "pass" if runtime_supported else "fail",
            "local",
            "当前平台可运行第一批只读 CLI"
            if runtime_supported
            else "当前平台不在第一批 CLI 运行范围内",
            {"system": system_name, "python": _python_version()},
            None if runtime_supported else "请改用 Linux 或 Windows 运行只读 CLI",
        )
    )
    checks.append(
        _check(
            "READ_ONLY_BOUNDARY",
            "pass",
            "safety",
            "未启用网络、服务、进程或配置写操作",
            {
                "network_probes": 0,
                "service_commands": 0,
                "referenced_secret_files_opened": 0,
            },
        )
    )

    state: ConfigState | None = None
    config_error: ConfigReadError | None = None
    try:
        state = _load_config_state(config_path)
    except ConfigReadError as exc:
        config_error = exc

    if state is None:
        checks.append(
            _check(
                "CONFIG_VALID",
                "fail",
                "config",
                "配置未通过本地静态校验；具体值未输出",
                {"error_kind": config_error.kind if config_error else "unknown"},
                "请在本机修复配置或缺失的环境引用后重试",
            )
        )
    else:
        checks.append(
            _check(
                "CONFIG_VALID",
                "pass",
                "config",
                "配置通过现有 orchestrator 静态校验器",
                {"schema_validation": True},
            )
        )
        profile = _profile_static_evidence(state.effective)
        checks.append(
            _check(
                "SUPPORTED_PROFILE_STATIC",
                "pass" if profile["static_match"] else "warn",
                "profile",
                "配置声明匹配首个支持 profile 的核心静态项"
                if profile["static_match"]
                else "配置可被校验，但未声明首个支持 profile 的全部核心静态项",
                profile,
                None
                if profile["static_match"]
                else "如需维护者支持，请核对 SUPPORT_MATRIX.md；不要把实验配置当作已验证 profile",
            )
        )

    if config_error is not None:
        result = _base_result("doctor", "error", "本地静态诊断未通过")
        exit_code = 2
    elif not runtime_supported:
        result = _base_result("doctor", "error", "当前平台不属于第一批 CLI 运行范围")
        exit_code = 3
    elif any(item["status"] == "warn" for item in checks):
        result = _base_result(
            "doctor", "error", "配置有效，但不属于首个维护者支持 profile"
        )
        exit_code = 3
    else:
        result = _base_result("doctor", "ok", "第一批本地静态检查通过")
        exit_code = 0
    result["checks"] = checks
    result["warnings"] = warnings
    return result, exit_code, state


def _config_validate(config_path: Path) -> tuple[dict[str, Any], int]:
    try:
        _load_config_state(config_path)
    except ConfigReadError as exc:
        result = _base_result("config validate", "error", "配置未通过静态校验")
        result["checks"] = [
            _check(
                "CONFIG_VALID",
                "fail",
                "config",
                "配置无效；配置值和秘密引用未输出",
                {"error_kind": exc.kind},
                "请在本机修复配置后重试",
            )
        ]
        return result, 2
    result = _base_result("config validate", "ok", "配置通过静态校验")
    result["checks"] = [
        _check(
            "CONFIG_VALID",
            "pass",
            "config",
            "配置通过现有 orchestrator 静态校验器",
            {"schema_validation": True},
        )
    ]
    return result, 0


def _config_show(config_path: Path) -> tuple[dict[str, Any], int]:
    try:
        state = _load_config_state(config_path)
        effective, sources, secret_candidates = _redact_config(state)
        result = _base_result(
            "config show", "ok", "已生成脱敏的有效配置视图"
        )
        result["checks"] = [
            _check(
                "CONFIG_VALID",
                "pass",
                "config",
                "显示前已通过静态校验",
                {"schema_validation": True},
            ),
            _check(
                "CONFIG_REDACTED",
                "pass",
                "safety",
                "秘密、身份、入口、URL 和拓扑字段已脱敏",
                {"redaction_categories": len(REDACTIONS)},
            ),
        ]
        result["effective_config"] = effective
        result["sources"] = dict(sorted(sources.items()))
        result["warnings"] = [
            "第一批 effective 视图展开文件中的环境引用；校验器内部未物化的隐式默认值不会新增到输出。"
        ]
        _assert_no_secret_leak(result, secret_candidates)
        return result, 0
    except ConfigReadError as exc:
        result = _base_result("config show", "error", "无法生成有效配置视图")
        result["checks"] = [
            _check(
                "CONFIG_VALID",
                "fail",
                "config",
                "配置无效；未输出任何配置值",
                {"error_kind": exc.kind},
                "请先运行 config validate 并在本机修复配置",
            )
        ]
        return result, 2
    except UnsafeOutputError:
        result = _base_result(
            "config show", "refused", "检测到潜在敏感输出，已拒绝显示"
        )
        result["checks"] = [
            _check(
                "OUTPUT_REDACTION_SAFE",
                "fail",
                "safety",
                "脱敏后仍可能包含敏感值",
                {"output_emitted": False},
                "请向维护者报告脱敏规则缺口，不要复制当前配置",
            )
        ]
        return result, 9


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_candidate_file(path: Path, *, maximum: int) -> bytes:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise InstallPlanError("candidate file is missing or unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallPlanError("candidate file is not a regular file")
    if metadata.st_size <= 0 or metadata.st_size > maximum:
        raise InstallPlanError("candidate file size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size != metadata.st_size:
            raise InstallPlanError("candidate file changed during open")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise InstallPlanError("candidate file identity changed during open")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(maximum + 1)
        if len(raw) != opened.st_size:
            raise InstallPlanError("candidate file changed during read")
        return raw
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _run_trusted_candidate_verifier(
    candidate: Path, archive_raw: bytes, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run only the verifier whose bytes came from the already verified archive."""
    with tempfile.TemporaryDirectory(prefix="noi-v1-candidate-verifier-") as raw:
        trusted_root = Path(raw)
        with tarfile.open(fileobj=io.BytesIO(archive_raw), mode="r:") as bundle:
            for member in bundle.getmembers():
                if not member.isfile():
                    continue
                handle = bundle.extractfile(member)
                if handle is None:
                    raise InstallQualificationError(
                        "trusted production verifier archive is unreadable"
                    )
                target = trusted_root / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    output.write(handle.read())
                target.chmod(member.mode & 0o777)
        source_root = trusted_root / "noi-linux-contest-system-v1"
        verifier = source_root / "scripts" / "verify_v1_candidate.py"
        if not verifier.is_file() or verifier.is_symlink():
            raise InstallQualificationError(
                "trusted production candidate verifier is missing"
            )
        return subprocess.run(
            [sys.executable, str(verifier), str(candidate.resolve()),
             "--require-production-qualified"],
            cwd=source_root, env=environment, capture_output=True, text=True,
            timeout=120, check=False,
        )


def _verify_install_candidate(
    candidate_argument: str, *, require_production_qualified: bool = False
) -> dict[str, Any]:
    candidate = Path(candidate_argument).expanduser()
    try:
        metadata = os.lstat(candidate)
    except OSError as exc:
        raise InstallPlanError("candidate directory is missing or unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise InstallPlanError("candidate must be a real directory")
    manifest_raw = _safe_candidate_file(
        candidate / "candidate-manifest.json", maximum=32 * 1024 * 1024
    )
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallPlanError("candidate manifest is not strict UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "$schema", "schema_version", "candidate", "source", "static_gates", "qualification"
    }:
        raise InstallPlanError("candidate manifest shape differs")
    if manifest["$schema"] != "v1-source-candidate.schema.json" or manifest["schema_version"] != 1:
        raise InstallPlanError("candidate manifest schema differs")
    identity = manifest.get("candidate")
    if not isinstance(identity, dict) or identity.get("profile") != SUPPORTED_PROFILE \
            or identity.get("product_contract") != "NOI Linux V1":
        raise InstallPlanError("candidate identity differs")
    if manifest.get("static_gates") != {
        "submission_fault_injection": "passed",
        "v1_product_contract": "passed",
        "public_release_boundary": "passed",
    }:
        raise InstallPlanError("candidate static gates are not passed")
    source_row = manifest.get("source")
    if not isinstance(source_row, dict) or set(source_row) != {
        "revision", "tree", "archive", "tracked_file_count", "files"
    }:
        raise InstallPlanError("candidate source shape differs")
    if not re.fullmatch(r"[a-f0-9]{40}", str(source_row["revision"])) or \
            not re.fullmatch(r"[a-f0-9]{40}", str(source_row["tree"])):
        raise InstallPlanError("candidate source identity is invalid")
    files = source_row.get("files")
    if not isinstance(files, list) or not files or source_row.get("tracked_file_count") != len(files):
        raise InstallPlanError("candidate file manifest is invalid")
    expected: dict[str, dict[str, Any]] = {}
    expected_directories = {"noi-linux-contest-system-v1"}
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "mode", "bytes", "sha256"}:
            raise InstallPlanError("candidate file row differs")
        path = row["path"]
        if not isinstance(path, str) or not path or "\\" in path or path.startswith("/") \
                or any(part in {"", ".", ".."} for part in path.split("/")):
            raise InstallPlanError("candidate contains an unsafe source path")
        if path in expected or row["mode"] not in {"100644", "100755"} \
                or not isinstance(row["bytes"], int) or row["bytes"] < 0 \
                or not re.fullmatch(r"[a-f0-9]{64}", str(row["sha256"])):
            raise InstallPlanError("candidate file metadata is invalid")
        expected[f"noi-linux-contest-system-v1/{path}"] = row
        parent = Path(f"noi-linux-contest-system-v1/{path}").parent
        while str(parent) not in {"", "."}:
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    archive_row = source_row.get("archive")
    if not isinstance(archive_row, dict) or set(archive_row) != {"name", "sha256", "bytes", "root"} \
            or archive_row.get("root") != "noi-linux-contest-system-v1/":
        raise InstallPlanError("candidate archive metadata differs")
    archive_name = archive_row.get("name")
    if not isinstance(archive_name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+\.tar", archive_name):
        raise InstallPlanError("candidate archive name is unsafe")
    archive_raw = _safe_candidate_file(candidate / archive_name, maximum=256 * 1024 * 1024)
    if len(archive_raw) != archive_row.get("bytes") or _sha256_bytes(archive_raw) != archive_row.get("sha256"):
        raise InstallPlanError("candidate archive digest differs")
    observed: set[str] = set()
    try:
        import io
        with tarfile.open(fileobj=io.BytesIO(archive_raw), mode="r:") as bundle:
            for member in bundle.getmembers():
                if member.isdir():
                    if member.name.rstrip("/") not in expected_directories:
                        raise InstallPlanError("candidate archive contains an unexpected directory")
                    continue
                if not member.isfile() or member.issym() or member.islnk() or member.name not in expected:
                    raise InstallPlanError("candidate archive contains an unexpected entry")
                if member.name in observed:
                    raise InstallPlanError("candidate archive contains a duplicate entry")
                handle = bundle.extractfile(member)
                content = handle.read() if handle is not None else b""
                row = expected[member.name]
                expected_mode = 0o755 if row["mode"] == "100755" else 0o644
                if len(content) != row["bytes"] or _sha256_bytes(content) != row["sha256"] \
                        or member.mode & 0o777 != expected_mode:
                    raise InstallPlanError("candidate archive member differs")
                observed.add(member.name)
    except (tarfile.TarError, OSError) as exc:
        raise InstallPlanError("candidate archive is invalid") from exc
    if observed != set(expected):
        raise InstallPlanError("candidate archive is incomplete")
    qualification = manifest.get("qualification")
    if not isinstance(qualification, dict) or not isinstance(
        qualification.get("production_qualified"), bool
    ):
        raise InstallPlanError("candidate qualification metadata differs")
    verified = {
        "revision": source_row["revision"], "tree": source_row["tree"],
        "archive_sha256": archive_row["sha256"],
        "manifest_sha256": _sha256_bytes(manifest_raw),
        "production_qualified": qualification["production_qualified"],
    }
    if require_production_qualified:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        }
        try:
            completed = _run_trusted_candidate_verifier(
                candidate, archive_raw, environment
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallQualificationError(
                "production candidate reverification could not complete"
            ) from exc
        if completed.returncode != 0:
            raise InstallQualificationError("production qualification reverification failed")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise InstallQualificationError(
                "production qualification verifier output is invalid"
            ) from exc
        expected_result = {
            "status": "qualified",
            "revision": verified["revision"],
            "archive_sha256": verified["archive_sha256"],
            "manifest_sha256": verified["manifest_sha256"],
        }
        if result != expected_result:
            raise InstallQualificationError(
                "production qualification verifier identity differs"
            )
        verified["production_qualified"] = True
    return verified


def _installation_config_binding(state: ConfigState) -> str:
    # A public plan_id must not become an offline oracle for a domain, IP,
    # instance ID, private path, user name, or secret.  The eventual apply
    # command must persist the exact private binding in a root-only plan file;
    # this public plan binds only the redacted contract shape.
    redacted, _, _ = _redact_config(state)
    raw = json.dumps(redacted, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _sha256_bytes(raw)


def _install_plan(
    config_path: Path, candidate: str, qualification_lab: bool,
    expected_manifest_sha256: str,
) -> tuple[dict[str, Any], int]:
    if not re.fullmatch(r"[a-f0-9]{64}", expected_manifest_sha256):
        result = _base_result("install --plan", "error", "外部候选摘要格式无效")
        result["checks"] = [_check("INSTALL_EXTERNAL_MANIFEST_PIN", "fail", "candidate",
            "必须从独立可信通道提供 64 位小写 manifest SHA256", {"external_pin_valid": False},
            "重新获取发布方公布的 candidate-manifest.json SHA256")]
        return result, 2
    if _runtime_system() != "Linux":
        result = _base_result("install --plan", "error", "安装计划只能在目标 Linux 主机生成")
        result["checks"] = [_check("INSTALL_TARGET_LINUX", "fail", "local",
            "当前不是目标 Linux 主机", {"target_linux": False}, "请在目标 Ubuntu 主机重新生成计划")]
        return result, 3
    doctor, doctor_code, state = _doctor(config_path)
    if doctor_code != 0 or state is None:
        result = _base_result("install --plan", "error", "静态配置门未通过，未生成安装计划")
        result["checks"] = doctor["checks"]
        return result, doctor_code
    try:
        candidate_row = _verify_install_candidate(
            candidate, require_production_qualified=not qualification_lab
        )
    except InstallQualificationError:
        result = _base_result("install --plan", "refused", "候选未通过完整生产资格重验")
        result["checks"] = [_check("INSTALL_PRODUCTION_QUALIFIED", "fail", "qualification",
            "签名资格报告、证据文件或候选身份未通过独立重验", {"production_qualified": False},
            "在独立资格机补齐全部签名验收证据后重新构建候选")]
        return result, 3
    except (InstallPlanError, OSError, MemoryError):
        result = _base_result("install --plan", "error", "候选未通过本机完整性核验")
        result["checks"] = [_check("INSTALL_CANDIDATE_VERIFIED", "fail", "candidate",
            "候选 manifest、源码归档或逐文件摘要不一致", {"candidate_verified": False},
            "请从可信交付通道重新取得候选并核对外部 manifest SHA256")]
        return result, 2
    if candidate_row["manifest_sha256"] != expected_manifest_sha256:
        result = _base_result("install --plan", "refused", "候选与外部可信摘要不一致")
        result["checks"] = [_check("INSTALL_EXTERNAL_MANIFEST_PIN", "fail", "candidate",
            "本机候选 manifest 未命中独立可信通道摘要", {"external_pin_matched": False},
            "停止安装并重新取得候选；不要采用候选目录内自带的摘要")]
        return result, 3
    binding = _installation_config_binding(state)
    plan_identity = {
        "schema_version": 1, "operation": "install", "scope": "qualification-lab" if qualification_lab else "production",
        "source_revision": candidate_row["revision"], "source_tree": candidate_row["tree"],
        "candidate_manifest_sha256": candidate_row["manifest_sha256"],
        "source_archive_sha256": candidate_row["archive_sha256"], "configuration_binding": binding,
    }
    plan_id = _sha256_bytes(json.dumps(plan_identity, sort_keys=True, separators=(",", ":")).encode())
    result = _base_result("install --plan", "planned", "安装事务计划已生成；尚未执行任何变更")
    result["plan_id"] = plan_id
    result["scope"] = plan_identity["scope"]
    result["checks"] = [
        _check("INSTALL_TARGET_LINUX", "pass", "local", "计划在目标 Linux 主机生成", {"target_linux": True}),
        _check("INSTALL_CONFIG_VALID", "pass", "config", "配置与首个支持 profile 匹配", {"profile": SUPPORTED_PROFILE}),
        _check("INSTALL_EXTERNAL_MANIFEST_PIN", "pass", "candidate", "候选命中独立可信通道 manifest SHA256",
               {"external_pin_matched": True}),
        _check("INSTALL_CANDIDATE_VERIFIED", "pass", "candidate", "候选 manifest、源码归档和逐文件摘要完全一致",
               {"source_revision": candidate_row["revision"], "production_qualified": candidate_row["production_qualified"],
                "qualification_reverified": not qualification_lab}),
        _check("INSTALL_PLAN_READ_ONLY", "pass", "safety", "计划阶段未调用网络、Docker、PM2、Caddy、数据库或云 API",
               {"changed": False, "service_commands": 0, "network_probes": 0}),
    ]
    result["actions"] = [
        {"sequence": 1, "code": "REVALIDATE_LIVE_GATES", "target": "ordinary OJ, active contests, queues, cloud and locks", "rollback": "not-needed"},
        {"sequence": 2, "code": "CREATE_DURABLE_BACKUP", "target": "source, config, secrets references, plugin, PM2 and Caddy state", "rollback": "retain-until-independent-verification"},
        {"sequence": 3, "code": "INSTALL_FROZEN_SOURCE", "target": "/opt/noi-linux-contest-system", "rollback": "restore-complete-source-snapshot"},
        {"sequence": 4, "code": "INSTALL_HYDRO_INTEGRATION", "target": "Hydro addon and idempotency state", "rollback": "restore-addon-and-exact-PM2-definition"},
        {"sequence": 5, "code": "PUBLISH_CLOSED_FRONTEND", "target": "closed exam Caddy snippet and one conditional config commit", "rollback": "restore-disk-and-live-Caddy-baseline"},
        {"sequence": 6, "code": "START_ORCHESTRATOR_ONLY", "target": "noi-orchestrator", "rollback": "restore-prior-container-and-config"},
        {"sequence": 7, "code": "VERIFY_AND_COMMIT", "target": "ordinary OJ continuity, closed ingress, queues, cloud and rollback marker", "rollback": "automatic-complete-combination"},
    ]
    result["warnings"] = [
        "计划有效性依赖候选、配置绑定和目标机事实；apply 前必须全部重验。",
        "当前命令只生成公开计划；执行前还必须生成并独立钉住 root-only 私有升级计划。",
    ]
    if qualification_lab:
        result["warnings"].append("这是资格实验室计划，不能用于生产比赛环境。")
    else:
        result["warnings"].append("生产资格证据已完整重验；本命令仍不会安装或修改服务。")
    _assert_no_secret_leak(result, state.secret_candidates)
    return result, 0


def _private_install_operation(path: str, expected_sha256: str) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 \
                or not 0 < metadata.st_size <= 2 * 1024 * 1024 \
                or (_runtime_system() == "Linux" and
                    (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o077)):
            raise ValueError("private install plan metadata differs")
        raw = b""
        while len(raw) <= metadata.st_size:
            chunk = os.read(descriptor, min(65536, metadata.st_size + 1 - len(raw)))
            if not chunk: break
            raw += chunk
        if len(raw) != metadata.st_size or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("private install plan trust pin differs")
    finally:
        os.close(descriptor)
    try: value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("private install plan is invalid") from exc
    operation = value.get("operation") if isinstance(value, dict) else None
    if operation not in {"upgrade", "clean-install"}:
        raise ValueError("private install operation differs")
    return operation


def _install_apply(private_plan: str | None, expected_plan_sha256: str | None) -> tuple[dict[str, Any], int]:
    result = _base_result("install --apply", "error", "安装事务未执行")
    if _runtime_system() != "Linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        result["checks"] = [_check("INSTALL_APPLY_ROOT_LINUX", "fail", "local",
            "安装事务只能由目标 Linux 主机的 root 执行", {"root_linux": False},
            "请在目标机 root-only 会话中执行同一条命令")]
        return result, 3
    if not isinstance(private_plan, str) or not private_plan \
            or not isinstance(expected_plan_sha256, str) \
            or not re.fullmatch(r"[a-f0-9]{64}", expected_plan_sha256):
        result["checks"] = [_check("INSTALL_PRIVATE_PLAN_PIN", "fail", "plan",
            "必须同时提供 root-only 私有计划和独立取得的 64 位 SHA256",
            {"private_plan_pinned": False}, "重新生成私有计划并从独立终端复制摘要")]
        return result, 2
    try:
        operation = _private_install_operation(private_plan, expected_plan_sha256)
    except (OSError, ValueError):
        result["checks"] = [_check("INSTALL_PRIVATE_PLAN_PIN", "fail", "plan",
            "私有计划的元数据、摘要、格式或事务类型不符合契约", {"private_plan_pinned": False},
            "重新生成私有计划并从独立终端复制摘要")]
        return result, 2
    executor = REPO_ROOT / "scripts" / (
        "apply_v1_clean_install.py" if operation == "clean-install" else "apply_v1_install.py"
    )
    command = [sys.executable, str(executor), "--apply", "--private-plan", private_plan,
               "--expected-plan-sha256", expected_plan_sha256]
    environment = {
        "HOME": "/root", "USER": "root", "LOGNAME": "root", "SHELL": "/bin/bash",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            timeout=3600, check=False)
    except (OSError, subprocess.TimeoutExpired):
        result["changed"] = True
        result["summary"] = "安装执行被中断；必须用完全相同的计划和摘要重新运行以触发恢复"
        result["checks"] = [_check("INSTALL_TRANSACTION_TERMINAL", "fail", "transaction",
            "执行器没有返回可验证终态；未输出异常或私有路径", {"terminal": False},
            "不要手工改服务；原样重跑 install --apply 让持久事务自动回滚")]
        return result, 4
    if completed.returncode != 0 or not 0 < len(completed.stdout) <= 1024 * 1024:
        result["changed"] = True
        result["summary"] = "安装事务未提交；请按同一计划重跑恢复并检查 root-only 事务日志"
        result["checks"] = [_check("INSTALL_TRANSACTION_TERMINAL", "fail", "transaction",
            "执行器拒绝、失败或输出不完整；stderr 已隐藏", {"terminal": False},
            "禁止改用旧脚本；原样重跑以收敛到 rollback_verified")]
        return result, 4
    try:
        terminal = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        terminal = None
    fields = {"status", "plan_id", "backup_manifest_sha256"} | ({"operation"} if operation == "clean-install" else set())
    if not isinstance(terminal, dict) or set(terminal) != fields \
            or terminal.get("status") not in {"committed", "rollback_verified"} \
            or (operation == "clean-install" and terminal.get("operation") != operation) \
            or not re.fullmatch(r"[a-f0-9]{64}", str(terminal.get("plan_id"))) \
            or not re.fullmatch(r"[a-f0-9]{64}", str(terminal.get("backup_manifest_sha256"))):
        result["changed"] = True
        result["summary"] = "安装执行器返回了无法验证的终态"
        result["checks"] = [_check("INSTALL_TRANSACTION_TERMINAL", "fail", "transaction",
            "终态结构或身份不符合 V1 契约", {"terminal": False},
            "原样重跑同一计划；不要手工启动或重载任何服务")]
        return result, 4
    committed = terminal["status"] == "committed"
    result = _base_result("install --apply", "ok" if committed else "rolled_back",
                          "安装已提交并通过终验" if committed else "安装失败但已完整回滚并通过终验")
    result["changed"] = committed
    result["plan_id"] = terminal["plan_id"]
    result["checks"] = [_check("INSTALL_TRANSACTION_TERMINAL", "pass", "transaction",
        "持久事务已到达唯一可接受终态", {"terminal": True, "status": terminal["status"]})]
    result["actions"] = [{"code": "INSTALL_CLEAN_TRANSACTION" if operation == "clean-install" else "INSTALL_UPGRADE_TRANSACTION",
        "status": terminal["status"], "backup_manifest_sha256": terminal["backup_manifest_sha256"]}]
    if not committed:
        result["warnings"] = [
            "当前仍是安装前的完整组合；修复失败原因后必须生成新计划。"
            if operation == "clean-install" else
            "当前仍是升级前的完整组合；修复失败原因后必须生成新计划。"
        ]
    return result, 0 if committed else 4


def _config_file_metadata(state: ConfigState | None) -> dict[str, Any]:
    if state is None:
        # An unvalidated --config value may name any local file.  Do not stat,
        # reread, size or hash it after validation failed: that would turn the
        # support command into an arbitrary-file metadata oracle.
        return {
            "validated_config": False,
            "metadata_collected": False,
            "size_bytes": None,
            "sha256": None,
            "hash_status": "omitted_unvalidated_input",
        }
    return {
        "validated_config": True,
        "metadata_collected": False,
        "size_bytes": None,
        # Raw configuration can contain inline secrets or secret defaults.  A
        # digest would be an offline password oracle, so only the separately
        # canonicalized redacted-effective digest is publishable.
        "sha256": None,
        "hash_status": "omitted_secret_bearing_input",
    }


def _support_bundle_payload(
    config_path: Path,
) -> tuple[dict[str, Any], set[str]]:
    doctor, _, state = _doctor(config_path)
    secret_candidates: set[str] = set()
    effective_digest = None
    if state is not None:
        redacted, _, redacted_candidates = _redact_config(state)
        secret_candidates.update(redacted_candidates)
        effective_digest = _sha256_bytes(
            json.dumps(
                redacted,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "bundle_type": "noictl-read-only-support",
        "generated_at": _utc_now().isoformat().replace("+00:00", "Z"),
        "runtime": {
            "system": _runtime_system(),
            "pointer_bits": 64 if sys.maxsize > 2**32 else 32,
            "python": _python_version(),
            "python_implementation": sys.implementation.name,
        },
        "tool": {
            "command_contract": 1,
            "script_sha256": None,
            "script_hash_status": "omitted_no_runtime_reread",
        },
        "configuration": {
            "file": _config_file_metadata(state),
            "redacted_effective_sha256": effective_digest,
        },
        "diagnostics": {"doctor": doctor},
        "collection": {
            "network_probes": 0,
            "service_commands": 0,
            "logs_read": 0,
            "databases_read": 0,
            "referenced_secret_files_read": 0,
            "selected_config_validation_attempted": True,
            "environment_enumerated": False,
        },
        "redactions": list(REDACTIONS),
    }
    return payload, secret_candidates


def _write_private_file(path: Path, payload: bytes) -> None:
    # Support artifacts are deliberately restricted to a safe basename in the
    # process current directory.  Relative open therefore has no replaceable
    # user-selected parent component, and also excludes UNC paths and NTFS ADS.
    if not _safe_support_filename(str(path)) or path.parent != Path("."):
        raise OSError("unsafe_output_name")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise OSError("output_not_regular")
        created_identity = (opened_stat.st_dev, opened_stat.st_ino)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created_identity is not None:
            try:
                current_stat = os.lstat(path)
                if (current_stat.st_dev, current_stat.st_ino) == created_identity:
                    path.unlink()
            except OSError:
                pass
        raise


def _support_bundle(
    config_path: Path, output_argument: str | None
) -> tuple[dict[str, Any], int]:
    default_output = output_argument is None
    if default_output:
        timestamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
        output_path = Path(f"noictl-support-{timestamp}.json")
    else:
        if not _safe_support_filename(output_argument):
            result = _base_result(
                "support-bundle",
                "error",
                "支持包文件名无效；未写入任何文件",
            )
            result["checks"] = [
                _check(
                    "SUPPORT_BUNDLE_OUTPUT_NAME_SAFE",
                    "fail",
                    "local",
                    "--output 只能是当前目录中的安全 .json 文件名",
                    {"file_written": False},
                    "请使用不含路径、空格、冒号或设备名的新 .json 文件名",
                )
            ]
            return result, 2
        output_path = Path(output_argument)
    try:
        payload, secret_candidates = _support_bundle_payload(config_path)
        _assert_no_secret_leak(payload, secret_candidates)
        encoded = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        bundle_sha256 = _sha256_bytes(encoded)
        _write_private_file(output_path, encoded)
    except UnsafeOutputError:
        result = _base_result(
            "support-bundle", "refused", "检测到潜在敏感内容，未生成支持包"
        )
        result["checks"] = [
            _check(
                "SUPPORT_BUNDLE_REDACTION_SAFE",
                "fail",
                "safety",
                "支持包脱敏门禁失败",
                {"file_written": False},
                "请向维护者报告，不要手工打包配置或秘密文件",
            )
        ]
        return result, 9
    except (OSError, ValueError):
        result = _base_result(
            "support-bundle", "error", "支持包未写入；目标必须是不存在的本地文件"
        )
        result["checks"] = [
            _check(
                "SUPPORT_BUNDLE_WRITABLE",
                "fail",
                "local",
                "无法安全创建支持包文件；未覆盖现有文件",
                {"file_written": False},
                "请选择已存在且可写目录中的新文件名",
            )
        ]
        return result, 4

    doctor_status = payload["diagnostics"]["doctor"]["status"]
    result_status = "ok" if doctor_status == "ok" else "warning"
    result = _base_result(
        "support-bundle", result_status, "脱敏支持包已写入本地文件"
    )
    result["checks"] = [
        _check(
            "SUPPORT_BUNDLE_REDACTION_SAFE",
            "pass",
            "safety",
            "支持包仅包含脱敏诊断、元数据和 SHA256",
            {"referenced_secret_files_read": 0, "network_probes": 0},
        ),
        _check(
            "SUPPORT_BUNDLE_WRITTEN",
            "pass",
            "local",
            "支持包以独占创建方式写入，未覆盖原文件",
            {"file_written": True},
        ),
    ]
    result["actions"] = [
        {
            "code": "LOCAL_SUPPORT_FILE_CREATED",
            "target": "default-current-directory"
            if default_output
            else "user-specified-local-file",
            "name": output_path.name if default_output else None,
            "sha256": bundle_sha256,
        }
    ]
    if doctor_status != "ok":
        result["warnings"] = [
            "支持包已生成，但其中的 doctor 静态诊断并非全部通过。"
        ]
    return result, 0


def _emit(result: dict[str, Any], json_output: bool) -> None:
    if json_output:
        _print_text(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return
    _print_text(result["summary"])
    for item in result.get("checks", []):
        label = {"pass": "通过", "warn": "警告", "fail": "失败"}.get(
            item["status"], item["status"]
        )
        _print_text(f"[{label}] {item['code']}: {item['message']}")
        if item.get("remediation"):
            _print_text(f"  建议: {item['remediation']}")
    for warning in result.get("warnings", []):
        _print_text(f"警告: {warning}")
    if result.get("command") == "config show" and result.get("status") == "ok":
        _print_text("\n有效配置（已脱敏）:")
        _print_text(
            yaml.safe_dump(
                result["effective_config"],
                allow_unicode=True,
                sort_keys=True,
                default_flow_style=False,
            ).rstrip()
        )
        _print_text("\n值来源:")
        for path, source in result["sources"].items():
            _print_text(f"{path}: {source}")
    if result.get("command") == "support-bundle" and result.get("actions"):
        action = result["actions"][0]
        if action.get("name"):
            _print_text(f"文件: {action['name']}")
        else:
            _print_text("文件: --output 指定的本地文件")
        _print_text(f"SHA256: {action['sha256']}")
    if result.get("command") == "install --plan" and result.get("plan_id"):
        _print_text(f"plan_id: {result['plan_id']}")
        for action in result.get("actions", []):
            _print_text(
                f"{action['sequence']}. {action['code']}: {action['target']} "
                f"(rollback={action['rollback']})"
            )


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="输出稳定 JSON 契约",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="显式配置文件；默认使用 ORCHESTRATOR_CONFIG 或本地 config.yaml",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = NoictlArgumentParser(
        prog="noictl",
        description="NOI Linux 比赛系统诊断、计划与事务 CLI",
    )
    _add_common_options(parser)
    commands = parser.add_subparsers(dest="command_group", required=True)

    doctor = commands.add_parser("doctor", help="本机静态只读诊断")
    _add_common_options(doctor)
    doctor.set_defaults(handler="doctor")

    config = commands.add_parser("config", help="配置只读命令")
    _add_common_options(config)
    config_commands = config.add_subparsers(dest="config_command", required=True)

    validate = config_commands.add_parser("validate", help="校验有效配置")
    _add_common_options(validate)
    validate.set_defaults(handler="config_validate")

    show = config_commands.add_parser("show", help="显示脱敏有效配置")
    _add_common_options(show)
    show.add_argument("--effective", action="store_true", required=True)
    show.add_argument("--redact", action="store_true", required=True)
    show.set_defaults(handler="config_show")

    support = commands.add_parser(
        "support-bundle", help="生成一个脱敏本地 JSON 支持包"
    )
    _add_common_options(support)
    support.add_argument(
        "--output",
        metavar="PATH",
        help="新文件路径；禁止覆盖，默认写当前目录中的时间戳文件",
    )
    support.set_defaults(handler="support_bundle")

    install = commands.add_parser("install", help="安装或升级事务")
    _add_common_options(install)
    install_mode = install.add_mutually_exclusive_group(required=True)
    install_mode.add_argument("--plan", action="store_true")
    install_mode.add_argument("--apply", action="store_true")
    install.add_argument(
        "--candidate", metavar="DIR",
        help="已冻结并通过完整性核验的 V1 候选目录",
    )
    install.add_argument(
        "--expected-manifest-sha256", metavar="HEX64",
        help="从候选目录之外的可信发布通道取得的 manifest SHA256",
    )
    install.add_argument(
        "--qualification-lab", action="store_true",
        help="仅生成隔离资格实验室计划；不得用于生产",
    )
    install.add_argument("--private-plan", metavar="PATH",
        help="build_v1_private_upgrade_plan.py 生成的 root-only 私有计划")
    install.add_argument("--expected-plan-sha256", metavar="HEX64",
        help="从计划生成器输出中独立复制的私有计划 SHA256")
    install.set_defaults(handler="install")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_output_streams()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in raw_argv
    try:
        args = _build_parser().parse_args(raw_argv)
    except CliArgumentError:
        result = _base_result(
            "unknown", "error", "命令参数无效；未执行任何操作"
        )
        result["checks"] = [
            _check(
                "CLI_ARGUMENTS_VALID",
                "fail",
                "cli",
                "命令参数不符合第一批 noictl 契约",
                {"arguments_valid": False},
                "请查看 noictl --help，并补齐命令所需的安全参数",
            )
        ]
        _emit(result, json_output)
        return 2
    json_output = bool(getattr(args, "json_output", False))
    if args.handler == "install":
        invalid_apply = args.apply and (
            args.candidate is not None or args.expected_manifest_sha256 is not None
            or args.qualification_lab or args.private_plan is None
            or args.expected_plan_sha256 is None
            or getattr(args, "config_path", None) is not None
        )
        invalid_plan = args.plan and (
            args.candidate is None or args.expected_manifest_sha256 is None
            or args.private_plan is not None or args.expected_plan_sha256 is not None
        )
        if invalid_apply or invalid_plan:
            result = _base_result(
                "install --apply" if args.apply else "install --plan",
                "error", "安装命令参数无效；未执行任何操作",
            )
            result["checks"] = [_check("CLI_ARGUMENTS_VALID", "fail", "cli",
                "计划与执行参数不能混用，执行模式也不读取 --config",
                {"arguments_valid": False}, "请查看 noictl install --help")]
            _emit(result, json_output)
            return 2
    command_names = {
        "doctor": "doctor",
        "config_validate": "config validate",
        "config_show": "config show",
        "support_bundle": "support-bundle",
        "install": "install --apply" if getattr(args, "apply", False) else "install --plan",
    }
    try:
        if args.handler == "install" and args.apply:
            config_path = None
        else:
            configured_path = getattr(args, "config_path", None)
            config_path = (Path(configured_path).expanduser()
                if configured_path is not None else _default_config_path())
    except MemoryError:
        raise
    except Exception:
        result = _base_result(
            command_names.get(args.handler, "unknown"),
            "error",
            "配置路径无效；未访问配置内容",
        )
        result["checks"] = [
            _check(
                "CONFIG_VALID",
                "fail",
                "config",
                "无法安全解析配置路径；路径详情未输出",
                {"error_kind": "invalid_path"},
                "请使用本机普通文件路径，避免用户缩写、UNC 或重解析点",
            )
        ]
        _emit(result, json_output)
        return 2
    try:
        if args.handler == "doctor":
            result, exit_code, _ = _doctor(config_path)
        elif args.handler == "config_validate":
            result, exit_code = _config_validate(config_path)
        elif args.handler == "config_show":
            result, exit_code = _config_show(config_path)
        elif args.handler == "support_bundle":
            result, exit_code = _support_bundle(config_path, args.output)
        elif args.handler == "install":
            if args.apply:
                result, exit_code = _install_apply(args.private_plan, args.expected_plan_sha256)
            else:
                result, exit_code = _install_plan(
                    config_path, args.candidate, args.qualification_lab,
                    args.expected_manifest_sha256,
                )
        else:  # pragma: no cover - argparse makes this unreachable.
            raise AssertionError("unknown handler")
        _emit(result, json_output)
        return exit_code
    except (Exception, KeyboardInterrupt):
        # Never print a traceback: exception text may contain a path, URL, or
        # value originating in a local configuration.  Tests and maintainers
        # can reproduce with a synthetic configuration instead.
        applying = args.handler == "install" and getattr(args, "apply", False)
        result = _base_result(command_names.get(args.handler, "unknown"), "error",
            "升级命令发生内部错误；状态可能需要恢复" if applying
            else "只读命令发生内部错误；异常详情因脱敏策略未输出")
        result["changed"] = applying
        result["checks"] = [
            _check(
                "READ_ONLY_INTERNAL_ERROR",
                "fail",
                "safety",
                "异常详情已隐藏；升级命令须以相同计划重跑恢复" if applying
                else "命令已停止，未执行网络、服务或配置写操作",
                {"exception_details_emitted": False},
                "原样重跑相同私有计划" if applying
                else "请使用不含真实秘密的最小配置向维护者报告",
            )
        ]
        _emit(result, json_output)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
