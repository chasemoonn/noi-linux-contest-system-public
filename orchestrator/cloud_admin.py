"""One-off, tightly scoped deployment actions for the contest ECS."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import time

from alibabacloud_ecs20140526 import models as ecs_models
from alibabacloud_ecs20140526.client import Client
from alibabacloud_tea_openapi import models as open_api_models
import paramiko

from services.aliyun import AliyunECS
from services.config import load_config
from services.remote import Remote


REGION = os.environ["ALIYUN_REGION_ID"]
INSTANCE_ID = os.environ["ALIYUN_INSTANCE_ID"]
SOURCE_CIDR = os.environ["CONTEST_SOURCE_CIDR"]
LEGACY_SOURCE_CIDR = os.environ.get("LEGACY_CONTEST_SOURCE_CIDR", "")
KEY_PATH = "/app/keys/contest.pem"
HOST_KEY_SHA256 = os.environ["CONTEST_SSH_HOST_KEY_SHA256"]
DEPLOYMENT_LABEL = os.environ.get("NOI_DEPLOYMENT_LABEL", "hydro-noi")


def cloud_config() -> dict:
    return {
        "access_key_id": os.environ["ALIYUN_ACCESS_KEY_ID"],
        "access_key_secret": os.environ["ALIYUN_ACCESS_KEY_SECRET"],
        "region_id": REGION,
        "instance_id": INSTANCE_ID,
    }


def client() -> Client:
    return Client(
        open_api_models.Config(
            access_key_id=os.environ["ALIYUN_ACCESS_KEY_ID"],
            access_key_secret=os.environ["ALIYUN_ACCESS_KEY_SECRET"],
            region_id=REGION,
            endpoint=f"ecs.{REGION}.aliyuncs.com",
        )
    )


def describe_instance(api: Client):
    response = api.describe_instances(
        ecs_models.DescribeInstancesRequest(
            region_id=REGION,
            instance_ids=json.dumps([INSTANCE_ID]),
        )
    )
    instances = list(response.body.instances.instance or [])
    if len(instances) != 1:
        raise RuntimeError(f"expected one ECS instance, found {len(instances)}")
    return instances[0]


def contest_security_group(instance) -> str:
    groups = list(instance.security_group_ids.security_group_id or [])
    if len(groups) != 1:
        raise RuntimeError(
            "contest ECS must have exactly one security group for fail-closed desktop access"
        )
    return str(groups[0])


def describe_ingress_rules(api: Client, security_group_id: str) -> list:
    """Read every ingress page; a partial snapshot is never an audit result."""
    rules = []
    next_token = ""
    seen_tokens: set[str] = set()
    while True:
        response = api.describe_security_group_attribute(
            ecs_models.DescribeSecurityGroupAttributeRequest(
                region_id=REGION,
                security_group_id=security_group_id,
                direction="ingress",
                nic_type="intranet",
                max_results=1000,
                next_token=next_token or None,
            )
        )
        rules.extend(list(response.body.permissions.permission or []))
        value = getattr(response.body, "next_token", "")
        next_token = value if isinstance(value, str) else ""
        if not next_token:
            return rules
        if next_token in seen_tokens:
            raise RuntimeError("security-group pagination returned a repeated token")
        seen_tokens.add(next_token)


def status() -> dict:
    instance = describe_instance(client())
    eip = getattr(instance, "eip_address", None)
    eip_address = str(getattr(eip, "ip_address", "") or "")
    public = [eip_address] if eip_address else list(
        getattr(instance.public_ip_address, "ip_address", None) or []
    )
    groups = list(instance.security_group_ids.security_group_id or [])
    return {
        "instance_id": instance.instance_id,
        "status": instance.status,
        "public_ip": public[0] if public else "",
        "eip": eip_address,
        "security_groups": groups,
    }


def authorize_oj_only() -> dict:
    api = client()
    instance = describe_instance(api)
    security_group_id = contest_security_group(instance)
    current = describe_ingress_rules(api, security_group_id)
    added = []
    for port in (22, 80):
        port_range = f"{port}/{port}"
        exists = any(
            str(item.ip_protocol or "").lower() == "tcp"
            and item.port_range == port_range
            and item.source_cidr_ip == SOURCE_CIDR
            and str(item.policy or "accept").lower() == "accept"
            for item in current
        )
        if exists:
            continue
        permission = ecs_models.AuthorizeSecurityGroupRequestPermissions(
            ip_protocol="TCP",
            port_range=port_range,
            source_cidr_ip=SOURCE_CIDR,
            policy="accept",
            priority="10",
            nic_type="intranet",
            description="NOI orchestrator host only",
        )
        api.authorize_security_group(
            ecs_models.AuthorizeSecurityGroupRequest(
                region_id=REGION,
                security_group_id=security_group_id,
                permissions=[permission],
            )
        )
        added.append(port)
    return {
        "security_group_id": security_group_id,
        "source": SOURCE_CIDR,
        "added_ports": added,
    }


def ingress_rules() -> dict:
    api = client()
    instance = describe_instance(api)
    security_group_id = contest_security_group(instance)
    rules = []
    for item in describe_ingress_rules(api, security_group_id):
        rules.append(
            {
                "rule_id": getattr(item, "security_group_rule_id", None),
                "protocol": item.ip_protocol,
                "ports": item.port_range,
                "source": (
                    getattr(item, "source_cidr_ip", "")
                    or getattr(item, "ipv6_source_cidr_ip", "")
                    or getattr(item, "ipv_6source_cidr_ip", "")
                    or getattr(item, "source_group_id", "")
                    or getattr(item, "source_prefix_list_id", "")
                ),
                "policy": item.policy,
                "priority": item.priority,
                "description": item.description,
            }
        )
    return {"security_group_id": security_group_id, "ingress": rules}


def revoke_legacy_source() -> dict:
    if not LEGACY_SOURCE_CIDR:
        return {"source": "", "removed_ports": []}
    if LEGACY_SOURCE_CIDR == SOURCE_CIDR:
        raise RuntimeError("legacy source CIDR must differ from the active source")

    api = client()
    instance = describe_instance(api)
    security_group_id = contest_security_group(instance)
    current = describe_ingress_rules(api, security_group_id)
    matches = [
        item
        for item in current
        if any(
            str(item.ip_protocol or "").lower() == "tcp"
            and item.port_range == f"{port}/{port}"
            and item.source_cidr_ip == LEGACY_SOURCE_CIDR
            and str(item.policy or "accept").lower() == "accept"
            for port in (22, 80)
        )
    ]
    rule_ids = [
        str(getattr(item, "security_group_rule_id", "") or "")
        for item in matches
    ]
    if matches and not all(rule_ids):
        raise RuntimeError("legacy security group rule is missing a rule ID")
    if rule_ids:
        api.revoke_security_group(
            ecs_models.RevokeSecurityGroupRequest(
                region_id=REGION,
                security_group_id=security_group_id,
                security_group_rule_id=rule_ids,
            )
        )
    return {
        "security_group_id": security_group_id,
        "source": LEGACY_SOURCE_CIDR,
        "removed_rule_ids": rule_ids,
    }


def ensure_running() -> dict:
    config_path = os.environ.get("ORCHESTRATOR_CONFIG", "config.yaml")
    runtime = load_config(config_path)
    if str(runtime["cloud"]["provider"]).lower() != "aliyun":
        raise RuntimeError("cloud_admin start only supports the configured Aliyun ECS")
    aliyun = dict(runtime["cloud"]["aliyun"])
    if (
        str(aliyun.get("instance_id")) != INSTANCE_ID
        or str(aliyun.get("region_id")) != REGION
    ):
        raise RuntimeError("cloud_admin environment and runtime config target differ")
    ecs = AliyunECS(aliyun)
    if ecs.desktop_access_enabled:
        # Deployment/diagnostic CLI must obey the same stale-rule barrier as
        # the teacher UI.  Do not resurrect a rule left by an earlier failed
        # close; the orchestrator reconciler owns any subsequent re-open.
        ecs.revoke_desktop_access()
    state, ip = ecs.status()
    if state == "STOPPED":
        ecs.start()
        ip = ecs.wait_running()
        state = "RUNNING"
    elif state in {"STARTING", "PENDING"}:
        ip = ecs.wait_running()
        state = "RUNNING"
    elif state != "RUNNING":
        raise RuntimeError(f"contest ECS cannot be started from state {state}")
    return {"status": state, "public_ip": ip}


def public_key_text() -> str:
    key = paramiko.RSAKey.from_private_key_file(KEY_PATH)
    return f"{key.get_name()} {key.get_base64()} noi-orchestrator@{DEPLOYMENT_LABEL}"


def install_public_key() -> dict:
    api = client()
    state = status()
    if state["status"].upper() != "RUNNING" or not state["public_ip"]:
        raise RuntimeError("contest ECS must be running before installing the key")
    public_key = public_key_text()
    quoted = shlex.quote(public_key)
    command = f"""#!/bin/sh
