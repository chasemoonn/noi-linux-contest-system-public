"""Read-only student feedback for verified deadline collection receipts."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from starlette.requests import Request

from tests.test_web_submit import main


class CollectionFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tid = "a" * 24
        self.directory = self.root / self.tid / "run"
        self.directory.mkdir(parents=True)
        self.seat = {"tid": self.tid, "uname": "alice", "uid": 42,
                     "candidate": "CSP001", "submit_token": "test"}
        self.contest = {"tid": self.tid, "state": "safe_wait", "title": "fixture",
                        "collection_dir": str(self.directory), "collection_run_id": "run",
                        "end_at_ms": 1000, "submission_mode": "both"}
        self.report = {"alice": {"apple": {"submission_source": "deadline_snapshot", "size": 8}},
                       "bob": {"apple": {"submission_source": "deadline_snapshot", "size": 999}}}
        self.delivered = {"alice": {"apple": {"ok": True, "rid": "b" * 24}},
                          "bob": {"apple": {"ok": True, "rid": "c" * 24, "source": "PRIVATE"}}}
        self.write_receipt()

    def write_receipt(self):
        files = {}
        for name, content in [("report.json", self.report), ("submit_log.json", self.delivered)]:
            raw = json.dumps(content).encode()
            (self.directory / name).write_bytes(raw)
            files[name] = hashlib.sha256(raw).hexdigest()
        receipt = {"schema_version": 1, "tid": self.tid, "run_id": "run",
                   "completed_at_ms": 2000, "submit_failures": 0, "files": files}
        raw = json.dumps(receipt).encode()
        (self.directory / "collection_receipt.json").write_bytes(raw)
        self.contest["collection_receipt_sha256"] = hashlib.sha256(raw).hexdigest()

    def read(self, latest=None):
        from services.collection_feedback import directory_receipts
        return directory_receipts(self.root, self.contest, self.seat, ["apple"], latest or {})

    def test_verified_feedback_is_filtered_and_does_not_include_source_or_scores(self):
        rows = self.read()
        self.assertEqual(rows["apple"]["state"], "submitted")
        self.assertEqual(rows["apple"]["size"], 8)
        self.assertEqual(set(rows["apple"]), {"state", "size", "created_at"})
        self.assertNotIn("PRIVATE", json.dumps(rows))
        self.assertNotIn("999", json.dumps(rows))

    def test_web_version_always_takes_precedence(self):
        self.assertEqual(self.read({"apple": {"id": 1}}), {})

    def test_missing_code_and_disabled_delivery_are_not_success(self):
        self.delivered["alice"]["apple"] = {"ok": True, "skipped": True, "submitted": False}
        self.write_receipt()
        self.assertEqual(self.read()["apple"]["state"], "missing")
        self.delivered = {"_status": "Hydro 回传已禁用"}
        self.write_receipt()
        self.assertEqual(self.read()["apple"]["state"], "stored")
        self.report["alice"]["apple"].pop("size")
        self.write_receipt()
        self.assertEqual(self.read()["apple"]["state"], "missing")

    def test_corrupt_missing_or_cross_contest_receipt_does_not_claim_success(self):
        for name in ["report.json", "submit_log.json", "collection_receipt.json"]:
            with self.subTest(name=name):
                self.write_receipt()
                (self.directory / name).write_text("{}")
                self.assertEqual(self.read()["apple"]["state"], "unverified")
        self.write_receipt()
        self.contest["collection_run_id"] = "wrong-run"
        self.assertEqual(self.read()["apple"]["state"], "unverified")
        self.contest["collection_dir"] = str(self.root)
        self.assertEqual(self.read()["apple"]["state"], "unverified")

    def test_other_student_and_other_problem_never_reused(self):
        self.report.pop("alice")
        self.write_receipt()
        self.assertEqual(self.read()["apple"]["state"], "unverified")

    def test_no_archive_is_read_during_active_contest(self):
        self.contest["state"] = "ready"
        self.contest["end_at_ms"] = 9999999999999
        self.assertEqual(self.read(), {})

    def test_old_receipt_is_not_reused_before_collection_completes(self):
        for state in ["ready", "collecting", "error"]:
            self.contest["state"] = state
            expected = "unverified" if state == "error" else "pending"
            self.assertEqual(self.read()["apple"]["state"], expected)

    def test_template_reports_directory_delivery_without_web_actions_or_scores(self):
        body = main.WEB_SUBMIT_PAGE.render(
            seat=self.seat, contest=self.contest, problems=["apple"], latest={},
            directory_receipts={"apple": {"state": "submitted", "size": 8,
                                          "created_at": "2026-09-11T08:00:00+00:00"}},
            opened=False, now="16:00:00",
        )
        self.assertIn("已目录收卷", body)
        self.assertIn("8B", body)
        self.assertIn("16:00:00", body)
        self.assertIn('class="judge-ok">已送入 OJ', body)
        self.assertNotIn("/view/apple", body)
        self.assertNotIn("最后一次网页递交</strong>", body)

    def test_status_revision_changes_when_directory_result_arrives(self):
        before = main._receipt_revision({}, {"apple": {"state": "pending"}})
        after = main._receipt_revision({}, {"apple": {"state": "submitted", "size": 8}})
        self.assertNotEqual(before, after)

    def test_authenticated_routes_render_verified_report_and_track_its_revision(self):
        config = {**main.cfg, "orchestrator": {
            **main.cfg["orchestrator"], "collected_dir": str(self.root)}}
        request = Request({"type": "http", "method": "GET", "path": "/submit/test", "headers": []})
        context = (self.seat, self.contest, ["apple"], {})
        with mock.patch.object(main, "cfg", config), mock.patch.object(
            main, "_web_submit_context", return_value=context
        ), mock.patch.object(main, "_submit_authenticated", return_value=True):
            body = main.web_submit_page(request, "test").body.decode()
            status = json.loads(main.web_submit_status(request, "test").body)
            self.assertIn("已目录收卷", body)
            self.assertIn('data-receipt-revision="' + status["receipt_revision"] + '"', body)
            self.assertEqual(set(status), {"server_ms", "begin_ms", "end_ms", "opened", "receipt_revision"})
            self.assertFalse(status["opened"])
            self.assertNotIn("PRIVATE", body)
            (self.directory / "report.json").unlink()
            revised = json.loads(main.web_submit_status(request, "test").body)
            self.assertNotEqual(status["receipt_revision"], revised["receipt_revision"])
            self.assertIn("暂时无法核验", main.web_submit_page(request, "test").body.decode())

    def test_unauthenticated_status_never_reads_collection_archive(self):
        request = Request({"type": "http", "method": "GET", "path": "/submit/test/status", "headers": []})
        with mock.patch.object(main, "_submit_authenticated", return_value=False), mock.patch.object(
            main, "_directory_feedback"
        ) as feedback:
            with self.assertRaises(main.HTTPException) as error:
                main.web_submit_status(request, "test")
            self.assertEqual(error.exception.status_code, 401)
            feedback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
