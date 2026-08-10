import tempfile
from pathlib import Path
import json
import sqlite3
import unittest

from services.store import (
    Store,
    SubmissionClosedError,
    SubmissionConflictError,
    SubmissionLeaseLostError,
)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / "state.db"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _put_released_pool(self, tid, seats):
        self.store.put_seat_pool(
            tid,
            None,
            {
                "schema_version": 1,
                "tid": tid,
                "revision": 1,
                "seats": [
                    {"state": "released", "slot_no": slot, "uid": uid}
                    for slot, uid in seats
                ],
            },
        )

    def _put_pool_resource(self, tid, slot_no, credential_revision=1):
        suffix = f"{tid}-{slot_no}"
        self.store.put_seat_pool_resource(
            tid,
            slot_no,
            token=f"token-{suffix}",
            vnc_pass=f"pass-{suffix}",
            submit_token=f"submit-{suffix}",
            candidate=f"CSP{slot_no:03d}",
            container=f"container-{suffix}",
            cip=f"172.20.0.{slot_no}",
            image_digest="sha256:image",
            material_digest="sha256:materials",
            credential_revision=credential_revision,
        )

    def test_transition_is_atomic(self):
        tid = "0" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        self.assertTrue(self.store.transition(tid, {"registered"}, "preparing"))
        self.assertFalse(self.store.transition(tid, {"registered"}, "preparing"))

    def test_upsert_resets_failed_contest(self):
        tid = "1" * 24
        self.store.upsert_contest(tid, "old", ["a"], {"a": "P1"})
        self.store.set_state(tid, "error", "boom")
        old_session = self.store.get_contest(tid)["submission_session"]
        self.store.upsert_contest(tid, "new", ["b"], {"b": "P2"})
        contest = self.store.get_contest(tid)
        self.assertEqual(contest["state"], "registered")
        self.assertEqual(contest["message"], "")
        self.assertRegex(contest["submission_session"], r"^[0-9a-f]{32}$")
        self.assertNotEqual(contest["submission_session"], old_session)

    def test_paper_metadata_survives_reregister_without_new_upload(self):
        tid = "5" * 24
        self.store.upsert_contest(tid, "old", ["a"], {"a": "P1"})
        self.store.set_paper(tid, "题面.pdf", "a" * 64, 12345)

        self.store.upsert_contest(tid, "new", ["b"], {"b": "P2"})

        contest = self.store.get_contest(tid)
        self.assertEqual(contest["paper_name"], "题面.pdf")
        self.assertEqual(contest["paper_sha256"], "a" * 64)
        self.assertEqual(contest["paper_size"], 12345)

    def test_testdata_metadata_survives_reregister_without_new_upload(self):
        tid = "6" * 24
        self.store.upsert_contest(tid, "old", ["a"], {"a": "P1"})
        self.store.set_testdata(tid, "data.zip", "b" * 64, 120, 4, 800)

        self.store.upsert_contest(tid, "new", ["a"], {"a": "P2"})

        contest = self.store.get_contest(tid)
        self.assertEqual(contest["testdata_name"], "data.zip")
        self.assertEqual(contest["testdata_sha256"], "b" * 64)
        self.assertEqual(contest["testdata_files"], 4)
        self.assertEqual(contest["testdata_expanded_size"], 800)

    def test_seat_lookup(self):
        tid = "2" * 24
        self.store.upsert_contest(tid, "test", ["a"])
        self.store.add_seat(
            tid,
            7,
            "alice",
            "token",
            "pass1234",
            "submit-token",
            "alice",
            "c1",
            "172.18.0.2",
        )
        self.assertEqual(self.store.seat_by_uname(tid, "alice")["uid"], 7)
        self.assertEqual(self.store.seat_by_gateway_token("token")["uid"], 7)
        self.assertEqual(self.store.seat_by_submit_token("submit-token")["uid"], 7)

    def test_artifact_approval_freezes_hashes(self):
        tid = "a" * 24
        self.store.upsert_contest(
            tid,
            "AI contest",
            ["apple"],
            {"apple": "P1"},
            materials_mode="ai",
            material_state="pending",
        )
        self.store.put_artifact_revision(
            tid,
            "r1",
            state="review",
            source_sha256="1" * 64,
            root_path="/artifacts/r1",
            manifest_sha256="2" * 64,
            manifest={"status": "awaiting_teacher_approval"},
            paper_name="paper.pdf",
            paper_sha256="3" * 64,
            paper_size=123,
            testdata_name="testdata.tar.gz",
            testdata_sha256="4" * 64,
            testdata_size=456,
            testdata_files=6,
            testdata_expanded_size=789,
        )
        approved = self.store.approve_artifact(tid, "r1", "teacher")
        contest = self.store.get_contest(tid)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(contest["material_state"], "approved")
        self.assertEqual(contest["active_material_revision"], "r1")
        self.assertEqual(contest["paper_sha256"], "3" * 64)
        self.assertEqual(contest["testdata_files"], 6)

        # Identical persistence retry is harmless, but immutable bytes cannot
        # be replaced and an approved row is never demoted.
        self.store.put_artifact_revision(
            tid,
            "r1",
            state="review",
            source_sha256="1" * 64,
            root_path="/artifacts/r1",
            manifest_sha256="2" * 64,
            manifest={"status": "awaiting_teacher_approval"},
            paper_name="paper.pdf",
            paper_sha256="3" * 64,
            paper_size=123,
            testdata_name="testdata.tar.gz",
            testdata_sha256="4" * 64,
            testdata_size=456,
            testdata_files=6,
            testdata_expanded_size=789,
        )
        self.assertEqual(self.store.artifact_revision(tid, "r1")["state"], "approved")
        with self.assertRaisesRegex(SubmissionConflictError, "不可覆盖"):
            self.store.put_artifact_revision(
                tid,
                "r1",
                state="review",
                source_sha256="1" * 64,
                root_path="/artifacts/r1",
                manifest_sha256="2" * 64,
                manifest={"status": "changed"},
                paper_name="paper.pdf",
                paper_sha256="3" * 64,
                paper_size=123,
                testdata_name="testdata.tar.gz",
                testdata_sha256="4" * 64,
                testdata_size=456,
                testdata_files=6,
                testdata_expanded_size=789,
            )

    def test_draft_artifact_cannot_be_approved_before_machine_review(self):
        tid = "9" * 24
        self.store.upsert_contest(
            tid,
            "AI contest",
            ["apple"],
            {"apple": "P1"},
            materials_mode="ai",
        )
        self.store.put_artifact_revision(
            tid,
            "draft-r1",
            state="draft",
            source_sha256="1" * 64,
            root_path="/artifacts/draft-r1",
            manifest_sha256="2" * 64,
            paper_name="paper.pdf",
            paper_sha256="3" * 64,
            paper_size=123,
        )
        with self.assertRaisesRegex(SubmissionConflictError, "机器校验"):
            self.store.approve_artifact(tid, "draft-r1", "teacher")

    def test_private_clone_pid_map_replacement_is_session_locked_and_atomic(self):
        tid = "b" * 24
        self.store.upsert_contest(
            tid,
            "AI contest",
            ["apple", "banana_long_slug"],
            {"apple": "P1", "banana_long_slug": "P2"},
            materials_mode="ai",
        )
        contest = self.store.get_contest(tid)
        session = contest["submission_session"]
        replacement = {
            "apple": "noi-private-p1",
            "banana_long_slug": "noi-private-p2",
        }

        with self.assertRaisesRegex(SubmissionConflictError, "重新登记"):
            self.store.replace_contest_pid_map(
                tid,
                expected_submission_session="0" * 32,
                pid_map=replacement,
            )
        with self.assertRaisesRegex(SubmissionConflictError, "完全一致"):
            self.store.replace_contest_pid_map(
                tid,
                expected_submission_session=session,
                pid_map={"apple": "noi-private-p1"},
            )

        updated = self.store.replace_contest_pid_map(
            tid,
            expected_submission_session=session,
            pid_map=replacement,
        )
        self.assertEqual(json.loads(updated["pids"]), replacement)
        self.assertEqual(updated["submission_session"], session)

        self.store.set_state(tid, "preparing")
        with self.assertRaisesRegex(SubmissionConflictError, "备赛"):
            self.store.replace_contest_pid_map(
                tid,
                expected_submission_session=session,
                pid_map=replacement,
            )

    def test_private_clone_pid_map_replacement_refuses_run_evidence(self):
        tid = "d" * 24
        self.store.upsert_contest(
            tid,
            "AI contest",
            ["apple"],
            {"apple": "P1"},
            materials_mode="ai",
        )
        contest = self.store.get_contest(tid)
        self.store.add_seat(
            tid,
            7,
            "alice",
            "token-private-clone",
            "password",
            "submit-private-clone",
            "CSP001",
            "seat-private-clone",
            "172.20.0.2",
        )
        with self.assertRaisesRegex(SubmissionConflictError, "证据"):
            self.store.replace_contest_pid_map(
                tid,
                expected_submission_session=contest["submission_session"],
                pid_map={"apple": "noi-private-p1"},
            )

    def test_artifact_job_details_exclusion_and_restart_recovery(self):
        tid = "f" * 24
        self.store.upsert_contest(
            tid,
            "AI contest",
            ["apple"],
            {"apple": "P1"},
            materials_mode="ai",
        )
        first = self.store.start_artifact_job(
            "1" * 32,
            tid,
            "ai-r1",
            details={"submission_session": "2" * 32, "stage": "preflight"},
        )
        self.assertEqual(first["state"], "queued")
        self.assertEqual(first["details"]["stage"], "preflight")
        self.assertEqual(self.store.get_contest(tid)["material_state"], "generating")
        with self.assertRaisesRegex(SubmissionConflictError, "正在执行"):
            self.store.start_artifact_job(
                "3" * 32,
                tid,
                "ai-r2",
                details={},
            )

        self.store.update_artifact_job(
            "1" * 32,
            "running",
            progress=25,
            message="已保存预检",
            details={"preflight": {"preflight_id": "a" * 64}},
        )
        self.assertEqual(
            self.store.artifact_job("1" * 32)["details"]["preflight"]["preflight_id"],
            "a" * 64,
        )
        self.assertEqual(self.store.recover_interrupted_artifact_jobs(), 1)
        recovered = self.store.artifact_job("1" * 32)
        self.assertEqual(recovered["state"], "interrupted")
        self.assertIn("再次点击", recovered["error"])
        self.assertEqual(self.store.get_contest(tid)["material_state"], "pending")

        second = self.store.start_artifact_job(
            "3" * 32,
            tid,
            "ai-r2",
            details={"resumed_from": "1" * 32},
        )
        self.assertEqual(second["state"], "queued")
        with self.assertRaisesRegex(SubmissionConflictError, "不能重新激活"):
            self.store.update_artifact_job("1" * 32, "running", progress=30)

    def test_seat_pool_cas_resource_and_binding(self):
        tid = "e" * 24
        self.store.upsert_contest(tid, "pool", ["apple"])
        state = {
            "schema_version": 1,
            "tid": tid,
            "max_participants": 1,
            "spare_count": 0,
            "begin_at_ms": 2_000_000,
            "release_at_ms": 1_700_000,
            "revision": 1,
            "seats": [
                {
                    "slot_no": 1,
                    "seat_key": "seat-001",
                    "role": "primary",
                    "state": "released",
                    "uid": 7,
                    "uname": "alice",
                    "container_ref": "pool-c1",
                    "image_digest": "sha256:image",
                    "material_digest": "sha256:materials",
                    "failure_count": 0,
                    "last_error": "",
                    "reserved_at_ms": 1,
                    "released_at_ms": 2,
                    "frozen_at_ms": None,
                    "collected_at_ms": None,
                }
            ],
            "receipts": [],
        }
        self.store.put_seat_pool(tid, None, state)
        with self.assertRaises(SubmissionConflictError):
            self.store.put_seat_pool(tid, 0, {**state, "revision": 2})
        self.store.put_seat_pool_resource(
            tid,
            1,
            token="pool-token",
            vnc_pass="pool-pass",
            submit_token="pool-submit",
            candidate="CSP001",
            container="pool-c1",
            cip="172.20.0.2",
            image_digest="sha256:image",
            material_digest="sha256:materials",
        )
        seat = self.store.bind_pool_seat(tid, 7, "alice", 1)
        self.assertEqual(seat["candidate"], "CSP001")
        assignment = self.store.seat_pool_assignment(tid, 7)
        self.assertEqual(assignment["state"], "released")
        self.assertEqual(assignment["resource"]["token"], "pool-token")

    def test_seat_notification_health_is_active_and_non_sensitive(self):
        active_tid = "7" * 24
        done_tid = "8" * 24
        self.store.upsert_contest(active_tid, "active", ["apple"])
        self.store.upsert_contest(done_tid, "done", ["apple"])
        self.store.set_state(active_tid, "ready")
        self.store.set_state(done_tid, "done")
        self._put_released_pool(active_tid, [(1, 7), (2, 8), (3, 10)])
        self._put_pool_resource(active_tid, 1, credential_revision=2)
        self._put_pool_resource(active_tid, 2, credential_revision=1)
        self._put_pool_resource(active_tid, 3, credential_revision=1)
        self._put_released_pool(done_tid, [(1, 9)])
        self._put_pool_resource(done_tid, 1, credential_revision=1)

        # The failed revision was superseded by revision 2 and must not keep
        # current health red after the replacement credential was delivered.
        self.store.queue_seat_notification(
            active_tid, 7, "seat_ready", 1, "obsolete-retry"
        )
        self.store.mark_seat_notification(
            "obsolete-retry", sent=False, error="obsolete secret"
        )
        self.store.queue_seat_notification(
            active_tid, 7, "seat_ready", 2, "active-sent"
        )
        self.store.mark_seat_notification("active-sent", sent=True)
        self.store.queue_seat_notification(
            active_tid, 8, "seat_ready", 1, "active-retry"
        )
        self.store.mark_seat_notification(
            "active-retry", sent=False, error="password=must-not-leak"
        )
        self.store.queue_seat_notification(
            active_tid, 10, "seat_ready", 1, "active-permanent"
        )
        self.store.mark_seat_notification(
            "active-permanent",
            sent=False,
            retryable=False,
            error="https://must-not-leak.invalid",
        )
        self.store.queue_seat_notification(
            done_tid, 9, "seat_ready", 1, "completed-retry"
        )
        self.store.mark_seat_notification(
            "completed-retry", sent=False, error="https://must-not-leak.invalid"
        )

        health = self.store.seat_notification_health()

        self.assertEqual(
            health["counts"],
            {
                "pending": 0,
                "retry": 1,
                "permanent_failed": 1,
                "sent": 1,
                "untracked": 0,
                "missing_resource": 0,
                "invalid_pool": 0,
            },
        )
        self.assertEqual(health["max_retry_attempts"], 1)
        self.assertTrue(health["oldest_retry_at"])
        serialized = json.dumps(health, ensure_ascii=False)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("notification", serialized)

    def test_seat_notification_health_detects_prequeue_integrity_gaps(self):
        tid = "9" * 24
        self.store.upsert_contest(tid, "gaps", ["apple"])
        self.store.set_state(tid, "ready")
        self._put_released_pool(tid, [(1, 7), (2, 8), (3, 7)])
        self._put_pool_resource(tid, 1)

        health = self.store.seat_notification_health()

        self.assertEqual(health["counts"]["untracked"], 1)
        self.assertEqual(health["counts"]["missing_resource"], 1)
        self.assertEqual(health["counts"]["invalid_pool"], 1)

    def test_latest_web_submission_wins(self):
        tid = "3" * 24
        self.store.upsert_contest(tid, "test", ["apple"], submission_mode="web")
        first = self.store.add_web_submission(tid, 7, "apple", "first")
        second = self.store.add_web_submission(tid, 7, "apple", "second")
        latest = self.store.latest_web_submissions(tid, 7)
        self.assertEqual(latest["apple"]["id"], second["id"])
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(self.store.get_contest(tid)["submission_mode"], "web")

    def test_reset_seats_also_clears_web_submissions(self):
        tid = "4" * 24
        self.store.upsert_contest(tid, "test", ["apple"])
        self.store.add_seat(
            tid,
            7,
            "alice",
            "desktop-token",
            "password",
            "submit-token",
            "alice",
            "seat-test-7",
            "172.20.0.2",
        )
        self.store.add_web_submission(tid, 7, "apple", "old run")

        self.store.reset_seats(tid)

        self.assertEqual(self.store.seats(tid), [])
        self.assertEqual(self.store.latest_web_submissions(tid, 7), {})

    def _enqueue(
        self,
        tid: str,
        nonce: str,
        source: str = "int main(){}",
        *,
        problem: str = "apple",
        submission_id: str | None = None,
        accepted_at_ms: int = 1_786_080_000_000,
        allow_new: bool = True,
    ) -> dict:
        contest = self.store.get_contest(tid)
        if contest["state"] == "registered":
            self.store.set_state(tid, "ready")
            contest = self.store.get_contest(tid)
        return self.store.enqueue_web_submission(
            tid,
            7,
            problem,
            source,
            client_nonce=nonce,
            submission_id=submission_id or (nonce[0] * 64),
            submission_session=contest["submission_session"],
            judge_pid="P1",
            judge_lang="cc",
            judge_source=f"// judged\n{source}",
            issues=["example issue"],
            accepted_at_ms=accepted_at_ms,
            allow_new=allow_new,
        )

    def test_realtime_enqueue_is_transactional_and_nonce_replay_is_idempotent(self):
        tid = "7" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})

        first = self._enqueue(tid, "a-nonce", submission_id="a" * 64)
        replay = self._enqueue(tid, "a-nonce", submission_id="a" * 64)

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(first["judge_state"], "pending")
        self.assertEqual(first["judge_source"], "// judged\nint main(){}")
        self.assertEqual(first["judge_issues"], '["example issue"]')
        self.assertRegex(first["submission_id"], r"^[0-9a-f]{64}$")
        delayed_retry = self._enqueue(
            tid,
            "a-nonce",
            submission_id="a" * 64,
            accepted_at_ms=1_786_080_999_999,
            allow_new=False,
        )
        self.assertTrue(delayed_retry["replayed"])
        self.assertEqual(delayed_retry["id"], first["id"])
        self.assertEqual(delayed_retry["accepted_at_ms"], 1_786_080_000_000)
        with self.assertRaises(SubmissionConflictError):
            self._enqueue(
                tid,
                "a-nonce",
                "int main(){return 1;}",
                submission_id="a" * 64,
            )

    def test_realtime_enqueue_rejects_state_change_before_transaction(self):
        tid = "c" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        self.store.set_state(tid, "collecting")

        with self.assertRaisesRegex(SubmissionConflictError, "stopped accepting"):
            self._enqueue(tid, "c-nonce", submission_id="c" * 64)

    def test_reregister_refuses_to_overwrite_run_evidence(self):
        tid = "5" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        row = self._enqueue(tid, "evidence", submission_id="5" * 64)

        with self.assertRaisesRegex(SubmissionConflictError, "run evidence"):
            self.store.upsert_contest(
                tid, "replacement", ["apple"], {"apple": "P1"}
            )

        self.assertIsNotNone(self.store.get_web_submission(row["id"]))

    def test_realtime_queue_health_reports_retry_and_oldest_age(self):
        tid = "4" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        row = self._enqueue(
            tid,
            "health",
            submission_id="4" * 64,
            accepted_at_ms=100_000,
        )
        claimed = self.store.claim_next_web_submission(now=100)
        self.store.mark_web_submission_failed(
            row["id"], claimed["lease_token"], "network", retry_at=120
        )

        health = self.store.realtime_queue_health(now_ms=160_000)

        self.assertEqual(health["counts"]["retry"], 1)
        self.assertEqual(health["oldest_waiting_ms"], 60_000)

    def test_realtime_enqueue_enforces_frozen_window_atomically(self):
        tid = "f" * 24
        begin = 1_786_080_000_000
        end = begin + 60_000
        self.store.upsert_contest(
            tid,
            "test",
            ["apple"],
            {"apple": "P1"},
            begin_at_ms=begin,
            end_at_ms=end,
            hydro_rule="oi",
        )
        first = self._enqueue(
            tid,
            "f-first",
            submission_id="f" * 64,
            accepted_at_ms=end - 1,
        )
        self.assertFalse(first["replayed"])
        with self.assertRaises(SubmissionClosedError):
            self._enqueue(
                tid,
                "e-late",
                submission_id="e" * 64,
                accepted_at_ms=end,
            )
        replay = self._enqueue(
            tid,
            "f-first",
            submission_id="f" * 64,
            accepted_at_ms=end + 10_000,
            allow_new=False,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], first["id"])

    def test_realtime_claim_recovers_lease_and_preserves_problem_fifo(self):
        tid = "8" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        first = self._enqueue(tid, "a-first", submission_id="a" * 64)
        second = self._enqueue(tid, "b-second", submission_id="b" * 64)

        original = self.store.claim_next_web_submission(
            now=100, lease_seconds=10, tid=tid, uid=7, problem="apple"
        )
        self.assertEqual(original["id"], first["id"])
        self.assertEqual(original["attempts"], 1)
        self.assertIsNone(
            self.store.claim_next_web_submission(
                now=105, lease_seconds=10, tid=tid, uid=7, problem="apple"
            )
        )

        recovered = self.store.claim_next_web_submission(
            now=111, lease_seconds=10, tid=tid, uid=7, problem="apple"
        )
        self.assertEqual(recovered["id"], first["id"])
        self.assertEqual(recovered["attempts"], 2)
        self.assertNotEqual(recovered["lease_token"], original["lease_token"])
        with self.assertRaises(SubmissionLeaseLostError):
            self.store.mark_web_submission_submitted(
                first["id"], original["lease_token"], "1" * 24, now=112
            )

        delivered = self.store.mark_web_submission_submitted(
            first["id"], recovered["lease_token"], "1" * 24, now=112
        )
        self.assertEqual(delivered["judge_state"], "submitted")
        next_row = self.store.claim_next_web_submission(
            now=112, lease_seconds=10, tid=tid, uid=7, problem="apple"
        )
        self.assertEqual(next_row["id"], second["id"])

    def test_realtime_retry_blocks_later_row_until_due(self):
        tid = "9" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        first = self._enqueue(tid, "a-first", submission_id="a" * 64)
        second = self._enqueue(tid, "b-second", submission_id="b" * 64)
        claimed = self.store.claim_next_web_submission(now=100, lease_seconds=10)
        retried = self.store.mark_web_submission_failed(
            first["id"], claimed["lease_token"], "temporary", retry_at=120
        )
        self.assertEqual(retried["judge_state"], "retry")
        self.assertIsNone(self.store.claim_next_web_submission(now=119))
        due = self.store.claim_next_web_submission(now=120)
        self.assertEqual(due["id"], first["id"])
        self.assertNotEqual(due["id"], second["id"])

    def test_final_requeue_resets_unfinished_lane_and_invalidates_old_lease(self):
        tid = "a" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        first = self._enqueue(tid, "a-first", submission_id="a" * 64)
        second = self._enqueue(tid, "b-second", submission_id="b" * 64)
        claimed = self.store.claim_next_web_submission(now=100, lease_seconds=60)
        old_token = claimed["lease_token"]

        target = self.store.requeue_web_submission_for_final(second["id"])

        self.assertEqual(target["judge_state"], "pending")
        first_reset = self.store.get_web_submission(first["id"])
        self.assertEqual(first_reset["judge_state"], "pending")
        self.assertEqual(first_reset["judge_kind"], "final")
        self.assertEqual(first_reset["lease_token"], "")
        with self.assertRaises(SubmissionLeaseLostError):
            self.store.mark_web_submission_submitted(
                first["id"], old_token, "1" * 24, now=101
            )
        reclaimed = self.store.claim_next_web_submission(now=101)
        self.assertEqual(reclaimed["id"], first["id"])

    def test_final_requeue_of_submitted_target_does_not_reorder_older_failure(self):
        tid = "d" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        first = self._enqueue(tid, "first", submission_id="d" * 64)
        second = self._enqueue(tid, "second", submission_id="e" * 64)
        first_claim = self.store.claim_next_web_submission(now=100)
        self.store.mark_web_submission_failed(
            first["id"], first_claim["lease_token"], "invalid", retry_at=None
        )
        second_claim = self.store.claim_next_web_submission(now=101)
        self.store.mark_web_submission_submitted(
            second["id"], second_claim["lease_token"], "8" * 24, now=102
        )

        target = self.store.requeue_web_submission_for_final(second["id"])

        self.assertEqual(target["judge_state"], "submitted")
        self.assertEqual(
            self.store.get_web_submission(first["id"])["judge_state"],
            "permanent_failed",
        )

    def test_old_web_submission_table_migrates_without_losing_source(self):
        self.store.close()
        legacy_path = Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE web_submissions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tid TEXT NOT NULL,
                uid INTEGER NOT NULL,
                problem TEXT NOT NULL,
                source TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            INSERT INTO web_submissions(tid,uid,problem,source,sha256,size)
            VALUES('bbbbbbbbbbbbbbbbbbbbbbbb',7,'apple','legacy','digest',6);
            """
        )
        connection.commit()
        connection.close()

        migrated = Store(str(legacy_path))
        try:
            row = migrated.get_web_submission(1)
            self.assertEqual(row["source"], "legacy")
            self.assertEqual(row["judge_state"], "local")
            self.assertEqual(row["submission_id"], "")
        finally:
            migrated.close()
        # Restore a live Store so tearDown remains uniform.
        self.store = Store(str(Path(self.temp.name) / "state.db"))


if __name__ == "__main__":
    unittest.main()
