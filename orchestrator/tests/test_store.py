import tempfile
from pathlib import Path
import json
import sqlite3
import threading
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

    @staticmethod
    def _material_publication(
        tid,
        revision,
        *,
        paper_sha256="3" * 64,
        paper_size=123,
        testdata_sha256="4" * 64,
        testdata_size=456,
    ):
        receipt = {
            "publication_id": "5" * 64,
            "tid": tid,
            "revision": revision,
            "attachments": [
                {
                    "name": "01_比赛题面.pdf",
                    "sha256": paper_sha256,
                    "size": paper_size,
                },
                {
                    "name": "02_辅助自测数据.tar.gz",
                    "sha256": testdata_sha256,
                    "size": testdata_size,
                },
            ],
        }
        return {
            "ok": True,
            **receipt,
            "receipt_sha256": Store._canonical_json_sha256(receipt),
        }

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
        events = self.store.audit_events(tid)
        self.assertEqual(events[0]["action"], "contest.state.transition")
        self.assertEqual(events[0]["details"]["to"], "preparing")

    def test_active_seat_count_excludes_safely_ended_contests_and_deduplicates_container(self):
        active_tid, ended_tid = "1" * 24, "2" * 24
        self.store.upsert_contest(active_tid, "active", ["a"], {"a": "P1"})
        self.store.upsert_contest(ended_tid, "ended", ["a"], {"a": "P1"})
        self.store.set_state(active_tid, "ready")
        self.store.set_state(ended_tid, "safe_ended")
        for uid in (1, 2):
            self.store.add_seat(
                active_tid, uid, f"u{uid}", f"token{uid}", f"pass{uid}",
                f"submit{uid}", f"candidate{uid}", "same-container", f"172.20.0.{uid}",
            )
        self.store.add_seat(
            ended_tid, 3, "u3", "token3", "pass3", "submit3", "candidate3",
            "ended-container", "172.20.0.3",
        )
        self.assertEqual(self.store.active_seat_count(), 1)

    def test_audit_log_is_scoped_and_rejects_sensitive_fields(self):
        tid = "e" * 24
        other = "d" * 24
        event_id = self.store.append_audit(
            actor="teacher",
            action="contest.materials.approve",
            outcome="accepted",
            tid=tid,
            details={"revision": "r1", "practice_groups": 3},
            created_at_ms=1234,
        )
        self.store.append_audit(
            actor="system",
            action="contest.state.set",
            outcome="completed",
            tid=other,
            details={"state": "ready"},
            created_at_ms=1235,
        )

        events = self.store.audit_events(tid)
        self.assertEqual([item["id"] for item in events], [event_id])
        self.assertEqual(
            events[0]["details"], {"practice_groups": 3, "revision": "r1"}
        )
        self.assertNotIn("details_json", events[0])
        with self.assertRaisesRegex(ValueError, "forbidden"):
            self.store.append_audit(
                actor="teacher",
                action="contest.export",
                outcome="completed",
                tid=tid,
                details={"submit_token": "must-not-be-stored"},
            )

    def test_startup_recovery_fails_preparing_closed_but_keeps_resumable_states(self):
        preparing = "1" * 24
        collecting = "2" * 24
        safe_wait = "3" * 24
        for tid, state in (
            (preparing, "preparing"),
            (collecting, "collecting"),
            (safe_wait, "safe_wait"),
        ):
            self.store.upsert_contest(tid, state, ["apple"], {"apple": "P1"})
            self.store.set_state(tid, state)

        self.assertEqual(
            self.store.recover_interrupted_contests(observed_at_ms=9000), 1
        )

        recovered = self.store.get_contest(preparing)
        self.assertEqual(recovered["state"], "error")
        self.assertIn("入口保持关闭", recovered["message"])
        self.assertEqual(self.store.get_contest(collecting)["state"], "collecting")
        self.assertEqual(self.store.get_contest(safe_wait)["state"], "safe_wait")
        event = next(
            item
            for item in self.store.audit_events(preparing)
            if item["action"] == "contest.recovery.prepare_interrupted"
        )
        self.assertEqual(event["outcome"], "failed")

    def test_schedule_and_pool_release_boundary_commit_atomically(self):
        tid = "f" * 24
        self.store.upsert_contest(
            tid,
            "time sync",
            ["apple"],
            {"apple": "P1"},
            begin_at_ms=2_000_000,
            end_at_ms=3_000_000,
            hydro_rule="oi",
        )
        self.store.set_state(tid, "ready")
        state = {
            "schema_version": 1,
            "tid": tid,
            "max_participants": 0,
            "spare_count": 2,
            "begin_at_ms": 2_000_000,
            "release_at_ms": 1_700_000,
            "revision": 1,
            "seats": [],
            "receipts": [],
        }
        self.store.put_seat_pool(tid, None, state)
        changed = {
            **state,
            "begin_at_ms": 2_600_000,
            "release_at_ms": 2_300_000,
            "revision": 2,
        }

        updated = self.store.commit_schedule_sync(
            tid,
            expected_begin_at_ms=2_000_000,
            expected_end_at_ms=3_000_000,
            begin_at_ms=2_600_000,
            end_at_ms=3_600_000,
            hydro_rule="oi",
            observed_at_ms=1_900_000,
            expected_pool_revision=1,
            pool_state=changed,
        )

        self.assertEqual(updated["begin_at_ms"], 2_600_000)
        self.assertEqual(updated["end_at_ms"], 3_600_000)
        self.assertEqual(updated["time_sync_at_ms"], 1_900_000)
        self.assertEqual(updated["time_sync_error"], "")
        persisted = self.store.seat_pool(tid)
        self.assertEqual(persisted["revision"], 2)
        self.assertEqual(persisted["state"]["release_at_ms"], 2_300_000)

    def test_failed_schedule_commit_leaves_contest_and_pool_unchanged(self):
        tid = "e" * 24
        self.store.upsert_contest(
            tid,
            "time sync conflict",
            ["apple"],
            {"apple": "P1"},
            begin_at_ms=2_000_000,
            end_at_ms=3_000_000,
            hydro_rule="oi",
        )
        self.store.set_state(tid, "ready")
        state = {
            "schema_version": 1,
            "tid": tid,
            "max_participants": 0,
            "spare_count": 2,
            "begin_at_ms": 2_000_000,
            "release_at_ms": 1_700_000,
            "revision": 4,
            "seats": [],
            "receipts": [],
        }
        self.store.put_seat_pool(tid, None, state)
        changed = {**state, "begin_at_ms": 2_600_000, "revision": 5}

        with self.assertRaises(SubmissionConflictError):
            self.store.commit_schedule_sync(
                tid,
                expected_begin_at_ms=2_000_000,
                expected_end_at_ms=3_000_000,
                begin_at_ms=2_600_000,
                end_at_ms=3_600_000,
                hydro_rule="oi",
                observed_at_ms=1_900_000,
                expected_pool_revision=3,
                pool_state=changed,
            )

        self.assertEqual(self.store.get_contest(tid)["begin_at_ms"], 2_000_000)
        self.assertEqual(self.store.seat_pool(tid)["revision"], 4)

    def test_time_sync_error_preserves_last_successful_sync(self):
        tid = "d" * 24
        self.store.upsert_contest(tid, "sync status", ["apple"], {"apple": "P1"})
        self.store.mark_time_sync(tid, observed_at_ms=1000, error="")
        self.store.mark_time_sync(tid, observed_at_ms=2000, error="OJ timeout")

        contest = self.store.get_contest(tid)
        self.assertEqual(contest["time_sync_at_ms"], 1000)
        self.assertEqual(contest["time_sync_checked_at_ms"], 2000)
        self.assertEqual(contest["time_sync_error"], "OJ timeout")

    def test_safe_wait_requires_durable_evidence_and_delayed_end(self):
        tid = "c" * 24
        self.store.upsert_contest(tid, "safe wait", ["apple"], {"apple": "P1"})
        self.store.set_state(tid, "collecting")

        self.assertTrue(
            self.store.enter_safe_wait(
                tid,
                run_id="20260811T080000Z",
                collection_dir="/archive/contest/run",
                receipt_sha256="a" * 64,
                completed_at_ms=1_000,
                shutdown_after_ms=2_000,
                message="waiting",
            )
        )
        contest = self.store.get_contest(tid)
        self.assertEqual(contest["state"], "safe_wait")
        self.assertEqual(contest["collection_receipt_sha256"], "a" * 64)
        self.assertFalse(
            self.store.mark_safe_ended(
                tid, observed_at_ms=1_999, message="too early"
            )
        )
        self.assertTrue(
            self.store.mark_safe_ended(
                tid, observed_at_ms=2_000, message="verified"
            )
        )
        ended = self.store.get_contest(tid)
        self.assertEqual(ended["state"], "safe_ended")
        self.assertEqual(ended["shutdown_verified_at_ms"], 2_000)

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

    def test_contest_notification_health_is_scoped_and_non_sensitive(self):
        tid = "7" * 24
        other = "6" * 24
        self.store.upsert_contest(tid, "notifications", ["a"])
        self.store.upsert_contest(other, "other", ["a"])
        self.store.queue_seat_notification(
            tid,
            7,
            "seat_ready",
            1,
            "a" * 64,
        )
        self.store.queue_seat_notification(
            other,
            8,
            "seat_ready",
            1,
            "b" * 64,
        )
        self.store.mark_seat_notification("a" * 64, sent=True)

        health = self.store.contest_notification_health(tid)

        self.assertEqual(health["counts"]["sent"], 1)
        self.assertEqual(health["counts"]["pending"], 0)
        self.assertTrue(health["safe"])
        self.assertNotIn("notification_id", health)

    def test_teacher_retry_requeues_only_current_failed_credentials(self):
        tid = "5" * 24
        self.store.upsert_contest(tid, "retry notifications", ["a"])
        self.store.set_state(tid, "ready")
        self._put_released_pool(tid, [(1, 7), (2, 8)])
        self._put_pool_resource(tid, 1, credential_revision=2)
        self._put_pool_resource(tid, 2, credential_revision=1)
        self.store.queue_seat_notification(tid, 7, "seat_ready", 1, "a" * 64)
        self.store.mark_seat_notification(
            "a" * 64, sent=False, retryable=False, error="old failure"
        )
        current = self.store.queue_seat_notification(
            tid, 7, "seat_ready", 2, "b" * 64
        )
        self.store.mark_seat_notification(
            "b" * 64, sent=False, retryable=False, error="current failure"
        )
        sent = self.store.queue_seat_notification(
            tid, 8, "seat_ready", 1, "c" * 64
        )
        self.store.mark_seat_notification("c" * 64, sent=True)

        self.assertEqual(self.store.retry_failed_seat_notifications(tid), 1)
        self.assertEqual(self.store.retry_failed_seat_notifications(tid), 0)

        rows = {
            row["notification_id"]: row
            for row in self.store.pending_seat_notifications(tid)
        }
        self.assertEqual(rows["a" * 64]["state"], "permanent_failed")
        self.assertEqual(rows[current["notification_id"]]["state"], "pending")
        self.assertNotIn(sent["notification_id"], rows)
        self.assertEqual(rows[current["notification_id"]]["attempts"], 1)
        self.assertEqual(rows[current["notification_id"]]["last_error"], "")

    def test_teacher_retry_requires_ready_contest_and_valid_pool(self):
        tid = "4" * 24
        self.store.upsert_contest(tid, "retry guard", ["a"])
        with self.assertRaises(SubmissionConflictError):
            self.store.retry_failed_seat_notifications(tid)
        self.store.set_state(tid, "ready")
        with self.assertRaises(SubmissionConflictError):
            self.store.retry_failed_seat_notifications(tid)

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
        publication = self._material_publication(tid, "r1")
        approved = self.store.approve_artifact_with_publication(
            tid, "r1", "teacher", publication
        )
        contest = self.store.get_contest(tid)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(contest["material_state"], "approved")
        self.assertEqual(contest["active_material_revision"], "r1")
        self.assertEqual(contest["paper_sha256"], "3" * 64)
        self.assertEqual(contest["testdata_files"], 6)
        self.assertEqual(
            self.store.material_publication(tid, "r1")["receipt_sha256"],
            publication["receipt_sha256"],
        )

        # An identical approval retry is idempotent and retains one receipt.
        retried = self.store.approve_artifact_with_publication(
            tid, "r1", "teacher-2", publication
        )
        self.assertEqual(retried["approved_by"], "teacher")

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

    def test_artifact_approval_rejects_material_receipt_byte_mismatch(self):
        tid = "8" * 24
        self.store.upsert_contest(
            tid,
            "receipt mismatch",
            ["apple"],
            {"apple": "P1"},
            materials_mode="ai",
        )
        self.store.put_artifact_revision(
            tid,
            "r1",
            state="review",
            source_sha256="1" * 64,
            root_path="/artifacts/r1",
            manifest_sha256="2" * 64,
            paper_name="paper.pdf",
            paper_sha256="3" * 64,
            paper_size=123,
            testdata_name="testdata.tar.gz",
            testdata_sha256="4" * 64,
            testdata_size=456,
            testdata_files=6,
            testdata_expanded_size=789,
        )
        publication = self._material_publication(
            tid,
            "r1",
            paper_sha256="9" * 64,
        )

        with self.assertRaisesRegex(SubmissionConflictError, "字节摘要"):
            self.store.approve_artifact_with_publication(
                tid, "r1", "teacher", publication
            )

        self.assertEqual(self.store.artifact_revision(tid, "r1")["state"], "review")
        self.assertIsNone(self.store.material_publication(tid, "r1"))

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

    def test_ambiguous_submission_blocks_health_and_final_requeue(self):
        tid = "7" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        row = self._enqueue(tid, "ambiguous", submission_id="7" * 64)
        claimed = self.store.claim_next_web_submission(now=100)
        ambiguous = self.store.mark_web_submission_failed(
            row["id"],
            claimed["lease_token"],
            "unknown OJ insert",
            retry_at=None,
            ambiguous=True,
        )

        self.assertEqual(ambiguous["judge_state"], "ambiguous")
        self.assertIsNone(self.store.claim_next_web_submission(now=200))
        self.assertEqual(
            self.store.requeue_web_submission_for_final(row["id"])["judge_state"],
            "ambiguous",
        )
        self.assertEqual(self.store.realtime_queue_health()["counts"]["ambiguous"], 1)
        health = self.store.contest_delivery_health(tid)
        self.assertEqual(health["counts"]["ambiguous"], 1)
        self.assertFalse(health["safe"])

    def test_ambiguous_resolution_claim_is_rate_limited_and_exact(self):
        tid = "8" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        row = self._enqueue(tid, "resolve", submission_id="8" * 64)
        delivery = self.store.claim_next_web_submission(now=100)
        self.store.mark_web_submission_failed(
            row["id"],
            delivery["lease_token"],
            "unknown OJ insert",
            retry_at=None,
            ambiguous=True,
            resolution_after=102,
        )
        self.assertIsNone(self.store.claim_ambiguous_web_submission(now=101))
        claimed = self.store.claim_ambiguous_web_submission(
            now=102, check_seconds=30
        )
        self.assertEqual(claimed["resolution_attempts"], 1)
        self.assertEqual(claimed["resolution_after"], 132)
        self.assertIsNone(self.store.claim_ambiguous_web_submission(now=102))

        unresolved = self.store.finish_ambiguous_web_submission(
            row["id"], "8" * 64, resolution_status="multiple", now=103
        )
        self.assertEqual(unresolved["judge_state"], "ambiguous")
        self.assertEqual(unresolved["last_error"], "OJ 只读核对：multiple")
        resolved = self.store.finish_ambiguous_web_submission(
            row["id"], "8" * 64, rid="c" * 24, now=133
        )
        self.assertEqual(resolved["judge_state"], "submitted")
        self.assertEqual(resolved["rid"], "c" * 24)
        self.assertEqual(resolved["last_error"], "")

    def test_ambiguous_resolution_rejects_wrong_submission_identity(self):
        tid = "9" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        row = self._enqueue(tid, "resolve", submission_id="9" * 64)
        delivery = self.store.claim_next_web_submission(now=100)
        self.store.mark_web_submission_failed(
            row["id"], delivery["lease_token"], "unknown", retry_at=None,
            ambiguous=True,
        )
        with self.assertRaisesRegex(Exception, "state changed"):
            self.store.finish_ambiguous_web_submission(
                row["id"], "a" * 64, rid="d" * 24, now=101
            )

    def test_ambiguous_resolution_claim_is_single_owner_across_store_connections(self):
        tid = "a" * 24
        self.store.upsert_contest(tid, "test", ["apple"], {"apple": "P1"})
        row = self._enqueue(tid, "cross-process", submission_id="b" * 64)
        delivery = self.store.claim_next_web_submission(now=100)
        self.store.mark_web_submission_failed(
            row["id"],
            delivery["lease_token"],
            "unknown OJ insert",
            retry_at=None,
            ambiguous=True,
            resolution_after=102,
        )
        second = Store(str(Path(self.temp.name) / "state.db"))
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def claim(store):
            try:
                barrier.wait(timeout=5)
                results.append(
                    store.claim_ambiguous_web_submission(
                        now=102, check_seconds=30
                    )
                )
            except Exception as exc:  # surfaced below with the original detail
                errors.append(exc)

        threads = [
            threading.Thread(target=claim, args=(self.store,)),
            threading.Thread(target=claim, args=(second,)),
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            claimed = [item for item in results if item is not None]
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0]["id"], row["id"])
            self.assertEqual(claimed[0]["resolution_attempts"], 1)
        finally:
            second.close()

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
