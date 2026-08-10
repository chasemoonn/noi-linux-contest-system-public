import unittest

from services.seat_pool import (
    CapacityExceededError,
    CommandConflictError,
    NoSpareSeatError,
    PoolConfigurationError,
    RevisionConflictError,
    SeatPoolState,
    TeacherApprovalRequiredError,
    TooEarlyToReleaseError,
    reservation_state,
)


BEGIN = 2_000_000
RELEASE = BEGIN - 300_000


class SeatPoolTests(unittest.TestCase):
    def make_pool(self, maximum=2, spares=1):
        return SeatPoolState.create(
            "c" * 24,
            max_participants=maximum,
            spare_count=spares,
            begin_at_ms=BEGIN,
        )

    def warming(self, pool, slot, command):
        return pool.mark_warming(
            slot,
            now_ms=100,
            command_id=command,
            expected_revision=pool.revision,
        ).state

    def verified(self, pool, slot, command):
        pool = self.warming(pool, slot, f"{command}-warm")
        return pool.mark_verified(
            slot,
            container_ref=f"container-{slot}",
            image_digest="sha256:image",
            material_digest="sha256:materials",
            now_ms=200,
            command_id=f"{command}-verify",
            expected_revision=pool.revision,
        ).state

    def test_capacity_and_spare_validation(self):
        for maximum, spares in ((0, 0), (1, -1), (2, 3)):
            with self.subTest(maximum=maximum, spares=spares):
                with self.assertRaises(PoolConfigurationError):
                    self.make_pool(maximum, spares)

    def test_json_round_trip_is_canonical(self):
        pool = self.verified(self.make_pool(), 1, "s1")
        restored = SeatPoolState.from_json(pool.to_json())
        self.assertEqual(restored, pool)
        self.assertEqual(restored.to_json(), pool.to_json())

    def test_command_retry_precedes_stale_revision_check(self):
        pool = self.make_pool()
        first = pool.mark_warming(
            1, now_ms=100, command_id="warm-one", expected_revision=0
        )
        replay = first.state.mark_warming(
            1, now_ms=100, command_id="warm-one", expected_revision=0
        )
        self.assertTrue(replay.replayed)
        self.assertIs(replay.state, first.state)
        self.assertEqual(replay.value, first.value)
        with self.assertRaises(CommandConflictError):
            first.state.mark_warming(
                2, now_ms=100, command_id="warm-one", expected_revision=1
            )
        with self.assertRaises(RevisionConflictError):
            first.state.mark_warming(
                2, now_ms=100, command_id="warm-two", expected_revision=0
            )

    def test_full_state_machine_and_release_boundary(self):
        pool = self.verified(self.make_pool(), 1, "s1")
        reserved = pool.reserve(
            7,
            "alice",
            now_ms=RELEASE - 1,
            command_id="reserve-alice",
            expected_revision=pool.revision,
        )
        pool = reserved.state
        self.assertEqual(reserved.value["state"], "reserved")
        with self.assertRaises(TooEarlyToReleaseError):
            pool.release(
                7,
                now_ms=RELEASE - 1,
                command_id="early",
                expected_revision=pool.revision,
            )
        released = pool.release(
            7,
            now_ms=RELEASE,
            command_id="release",
            expected_revision=pool.revision,
        )
        self.assertEqual(released.value["state"], "released")
        frozen = released.state.freeze(
            now_ms=BEGIN + 1,
            command_id="freeze",
            expected_revision=released.state.revision,
        )
        self.assertEqual(frozen.value["frozen"][0]["state"], "frozen")
        collected = frozen.state.collect(
            now_ms=BEGIN + 2,
            command_id="collect",
            expected_revision=frozen.state.revision,
        )
        self.assertEqual(collected.value["collected"][0]["state"], "collected")
        self.assertEqual(collected.state.state_counts()["collected"], 1)

    def test_join_at_t_minus_five_is_immediately_released(self):
        pool = self.verified(self.make_pool(), 1, "s1")
        result = pool.reserve(
            8,
            "bob",
            now_ms=RELEASE,
            command_id="reserve-bob",
            expected_revision=pool.revision,
        )
        self.assertEqual(result.value["state"], "released")
        self.assertEqual(result.value["released_at_ms"], RELEASE)

    def test_join_after_start_requires_explicit_teacher_approval(self):
        pool = self.verified(self.make_pool(), 1, "s1")
        with self.assertRaises(TeacherApprovalRequiredError):
            pool.reserve(
                9,
                "carol",
                now_ms=BEGIN,
                command_id="late-no",
                expected_revision=pool.revision,
            )
        approved = pool.reserve(
            9,
            "carol",
            now_ms=BEGIN,
            teacher_approved=True,
            command_id="late-yes",
            expected_revision=pool.revision,
        )
        self.assertEqual(approved.value["state"], "released")

    def test_reservation_stays_on_same_seat(self):
        pool = self.verified(self.make_pool(), 1, "s1")
        first = pool.reserve(
            10,
            "dana",
            now_ms=RELEASE - 10,
            command_id="first",
            expected_revision=pool.revision,
        )
        second = first.state.reserve(
            10,
            "dana",
            now_ms=RELEASE + 10,
            command_id="same-participant",
            expected_revision=first.state.revision,
        )
        self.assertEqual(first.value["slot_no"], second.value["slot_no"])
        self.assertEqual(second.value["state"], "reserved")

    def test_verified_spares_do_not_increase_participant_limit(self):
        pool = self.verified(self.make_pool(1, 1), 1, "main")
        pool = self.verified(pool, 2, "spare")
        first = pool.reserve(
            11,
            "erin",
            now_ms=RELEASE - 1,
            command_id="erin",
            expected_revision=pool.revision,
        )
        with self.assertRaises(CapacityExceededError):
            first.state.reserve(
                12,
                "fred",
                now_ms=RELEASE - 1,
                command_id="fred",
                expected_revision=first.state.revision,
            )

    def test_failed_assigned_seat_moves_to_verified_spare(self):
        pool = self.verified(self.make_pool(), 1, "main")
        pool = self.verified(pool, 3, "spare")
        pool = pool.reserve(
            13,
            "gina",
            now_ms=RELEASE - 1,
            command_id="gina",
            expected_revision=pool.revision,
        ).state
        replaced = pool.replace_failed(
            1,
            reason="VNC preflight failed",
            now_ms=RELEASE,
            command_id="replace-one",
            expected_revision=pool.revision,
        )
        self.assertEqual(replaced.value["replacement"]["slot_no"], 3)
        self.assertEqual(replaced.value["replacement"]["state"], "released")
        self.assertEqual(replaced.state.assignment(13).slot_no, 3)
        failed = replaced.state.seat(1)
        self.assertEqual(failed.state, "planned")
        self.assertIsNone(failed.uid)
        self.assertEqual(failed.failure_count, 1)
        replay = replaced.state.replace_failed(
            1,
            reason="VNC preflight failed",
            now_ms=RELEASE,
            command_id="replace-one",
            expected_revision=pool.revision,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.state.assignment(13).slot_no, 3)

    def test_no_spare_failure_leaves_original_snapshot_untouched(self):
        pool = self.verified(self.make_pool(1, 0), 1, "main")
        pool = pool.reserve(
            14,
            "hank",
            now_ms=RELEASE - 1,
            command_id="hank",
            expected_revision=pool.revision,
        ).state
        before = pool.to_json()
        with self.assertRaises(NoSpareSeatError):
            pool.replace_failed(
                1,
                reason="broken",
                now_ms=RELEASE - 1,
                command_id="broken",
                expected_revision=pool.revision,
            )
        self.assertEqual(pool.to_json(), before)
        self.assertEqual(pool.assignment(14).slot_no, 1)

    def test_release_due_is_bulk_and_idempotent(self):
        pool = self.verified(self.make_pool(), 1, "one")
        pool = self.verified(pool, 2, "two")
        pool = pool.reserve(
            15,
            "iris",
            now_ms=RELEASE - 10,
            command_id="iris",
            expected_revision=pool.revision,
        ).state
        pool = pool.reserve(
            16,
            "jane",
            now_ms=RELEASE - 10,
            command_id="jane",
            expected_revision=pool.revision,
        ).state
        due = pool.release_due(
            now_ms=RELEASE,
            command_id="release-due",
            expected_revision=pool.revision,
        )
        self.assertEqual(
            {seat["uid"] for seat in due.value["released"]}, {15, 16}
        )
        replay = due.state.release_due(
            now_ms=RELEASE,
            command_id="release-due",
            expected_revision=pool.revision,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.value, due.value)

    def test_teacher_approved_grow_only_appends_slots(self):
        pool = self.verified(self.make_pool(1, 1), 1, "main")
        pool = pool.reserve(
            17,
            "kate",
            now_ms=RELEASE - 1,
            command_id="kate",
            expected_revision=pool.revision,
        ).state
        before = pool.seats
        with self.assertRaises(TeacherApprovalRequiredError):
            pool.grow(
                additional_main=1,
                additional_spares=1,
                teacher_approved=False,
                command_id="grow-no",
                expected_revision=pool.revision,
            )
        grown = pool.grow(
            additional_main=2,
            additional_spares=1,
            teacher_approved=True,
            command_id="grow-yes",
            expected_revision=pool.revision,
        )
        self.assertEqual(grown.state.seats[: len(before)], before)
        self.assertEqual(grown.state.assignment(17).slot_no, 1)
        self.assertEqual(grown.state.max_participants, 3)
        self.assertEqual(grown.state.spare_count, 2)
        self.assertEqual(
            [seat.role for seat in grown.state.seats[-3:]],
            ["primary", "primary", "spare"],
        )
        self.assertTrue(all(seat.state == "planned" for seat in grown.state.seats[-3:]))


class ReservationRuleTests(unittest.TestCase):
    def test_pure_schedule_rule(self):
        self.assertEqual(
            reservation_state(
                now_ms=RELEASE - 1,
                release_at_ms=RELEASE,
                begin_at_ms=BEGIN,
            ),
            "reserved",
        )
        self.assertEqual(
            reservation_state(
                now_ms=RELEASE,
                release_at_ms=RELEASE,
                begin_at_ms=BEGIN,
            ),
            "released",
        )
        with self.assertRaises(TeacherApprovalRequiredError):
            reservation_state(
                now_ms=BEGIN,
                release_at_ms=RELEASE,
                begin_at_ms=BEGIN,
            )


if __name__ == "__main__":
    unittest.main()
