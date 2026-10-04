"""Focused ordinary-contest UX regressions, without network or service startup."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest import mock

from tests.test_web_submit import main


def request(path, method="GET", body=b"", headers=()):
    async def run():
        messages = []
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        async def send(message):
            messages.append(message)
        await main.app({"type": "http", "http_version": "1.1", "method": method,
                        "scheme": "https", "path": path, "raw_path": path.encode(),
                        "query_string": b"", "root_path": "", "headers": list(headers),
                        "server": ("exam.example.test", 443), "client": ("127.0.0.1", 1234)}, receive, send)
        status = next(m for m in messages if m["type"] == "http.response.start")
        content = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return status["status"], dict(status["headers"]), content.decode()
    return asyncio.run(run())


class WorkflowUXTests(unittest.TestCase):
    def test_invalid_submission_link_is_a_recoverable_html_page(self):
        with mock.patch.object(main.store, "seat_by_submit_token", return_value=None):
            status, headers, body = request("/submit/expired-test-link")
        self.assertEqual(status, 404)
        self.assertIn(b"text/html", headers[b"content-type"])
        self.assertIn("查询最新考试入口", body)
        self.assertNotIn('"detail":', body)
        self.assertIn("已有递交不会因此被覆盖", body)

    def test_missing_upload_fields_are_html_not_a_validation_dump(self):
        status, headers, body = request("/submit/test-token", "POST")
        self.assertEqual(status, 422)
        self.assertIn(b"text/html", headers[b"content-type"])
        self.assertIn("请选择 .cpp 文件", body)
        self.assertNotIn('"loc"', body)

    def test_status_endpoint_stays_json_and_requires_login(self):
        status, headers, _ = request("/submit/test-token/status")
        self.assertEqual(status, 401)
        self.assertIn(b"application/json", headers[b"content-type"])

    def test_status_tracks_changed_deadline_without_returning_credentials(self):
        token = "test-token"
        headers = [(b"cookie", f"{main._submit_cookie_name(token)}={main._submit_cookie_value(token)}".encode())]
        contest = {"begin_at_ms": 100, "end_at_ms": 200}
        with mock.patch.object(main, "_web_submit_context", return_value=({}, contest, [], {})), mock.patch.object(main, "_submission_window_open", return_value=True):
            first = json.loads(request(f"/submit/{token}/status", headers=headers)[2])
            contest["end_at_ms"] = 300
            second = json.loads(request(f"/submit/{token}/status", headers=headers)[2])
        self.assertEqual(first["end_ms"], 200)
        self.assertEqual(second["end_ms"], 300)
        self.assertEqual(set(second), {"server_ms", "begin_ms", "end_ms", "opened", "receipt_revision"})

    def test_beijing_display_is_independent_of_utc_container(self):
        self.assertEqual(main._beijing_time("2026-09-05T06:00:00+00:00"), "2026-09-05 14:00:00")

    def test_source_view_uses_the_same_beijing_time_as_the_submission_list(self):
        page = main.WEB_VIEW_PAGE.render(
            seat={"submit_token":"test-token", "candidate":"CSP001"}, problem="apple",
            submission={"created_at":"2026-09-05T06:00:00+00:00", "size":4,
                        "sha256":"test", "judge_state":"submitted", "source":"code"},
        )
        self.assertIn("2026-09-05 14:00:00", page)
        self.assertIn("北京时间", page)
        self.assertNotIn("06:00:00", page)

    def test_frozen_or_collected_seat_keeps_readonly_receipt_access(self):
        seat={"tid":"f"*24,"uid":42}
        contest={"tid":seat["tid"],"state":"safe_wait","files":'["apple"]',
                 "begin_at_ms":100,"end_at_ms":200,"hydro_rule":"oi"}
        for state in ("frozen","collected"):
            with self.subTest(state=state), mock.patch.object(main.store,"seat_by_submit_token",return_value=seat), mock.patch.object(main.store,"get_contest",return_value=contest), mock.patch.object(main.store,"seat_pool",return_value={}), mock.patch.object(main.store,"seat_pool_assignment",return_value={"state":state}), mock.patch.object(main.store,"latest_web_submissions",return_value={}):
                context=main._web_submit_context("test-token")
                self.assertEqual(context[1],contest)
                self.assertFalse(main._submission_window_open(contest))
                with mock.patch.object(main,"_submit_authenticated",return_value=True), mock.patch.object(main,"_enqueue_web_source") as enqueue:
                    with self.assertRaises(main.HTTPException) as error:
                        main.web_submit_code(mock.Mock(),"test-token","apple","a"*32,mock.Mock())
                    self.assertEqual(error.exception.status_code,409)
                    enqueue.assert_not_called()

    def test_unreleased_seat_does_not_gain_readonly_access_from_elapsed_time(self):
        seat={"tid":"f"*24,"uid":42}
        contest={"state":"safe_wait","end_at_ms":200}
        with mock.patch.object(main.store,"seat_by_submit_token",return_value=seat), mock.patch.object(main.store,"get_contest",return_value=contest), mock.patch.object(main.store,"seat_pool",return_value={}), mock.patch.object(main.store,"seat_pool_assignment",return_value={"state":"reserved"}):
            with self.assertRaises(main.HTTPException) as error:
                main._web_submit_context("test-token")
            self.assertEqual(error.exception.status_code,403)

    def test_early_collection_is_rejected_before_changing_state(self):
        with mock.patch.object(main.hydro, "get_contest", return_value={"endAt": datetime.now(timezone.utc)+timedelta(hours=1)}), mock.patch.object(main.store, "transition") as transition, mock.patch.object(main, "_spawn") as spawn:
            with self.assertRaisesRegex(main.HTTPException, "先在 OJ 修改结束时间"):
                main.admin_collect("f"*24, main.ADMIN_CSRF, "teacher")
        transition.assert_not_called()
        spawn.assert_not_called()

    def test_disabled_ai_cannot_be_registered_through_a_stale_form(self):
        with mock.patch.object(main, "artifact_runner", None), mock.patch.object(main.store, "upsert_contest") as register:
            with self.assertRaisesRegex(main.HTTPException, "未启用自动生成"):
                main.admin_register(tid="f"*24, materials_mode="ai", csrf=main.ADMIN_CSRF, _="teacher")
        register.assert_not_called()

    def test_teacher_default_is_manual_and_keeps_publication_explicit(self):
        page = main.ADMIN_V1_PAGE.render(section="overview", section_label="比赛总览", sections=main.ADMIN_SECTIONS, contests=[], csrf="test", ai_enabled=False, defaults={"release_lead_minutes":5,"practice_groups":3})
        self.assertIn('value="manual" selected', page)
        self.assertNotIn('<option value="ai">', page)
        self.assertIn("保存待审核材料", page)
        self.assertNotIn("提前结束 / 重试收卷", page)

    def test_unexpected_error_never_exposes_remote_commands(self):
        scope = {"type":"http", "path":"/submit/token", "path_params":{"token":"token"}, "headers":[]}
        response = main._workflow_error(main.Request(scope), 500, "docker exec password SECRET traceback")
        self.assertNotIn("SECRET", response.body.decode())
        self.assertNotIn("docker", response.body.decode())

    def test_teacher_operation_pages_refresh_only_while_work_is_active(self):
        for section in ("seats", "submissions", "finish"):
            for state in ("preparing", "ready", "collecting", "safe_wait"):
                self.assertTrue(main._teacher_live_refresh(section, [{"state": state}]))
            for state in ("registered", "safe_ended", "error"):
                self.assertFalse(main._teacher_live_refresh(section, [{"state": state}]))
        for section in ("overview", "materials"):
            self.assertFalse(main._teacher_live_refresh(section, [{"state": "ready"}]))
        self.assertFalse(main._teacher_live_refresh("seats", []))

    def test_teacher_refresh_preserves_form_edits_and_open_details(self):
        page = main.ADMIN_V1_PAGE.render(section="seats", section_label="学生座位",
            sections=main.ADMIN_SECTIONS, contests=[], csrf="test", live_refresh=True)
        self.assertIn("运行状态每 15 秒自动更新", page)
        self.assertIn("document.hidden || editing || submitting", page)
        self.assertIn("document.querySelector('details[open]')", page)
        self.assertIn("document.addEventListener('input', preserveInput)", page)
        self.assertIn("document.addEventListener('change', preserveInput)", page)
        self.assertNotIn("已验收备用", page)

    def test_resend_is_scoped_and_idempotent_without_exposing_credentials(self):
        tid = "f"*24
        contest = {"state":"ready", "end_at_ms":int((datetime.now(timezone.utc)+timedelta(hours=1)).timestamp()*1000)}
        with mock.patch.object(main, "notifier", object()), mock.patch.object(main.store, "get_contest", return_value=contest), mock.patch.object(main.store, "seat_pool_assignment", return_value={"slot_no":3,"state":"released"}), mock.patch.object(main.store, "seat_pool_resource", return_value={"credential_revision":2}), mock.patch.object(main.store, "queue_seat_notification") as queue, mock.patch.object(main, "_spawn_notification_retry"), mock.patch.object(main, "_audit_teacher"):
            first = main.admin_resend_notification(tid, 42, "a"*32, main.ADMIN_CSRF, "teacher")
            main.admin_resend_notification(tid, 42, "a"*32, main.ADMIN_CSRF, "teacher")
        self.assertEqual(queue.call_args_list[0], queue.call_args_list[1])
        self.assertEqual(first.headers["location"], "/admin/seats")
        with self.assertRaisesRegex(main.HTTPException, "只允许管理所选比赛"):
            main.admin_resend_notification(tid, 42, "a"*32, main.ADMIN_CSRF, main.TeacherIdentity(42,"coach","e"*24,1900000000))


if __name__ == "__main__":
    unittest.main()
