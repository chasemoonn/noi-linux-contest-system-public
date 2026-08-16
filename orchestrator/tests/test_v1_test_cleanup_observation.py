import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "collect_v1_test_cleanup_observation.py"
SPEC = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(collector)


class TestCleanupObservationTests(unittest.TestCase):
    def counts(self):
        return {
            "contest": 0,
            "discussion": 0,
            "linked_record": 0,
            "registration_status": 0,
            "scheduled_task": 0,
        }

    def test_zero_counts_produce_bound_receipt(self):
        row = collector.build_observation(
            session_id="a" * 64,
            contest_id="b" * 24,
            counts=self.counts(),
            observed_at_ms=123456,
        )
        self.assertTrue(row["contest_absent"])
        self.assertEqual(row["linked_record_count"], 0)
        self.assertRegex(row["cleanup_receipt_sha256"], r"^[a-f0-9]{64}$")

    def test_any_remaining_object_fails_closed(self):
        counts = self.counts()
        counts["registration_status"] = 1
        with self.assertRaisesRegex(collector.CleanupError, "incomplete"):
            collector.build_observation(
                session_id="a" * 64,
                contest_id="b" * 24,
                counts=counts,
                observed_at_ms=123456,
            )


if __name__ == "__main__":
    unittest.main()
