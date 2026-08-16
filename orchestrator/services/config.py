"""YAML configuration loading with explicit environment interpolation."""
from __future__ import annotations

import os
import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

_ENV = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_SHA256_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_ARTIFACT_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def _expand(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ValueError(f"缺少环境变量 {name}")

    return _ENV.sub(replace, value)


def _require(cfg: dict, *path: str) -> Any:
    value: Any = cfg
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"配置缺失: {'.'.join(path)}")
        value = value[key]
    if value in (None, ""):
        raise ValueError(f"配置为空: {'.'.join(path)}")
    return value


def validate_config(cfg: dict) -> None:
    provider = str(_require(cfg, "cloud", "provider")).lower()
    if provider not in {"aliyun", "tencent"}:
        raise ValueError("cloud.provider 只支持 aliyun 或 tencent")
    provider_cfg = _require(cfg, "cloud", provider)
    required = {
        "aliyun": ("access_key_id", "access_key_secret", "region_id", "instance_id"),
        "tencent": ("secret_id", "secret_key", "region", "instance_id"),
    }[provider]
    for key in required:
        if not provider_cfg.get(key):
            raise ValueError(f"配置为空: cloud.{provider}.{key}")

    desktop_access = provider_cfg.get("desktop_access") or {"enabled": False}
    if not isinstance(desktop_access, dict):
        raise ValueError(f"cloud.{provider}.desktop_access 必须是映射")
    desktop_access_enabled = desktop_access.get("enabled", False)
    if not isinstance(desktop_access_enabled, bool):
        raise ValueError(f"cloud.{provider}.desktop_access.enabled 必须是布尔值")
    if desktop_access_enabled and provider != "aliyun":
        raise ValueError("临时桌面入站规则目前只支持 aliyun")
    if desktop_access_enabled:
        security_group_id = str(desktop_access.get("security_group_id") or "")
        if not re.fullmatch(r"sg-[A-Za-z0-9]+", security_group_id):
            raise ValueError(
                "cloud.aliyun.desktop_access.security_group_id 格式无效"
            )
        try:
            source = ipaddress.ip_network(
                str(desktop_access.get("source_cidr") or ""), strict=True
            )
        except ValueError as exc:
            raise ValueError(
                "cloud.aliyun.desktop_access.source_cidr 必须是标准 IPv4 CIDR"
            ) from exc
        if source.version != 4:
            raise ValueError(
                "cloud.aliyun.desktop_access.source_cidr 只支持 IPv4"
            )
        management_sources = desktop_access.get("management_source_cidrs") or []
        if isinstance(management_sources, str):
            management_sources = [
                item.strip() for item in management_sources.split(",") if item.strip()
            ]
        if not isinstance(management_sources, list) or not management_sources:
            raise ValueError(
                "cloud.aliyun.desktop_access.management_source_cidrs 不能为空"
            )
        if len(management_sources) != 1:
            raise ValueError(
                "cloud.aliyun.desktop_access.management_source_cidrs "
                "必须且只能配置一个 OJ 主机 IPv4 /32"
            )
        for item in management_sources:
            try:
                network = ipaddress.ip_network(str(item), strict=True)
            except ValueError as exc:
                raise ValueError(
                    "cloud.aliyun.desktop_access.management_source_cidrs "
                    "必须是标准 IPv4 CIDR 列表"
                ) from exc
            if network.version != 4:
                raise ValueError(
                    "cloud.aliyun.desktop_access.management_source_cidrs "
                    "只支持 IPv4"
                )
            if network.prefixlen != 32:
                raise ValueError(
                    "cloud.aliyun.desktop_access.management_source_cidrs "
                    "必须是 OJ 主机 IPv4 /32"
                )
        if source == network:
            raise ValueError(
                "cloud.aliyun.desktop_access.source_cidr 不能与 "
                "OJ 管理 IPv4 /32 完全相同"
            )
        try:
            direct_port = int(desktop_access.get("port", 80))
            direct_priority = int(desktop_access.get("priority", 20))
            reconcile_seconds = int(desktop_access.get("reconcile_seconds", 5))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cloud.aliyun.desktop_access port/priority/reconcile_seconds 必须是整数"
            ) from exc
        if direct_port != 80:
            raise ValueError("cloud.aliyun.desktop_access.port 必须是 80")
        if not 1 <= direct_priority <= 100:
            raise ValueError("cloud.aliyun.desktop_access.priority 必须在 1 到 100 之间")
        if not 1 <= reconcile_seconds <= 30:
            raise ValueError(
                "cloud.aliyun.desktop_access.reconcile_seconds 必须在 1 到 30 之间"
            )
        prefix = str(
            desktop_access.get("description_prefix")
            or "NOI-DESKTOP-DIRECT-MANAGED"
        ).strip()
        if (
            len(prefix) < 8
            or len(prefix) > 80
            or not re.fullmatch(r"[A-Za-z0-9_.: -]+", prefix)
        ):
            raise ValueError(
                "cloud.aliyun.desktop_access.description_prefix 格式无效"
            )

    server = _require(cfg, "contest_server")
    for key in ("ssh_user", "ssh_key", "seats_root", "docker_image", "docker_network"):
        if not server.get(key):
            raise ValueError(f"配置为空: contest_server.{key}")
    if not str(server["seats_root"]).startswith("/"):
        raise ValueError("contest_server.seats_root 必须是绝对路径")
    for key in ("seats_root", "docker_image", "docker_network"):
        if not _SAFE_NAME.fullmatch(str(server[key])):
            raise ValueError(f"contest_server.{key} 含不安全字符")
    if not re.fullmatch(r"[0-9]{3,4}x[0-9]{3,4}", str(server.get("resolution", "1366x768"))):
        raise ValueError("contest_server.resolution 格式必须为 宽x高")
    for key, minimum, maximum in (
        ("frame_rate", 10, 60),
        ("no_vnc_quality", 0, 9),
        ("no_vnc_compression", 0, 9),
    ):
        try:
            value = int(server.get(key, {"frame_rate": 30, "no_vnc_quality": 9, "no_vnc_compression": 2}[key]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"contest_server.{key} 必须是整数") from exc
        if not minimum <= value <= maximum:
            raise ValueError(
                f"contest_server.{key} 必须在 {minimum} 到 {maximum} 之间"
            )
    gateway_base = str(server.get("gateway_public_base_url") or "").strip()
    gateway_port = None
    if gateway_base:
        parsed_gateway = urlsplit(gateway_base)
        try:
            gateway_port = parsed_gateway.port
        except ValueError as exc:
            raise ValueError(
                "contest_server.gateway_public_base_url 端口无效"
            ) from exc
        if (
            parsed_gateway.scheme not in {"http", "https"}
            or not parsed_gateway.hostname
            or parsed_gateway.username
            or parsed_gateway.password
            or parsed_gateway.path not in {"", "/"}
            or parsed_gateway.query
            or parsed_gateway.fragment
        ):
            raise ValueError(
                "contest_server.gateway_public_base_url 必须是根路径 HTTP(S) 绝对地址"
            )
    gateway_scheme = str(server.get("gateway_scheme", "http")).lower()
    if gateway_scheme not in {"http", "https"}:
        raise ValueError("contest_server.gateway_scheme 只支持 http 或 https")
    gateway_bind_raw = str(
        server.get("gateway_bind_address", "0.0.0.0")
    ).strip()
    try:
        gateway_bind = ipaddress.ip_address(gateway_bind_raw)
    except ValueError as exc:
        raise ValueError(
            "contest_server.gateway_bind_address 必须是 IPv4 地址"
        ) from exc
    if gateway_bind.version != 4 or gateway_bind.is_loopback or gateway_bind.is_multicast:
        raise ValueError(
            "contest_server.gateway_bind_address 必须是非回环 IPv4 地址"
        )
    if desktop_access_enabled:
        if int(server.get("gateway_listen", 80)) != direct_port:
            raise ValueError(
                "contest_server.gateway_listen 必须与 "
                "cloud.aliyun.desktop_access.port 一致"
            )
        direct_scheme = urlsplit(gateway_base).scheme if gateway_base else gateway_scheme
        if direct_scheme != "http":
            raise ValueError("裸 EIP 直连模式必须使用 http")
        if gateway_base and gateway_port not in {None, direct_port}:
            raise ValueError(
                "裸 EIP 直连模式的 gateway_public_base_url "
                "端口必须省略或为 80"
            )
        if gateway_base:
            try:
                gateway_ip = ipaddress.ip_address(
                    str(urlsplit(gateway_base).hostname or "")
                )
            except ValueError as exc:
                raise ValueError(
                    "裸 EIP 直连模式的 gateway_public_base_url 必须使用 IP"
                ) from exc
            if gateway_ip.version != 4:
                raise ValueError(
                    "裸 EIP 直连模式的 gateway_public_base_url 只支持 IPv4"
                )
    fingerprint = str(server.get("host_key_sha256", "")).strip()
    if fingerprint and not _SHA256_FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("contest_server.host_key_sha256 格式无效")
    if (
        server.get("strict_host_key", True)
        and not server.get("known_hosts")
        and not fingerprint
    ):
        raise ValueError(
            "strict_host_key=true 时必须配置 known_hosts 或 host_key_sha256"
        )

    hydro = _require(cfg, "hydro")
    for key in ("public_base_url", "internal_base_url", "mongo_uri", "domain_id"):
        if not hydro.get(key):
            raise ValueError(f"配置为空: hydro.{key}")
    # V1 always publishes the approved PDF and practice data into the OJ.
    # The private addon token is therefore a platform prerequisite, not only
    # a web-submit/notification option.
    if len(str(hydro.get("orchestrator_token", ""))) < 32:
        raise ValueError("hydro.orchestrator_token 至少 32 个字符")
    qualification_failure_path = str(
        hydro.get("qualification_failure_marker_path") or ""
    )
    qualification_marker = str(hydro.get("qualification_marker") or "")
    if qualification_failure_path:
        # Runtime configuration is for the Linux container even when this
        # validator is exercised by the Windows development test suite.
        if not re.fullmatch(
            r"/app/data/qualification/[A-Za-z0-9_.-]{1,128}[.]json",
            qualification_failure_path,
        ):
            raise ValueError(
                "hydro.qualification_failure_marker_path 必须位于 "
                "/app/data/qualification 且为固定 JSON 文件"
            )
        if not re.fullmatch(
            r"NOI-V1-QUAL-[A-Z0-9]{16,64}", qualification_marker
        ):
            raise ValueError("资格故障注入必须配置固定 qualification_marker")
    elif qualification_marker:
        raise ValueError("生产配置不得单独启用 qualification_marker")
    notify_hosts: list[str] = []
    if hydro.get("notify_enabled", False):
        if len(str(hydro.get("orchestrator_token", ""))) < 32:
            raise ValueError("启用 Hydro 通知时 orchestrator_token 至少 32 个字符")
        hosts = hydro.get("notify_allowed_https_hosts") or []
        if isinstance(hosts, str):
            hosts = [value.strip() for value in hosts.split(",") if value.strip()]
        if not isinstance(hosts, list) or not hosts:
            raise ValueError("启用 Hydro 通知时必须配置 notify_allowed_https_hosts")
        notify_hosts = [str(value).strip().lower().rstrip(".") for value in hosts]

    admin = _require(cfg, "orchestrator")
    if len(str(admin.get("admin_password", ""))) < 16:
        raise ValueError("orchestrator.admin_password 至少 16 个字符")
    if desktop_access_enabled and hydro.get("notify_enabled", False):
        notification_base = str(admin.get("public_base_url") or "").strip()
        parsed_notification = urlsplit(notification_base)
        try:
            notification_port = parsed_notification.port
        except ValueError as exc:
            raise ValueError(
                "裸 EIP 直连通知要求有效的 orchestrator.public_base_url"
            ) from exc
        notification_host = (
            str(parsed_notification.hostname or "").lower().rstrip(".")
        )
        if (
            parsed_notification.scheme != "https"
            or not notification_host
            or parsed_notification.username
            or parsed_notification.password
            or notification_port not in {None, 443}
            or parsed_notification.path not in {"", "/"}
            or parsed_notification.query
            or parsed_notification.fragment
            or notification_host not in set(notify_hosts)
        ):
            raise ValueError(
                "裸 EIP 直连通知要求 orchestrator.public_base_url 使用 "
                "notify_allowed_https_hosts 中的 HTTPS 根域名"
            )
    _require(cfg, "orchestrator", "db")
    deployment_lock = str(admin.get("deployment_lock", "")).strip()
    if deployment_lock and not deployment_lock.startswith("/"):
        raise ValueError("orchestrator.deployment_lock 必须是绝对路径")
    collected_dir = str(_require(cfg, "orchestrator", "collected_dir"))
    if not collected_dir.startswith("/"):
        raise ValueError("orchestrator.collected_dir 必须是绝对路径")
    materials_dir = str(admin.get("materials_dir", "/app/data/materials"))
    if not materials_dir.startswith("/"):
        raise ValueError("orchestrator.materials_dir 必须是绝对路径")
    paper_maximum = int(admin.get("paper_max_bytes", 64 * 1024 * 1024))
    if paper_maximum < 1024 or paper_maximum > 256 * 1024 * 1024:
        raise ValueError("orchestrator.paper_max_bytes 必须在 1 KiB 到 256 MiB 之间")
    testdata_maximum = int(
        admin.get("testdata_max_bytes", 64 * 1024 * 1024)
    )
    if testdata_maximum < 1024 or testdata_maximum > 512 * 1024 * 1024:
        raise ValueError(
            "orchestrator.testdata_max_bytes 必须在 1 KiB 到 512 MiB 之间"
        )
    material_publish_maximum = int(
        admin.get("material_publish_max_bytes", 128 * 1024 * 1024)
    )
    if (
        material_publish_maximum < paper_maximum + testdata_maximum
        or material_publish_maximum > 512 * 1024 * 1024
    ):
        raise ValueError(
            "orchestrator.material_publish_max_bytes 必须覆盖 PDF 与辅助数据上限之和，"
            "且不超过 512 MiB"
        )
    expanded_maximum = int(
        admin.get("testdata_expanded_max_bytes", 256 * 1024 * 1024)
    )
    if expanded_maximum < testdata_maximum or expanded_maximum > 2 * 1024**3:
        raise ValueError(
            "orchestrator.testdata_expanded_max_bytes 必须不小于 ZIP 限制且不超过 2 GiB"
        )
    testdata_max_files = int(admin.get("testdata_max_files", 1000))
    if testdata_max_files < 1 or testdata_max_files > 10000:
        raise ValueError("orchestrator.testdata_max_files 必须在 1 到 10000 之间")
    maximum = int(admin.get("web_submit_max_bytes", 102400))
    if maximum < 1024 or maximum > 512 * 1024:
        raise ValueError("orchestrator.web_submit_max_bytes 必须在 1 KiB 到 512 KiB 之间")
    realtime_lease = float(admin.get("realtime_judge_lease_seconds", 45))
    if realtime_lease < 35 or realtime_lease > 300:
        raise ValueError(
            "orchestrator.realtime_judge_lease_seconds 必须在 35 到 300 秒之间"
        )
    realtime_idle = float(admin.get("realtime_judge_idle_seconds", 0.5))
    if realtime_idle <= 0 or realtime_idle > 10:
        raise ValueError(
            "orchestrator.realtime_judge_idle_seconds 必须大于 0 且不超过 10 秒"
        )
    for key, default, minimum, maximum in (
        ("default_max_participants", 15, 1, 30),
        ("default_spare_seats", 2, 0, 10),
        ("release_lead_minutes", 5, 1, 60),
        ("practice_groups_per_problem", 3, 2, 4),
        ("shutdown_grace_minutes", 30, 1, 120),
    ):
        try:
            value = int(admin.get(key, default))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"orchestrator.{key} 必须是整数") from exc
        if not minimum <= value <= maximum:
            raise ValueError(
                f"orchestrator.{key} 必须在 {minimum} 到 {maximum} 之间"
            )
    try:
        seat_pool_maximum = int(admin.get("seat_pool_maximum", 30))
        seat_pool_total_maximum = int(admin.get("seat_pool_total_maximum", 40))
    except (TypeError, ValueError) as exc:
        raise ValueError("seat_pool_maximum/seat_pool_total_maximum 必须是整数") from exc
    if not 1 <= seat_pool_maximum <= 30:
        raise ValueError("orchestrator.seat_pool_maximum 必须在 1 到 30 之间")
    if not seat_pool_maximum <= seat_pool_total_maximum <= 40:
        raise ValueError(
            "orchestrator.seat_pool_total_maximum 必须不小于正式人数上限且不超过 40"
        )
    default_maximum = int(admin.get("default_max_participants", 15))
    default_spares = int(admin.get("default_spare_seats", 2))
    if default_maximum > seat_pool_maximum:
        raise ValueError("default_max_participants 不能超过 seat_pool_maximum")
    if default_maximum + default_spares > seat_pool_total_maximum:
        raise ValueError(
            "default_max_participants + default_spare_seats 不能超过 seat_pool_total_maximum"
        )
    if int(admin.get("default_spare_seats", 2)) > int(
        admin.get("default_max_participants", 15)
    ):
        raise ValueError("default_spare_seats 不能超过 default_max_participants")
    artifact_root = str(admin.get("artifact_root", "/app/data/artifacts"))
    if not artifact_root.startswith("/"):
        raise ValueError("orchestrator.artifact_root 必须是绝对路径")
    if len({artifact_root, materials_dir, collected_dir}) != 3:
        raise ValueError("材料、工件和收卷目录必须彼此独立")
    try:
        workspace_retention = int(admin.get("workspace_retention_days", 30))
        evidence_retention = int(admin.get("evidence_retention_days", 180))
    except (TypeError, ValueError) as exc:
        raise ValueError("本地数据保留天数必须是整数") from exc
    if not 1 <= workspace_retention <= 365:
        raise ValueError("orchestrator.workspace_retention_days 必须在 1 到 365 之间")
    if not workspace_retention <= evidence_retention <= 3650:
        raise ValueError(
            "orchestrator.evidence_retention_days 必须不短于工作区且不超过 3650 天"
        )

    generation = cfg.get("artifact_generation") or {"enabled": False}
    if not isinstance(generation, dict):
        raise ValueError("artifact_generation 必须是映射")
    enabled = generation.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("artifact_generation.enabled 必须是布尔值")
    if enabled:
        ai = generation.get("ai")
        tools = generation.get("tools")
        if not isinstance(ai, dict) or not isinstance(tools, dict):
            raise ValueError("启用材料生成时必须配置 artifact_generation.ai/tools")
        endpoint = str(ai.get("endpoint") or "").strip()
        parsed_endpoint = urlsplit(endpoint)
        loopback = (parsed_endpoint.hostname or "").lower() in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
        allow_loopback = ai.get("allow_insecure_loopback", False)
        if not isinstance(allow_loopback, bool):
            raise ValueError("artifact_generation.ai.allow_insecure_loopback 必须是布尔值")
        if (
            not parsed_endpoint.netloc
            or parsed_endpoint.username
            or parsed_endpoint.password
            or parsed_endpoint.query
            or parsed_endpoint.fragment
            or (
                parsed_endpoint.scheme != "https"
                and not (
                    allow_loopback
                    and parsed_endpoint.scheme == "http"
                    and loopback
                )
            )
        ):
            raise ValueError("artifact_generation.ai.endpoint 必须是安全的绝对 URL")
        if not str(ai.get("model") or "").strip():
            raise ValueError("artifact_generation.ai.model 不能为空")
        direct_key = str(ai.get("api_key") or "").strip()
        key_env = str(ai.get("api_key_env") or "").strip()
        if bool(direct_key) == bool(key_env):
            raise ValueError("artifact_generation.ai 必须且只能配置 api_key/api_key_env 之一")
        if key_env and not _ENV_NAME.fullmatch(key_env):
            raise ValueError("artifact_generation.ai.api_key_env 格式无效")
        roots = tools.get("approved_roots")
        if (
            not isinstance(roots, list)
            or not roots
            or any(not isinstance(value, str) or not value.startswith("/") for value in roots)
        ):
            raise ValueError("artifact_generation.tools.approved_roots 必须是绝对路径列表")
        for kind in ("validators", "oracles"):
            mapping = tools.get(kind)
            if not isinstance(mapping, dict):
                raise ValueError(f"artifact_generation.tools.{kind} 必须是映射")
            for slug, spec in mapping.items():
                if not isinstance(slug, str) or not _ARTIFACT_SLUG.fullmatch(slug):
                    raise ValueError(f"artifact_generation.tools.{kind} 题目 slug 无效")
                if (
                    not isinstance(spec, dict)
                    or not isinstance(spec.get("executable"), str)
                    or not spec["executable"].startswith("/")
                ):
                    raise ValueError(
                        f"artifact_generation.tools.{kind}.{slug}.executable 必须是绝对路径"
                    )

    frontend = cfg.get("frontend_proxy") or {"provider": "none"}
    frontend_provider = str(frontend.get("provider", "none")).lower()
    if frontend_provider not in {"none", "caddy"}:
        raise ValueError("frontend_proxy.provider 只支持 none 或 caddy")
    if desktop_access_enabled and frontend_provider != "caddy":
        raise ValueError(
            "固定 EIP 直连必须配置 frontend_proxy.provider=caddy，"
            "以证明 OJ /s/* 代理旁路保持关闭"
        )
    if frontend_provider == "caddy":
        for key in ("domain", "snippet_path", "caddyfile_path", "admin_url"):
            if not frontend.get(key):
                raise ValueError(f"配置为空: frontend_proxy.{key}")
        for key in ("snippet_path", "caddyfile_path"):
            if not str(frontend[key]).startswith("/"):
                raise ValueError(f"frontend_proxy.{key} 必须是绝对路径")


def load_config(path: str | os.PathLike[str]) -> dict:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config.yaml 顶层必须是映射")
    cfg = _expand(raw)
    validate_config(cfg)
    return cfg
