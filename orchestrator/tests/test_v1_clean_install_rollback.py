import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "clean_install_rollback", ROOT / "scripts/verify_v1_clean_install_rollback.py"
)
rollback = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(rollback)


class CleanInstallRollbackTests(unittest.TestCase):
    def provider_status(self):
        return {
            "enabled": True,
            "open": False,
            "closed": True,
            "healthy": True,
            "managed_count": 0,
            "conflict_count": 0,
            "management_healthy": True,
            "management_missing_count": 0,
            "instance_state": "STOPPED",
            "managed_rule_ids": [],
            "security_group_id": "sg-example",
            "eip": "198.51.100.10",
        }

    def test_provider_actual_state_is_bound_to_closed_rollback_intent(self):
        value = rollback.closed_cloud_snapshot(self.provider_status())
        self.assertIs(value["desired_open"], False)
        self.assertEqual(value["managed_count"], 0)
        self.assertNotIn("managed_rule_ids", value)

    def test_open_provider_state_is_rejected(self):
        value = self.provider_status()
        value.update({"open": True, "closed": False, "managed_count": 1})
        with self.assertRaisesRegex(ValueError, "not exactly closed"):
            rollback.closed_cloud_snapshot(value)


if __name__ == "__main__":
    unittest.main()
