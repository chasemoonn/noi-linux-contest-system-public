"""Tests of evidence rejection, not real-systemd qualification themselves."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("systemd_evidence", ROOT / "scripts/verify_maintenance_systemd_ci.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
REV = "a" * 40


class SystemdEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.value = {"schema_version": 1, "source_revision": REV, "passed": True,
                      "environment": {"os": "ubuntu-22.04", "pid1": "systemd"},
                      "policy_unchanged": True, "policy_before": {"unit": "masked"}, "policy_after": {"unit": "masked"},
                      "sources": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in check.SOURCES},
                      "scenarios": [], "limitations": ["fixture"] * 4}
        for name in check.CASES:
            journal = b'{"event": "protected"}\n{"event": "systemd_close_confirmed"}\n'
            (self.directory / (name + ".journal.txt")).write_bytes(journal)
            self.value["scenarios"].append({"name": name, "passed": True, "fixture_units_removed": True,
                "ready_state": {"ActiveState": "active", "MainPID": "42", "Type": "notify", "WatchdogUSec": "15s", "RuntimeMaxUSec": "4h", "Restart": "no"},
                "end_state": {"ActiveState": "inactive", "MainPID": "0", "ControlPID": "0", "NRestarts": "0", "Result": {"sigkill": "signal", "watchdog-sigstop": "watchdog", "expiry": "exit-code"}.get(name, "success")},
                "apt_busy_while_protected": [True] * 4, "apt_busy_after_release": [False] * 4,
                "gateway_closure": {"locks_busy": [True] * 4}, "end_after_action_seconds": 1,
                "total_seconds": 145 if name == "expiry" else 1, "journal_sha256": hashlib.sha256(journal).hexdigest()})

    def verify(self, value=None):
        (self.directory / "evidence.json").write_text(json.dumps(value or self.value))
        return check.verify(self.directory, ROOT, REV)

    def test_complete_fixture_envelope(self):
        self.verify()

    def test_failed_incomplete_revision_or_source_rejected(self):
        for extra in ({"passed": False}, {"failure": {}}, {"source_revision": "b" * 40},
                      {"sources": {}}, {"scenarios": self.value["scenarios"][:-1]}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.verify({**self.value, **extra})

    def test_policy_and_non_jammy_rejected(self):
        for extra in ({"policy_unchanged": False}, {"policy_after": {"unit": "enabled"}},
                      {"environment": {"os": "ubuntu-24.04", "pid1": "systemd"}}):
            with self.assertRaises(ValueError):
                self.verify({**self.value, **extra})

    def test_cleanup_unlock_order_or_deadline_relaxation_rejected(self):
        for field, value in (("fixture_units_removed", False), ("apt_busy_after_release", [True] * 4),
                             ("gateway_closure", {"locks_busy": [False] * 4}), ("end_after_action_seconds", 25)):
            record = copy.deepcopy(self.value)
            record["scenarios"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(record)

    def test_wrong_watchdog_cause_and_missing_real_expiry_rejected(self):
        for index, field, value in ((4, "end_state", {"ActiveState": "failed", "MainPID": "0", "ControlPID": "0", "NRestarts": "0", "Result": "signal"}),
                                    (5, "total_seconds", 1)):
            record = copy.deepcopy(self.value)
            record["scenarios"][index][field] = value
            with self.assertRaises(ValueError):
                self.verify(record)

    def test_modified_journal_rejected(self):
        (self.directory / "sigterm.journal.txt").write_text("replacement")
        with self.assertRaises(ValueError):
            self.verify()


if __name__ == "__main__":
    unittest.main()
