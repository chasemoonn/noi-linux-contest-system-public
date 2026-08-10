"""Regression tests for non-destructive orchestrator config upgrades."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy" / "merge_orchestrator_config.py"
SPEC = importlib.util.spec_from_file_location("merge_orchestrator_config", HELPER)
assert SPEC and SPEC.loader
merge_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge_module)


class InstallConfigMergeTests(unittest.TestCase):
    def test_rerun_is_idempotent_and_preserves_custom_artifact_tools(self):
        artifact_block = """artifact_generation:
  enabled: true
  ai:
    api_key_env: SCHOOL_PRIVATE_AI_KEY
  approved_roots:
    - /opt/noi-artifact-tools
  validators:
    apple:
      executable: /opt/noi-artifact-tools/custom-apple-validator
      timeout_seconds: 9
  oracles:
    apple:
      executable: /opt/noi-artifact-tools/custom-apple-oracle
      timeout_seconds: 17
"""
        source = """hydro:
  submit_enabled: true
  notify_allowed_https_hosts:
    - "old.example.com"
  domain_id: system
orchestrator:
  release_lead_minutes: 10
""" + artifact_block
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.yaml"
            config.write_text(source, encoding="utf-8")

            changed = merge_module.merge_config(config, "exam.example.test")
            once = config.read_text(encoding="utf-8")
            changed_again = merge_module.merge_config(config, "exam.example.test")
            twice = config.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(once, twice)
        self.assertIn('    - "exam.example.test"', twice)
        self.assertNotIn("old.example.com", twice)
        self.assertIn("  submit_enabled: true", twice)
        self.assertIn("  release_lead_minutes: 10", twice)
        self.assertEqual(twice.split("artifact_generation:\n", 1)[1], artifact_block.split("artifact_generation:\n", 1)[1])

    def test_generic_installer_restores_existing_config_after_staging_copy(self):
        for name in ("install-hydro-host.sh",):
            source = (ROOT / "deploy" / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                preserve = source.index('preserved_config="${backup}/orchestrator-config.yaml.preserved"')
                staging_copy = source.index('cp -a "${stage}/." "${app}/"')
                restore = source.index(
                    'cp -a "${preserved_config}" "${app}/orchestrator/config.yaml"'
                )
                merge = source.index('merge_orchestrator_config.py')
                self.assertLess(preserve, staging_copy)
                self.assertLess(staging_copy, restore)
                self.assertLess(restore, merge)


if __name__ == "__main__":
    unittest.main()
