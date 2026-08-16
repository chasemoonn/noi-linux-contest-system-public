import tempfile
from pathlib import Path
import threading
import unittest
from unittest import mock

from services.hydro_submit import HydroSubmitter
from services.realtime_judge import RealtimeJudge, RealtimeJudgeAmbiguousError
from services.store import Store


class FakeClock:
    def __init__(self, value=100.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += float(seconds)


class FakeSubmitter:
    realtime_submission_id = staticmethod(HydroSubmitter.realtime_submission_id)

    def __init__(self, results, lang="cc", resolution_results=()):
        self.lang = lang
        self.results = list(results)
        self.calls = []
        self.resolution_results = list(resolution_results)
        self.resolution_calls = []

    def submit_one(
        self,
        tid,
        uid,
        pid,
        code,
        submission_id,
        *,
        lang=None,
        submission_kind="final",
        accepted_at_ms=None,
    ):
        self.calls.append(
            {
                "tid": tid,
                "uid": uid,
                "pid": pid,
                "code": code,
                "submission_id": submission_id,
                "lang": lang,
                "submission_kind": submission_kind,
                "accepted_at_ms": accepted_at_ms,
            }
        )
        return self.results.pop(0)

    def resolve_submission(self, submission_id):
        self.resolution_calls.append(submission_id)
        return self.resolution_results.pop(0)


class RealtimeJudgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / "state.db"))
        self.tid = "a" * 24
        self.store.upsert_contest(
            self.tid, "test", ["apple"], {"apple": "P1"}, "web"
        )
        self.store.set_state(self.tid, "ready")
        self.session = self.store.get_contest(self.tid)["submission_session"]
        self.clock = FakeClock()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def make_judge(self, results, **kwargs):
        submitter = FakeSubmitter(results)
        judge = RealtimeJudge(
            self.store,
            submitter,
            clock=self.clock,
            sleeper=self.clock.sleep,
            **kwargs,
        )
        return judge, submitter

    def enqueue(self, judge, nonce, source="int main(){}"):
        return judge.enqueue(
            submission_session=self.session,
            tid=self.tid,
            uid=7,
            problem="apple",
            pid="P1",
            source=source,
            judge_source=f"// exact judge source\n{source}",
            issues=["missing freopen"],
            client_nonce=nonce,
            accepted_at_ms=1_786_080_000_000,
        )

    def test_each_logical_submit_gets_distinct_id_and_exact_payload(self):
        judge, submitter = self.make_judge(
            [
                {"ok": True, "rid": "1" * 24},
                {"ok": True, "rid": "2" * 24},
            ]
        )
        first = self.enqueue(judge, "nonce-one", "first")
        second = self.enqueue(judge, "nonce-two", "second")

        self.assertRegex(first["submission_id"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(first["submission_id"], second["submission_id"])
        delivered_first = judge.process_once()
        delivered_second = judge.process_once()

        self.assertEqual(delivered_first["rid"], "1" * 24)
        self.assertEqual(delivered_second["rid"], "2" * 24)
        self.assertEqual(submitter.calls[0]["code"], "// exact judge source\nfirst")
        self.assertEqual(submitter.calls[0]["lang"], "cc")
        self.assertEqual(submitter.calls[0]["submission_kind"], "realtime")
        self.assertEqual(
            submitter.calls[0]["submission_id"], first["submission_id"]
        )

    def test_nonce_replay_returns_existing_row_without_second_delivery(self):
        judge, submitter = self.make_judge([{"ok": True, "rid": "1" * 24}])
        first = self.enqueue(judge, "same-nonce")
        replay = self.enqueue(judge, "same-nonce")
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])

        judge.process_once()
        self.assertEqual(len(submitter.calls), 1)
        self.assertIsNone(judge.process_once())

    def test_retry_reuses_exact_id_and_recovers(self):
        judge, submitter = self.make_judge(
            [
                {"ok": False, "retryable": True, "error": "network down"},
                {"ok": True, "rid": "3" * 24},
            ],
            retry_delays=(2,),
        )
        queued = self.enqueue(judge, "retry-nonce")

        failed = judge.process_once()
        self.assertEqual(failed["judge_state"], "retry")
        self.assertEqual(failed["next_retry_at"], 102.0)
        self.assertIsNone(judge.process_once())
        self.clock.value = 102.0
        delivered = judge.process_once()

        self.assertEqual(delivered["judge_state"], "submitted")
        self.assertEqual(delivered["attempts"], 2)
        self.assertEqual(submitter.calls[0]["submission_id"], queued["submission_id"])
        self.assertEqual(
            submitter.calls[0]["submission_id"], submitter.calls[1]["submission_id"]
        )
        self.assertEqual(submitter.calls[0]["code"], submitter.calls[1]["code"])

    def test_ambiguous_result_is_persisted_and_never_claimed_again(self):
        judge, submitter = self.make_judge(
            [
                {
                    "ok": False,
                    "retryable": False,
                    "ambiguous": True,
                    "status_code": 409,
                    "error": {
                        "error": {"name": "OrchestratorSubmissionAmbiguousError"}
                    },
                }
            ]
        )
        queued = self.enqueue(judge, "ambiguous-nonce")

        result = judge.process_once()

        self.assertEqual(result["judge_state"], "ambiguous")
        self.assertEqual(len(submitter.calls), 1)

    def test_ambiguous_result_is_resolved_only_by_exact_read_only_rid(self):
        submitter = FakeSubmitter(
            [{"ok": False, "retryable": False, "ambiguous": True}],
            resolution_results=[{"ok": True, "status": "resolved", "rid": "c" * 24}],
        )
        judge = RealtimeJudge(
            self.store, submitter, clock=self.clock, sleeper=self.clock.sleep
        )
        queued = self.enqueue(judge, "resolve-nonce")
        ambiguous = judge.process_once()
        self.assertEqual(ambiguous["judge_state"], "ambiguous")
        self.assertIsNone(judge.resolve_ambiguous_once())

        self.clock.value += 2
        resolved = judge.resolve_ambiguous_once()

        self.assertEqual(resolved["judge_state"], "submitted")
        self.assertEqual(resolved["rid"], "c" * 24)
        self.assertEqual(submitter.resolution_calls, [queued["submission_id"]])
        self.assertEqual(len(submitter.calls), 1)

    def test_non_unique_resolution_stays_ambiguous_and_is_rate_limited(self):
        submitter = FakeSubmitter(
            [{"ok": False, "retryable": False, "ambiguous": True}],
            resolution_results=[{"ok": False, "status": "multiple"}],
        )
        judge = RealtimeJudge(
            self.store, submitter, clock=self.clock, sleeper=self.clock.sleep
        )
        queued = self.enqueue(judge, "multiple-nonce")
        judge.process_once()
        self.clock.value += 2

        checked = judge.resolve_ambiguous_once()

        self.assertEqual(checked["judge_state"], "ambiguous")
        self.assertEqual(checked["last_error"], "OJ 只读核对：multiple")
        self.assertEqual(checked["resolution_attempts"], 1)
        self.assertIsNone(judge.resolve_ambiguous_once())
        self.assertEqual(submitter.resolution_calls, [queued["submission_id"]])
        self.assertIsNone(judge.process_once())
        with self.assertRaisesRegex(RealtimeJudgeAmbiguousError, "禁止自动重发"):
            judge.ensure_submitted(queued["id"])
        self.assertEqual(len(submitter.calls), 1)

    def test_controller_restart_resolves_persisted_ambiguous_submission(self):
        first_submitter = FakeSubmitter(
            [{"ok": False, "retryable": False, "ambiguous": True}]
        )
        first_judge = RealtimeJudge(
            self.store, first_submitter, clock=self.clock, sleeper=self.clock.sleep
        )
        queued = self.enqueue(first_judge, "controller-restart")
        ambiguous = first_judge.process_once()
        self.assertEqual(ambiguous["judge_state"], "ambiguous")
        self.clock.value += 2

        self.store.close()
        self.store = Store(str(Path(self.temp.name) / "state.db"))
        restarted_submitter = FakeSubmitter(
            [],
            resolution_results=[
                {"ok": True, "status": "resolved", "rid": "d" * 24}
            ],
        )
        restarted_judge = RealtimeJudge(
            self.store,
            restarted_submitter,
            clock=self.clock,
            sleeper=self.clock.sleep,
        )

        resolved = restarted_judge.resolve_ambiguous_once()

        self.assertEqual(resolved["judge_state"], "submitted")
        self.assertEqual(resolved["rid"], "d" * 24)
        self.assertEqual(restarted_submitter.calls, [])
        self.assertEqual(
            restarted_submitter.resolution_calls, [queued["submission_id"]]
        )

    def test_resolution_network_failure_survives_restart_without_replay(self):
        first_submitter = FakeSubmitter(
            [{"ok": False, "retryable": False, "ambiguous": True}]
        )
        first_judge = RealtimeJudge(
            self.store, first_submitter, clock=self.clock, sleeper=self.clock.sleep
        )
        queued = self.enqueue(first_judge, "resolution-network-failure")
        first_judge.process_once()
        self.clock.value += 2

        class FailingResolver(FakeSubmitter):
            def resolve_submission(self, submission_id):
                self.resolution_calls.append(submission_id)
                raise OSError("status endpoint unavailable")

        failing_submitter = FailingResolver([])
        failing_judge = RealtimeJudge(
            self.store,
            failing_submitter,
            clock=self.clock,
            sleeper=self.clock.sleep,
        )
        with self.assertRaisesRegex(OSError, "status endpoint unavailable"):
            failing_judge.resolve_ambiguous_once()
        persisted = self.store.get_web_submission(queued["id"])
        self.assertEqual(persisted["judge_state"], "ambiguous")
        self.assertEqual(persisted["resolution_attempts"], 1)
        self.assertEqual(len(first_submitter.calls), 1)

        self.store.close()
        self.store = Store(str(Path(self.temp.name) / "state.db"))
        self.clock.value = persisted["resolution_after"]
        recovered_submitter = FakeSubmitter(
            [],
            resolution_results=[
                {"ok": True, "status": "resolved", "rid": "e" * 24}
            ],
        )
        recovered_judge = RealtimeJudge(
            self.store,
            recovered_submitter,
            clock=self.clock,
            sleeper=self.clock.sleep,
        )

        resolved = recovered_judge.resolve_ambiguous_once()

        self.assertEqual(resolved["judge_state"], "submitted")
        self.assertEqual(resolved["rid"], "e" * 24)
        self.assertEqual(recovered_submitter.calls, [])
        self.assertEqual(
            recovered_submitter.resolution_calls, [queued["submission_id"]]
        )

    def test_ensure_submitted_processes_older_lane_rows_first(self):
        judge, submitter = self.make_judge(
            [
                {"ok": True, "rid": "4" * 24},
                {"ok": True, "rid": "5" * 24},
            ]
        )
        first = self.enqueue(judge, "first-nonce", "first")
        target = self.enqueue(judge, "target-nonce", "target")

        result = judge.ensure_submitted(target["id"], timeout_seconds=5)

        self.assertEqual(result["rid"], "5" * 24)
        self.assertEqual(
            self.store.get_web_submission(first["id"])["rid"], "4" * 24
        )
        self.assertEqual([call["code"] for call in submitter.calls], [
            "// exact judge source\nfirst",
            "// exact judge source\ntarget",
        ])
        self.assertEqual(
            [call["submission_kind"] for call in submitter.calls],
            ["final", "final"],
        )

    def test_realtime_contest_closed_failure_can_be_retried_as_final(self):
        judge, submitter = self.make_judge(
            [
                {
                    "ok": False,
                    "retryable": False,
                    "status_code": 400,
                    "error": "contest not live",
                },
                {"ok": True, "rid": "6" * 24},
            ]
        )
        queued = self.enqueue(judge, "bad-nonce")
        result = judge.process_once()
        self.assertEqual(result["judge_state"], "permanent_failed")
        self.assertEqual(result["last_error"], "contest not live")

        delivered = judge.ensure_submitted(queued["id"])

        self.assertEqual(delivered["rid"], "6" * 24)
        self.assertEqual(len(submitter.calls), 2)
        self.assertEqual(
            [call["submission_kind"] for call in submitter.calls],
            ["realtime", "final"],
        )
        self.assertEqual(
            submitter.calls[0]["submission_id"],
            submitter.calls[1]["submission_id"],
        )

    def test_background_worker_honors_persisted_final_kind(self):
        judge, submitter = self.make_judge(
            [{"ok": True, "rid": "7" * 24}]
        )
        queued = self.enqueue(judge, "final-race-nonce")
        self.store.requeue_web_submission_for_final(queued["id"])

        delivered = judge.process_once()

        self.assertEqual(delivered["rid"], "7" * 24)
        self.assertEqual(submitter.calls[0]["submission_kind"], "final")

    def test_ensure_returns_success_that_arrives_at_timeout_boundary(self):
        clock = self.clock

        class SlowSuccessfulSubmitter(FakeSubmitter):
            def submit_one(self, *args, **kwargs):
                clock.value += 5
                return super().submit_one(*args, **kwargs)

        submitter = SlowSuccessfulSubmitter(
            [{"ok": True, "rid": "8" * 24}]
        )
        judge = RealtimeJudge(
            self.store,
            submitter,
            clock=self.clock,
            sleeper=self.clock.sleep,
        )
        queued = self.enqueue(judge, "timeout-boundary")

        delivered = judge.ensure_submitted(queued["id"], timeout_seconds=5)

        self.assertEqual(delivered["rid"], "8" * 24)

    def test_background_worker_recovers_after_store_iteration_error(self):
        judge, _ = self.make_judge([])
        stop = threading.Event()
        calls = 0

        def flaky_process_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary sqlite failure")
            stop.set()
            return None

        with mock.patch.object(
            judge, "process_once", side_effect=flaky_process_once
        ):
            judge.run_forever(stop, idle_seconds=0.001)

        health = judge.worker_health()
        self.assertEqual(calls, 2)
        self.assertEqual(health["error_count"], 1)
        self.assertEqual(health["last_error"], "")
        self.assertFalse(health["running"])


if __name__ == "__main__":
    unittest.main()
