"""Crash-recovery regression for pool assignment to legacy-seat projection."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from services.pipeline import Pipeline
from services.seat_pool import SeatPoolState
from services.store import Store


class RosterBindingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temporary.name) / "state.db"))
        self.tid = "a" * 24
        now_ms = int(time.time() * 1000)
        self.begin_at_ms = now_ms + 3_600_000
        self.store.upsert_contest(
            self.tid,
            "binding recovery",
            ["apple"],
            {"apple": "P1"},
            submission_mode="both",
            begin_at_ms=self.begin_at_ms,
            end_at_ms=self.begin_at_ms + 3_600_000,
            hydro_rule="oi",
            max_participants=1,
            spare_seats=0,
            release_lead_minutes=5,
        )
        self.store.set_state(self.tid, "ready", "running")
        pool = SeatPoolState.create(
            self.tid,
            max_participants=1,
            spare_count=0,
            begin_at_ms=self.begin_at_ms,
        )
        pool = pool.mark_warming(
            1,
            now_ms=now_ms,
            command_id="warm:1",
            expected_revision=pool.revision,
        ).state
        pool = pool.mark_verified(
            1,
            container_ref="container-1",
            image_digest="sha256:image",
            material_digest="sha256:material",
            now_ms=now_ms,
            command_id="verify:1",
            expected_revision=pool.revision,
        ).state
        self.store.put_seat_pool(self.tid, None, pool.to_dict())
        self.store.put_seat_pool_resource(
            self.tid,
            1,
            token="stable-seat-token",
            vnc_pass="stable-vnc",
            submit_token="stable-submit-token",
            candidate="CSP001",
            container="container-1",
            cip="172.18.0.2",
            image_digest="sha256:image",
            material_digest="sha256:material",
        )
        self.hydro = mock.Mock()
        self.hydro.roster.return_value = [{"uid": 7, "uname": "alice"}]
        self.pipeline = Pipeline(
            {}, mock.Mock(), self.hydro, self.store, mock.Mock()
        )
        self.pipeline._validate_contest_snapshot = mock.Mock()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_second_sync_repairs_legacy_seat_without_moving_assignment(self):
        real_bind = self.store.bind_pool_seat
        calls = 0

        def fail_first_bind(tid, uid, uname, slot_no):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated crash before legacy seat bind")
            return real_bind(tid, uid, uname, slot_no)

        with mock.patch.object(
            self.store, "bind_pool_seat", side_effect=fail_first_bind
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.pipeline.sync_roster(self.tid)

            after_failure = self.store.seat_pool_assignment(self.tid, 7)
            self.assertIsNotNone(after_failure)
            self.assertEqual(after_failure["slot_no"], 1)
            self.assertEqual(
                after_failure["resource"]["token"], "stable-seat-token"
            )
            persisted_revision = self.store.seat_pool(self.tid)["revision"]
            self.assertIsNone(self.store.seat_by_uname(self.tid, "alice"))

            result = self.pipeline.sync_roster(self.tid)

        self.assertEqual(result["assigned"], 1)
        self.assertEqual(self.store.seat_pool(self.tid)["revision"], persisted_revision)
        repaired = self.store.seat_pool_assignment(self.tid, 7)
        self.assertEqual(repaired["slot_no"], 1)
        self.assertEqual(repaired["resource"]["token"], "stable-seat-token")

        # Student query reads the legacy row, while notification reads the
        # pool resource.  Both projections must now expose the same complete
        # credentials without creating a new slot or rotating any token.
        query_seat = self.store.seat_by_uname(self.tid, "alice")
        notify_resource = self.store.seat_pool_resource(self.tid, 1)
        self.assertIsNotNone(query_seat)
        for key in (
            "token",
            "vnc_pass",
            "submit_token",
            "candidate",
            "container",
            "cip",
        ):
            self.assertEqual(query_seat[key], notify_resource[key])
        self.assertEqual(calls, 2)

    def test_consecutive_release_ticks_do_not_conflict_as_time_advances(self):
        stored = self.store.seat_pool(self.tid)
        pool = SeatPoolState.from_dict(stored["state"])
        reserved = pool.reserve(
            7,
            "alice",
            now_ms=pool.release_at_ms - 1_000,
            command_id="reserve:alice",
            expected_revision=pool.revision,
        ).state
        self.store.put_seat_pool(self.tid, stored["revision"], reserved.to_dict())
        self.store.bind_pool_seat(self.tid, 7, "alice", 1)

        first_now = (reserved.release_at_ms + 1_000) / 1000
        second_now = (reserved.release_at_ms + 2_000) / 1000
        with mock.patch(
            "services.pipeline.time.time", side_effect=[first_now, second_now]
        ):
            first = self.pipeline.sync_roster(self.tid)
            revision_after_release = self.store.seat_pool(self.tid)["revision"]
            second = self.pipeline.sync_roster(self.tid)

        self.assertEqual(first["released"], [7])
        self.assertEqual(second["released"], [])
        self.assertEqual(
            self.store.seat_pool(self.tid)["revision"], revision_after_release
        )
        self.assertEqual(
            self.store.seat_pool_assignment(self.tid, 7)["state"], "released"
        )


if __name__ == "__main__":
    unittest.main()
