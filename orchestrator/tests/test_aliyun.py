import unittest
from types import SimpleNamespace
import time
from unittest.mock import MagicMock, patch

from services.aliyun import AliyunECS


class AliyunTests(unittest.TestCase):
    @staticmethod
    def _desktop_fixture(client_type, extra_rules=()):
        instance = SimpleNamespace(
            instance_id="i-test",
            status="Running",
            public_ip_address=None,
            eip_address=SimpleNamespace(ip_address="198.51.100.10"),
            security_group_ids=SimpleNamespace(security_group_id=["sg-test"]),
        )
        rules = [
            SimpleNamespace(
                ip_protocol="TCP",
                port_range=f"{port}/{port}",
                source_cidr_ip="203.0.113.7/32",
                policy="Accept",
                priority="10",
                nic_type="intranet",
                description="NOI orchestrator host only",
                security_group_rule_id=f"sgr-management-{port}",
            )
            for port in (22, 80)
        ] + list(extra_rules)
        api = client_type.return_value
        api.describe_instances.return_value = SimpleNamespace(
            body=SimpleNamespace(
                instances=SimpleNamespace(instance=[instance]),
                next_token="",
            )
        )
        api.describe_security_groups.return_value = SimpleNamespace(
            body=SimpleNamespace(
                security_groups=SimpleNamespace(
                    security_group=[
                        SimpleNamespace(
                            security_group_id="sg-test",
                            security_group_type="normal",
                            service_managed=False,
                        )
                    ]
                ),
                next_token="",
            )
        )
        api.describe_network_interfaces.return_value = SimpleNamespace(
            body=SimpleNamespace(
                network_interface_sets=SimpleNamespace(
                    network_interface_set=[]
                ),
                next_token="",
            )
        )

        def describe_rules(_request):
            return SimpleNamespace(
                body=SimpleNamespace(
                    permissions=SimpleNamespace(permission=list(rules)),
                    next_token="",
                )
            )

        def authorize(request):
            permission = request.permissions[0]
            rules.append(
                SimpleNamespace(
                    ip_protocol=permission.ip_protocol,
                    port_range=permission.port_range,
                    source_cidr_ip=permission.source_cidr_ip,
                    policy=permission.policy,
                    priority=permission.priority,
                    nic_type=permission.nic_type,
                    description=permission.description,
                    security_group_rule_id="sgr-managed",
                )
            )

        def revoke(request):
            removed = set(request.security_group_rule_id)
            rules[:] = [
                rule
                for rule in rules
                if rule.security_group_rule_id not in removed
            ]

        api.describe_security_group_attribute.side_effect = describe_rules
        api.authorize_security_group.side_effect = authorize
        api.revoke_security_group.side_effect = revoke
        cfg = {
            "access_key_id": "test-id",
            "access_key_secret": "test-secret",
            "region_id": "cn-hangzhou",
            "instance_id": "i-test",
            "desktop_access": {
                "enabled": True,
                "security_group_id": "sg-test",
                "source_cidr": "0.0.0.0/0",
                "management_source_cidrs": ["203.0.113.7/32"],
                "port": 80,
                "priority": 20,
                "description_prefix": "NOI-DESKTOP-DIRECT-MANAGED",
            },
        }
        return AliyunECS(cfg), api, rules

    @patch("services.aliyun.Client")
    def test_describe_instances_includes_required_region_id(self, client_type):
        instance = MagicMock()
        instance.status = "Stopped"
        instance.public_ip_address = None
        instance.eip_address = None
        response = MagicMock()
        response.body.instances.instance = [instance]
        client_type.return_value.describe_instances.return_value = response
        ecs = AliyunECS(
            {
                "access_key_id": "test-id",
                "access_key_secret": "test-secret",
                "region_id": "cn-hangzhou",
                "instance_id": "i-test",
            }
        )

        state, ip = ecs.status()

        request = client_type.return_value.describe_instances.call_args.args[0]
        self.assertEqual(request.region_id, "cn-hangzhou")
        self.assertEqual(state, "STOPPED")
        self.assertEqual(ip, "")

    @patch("services.aliyun.Client")
    def test_status_uses_eip_after_public_ip_conversion(self, client_type):
        instance = MagicMock()
        instance.status = "Running"
        instance.public_ip_address = None
        instance.eip_address.ip_address = "203.0.113.8"
        response = MagicMock()
        response.body.instances.instance = [instance]
        client_type.return_value.describe_instances.return_value = response
        ecs = AliyunECS(
            {
                "access_key_id": "test-id",
                "access_key_secret": "test-secret",
                "region_id": "cn-hangzhou",
                "instance_id": "i-test",
            }
        )

        self.assertEqual(ecs.status(), ("RUNNING", "203.0.113.8"))

    @patch("services.aliyun.Client")
    def test_status_prefers_fixed_eip_if_both_address_fields_exist(self, client_type):
        instance = MagicMock()
        instance.status = "Running"
        instance.public_ip_address.ip_address = ["198.51.100.99"]
        instance.eip_address.ip_address = "203.0.113.8"
        response = MagicMock()
        response.body.instances.instance = [instance]
        client_type.return_value.describe_instances.return_value = response
        ecs = AliyunECS(
            {
                "access_key_id": "test-id",
                "access_key_secret": "test-secret",
                "region_id": "cn-hangzhou",
                "instance_id": "i-test",
            }
        )

        self.assertEqual(ecs.status(), ("RUNNING", "203.0.113.8"))

    @patch("services.aliyun.Client")
    def test_desktop_rule_is_idempotent_and_preserves_management_rules(
        self, client_type
    ):
        ecs, api, rules = self._desktop_fixture(client_type)
        end_at_ms = int(time.time() * 1000) + 60_000

        first = ecs.ensure_desktop_access(tid="a" * 24, end_at_ms=end_at_ms)
        second = ecs.ensure_desktop_access(tid="a" * 24, end_at_ms=end_at_ms)

        self.assertTrue(first["open"])
        self.assertTrue(second["open"])
        api.authorize_security_group.assert_called_once()
        describe_request = api.describe_security_group_attribute.call_args.args[0]
        self.assertEqual(describe_request.nic_type, "intranet")
        self.assertEqual(describe_request.max_results, 1000)
        permission = api.authorize_security_group.call_args.args[0].permissions[0]
        self.assertEqual(permission.nic_type, "intranet")
        self.assertIn("instance=i-test", permission.description)
        self.assertEqual(len(rules), 3)
        self.assertEqual(
            {rule.security_group_rule_id for rule in rules},
            {"sgr-management-22", "sgr-management-80", "sgr-managed"},
        )

        closed = ecs.revoke_desktop_access()
        self.assertTrue(closed["closed"])
        self.assertEqual(
            {rule.security_group_rule_id for rule in rules},
            {"sgr-management-22", "sgr-management-80"},
        )

    @patch("services.aliyun.Client")
    def test_ambiguous_authorize_error_removes_committed_owned_rule(
        self, client_type
    ):
        ecs, api, rules = self._desktop_fixture(client_type)
        original_authorize = api.authorize_security_group.side_effect

        def commit_then_timeout(request):
            original_authorize(request)
            raise TimeoutError("authorize response lost")

        api.authorize_security_group.side_effect = commit_then_timeout

        with self.assertRaisesRegex(TimeoutError, "response lost"):
            ecs.ensure_desktop_access(
                tid="a" * 24, end_at_ms=int(time.time() * 1000) + 60_000
            )

        self.assertFalse(
            any("NOI-DESKTOP-DIRECT-MANAGED" in rule.description for rule in rules)
        )
        api.revoke_security_group.assert_called()

    @patch("services.aliyun.Client")
    def test_post_authorize_topology_drift_removes_owned_rule(self, client_type):
        ecs, api, rules = self._desktop_fixture(client_type)
        instance = api.describe_instances.return_value.body.instances.instance[0]
        original_authorize = api.authorize_security_group.side_effect

        def authorize_then_attach_other_group(request):
            original_authorize(request)
            instance.security_group_ids.security_group_id = ["sg-test", "sg-other"]

        api.authorize_security_group.side_effect = authorize_then_attach_other_group

        with self.assertRaisesRegex(RuntimeError, "其他安全组"):
            ecs.ensure_desktop_access(
                tid="a" * 24, end_at_ms=int(time.time() * 1000) + 60_000
            )

        self.assertFalse(
            any("NOI-DESKTOP-DIRECT-MANAGED" in rule.description for rule in rules)
        )
        api.revoke_security_group.assert_called()

    @patch("services.aliyun.Client")
    def test_unmanaged_public_port_80_rule_blocks_open_and_close(
        self, client_type
    ):
        conflict = SimpleNamespace(
            ip_protocol="TCP",
            port_range="80/80",
            source_cidr_ip="0.0.0.0/0",
            policy="Accept",
            priority="30",
            description="manual public web rule",
            security_group_rule_id="sgr-unmanaged",
        )
        ecs, api, _ = self._desktop_fixture(client_type, [conflict])
        end_at_ms = int(time.time() * 1000) + 60_000

        with self.assertRaisesRegex(RuntimeError, "非编排服务"):
            ecs.ensure_desktop_access(tid="b" * 24, end_at_ms=end_at_ms)
        with self.assertRaisesRegex(RuntimeError, "非编排服务"):
            ecs.revoke_desktop_access()

        api.authorize_security_group.assert_not_called()
        api.revoke_security_group.assert_not_called()

    @patch("services.aliyun.Client")
    def test_same_contest_can_reopen_after_owned_rule_was_revoked(
        self, client_type
    ):
        ecs, api, _ = self._desktop_fixture(client_type)
        end_at_ms = int(time.time() * 1000) + 60_000

        first = ecs.ensure_desktop_access(tid="a" * 24, end_at_ms=end_at_ms)
        closed = ecs.revoke_desktop_access()
        reopened = ecs.ensure_desktop_access(tid="a" * 24, end_at_ms=end_at_ms)

        self.assertTrue(first["open"])
        self.assertTrue(closed["closed"])
        self.assertTrue(reopened["open"])
        self.assertEqual(api.authorize_security_group.call_count, 2)
        for call in api.authorize_security_group.call_args_list:
            self.assertIsNone(call.args[0].client_token)

    @patch("services.aliyun.Client")
    def test_non_cidr_port_80_rule_also_blocks_unprovable_lifecycle(
        self, client_type
    ):
        source_group_rule = SimpleNamespace(
            ip_protocol="TCP",
            port_range="80/80",
            source_cidr_ip="",
            source_group_id="sg-source",
            policy="Accept",
            priority="30",
            description="unrelated source group",
            security_group_rule_id="sgr-source-group",
        )
        ecs, api, _ = self._desktop_fixture(client_type, [source_group_rule])

        with self.assertRaisesRegex(RuntimeError, "非编排服务"):
            ecs.ensure_desktop_access(
                tid="8" * 24,
                end_at_ms=int(time.time() * 1000) + 60_000,
            )

        api.authorize_security_group.assert_not_called()

    @patch("services.aliyun.Client")
    def test_drop_port_list_and_numeric_protocol_are_strict_conflicts(
        self, client_type
    ):
        cases = (
            SimpleNamespace(
                ip_protocol="TCP",
                port_range="80/80",
                source_cidr_ip="0.0.0.0/0",
                policy="Drop",
                priority="15",
                description="higher priority student drop",
                security_group_rule_id="sgr-drop",
            ),
            SimpleNamespace(
                ip_protocol="TCP",
                port_range="",
                port_range_list_id="prl-maybe-80",
                source_cidr_ip="0.0.0.0/0",
                policy="Accept",
                priority="30",
                description="port address book",
                security_group_rule_id="sgr-port-list",
            ),
            SimpleNamespace(
                ip_protocol="6",
                port_range="80/80",
                source_cidr_ip="0.0.0.0/0",
                policy="Accept",
                priority="30",
                description="numeric tcp",
                security_group_rule_id="sgr-numeric",
            ),
        )
        for rule in cases:
            with self.subTest(rule=rule.security_group_rule_id):
                ecs, api, _ = self._desktop_fixture(client_type, [rule])
                with self.assertRaisesRegex(RuntimeError, "非编排服务"):
                    ecs.ensure_desktop_access(
                        tid="7" * 24,
                        end_at_ms=int(time.time() * 1000) + 60_000,
                    )
                api.authorize_security_group.assert_not_called()
                client_type.reset_mock(return_value=True)

    @patch("services.aliyun.Client")
    def test_missing_oj_management_rule_blocks_open_but_not_owned_cleanup(
        self, client_type
    ):
        ecs, api, rules = self._desktop_fixture(client_type)
        rules[:] = [
            rule
            for rule in rules
            if rule.security_group_rule_id != "sgr-management-80"
        ]

        status = ecs.desktop_access_status()
        self.assertFalse(status["management_healthy"])
        self.assertEqual(status["management_missing_count"], 1)
        with self.assertRaisesRegex(RuntimeError, "管理与回退"):
            ecs.ensure_desktop_access(
                tid="9" * 24,
                end_at_ms=int(time.time() * 1000) + 60_000,
            )
        closed = ecs.revoke_desktop_access()

        self.assertTrue(closed["closed"])
        self.assertFalse(closed["management_healthy"])
        api.authorize_security_group.assert_not_called()

    @patch("services.aliyun.Client")
    def test_stale_managed_rule_is_replaced_by_current_contest(self, client_type):
        stale = SimpleNamespace(
            ip_protocol="TCP",
            port_range="80/80",
            source_cidr_ip="0.0.0.0/0",
            policy="Accept",
            priority="20",
            description=(
                "NOI-DESKTOP-DIRECT-MANAGED instance=i-test tid="
                + "c" * 24
                + " end=1"
            ),
            security_group_rule_id="sgr-stale",
        )
        ecs, api, rules = self._desktop_fixture(client_type, [stale])
        end_at_ms = int(time.time() * 1000) + 60_000

        status = ecs.ensure_desktop_access(tid="d" * 24, end_at_ms=end_at_ms)

        self.assertTrue(status["open"])
        api.revoke_security_group.assert_called_once()
        api.authorize_security_group.assert_called_once()
        self.assertNotIn(
            "sgr-stale", {rule.security_group_rule_id for rule in rules}
        )

    @patch("services.aliyun.Client")
    def test_expired_contest_never_opens_rule(self, client_type):
        ecs, api, _ = self._desktop_fixture(client_type)

        with self.assertRaisesRegex(RuntimeError, "已截止"):
            ecs.ensure_desktop_access(tid="e" * 24, end_at_ms=1)

        api.authorize_security_group.assert_not_called()

    @patch("services.aliyun.Client")
    def test_additional_attached_security_group_blocks_lifecycle(self, client_type):
        ecs, api, _ = self._desktop_fixture(client_type)
        instance = (
            api.describe_instances.return_value.body.instances.instance[0]
        )
        instance.security_group_ids.security_group_id.append("sg-other")

        with self.assertRaisesRegex(RuntimeError, "其他安全组"):
            ecs.ensure_desktop_access(
                tid="f" * 24,
                end_at_ms=int(time.time() * 1000) + 60_000,
            )

        # Failed-open cleanup deliberately reads the configured SG without
        # trusting the now-invalid attachment topology.
        api.describe_security_group_attribute.assert_called()
        api.authorize_security_group.assert_not_called()

    @patch("services.aliyun.Client")
    def test_close_removes_owned_rule_before_reporting_topology_drift(
        self, client_type
    ):
        ecs, api, rules = self._desktop_fixture(client_type)
        ecs.ensure_desktop_access(
            tid="1" * 24,
            end_at_ms=int(time.time() * 1000) + 60_000,
        )
        instance = api.describe_instances.return_value.body.instances.instance[0]
        instance.security_group_ids.security_group_id.append("sg-other")

        with self.assertRaisesRegex(RuntimeError, "其他安全组"):
            ecs.revoke_desktop_access()

        self.assertNotIn(
            "sgr-managed", {rule.security_group_rule_id for rule in rules}
        )

    @patch("services.aliyun.Client")
    def test_missing_rule_id_race_is_idempotent_only_after_reread(
        self, client_type
    ):
        ecs, api, rules = self._desktop_fixture(client_type)
        ecs.ensure_desktop_access(
            tid="2" * 24,
            end_at_ms=int(time.time() * 1000) + 60_000,
        )

        def concurrent_delete(request):
            removed = set(request.security_group_rule_id)
            rules[:] = [
                rule
                for rule in rules
                if rule.security_group_rule_id not in removed
            ]
            raise RuntimeError("InvalidParam.SecurityGroupRuleId")

        api.revoke_security_group.side_effect = concurrent_delete
        status = ecs.revoke_desktop_access()

        self.assertTrue(status["closed"])
        self.assertNotIn(
            "sgr-managed", {rule.security_group_rule_id for rule in rules}
        )

    @patch("services.aliyun.Client")
    def test_partial_missing_id_race_retries_only_remaining_owned_ids(
        self, client_type
    ):
        extra = SimpleNamespace(
            ip_protocol="TCP",
            port_range="80/80",
            source_cidr_ip="0.0.0.0/0",
            policy="Accept",
            priority="20",
            nic_type="intranet",
            description=(
                "NOI-DESKTOP-DIRECT-MANAGED instance=i-test tid="
                + "2" * 24
                + " end=9999999999999 duplicate=1"
            ),
            security_group_rule_id="sgr-managed-extra",
        )
        remaining_owned = SimpleNamespace(
            **{
                **vars(extra),
                "description": extra.description.replace(
                    "duplicate=1", "duplicate=2"
                ),
                "security_group_rule_id": "sgr-managed-remain",
            }
        )
        ecs, api, rules = self._desktop_fixture(
            client_type, [extra, remaining_owned]
        )
        original_revoke = api.revoke_security_group.side_effect
        first = True

        def partial_race(request):
            nonlocal first
            if first:
                first = False
                rules[:] = [
                    rule
                    for rule in rules
                    if rule.security_group_rule_id != "sgr-managed-extra"
                ]
                raise RuntimeError("InvalidParam.SecurityGroupRuleId")
            return original_revoke(request)

        api.revoke_security_group.side_effect = partial_race
        status = ecs.revoke_desktop_access()

        self.assertTrue(status["closed"])
        self.assertEqual(api.revoke_security_group.call_count, 2)
        retried = api.revoke_security_group.call_args_list[1].args[0]
        self.assertNotIn("sgr-managed-extra", retried.security_group_rule_id)
        self.assertEqual(
            retried.security_group_rule_id, ["sgr-managed-remain"]
        )
        self.assertEqual(len(rules), 2)

    @patch("services.aliyun.Client")
    def test_shared_security_group_or_secondary_eni_blocks_open(
        self, client_type
    ):
        ecs, api, _ = self._desktop_fixture(client_type)
        target = api.describe_instances.return_value.body.instances.instance[0]
        other = SimpleNamespace(
            instance_id="i-other",
            security_group_ids=SimpleNamespace(security_group_id=["sg-test"]),
            eip_address=SimpleNamespace(ip_address="198.51.100.11"),
        )
        api.describe_instances.return_value.body.instances.instance = [target, other]
        with self.assertRaisesRegex(RuntimeError, "不是目标 ECS 专属"):
            ecs.ensure_desktop_access(
                tid="3" * 24,
                end_at_ms=int(time.time() * 1000) + 60_000,
            )

        api.describe_instances.return_value.body.instances.instance = [target]
        api.describe_network_interfaces.return_value.body.network_interface_sets.network_interface_set = [
            SimpleNamespace(network_interface_id="eni-other")
        ]
        with self.assertRaisesRegex(RuntimeError, "辅助 ENI"):
            ecs.ensure_desktop_access(
                tid="3" * 24,
                end_at_ms=int(time.time() * 1000) + 60_000,
            )

    @patch("services.aliyun.Client")
    def test_target_secondary_eni_with_other_group_blocks_open(self, client_type):
        ecs, api, _ = self._desktop_fixture(client_type)
        empty = api.describe_network_interfaces.return_value
        other_eni = SimpleNamespace(
            network_interface_id="eni-other",
            instance_id="i-test",
            type="Secondary",
            security_group_ids=SimpleNamespace(security_group_id=["sg-other"]),
        )

        def describe_enis(request):
            if str(getattr(request, "instance_id", "") or "") == "i-test":
                return SimpleNamespace(
                    body=SimpleNamespace(
                        network_interface_sets=SimpleNamespace(
                            network_interface_set=[other_eni]
                        ),
                        next_token="",
                    )
                )
            return empty

        api.describe_network_interfaces.side_effect = describe_enis

        with self.assertRaisesRegex(RuntimeError, "其他安全组"):
            ecs.ensure_desktop_access(
                tid="3" * 24,
                end_at_ms=int(time.time() * 1000) + 60_000,
            )

        api.authorize_security_group.assert_not_called()

    @patch("services.aliyun.Client")
    def test_target_primary_eni_is_not_misclassified_as_auxiliary(self, client_type):
        ecs, api, _ = self._desktop_fixture(client_type)
        empty = api.describe_network_interfaces.return_value
        primary = SimpleNamespace(
            network_interface_id="eni-primary",
            instance_id="i-test",
            type="Primary",
        )

        def describe_enis(request):
            if str(getattr(request, "instance_id", "") or "") == "i-test":
                self.assertEqual(request.type, "Secondary")
                return SimpleNamespace(
                    body=SimpleNamespace(
                        network_interface_sets=SimpleNamespace(
                            network_interface_set=[primary]
                        ),
                        next_token="",
                    )
                )
            return empty

        api.describe_network_interfaces.side_effect = describe_enis

        status = ecs.ensure_desktop_access(
            tid="3" * 24,
            end_at_ms=int(time.time() * 1000) + 60_000,
        )

        self.assertTrue(status["open"])

    @patch("services.aliyun.Client")
    def test_enterprise_group_or_missing_fixed_eip_blocks_open(
        self, client_type
    ):
        ecs, api, _ = self._desktop_fixture(client_type)
        group = (
            api.describe_security_groups.return_value.body.security_groups.security_group[0]
        )
        group.security_group_type = "enterprise"
        with self.assertRaisesRegex(RuntimeError, "basic"):
            ecs.ensure_desktop_access(
                tid="4" * 24,
                end_at_ms=int(time.time() * 1000) + 60_000,
            )

        group.security_group_type = "normal"
        instance = api.describe_instances.return_value.body.instances.instance[0]
        instance.eip_address.ip_address = ""
        with self.assertRaisesRegex(RuntimeError, "固定 EIP"):
            ecs.ensure_desktop_access(
                tid="4" * 24,
                end_at_ms=int(time.time() * 1000) + 60_000,
            )

    @patch("services.aliyun.Client")
    def test_deadline_crossing_during_authorize_revokes_before_return(
        self, client_type
    ):
        ecs, api, rules = self._desktop_fixture(client_type)
        with patch("services.aliyun.time.time", side_effect=[100.0, 100.0, 151.0]):
            with self.assertRaisesRegex(RuntimeError, "收敛时已截止"):
                ecs.ensure_desktop_access(
                    tid="5" * 24,
                    end_at_ms=150_000,
                )

        api.authorize_security_group.assert_called_once()
        self.assertNotIn(
            "sgr-managed", {rule.security_group_rule_id for rule in rules}
        )

    @patch("services.aliyun.Client")
    def test_revoke_batches_abnormal_duplicate_owned_rules_by_100(
        self, client_type
    ):
        owned = [
            SimpleNamespace(
                ip_protocol="TCP",
                port_range="80/80",
                source_cidr_ip="0.0.0.0/0",
                policy="Accept",
                priority="20",
                description=(
                    "NOI-DESKTOP-DIRECT-MANAGED instance=i-test tid="
                    + "6" * 24
                    + f" end=9999999999999 duplicate={index}"
                ),
                security_group_rule_id=f"sgr-owned-{index}",
            )
            for index in range(101)
        ]
        ecs, api, rules = self._desktop_fixture(client_type, owned)

        status = ecs.revoke_desktop_access()

        self.assertTrue(status["closed"])
        self.assertEqual(api.revoke_security_group.call_count, 2)
        batches = [
            call.args[0].security_group_rule_id
            for call in api.revoke_security_group.call_args_list
        ]
        self.assertEqual([len(batch) for batch in batches], [100, 1])
        self.assertEqual(len(rules), 2)

    @patch("services.aliyun.Client")
    def test_ingress_pagination_cannot_hide_second_page_drift(
        self, client_type
    ):
        ecs, api, rules = self._desktop_fixture(client_type)
        hidden = SimpleNamespace(
            ip_protocol="TCP",
            port_range="80/80",
            source_cidr_ip="0.0.0.0/0",
            policy="Accept",
            priority="30",
            description="second page drift",
            security_group_rule_id="sgr-second-page",
        )

        def paged(request):
            if not request.next_token:
                page, token = list(rules), "page-2"
            else:
                page, token = [hidden], ""
            return SimpleNamespace(
                body=SimpleNamespace(
                    permissions=SimpleNamespace(permission=page),
                    next_token=token,
                )
            )

        api.describe_security_group_attribute.side_effect = paged
        status = ecs.desktop_access_status()

        self.assertEqual(status["conflict_count"], 1)
        self.assertFalse(status["closed"])
        self.assertEqual(
            api.describe_security_group_attribute.call_args_list[1].args[0].next_token,
            "page-2",
        )


if __name__ == "__main__":
    unittest.main()