set -eu
install -d -m 0700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 0600 /root/.ssh/authorized_keys
grep -Fqx -- {quoted} /root/.ssh/authorized_keys || printf '%s\\n' {quoted} >> /root/.ssh/authorized_keys
"""
    response = api.run_command(
        ecs_models.RunCommandRequest(
            region_id=REGION,
            type="RunShellScript",
            command_content=base64.b64encode(command.encode()).decode(),
            content_encoding="Base64",
            instance_id=[INSTANCE_ID],
            timeout=60,
            username="root",
            keep_command=False,
            name="noi-install-orchestrator-public-key",
        )
    )
    invoke_id = response.body.invoke_id
    remote = Remote(
        state["public_ip"],
        "root",
        KEY_PATH,
        strict_host_key=True,
        host_key_sha256=HOST_KEY_SHA256,
    )
    last_error = ""
    for _ in range(30):
        try:
            proof = remote.run("printf 'noi-key-ok'").strip()
            if proof == "noi-key-ok":
                return {
                    "invoke_id": invoke_id,
                    "public_ip": state["public_ip"],
                    "ssh_verified": True,
                }
        except Exception as exc:  # Cloud Assistant is asynchronous.
            last_error = str(exc).splitlines()[-1][:300]
        time.sleep(4)
    raise RuntimeError(f"public key was not usable after Cloud Assistant run: {last_error}")


def stop() -> dict:
    ecs = AliyunECS(cloud_config())
    state, _ = ecs.status()
    if state == "RUNNING":
        ecs.stop()
        return {"requested": True, "previous_status": state}
    return {"requested": False, "previous_status": state}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "status",
            "rules",
            "authorize",
            "revoke-legacy",
            "start",
            "install-key",
            "stop",
        ),
    )
    action = parser.parse_args().action
    result = {
        "status": status,
        "rules": ingress_rules,
        "authorize": authorize_oj_only,
        "revoke-legacy": revoke_legacy_source,
        "start": ensure_running,
        "install-key": install_public_key,
        "stop": stop,
    }[action]()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
