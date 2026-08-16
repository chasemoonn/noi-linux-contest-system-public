"""Crash-recovery regression for pool assignment to legacy-seat projection."""
from pathlib import Path
import base64
import hashlib
import json
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
            spare_seats=2,
            release_lead_minutes=5,
        )
        self.store.set_state(self.tid, "ready", "running")
        pool = SeatPoolState.create(
            self.tid,
            max_participants=1,
            spare_count=2,
            begin_at_ms=self.begin_at_ms,
        )
        for slot_no in (1, 2, 3):
            pool = pool.mark_warming(
                slot_no,
                now_ms=now_ms,
                command_id=f"warm:{slot_no}",
                expected_revision=pool.revision,
            ).state
            pool = pool.mark_verified(
                slot_no,
                container_ref=f"container-{slot_no}",
                image_digest="sha256:image",
                material_digest="sha256:material",
                now_ms=now_ms,
                command_id=f"verify:{slot_no}",
                expected_revision=pool.revision,
            ).state
        self.store.put_seat_pool(self.tid, None, pool.to_dict())
        for slot_no in (1, 2, 3):
            self.store.put_seat_pool_resource(
                self.tid,
                slot_no,
                token=f"stable-seat-token-{slot_no}",
                vnc_pass=f"stable-vnc-{slot_no}",
                submit_token=f"stable-submit-token-{slot_no}",
                candidate=f"CSP{slot_no:03d}",
                container=f"container-{slot_no}",
                cip=f"172.18.0.{slot_no + 1}",
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
                after_failure["resource"]["token"], "stable-seat-token-1"
            )
            persisted_revision = self.store.seat_pool(self.tid)["revision"]
            self.assertIsNone(self.store.seat_by_uname(self.tid, "alice"))

            result = self.pipeline.sync_roster(self.tid)

        self.assertEqual(result["assigned"], 1)
        self.assertEqual(self.store.seat_pool(self.tid)["revision"], persisted_revision)
        repaired = self.store.seat_pool_assignment(self.tid, 7)
        self.assertEqual(repaired["slot_no"], 1)
        self.assertEqual(repaired["resource"]["token"], "stable-seat-token-1")

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

    def test_roster_target_triggers_automatic_append_without_teacher_gate(self):
        roster = [
            {"uid": uid, "uname": f"student-{uid}"}
            for uid in range(1, 22)
        ]
        self.pipeline.grow_pool = mock.Mock(
            return_value={
                "replayed": False,
                "revision": self.store.seat_pool(self.tid)["revision"] + 1,
                "added": list(range(4, 25)),
                "counts": {},
            }
        )

        result = self.pipeline._ensure_automatic_roster_capacity(self.tid, roster)

        self.assertTrue(result["grown"])
        self.pipeline.grow_pool.assert_called_once_with(
            self.tid,
            additional_main=20,
            additional_spares=1,
            expected_revision=self.store.seat_pool(self.tid)["revision"],
        )

    def test_roster_target_never_shrinks_existing_pool(self):
        result = self.pipeline._ensure_automatic_roster_capacity(
            self.tid, [{"uid": 7, "uname": "alice"}]
        )
        self.assertEqual(result, {"grown": False, "added": []})

    def test_duplicate_roster_identity_is_rejected_before_growth(self):
        with self.assertRaisesRegex(RuntimeError, "重复账号"):
            self.pipeline._validate_roster(
                [
                    {"uid": 7, "uname": "alice"},
                    {"uid": 7, "uname": "other"},
                ]
            )


class FormalSourceReadTests(unittest.TestCase):
    def setUp(self):
        self.tid = "f" * 24
        self.store = mock.Mock()
        self.store.get_contest.return_value = {
            "tid": self.tid,
            "state": "ready",
            "files": '["apple"]',
        }
        self.store.seat_pool_assignment.return_value = {
            "state": "released",
            "container_ref": "seat-ffffffff-slot-003",
            "resource": {
                "container": "seat-ffffffff-slot-003",
                "candidate": "CSP003",
            },
        }
        self.cvm = mock.Mock()
        self.cvm.status.return_value = ("RUNNING", "203.0.113.8")
        self.remote = mock.Mock()
        self.remote.wait_ssh.return_value = True
        self.source = b"int main(){return 0;}\n"
        self.remote.run.return_value = json.dumps(
            {
                "schema": 1,
                "size": len(self.source),
                "sha256": hashlib.sha256(self.source).hexdigest(),
                "base64": base64.b64encode(self.source).decode(),
            }
        )
        self.pipeline = Pipeline({}, self.cvm, mock.Mock(), self.store, mock.Mock())
        self.pipeline._remote = mock.Mock(return_value=self.remote)

    def test_reads_only_the_exact_released_csp_path(self):
        payload = self.pipeline.read_formal_source(
            self.tid, 7, "apple", maximum_bytes=1024
        )
        self.assertEqual(payload, self.source)
        command = self.remote.run.call_args.args[0]
        self.assertIn("seat-ffffffff-slot-003", command)
        self.assertIn("/usr/local/bin/capture-formal-source.py", command)
        self.assertIn("/home/student/答案", command)
        self.assertIn("CSP003", command)
        self.assertIn("apple", command)

    def test_invalid_base64_is_rejected_and_process_lock_is_released(self):
        self.remote.run.return_value = "not-base64!"
        with self.assertRaisesRegex(RuntimeError, "读取结果无效"):
            self.pipeline.read_formal_source(
                self.tid, 7, "apple", maximum_bytes=1024
            )
        self.remote.run.return_value = json.dumps(
            {
                "schema": 1,
                "size": len(self.source),
                "sha256": hashlib.sha256(self.source).hexdigest(),
                "base64": base64.b64encode(self.source).decode(),
            }
        )
        self.assertEqual(
            self.pipeline.read_formal_source(
                self.tid, 7, "apple", maximum_bytes=1024
            ),
            self.source,
        )

    def test_snapshot_digest_or_size_mismatch_is_rejected(self):
        envelope = {
            "schema": 1,
            "size": len(self.source),
            "sha256": "0" * 64,
            "base64": base64.b64encode(self.source).decode(),
        }
        self.remote.run.return_value = json.dumps(envelope)
        with self.assertRaisesRegex(RuntimeError, "稳定快照校验失败"):
            self.pipeline.read_formal_source(
                self.tid, 7, "apple", maximum_bytes=1024
            )


if __name__ == "__main__":
    unittest.main()
