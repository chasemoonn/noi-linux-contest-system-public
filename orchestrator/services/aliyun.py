"""Alibaba Cloud ECS and fail-closed temporary desktop ingress client."""
from __future__ import annotations

import json
import re
import threading
import time

from alibabacloud_ecs20140526 import models as ecs_models
from alibabacloud_ecs20140526.client import Client
from alibabacloud_tea_openapi import models as open_api_models


class AliyunECS:
    def __init__(self, cfg: dict):
        region = cfg["region_id"]
        self.region = region
        config = open_api_models.Config(
            access_key_id=cfg["access_key_id"],
            access_key_secret=cfg["access_key_secret"],
            region_id=region,
            endpoint=f"ecs.{region}.aliyuncs.com",
        )
        self.client = Client(config)
        self.instance_id = cfg["instance_id"]
        self.desktop_access = dict(cfg.get("desktop_access") or {})
        self.desktop_access_enabled = bool(
            self.desktop_access.get("enabled", False)
        )
        management_sources = self.desktop_access.get("management_source_cidrs") or []
        if isinstance(management_sources, str):
            management_sources = [
                item.strip() for item in management_sources.split(",") if item.strip()
            ]
        self.desktop_access["management_source_cidrs"] = list(management_sources)
        self._desktop_access_lock = threading.RLock()

    def _instance(self):
        req = ecs_models.DescribeInstancesRequest(
            region_id=self.region,
            instance_ids=json.dumps([self.instance_id]),
        )
        resp = self.client.describe_instances(req)
        instances = list(resp.body.instances.instance or [])
        if not instances:
            raise RuntimeError(f"阿里云实例不存在: {self.instance_id}")
        if len(instances) != 1:
            raise RuntimeError(
                f"阿里云实例查询返回 {len(instances)} 条，期望唯一实例"
            )
        return instances[0]

    def status(self) -> tuple[str, str]:
        ins = self._instance()
        state = (ins.status or "").upper()
        eip = str(
            getattr(getattr(ins, "eip_address", None), "ip_address", "") or ""
        )
        if eip:
            return state, eip
        ips = (ins.public_ip_address.ip_address if ins.public_ip_address else []) or []
        if ips:
            return state, ips[0]
        return state, ""

    @staticmethod
    def _string_attr(value, name: str) -> str:
        item = getattr(value, name, "")
        return item if isinstance(item, str) else ""

    @staticmethod
    def _is_accept(permission) -> bool:
        policy = AliyunECS._string_attr(permission, "policy").lower()
        return policy in {"", "accept"}

    @staticmethod
    def _next_token(response) -> str:
        value = getattr(getattr(response, "body", None), "next_token", "")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _exact_ipv4_tcp_accept(permission, source: str, port: int) -> bool:
        """Match one plain four-tuple rule, never an address/port-list rule."""
        return (
            AliyunECS._is_accept(permission)
            and AliyunECS._string_attr(permission, "ip_protocol").lower() == "tcp"
            and AliyunECS._string_attr(permission, "port_range")
            == f"{int(port)}/{int(port)}"
            and AliyunECS._string_attr(permission, "source_cidr_ip") == source
            and AliyunECS._string_attr(permission, "nic_type").lower()
            == "intranet"
            and not AliyunECS._string_attr(permission, "ipv_6source_cidr_ip")
            and not AliyunECS._string_attr(permission, "source_group_id")
            and not AliyunECS._string_attr(
                permission, "source_group_owner_account"
            )
            and not AliyunECS._string_attr(permission, "source_prefix_list_id")
            and not AliyunECS._string_attr(permission, "port_range_list_id")
            and not AliyunECS._string_attr(permission, "source_port_range")
            and not AliyunECS._string_attr(permission, "dest_cidr_ip")
            and not AliyunECS._string_attr(permission, "ipv_6dest_cidr_ip")
            and not AliyunECS._string_attr(permission, "dest_group_id")
            and not AliyunECS._string_attr(permission, "dest_prefix_list_id")
        )

    def _desktop_description(self, tid: str, end_at_ms: int) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{24}", str(tid)):
            raise ValueError("桌面入站规则的比赛 tid 无效")
        deadline = int(end_at_ms)
        if deadline <= 0:
            raise ValueError("桌面入站规则缺少截止时间")
        prefix = str(
            self.desktop_access.get("description_prefix")
            or "NOI-DESKTOP-DIRECT-MANAGED"
        ).strip()
        return (
            f"{prefix} instance={self.instance_id} "
            f"tid={tid} end={deadline}"
        )

    def _configured_security_group_id(self) -> str:
        return str(self.desktop_access.get("security_group_id") or "")

    def _security_group_id(self, instance) -> str:
        configured = self._configured_security_group_id()
        groups_node = getattr(instance, "security_group_ids", None)
        groups = list(getattr(groups_node, "security_group_id", None) or [])
        if configured not in groups:
            raise RuntimeError(
                "配置的桌面安全组未绑定到比赛 ECS，拒绝修改入站规则"
            )
        if len(groups) != 1:
            raise RuntimeError(
                "比赛 ECS 还绑定其他安全组，无法证明 TCP/80 已失败关闭"
            )
        return configured

    @staticmethod
    def _instance_eip(instance) -> str:
        return str(
            getattr(getattr(instance, "eip_address", None), "ip_address", "")
            or ""
        )

    def _instances_in_security_group(self, security_group_id: str) -> list:
        instances = []
        next_token = ""
        seen_tokens: set[str] = set()
        while True:
            response = self.client.describe_instances(
                ecs_models.DescribeInstancesRequest(
                    region_id=self.region,
                    security_group_id=security_group_id,
                    max_results=100,
                    next_token=next_token or None,
                )
            )
            instances.extend(
                list(response.body.instances.instance or [])
            )
            next_token = self._next_token(response)
            if not next_token:
                return instances
            if next_token in seen_tokens:
                raise RuntimeError("安全组实例分页 token 重复，拒绝开放")
            seen_tokens.add(next_token)

    def _secondary_enis_in_security_group(self, security_group_id: str) -> list:
        interfaces = []
        next_token = ""
        seen_tokens: set[str] = set()
        while True:
            response = self.client.describe_network_interfaces(
                ecs_models.DescribeNetworkInterfacesRequest(
                    region_id=self.region,
                    security_group_id=security_group_id,
                    max_results=500,
                    next_token=next_token or None,
                )
            )
            node = getattr(response.body, "network_interface_sets", None)
            interfaces.extend(
                list(getattr(node, "network_interface_set", None) or [])
            )
            next_token = self._next_token(response)
            if not next_token:
                return interfaces
            if next_token in seen_tokens:
                raise RuntimeError("安全组 ENI 分页 token 重复，拒绝开放")
            seen_tokens.add(next_token)

    def _secondary_enis_for_instance(self) -> list:
        """Return every secondary ENI on the target, regardless of its SG."""
        interfaces = []
        next_token = ""
        seen_tokens: set[str] = set()
        while True:
            response = self.client.describe_network_interfaces(
                ecs_models.DescribeNetworkInterfacesRequest(
                    region_id=self.region,
                    instance_id=self.instance_id,
                    type="Secondary",
                    max_results=500,
                    next_token=next_token or None,
                )
            )
            node = getattr(response.body, "network_interface_sets", None)
            for interface in list(
                getattr(node, "network_interface_set", None) or []
            ):
                # The request filter should already exclude Primary. Keep this
                # defensive check because treating the target's mandatory main
                # ENI as auxiliary would make every real direct open fail.
                if self._string_attr(interface, "type").lower() != "primary":
                    interfaces.append(interface)
            next_token = self._next_token(response)
            if not next_token:
                return interfaces
            if next_token in seen_tokens:
                raise RuntimeError("目标 ECS ENI 分页 token 重复，拒绝开放")
            seen_tokens.add(next_token)

    def _validated_desktop_instance(self, security_group_id: str):
        """Prove this is a normal SG dedicated to the target primary ENI."""
        response = self.client.describe_security_groups(
            ecs_models.DescribeSecurityGroupsRequest(
                region_id=self.region,
                security_group_id=security_group_id,
                max_results=100,
            )
        )
        groups_node = getattr(response.body, "security_groups", None)
        groups = list(getattr(groups_node, "security_group", None) or [])
        exact_groups = [
            group
            for group in groups
            if self._string_attr(group, "security_group_id") == security_group_id
        ]
        if len(exact_groups) != 1:
            raise RuntimeError("配置的桌面安全组不存在或查询结果不唯一")
        group_type = self._string_attr(
            exact_groups[0], "security_group_type"
        ).lower()
        if group_type != "normal":
            raise RuntimeError("桌面直连只允许专用普通 basic 安全组")
        if bool(getattr(exact_groups[0], "service_managed", False)):
            raise RuntimeError("桌面直连不允许服务托管安全组")

        instances = self._instances_in_security_group(security_group_id)
        if len(instances) != 1:
            raise RuntimeError(
                "比赛安全组不是目标 ECS 专属，拒绝开放 TCP/80"
            )
        instance = instances[0]
        if self._string_attr(instance, "instance_id") != self.instance_id:
            raise RuntimeError(
                "比赛安全组绑定了非目标 ECS，拒绝开放 TCP/80"
            )
        self._security_group_id(instance)
        if self._secondary_enis_in_security_group(security_group_id):
            raise RuntimeError(
                "比赛安全组还绑定辅助 ENI，无法证明主网卡专属"
            )
        if self._secondary_enis_for_instance():
            raise RuntimeError(
                "比赛 ECS 还绑定辅助 ENI，其他安全组可能绕过桌面关闭策略"
            )
        if not self._instance_eip(instance):
            raise RuntimeError("比赛 ECS 未绑定固定 EIP，拒绝开放桌面")
        return instance

    def _ingress(self, security_group_id: str) -> list:
        rules = []
        next_token = ""
        seen_tokens: set[str] = set()
        while True:
            response = self.client.describe_security_group_attribute(
                ecs_models.DescribeSecurityGroupAttributeRequest(
                    region_id=self.region,
                    security_group_id=security_group_id,
                    direction="ingress",
                    # The contest ECS is in a VPC. EIP traffic is filtered by
                    # VPC NIC rules; Alibaba Cloud supports ``intranet`` here.
                    nic_type="intranet",
                    max_results=1000,
                    next_token=next_token or None,
                )
            )
            rules.extend(list(response.body.permissions.permission or []))
            next_token = self._next_token(response)
            if not next_token:
                return rules
            if next_token in seen_tokens:
                raise RuntimeError("安全组规则分页 token 重复，拒绝开放")
            seen_tokens.add(next_token)

    def _desktop_snapshot(
        self,
        *,
        tid: str = "",
        end_at_ms: int = 0,
        validate_topology: bool = True,
    ) -> dict:
        if not self.desktop_access_enabled:
            return {
                "enabled": False,
                "open": False,
                "closed": True,
                "healthy": True,
                "managed_count": 0,
                "conflict_count": 0,
            }
        security_group_id = self._configured_security_group_id()
        instance = (
            self._validated_desktop_instance(security_group_id)
            if validate_topology
            else None
        )
        instance_state = self._string_attr(instance, "status").upper() if instance is not None else "UNKNOWN"
        if instance_state not in {"PENDING", "RUNNING", "STARTING", "STOPPING", "STOPPED"}:
            instance_state = "UNKNOWN"
        rules = self._ingress(security_group_id)
        port = int(self.desktop_access.get("port", 80))
        source = str(self.desktop_access["source_cidr"])
        priority = int(self.desktop_access.get("priority", 20))
        management_sources = {
            str(item)
            for item in self.desktop_access.get("management_source_cidrs", [])
        }
        prefix = str(
            self.desktop_access.get("description_prefix")
            or "NOI-DESKTOP-DIRECT-MANAGED"
        ).strip()
        marker = f"{prefix} instance={self.instance_id} "
        desired_description = (
            self._desktop_description(tid, int(end_at_ms)) if tid else ""
        )
        managed = []
        conflicts = []
        desired = []
        management_seen: set[tuple[str, int]] = set()
        for rule in rules:
            description = self._string_attr(rule, "description")
            is_managed = description.startswith(marker)
            if is_managed:
                managed.append(rule)
            rule_source = self._string_attr(rule, "source_cidr_ip")
            management_match = None
            if not is_managed:
                for management_source in management_sources:
                    for management_port in (22, port):
                        candidate = (management_source, management_port)
                        if self._exact_ipv4_tcp_accept(
                            rule, management_source, management_port
                        ):
                            management_match = candidate
                            break
                    if management_match is not None:
                        break
                if (
                    management_match is None
                    or management_match in management_seen
                ):
                    # This dedicated SG has an exact allow-list contract. Any
                    # other ingress rule (Drop, numeric protocol, prefix/port
                    # list, source group, broad range, or duplicate) is drift.
                    conflicts.append(rule)
                else:
                    management_seen.add(management_match)
            try:
                rule_priority = int(getattr(rule, "priority", -1))
            except (TypeError, ValueError):
                rule_priority = -1
            if (
                is_managed
                and self._exact_ipv4_tcp_accept(rule, source, port)
                and rule_priority == priority
                and (not desired_description or description == desired_description)
            ):
                desired.append(rule)
        managed_ids = [
            self._string_attr(rule, "security_group_rule_id") for rule in managed
        ]
        management_expected = {
            (management_source, management_port)
            for management_source in management_sources
            for management_port in (22, port)
        }
        management_missing = management_expected - management_seen
        management_healthy = not management_missing
        desired_open = (
            len(managed) == 1
            and len(desired) == 1
            and not conflicts
            and management_healthy
        )
        closed = not managed and not conflicts
        eip = ""
        if instance is not None:
            eip = self._instance_eip(instance)
        return {
            "enabled": True,
            "open": desired_open,
            "closed": closed,
            "healthy": desired_open if tid else closed and management_healthy,
            "managed_count": len(managed),
            "conflict_count": len(conflicts),
            "management_healthy": management_healthy,
            "management_missing_count": len(management_missing),
            "managed_rule_ids": managed_ids,
            "security_group_id": security_group_id,
            "eip": eip,
            "instance_state": instance_state,
            "_managed": managed,
        }

    def desktop_access_status(
        self, *, tid: str = "", end_at_ms: int = 0
    ) -> dict:
        """Return a sanitized snapshot without mutating any security-group rule."""
        with self._desktop_access_lock:
            status = self._desktop_snapshot(tid=tid, end_at_ms=end_at_ms)
        return {key: value for key, value in status.items() if not key.startswith("_")}

    def _revoke_snapshot(self, snapshot: dict) -> None:
        managed = list(snapshot.get("_managed") or [])
        if not managed:
            return
        rule_ids = [
            self._string_attr(rule, "security_group_rule_id") for rule in managed
        ]
        if not all(rule_ids):
            raise RuntimeError(
                "自管桌面入站规则缺少稳定 rule ID，拒绝模糊删除"
            )
        for start in range(0, len(rule_ids), 100):
            pending = rule_ids[start : start + 100]
            while pending:
                try:
                    self.client.revoke_security_group(
                        ecs_models.RevokeSecurityGroupRequest(
                            region_id=self.region,
                            security_group_id=str(snapshot["security_group_id"]),
                            security_group_rule_id=pending,
                        )
                    )
                    break
                except Exception:
                    # Since 2024-07-08 Aliyun rejects a missing rule ID. Re-read
                    # and retry only the IDs that still exist. If no ID vanished,
                    # the error was not proven to be an idempotent race.
                    current = self._desktop_snapshot(validate_topology=False)
                    existing = set(current.get("managed_rule_ids") or [])
                    remaining = [
                        rule_id for rule_id in pending if rule_id in existing
                    ]
                    if not remaining:
                        break
                    if remaining == pending:
                        raise
                    pending = remaining

    def revoke_desktop_access(self) -> dict:
        """Idempotently remove only orchestrator-owned desktop rules."""
        if not self.desktop_access_enabled:
            return self.desktop_access_status()
        with self._desktop_access_lock:
            # Closing must not depend on the instance still having exactly the
            # expected SG topology.  Remove owned IDs from the configured SG
            # first, then validate topology and report any drift separately.
            snapshot = self._desktop_snapshot(validate_topology=False)
            try:
                self._revoke_snapshot(snapshot)
            except Exception:
                # Another reconciler may have removed the same stable IDs
                # after our read. Re-read before declaring an idempotent close
                # failed; never infer success from an API error alone.
                raced = self._desktop_snapshot(validate_topology=False)
                if raced["managed_count"]:
                    raise
            for _ in range(20):
                current = self._desktop_snapshot(validate_topology=False)
                if current["closed"]:
                    # This second read intentionally occurs after cleanup.
                    # Extra/detached SGs are unsafe, but must never prevent us
                    # from first revoking our rule on the configured SG.
                    verified = self._desktop_snapshot()
                    if verified["closed"]:
                        return {
                            key: value
                            for key, value in verified.items()
                            if not key.startswith("_")
                        }
                if current["conflict_count"]:
                    raise RuntimeError(
                        "安全组存在非编排服务管理的公网桌面入站规则"
                    )
                time.sleep(0.25)
            raise RuntimeError("桌面入站规则撤销后未收敛")

    def ensure_desktop_access(self, *, tid: str, end_at_ms: int) -> dict:
        """Publish exactly one managed TCP rule for one unexpired contest."""
        if not self.desktop_access_enabled:
            return self.desktop_access_status()
        with self._desktop_access_lock:
            try:
                return self._ensure_desktop_access_locked(
                    tid=str(tid), end_at_ms=int(end_at_ms)
                )
            except Exception as open_error:
                # Authorize can have an ambiguous commit (for example a
                # timeout after Aliyun accepted it), and topology can drift
                # between the pre-check and post-check.  Never return/raise
                # from this public mutation while leaving an owned rule merely
                # because the normal topology validator can no longer run.
                try:
                    self._revoke_owned_after_failed_open()
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "桌面入站规则开放失败，且自管规则未确认回收: "
                        f"{cleanup_error}"
                    ) from open_error
                raise

    def _revoke_owned_after_failed_open(self) -> None:
        """Remove owned IDs without requiring the instance/SG topology."""
        snapshot = self._desktop_snapshot(validate_topology=False)
        try:
            self._revoke_snapshot(snapshot)
        except Exception:
            raced = self._desktop_snapshot(validate_topology=False)
            if raced["managed_count"]:
                raise
        for _ in range(20):
            current = self._desktop_snapshot(validate_topology=False)
            if not current["managed_count"]:
                return
            time.sleep(0.25)
        raise RuntimeError("开放失败后的桌面入站规则回收未收敛")

    def _ensure_desktop_access_locked(self, *, tid: str, end_at_ms: int) -> dict:
        if int(end_at_ms) <= int(time.time() * 1000):
            self.revoke_desktop_access()
            raise RuntimeError("比赛已截止，拒绝开放桌面入站规则")
        snapshot = self._desktop_snapshot(tid=tid, end_at_ms=end_at_ms)
        if snapshot["conflict_count"]:
            raise RuntimeError("安全组存在非编排服务管理的公网桌面入站规则")
        if not snapshot["management_healthy"]:
            raise RuntimeError("安全组缺少 OJ /32 的 TCP 22/80 管理与回退规则")
        if snapshot["open"]:
            if int(end_at_ms) <= int(time.time() * 1000):
                self.revoke_desktop_access()
                raise RuntimeError("比赛在验收入站规则时已截止，已撤销")
            return {
                key: value
                for key, value in snapshot.items()
                if not key.startswith("_")
            }
        self._revoke_snapshot(snapshot)
        if snapshot.get("_managed"):
            for _ in range(20):
                cleared = self._desktop_snapshot()
                if not cleared["managed_count"]:
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("旧桌面入站规则未收敛，拒绝开放新规则")
        if int(end_at_ms) <= int(time.time() * 1000):
            self.revoke_desktop_access()
            raise RuntimeError("比赛已截止，拒绝授权桌面入站规则")
        description = self._desktop_description(tid, int(end_at_ms))
        permission = ecs_models.AuthorizeSecurityGroupRequestPermissions(
            ip_protocol="TCP",
            port_range=(
                f"{int(self.desktop_access.get('port', 80))}/"
                f"{int(self.desktop_access.get('port', 80))}"
            ),
            source_cidr_ip=str(self.desktop_access["source_cidr"]),
            policy="accept",
            priority=str(int(self.desktop_access.get("priority", 20))),
            nic_type="intranet",
            description=description,
        )
        self.client.authorize_security_group(
            ecs_models.AuthorizeSecurityGroupRequest(
                region_id=self.region,
                security_group_id=str(snapshot["security_group_id"]),
                permissions=[permission],
            )
        )
        for _ in range(20):
            current = self._desktop_snapshot(tid=tid, end_at_ms=end_at_ms)
            if current["open"]:
                if int(end_at_ms) <= int(time.time() * 1000):
                    self.revoke_desktop_access()
                    raise RuntimeError(
                        "比赛在入站规则收敛时已截止，已撤销"
                    )
                return {
                    key: value
                    for key, value in current.items()
                    if not key.startswith("_")
                }
            if current["conflict_count"]:
                self._revoke_snapshot(current)
                raise RuntimeError("开放桌面入站规则时检测到未托管规则")
            if not current["management_healthy"]:
                self._revoke_snapshot(current)
                raise RuntimeError("开放桌面入站规则时 OJ /32 管理规则发生漂移")
            time.sleep(0.25)
        final = self._desktop_snapshot(tid=tid, end_at_ms=end_at_ms)
        self._revoke_snapshot(final)
        raise RuntimeError("桌面入站规则开放后未收敛")

    def start(self):
        return self.client.start_instance(
            ecs_models.StartInstanceRequest(instance_id=self.instance_id)
        )

    def stop(self):
        return self.client.stop_instance(
            ecs_models.StopInstanceRequest(
                instance_id=self.instance_id,
                force_stop=False,
                stopped_mode="StopCharging",
            )
        )

    def wait_running(self, timeout: int = 300) -> str:
        deadline = time.monotonic() + timeout
        last_state = "UNKNOWN"
        while time.monotonic() < deadline:
            last_state, ip = self.status()
            if last_state == "RUNNING" and ip:
                return ip
            time.sleep(5)
        raise TimeoutError(f"等待阿里云实例开机超时，最后状态 {last_state}")
