"""Tests for the Beijing-style program collection workflow."""
from __future__ import annotations

import atexit
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request

# Test state belongs in the platform temporary directory.  Keeping it out of
# ``tests/`` lets the suite run against a read-only application image and
# prevents interrupted runs from contaminating release archives.
_runtime = Path(tempfile.mkdtemp(prefix="noi-web-submit-tests-"))


def _service_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if os.name == "nt":
        # The production config contract deliberately accepts only
        # slash-prefixed service paths.  A Windows extended path preserves that
        # contract while still addressing the real platform temp directory.
        return "//?/" + value
    return value


_config = _runtime / "config.yaml"
_config.write_text(
    f"""
cloud:
  provider: aliyun
  aliyun:
    access_key_id: test
    access_key_secret: test
    region_id: cn-test
    instance_id: i-test
contest_server:
  ssh_user: root
  ssh_key: /keys/test
  strict_host_key: false
  seats_root: /data/seats
  docker_image: noi-linux-official:2.0
  docker_network: seats
hydro:
  public_base_url: https://oj.example.test
  internal_base_url: http://127.0.0.1:8888
  mongo_uri: mongodb://127.0.0.1:27017/test
  domain_id: system
  orchestrator_token: 0123456789abcdef0123456789abcdef
  submit_enabled: false
orchestrator:
  admin_password: 1234567890123456
  db: {_service_path(_runtime / 'state.db')}
  collected_dir: {_service_path(_runtime / 'collected')}
  materials_dir: {_service_path(_runtime / 'materials')}
  public_base_url: https://exam.example.test
  prepare_before_minutes: 15
frontend_proxy:
  provider: none
""",
    encoding="utf-8",
)
os.environ["ORCHESTRATOR_CONFIG"] = str(_config)

with mock.patch("services.cloud.make_cvm", return_value=mock.Mock()):
    import main


def _cleanup_runtime() -> None:
    main.store.close()
    shutil.rmtree(_runtime, ignore_errors=True)


atexit.register(_cleanup_runtime)


class CloudAdminRouteTests(unittest.TestCase):
    def test_manual_shutdown_uses_fail_closed_pipeline_operation(self):
        with mock.patch.object(main.pipe, "shutdown_server") as shutdown, mock.patch.object(
            main.cvm, "status"
        ) as direct_status, mock.patch.object(main.cvm, "stop") as direct_stop:
            response = main.admin_shutdown(main.ADMIN_CSRF, "teacher")

        shutdown.assert_called_once_with()
        direct_status.assert_not_called()
        direct_stop.assert_not_called()
        self.assertEqual(response.status_code, 303)

    def test_manual_boot_uses_stale_rule_barrier(self):
        with mock.patch.object(main.pipe, "boot_server") as boot, mock.patch.object(
            main.cvm, "status"
        ) as direct_status, mock.patch.object(main.cvm, "start") as direct_start:
            response = main.admin_boot(main.ADMIN_CSRF, "teacher")

        boot.assert_called_once_with()
        direct_status.assert_not_called()
        direct_start.assert_not_called()
        self.assertEqual(response.status_code, 303)

    def test_teacher_can_requeue_failed_current_notifications(self):
        tid = "3" * 24
        with mock.patch.object(main, "notifier", object()), mock.patch.object(
            main.store, "retry_failed_seat_notifications", return_value=4
        ) as retry, mock.patch.object(main, "_spawn_notification_retry") as spawn:
            response = main.admin_retry_notifications(tid, main.ADMIN_CSRF, "teacher")

        retry.assert_called_once_with(tid)
        spawn.assert_called_once_with(tid)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/submissions")

    def test_teacher_notification_retry_fails_closed_when_disabled(self):
        with mock.patch.object(main, "notifier", None):
            with self.assertRaisesRegex(HTTPException, "入口通知未启用"):
                main.admin_retry_notifications("3" * 24, main.ADMIN_CSRF, "teacher")

    def test_teacher_seat_export_contains_status_but_never_credentials(self):
        tid = "4" * 24
        main.store.upsert_contest(tid, "status export", ["apple"], {"apple": "P1"})
        main.store.add_seat(
            tid,
            7,
            "alice",
            "secret-gateway-token",
            "secret-vnc-password",
            "secret-submit-token",
            "CSP007",
            "seat-fixture",
            "172.18.0.7",
        )
        main.store.put_seat_pool(
            tid,
            None,
            {
                "schema_version": 1,
                "tid": tid,
                "revision": 1,
                "seats": [
                    {"slot_no": 1, "uid": 7, "uname": "alice", "state": "released"}
                ],
            },
        )

        response = main.admin_export(tid, "teacher")
        body = response.body.decode("utf-8-sig")

        self.assertIn("用户名,考号,座位号,座位状态", body)
        self.assertIn("alice,CSP007,1,released", body)
        self.assertNotIn("secret-gateway-token", body)
        self.assertNotIn("secret-vnc-password", body)
        self.assertNotIn("secret-submit-token", body)

    def test_reconcile_wrapper_keeps_service_alive_for_scheduled_retry(self):
        with mock.patch.object(
            main.pipe,
            "reconcile_frontend",
            side_effect=RuntimeError("cloud unavailable"),
        ), mock.patch.object(main.log, "exception") as logged:
            main._reconcile_frontend()

        logged.assert_called_once_with("frontend/cloud state reconciliation failed")

    def test_direct_frontend_reconcile_failure_stops_only_contest_vm(self):
        with mock.patch.object(
            main.pipe,
            "reconcile_frontend",
            side_effect=RuntimeError("caddy reload failed"),
        ), mock.patch.object(
            main.pipe, "_direct_access_enabled", return_value=True
        ), mock.patch.object(main.pipe, "shutdown_server") as shutdown, mock.patch.object(
            main.log, "exception"
        ) as logged:
            self.assertFalse(main._reconcile_frontend(force=True))

        shutdown.assert_called_once_with()
        logged.assert_called_once_with("frontend/cloud state reconciliation failed")

    def test_scheduler_start_failure_still_runs_fail_closed_cleanup(self):
        scheduler = mock.MagicMock()
        scheduler.start.side_effect = RuntimeError("scheduler start failed")

        async def exercise():
            async with main.lifespan(main.app):
                self.fail("lifespan must not yield after scheduler start failure")

        with mock.patch.object(
            main, "BackgroundScheduler", return_value=scheduler
        ), mock.patch.object(main, "_reconcile_frontend", return_value=True), mock.patch.object(
            main.pipe, "reconcile_desktop_access"
        ), mock.patch.object(main.pipe, "begin_shutdown") as closing, mock.patch.object(
            main.pipe, "fail_closed_desktop_cleanup"
        ) as cleanup, mock.patch.object(main.hydro, "close"), mock.patch.object(
            main.store, "close"
        ), mock.patch.object(main, "realtime_judge", None):
            with self.assertRaisesRegex(RuntimeError, "scheduler start failed"):
                asyncio.run(exercise())

        closing.assert_called_once_with()
        cleanup.assert_called_once_with()
        scheduler.shutdown.assert_not_called()

    def test_realtime_thread_start_failure_still_revokes_and_stops_scheduler(self):
        scheduler = mock.MagicMock()
        thread = mock.MagicMock()
        thread.start.side_effect = RuntimeError("worker start failed")

        async def exercise():
            async with main.lifespan(main.app):
                self.fail("lifespan must not yield after worker start failure")

        with mock.patch.object(
            main, "BackgroundScheduler", return_value=scheduler
        ), mock.patch.object(main, "_reconcile_frontend", return_value=True), mock.patch.object(
            main.pipe, "reconcile_desktop_access"
        ), mock.patch.object(main.pipe, "begin_shutdown") as closing, mock.patch.object(
            main.pipe, "fail_closed_desktop_cleanup"
        ) as cleanup, mock.patch.object(main.hydro, "close"), mock.patch.object(
            main.store, "close"
        ), mock.patch.object(main, "realtime_judge", mock.MagicMock()), mock.patch.object(
            main.threading, "Thread", return_value=thread
        ):
            with self.assertRaisesRegex(RuntimeError, "worker start failed"):
                asyncio.run(exercise())

        closing.assert_called_once_with()
        cleanup.assert_called_once_with()
        scheduler.shutdown.assert_called_once_with(wait=False)
        thread.join.assert_not_called()

    def test_scheduler_shutdown_failure_cannot_skip_final_cloud_cleanup(self):
        scheduler = mock.MagicMock()
        scheduler.shutdown.side_effect = RuntimeError("scheduler shutdown failed")

        async def exercise():
            async with main.lifespan(main.app):
                pass

        with mock.patch.object(
            main, "BackgroundScheduler", return_value=scheduler
        ), mock.patch.object(main, "_reconcile_frontend", return_value=True), mock.patch.object(
            main.pipe, "reconcile_desktop_access"
        ), mock.patch.object(main.pipe, "begin_shutdown"), mock.patch.object(
            main.pipe, "fail_closed_desktop_cleanup"
        ) as cleanup, mock.patch.object(main.hydro, "close"), mock.patch.object(
            main.store, "close"
        ), mock.patch.object(main, "realtime_judge", None), mock.patch.object(
            main.log, "exception"
        ):
            asyncio.run(exercise())

        cleanup.assert_called_once_with()


class WebSubmitSourceTests(unittest.TestCase):
    def test_pasted_source_normalizes_newlines(self):
        self.assertEqual(
            main._normalize_pasted_source("int main(){}\r\n", 1024),
            "int main(){}\n",
        )

    def test_uploaded_source_accepts_gb18030(self):
        source = "// 中文\r\nint main(){}\r\n".encode("gb18030")
        self.assertEqual(
            main._normalize_uploaded_source(source, 1024),
            "// 中文\nint main(){}\n",
        )

    def test_source_rejects_blank_nul_and_oversize(self):
        for source in ("   \n", "int main(){}\x00"):
            with self.subTest(source=source), self.assertRaises(HTTPException):
                main._normalize_pasted_source(source, 1024)
        with self.assertRaises(HTTPException) as raised:
            main._normalize_pasted_source("12345", 4)
        self.assertEqual(raised.exception.status_code, 413)


class MaterialAdminRouteTests(unittest.TestCase):
    @staticmethod
    def _contest(tid: str) -> None:
        main.store.upsert_contest(
            tid,
            "材料批准安全测试",
            ["apple"],
            {"apple": "P1001"},
            materials_mode="ai",
            material_state="pending",
            begin_at_ms=1_786_000_000_000,
            end_at_ms=1_786_018_000_000,
            hydro_rule="oi",
        )

    @staticmethod
    def _revision(tid: str, revision: str) -> None:
        main.store.put_artifact_revision(
            tid,
            revision,
            state="review",
            source_sha256="1" * 64,
            root_path="/artifacts/fixture",
            manifest_sha256="2" * 64,
            manifest={"schema_version": 1},
            paper_name="paper.pdf",
            paper_sha256="3" * 64,
            paper_size=10,
            testdata_name="testdata.tar.gz",
            testdata_sha256="4" * 64,
            testdata_size=20,
            testdata_files=6,
            testdata_expanded_size=60,
        )

    @staticmethod
    def _publication(
        tid: str,
        revision: str,
        *,
        paper_sha256: str = "3" * 64,
        paper_size: int = 10,
        testdata_sha256: str = "4" * 64,
        testdata_size: int = 20,
    ) -> dict:
        receipt = {
            "publication_id": main.hashlib.sha256(
                f"{tid}:{revision}".encode("ascii")
            ).hexdigest(),
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
            "receipt_sha256": main.store._canonical_json_sha256(receipt),
        }

    def test_generate_route_requires_enabled_safe_runner(self):
        with mock.patch.object(main, "artifact_runner", None):
            with self.assertRaises(HTTPException) as raised:
                main.admin_material_generate(
                    "a" * 24, main.ADMIN_CSRF, "teacher"
                )
        self.assertEqual(raised.exception.status_code, 503)

    def test_ai_registration_allows_empty_mapping_and_reads_hydro_order(self):
        tid = "6" * 24
        begin = datetime.now(timezone.utc) + timedelta(hours=1)
        document = {
            "title": "自动题目映射",
            "rule": "oi",
            "beginAt": begin,
            "endAt": begin + timedelta(hours=5),
            "pids": [102, 101],
        }
        problems = {
            102: {
                "docId": 102,
                "pid": "P1002",
                "config": {"filename": "banana.out"},
            },
            101: {
                "docId": 101,
                "pid": "apple",
                "config": {},
            },
            "P1002": {"docId": 102, "pid": "P1002"},
            "apple": {"docId": 101, "pid": "apple"},
        }
        with mock.patch.object(
            main.hydro, "get_contest", return_value=document
        ), mock.patch.object(
            main.hydro, "get_problem", side_effect=lambda pid: problems.get(pid)
        ), mock.patch.dict(
            main.cfg["hydro"], {"submit_enabled": True}
        ):
            response = main.admin_register(
                tid=tid,
                files="",
                submission_mode="folder",
                materials_mode="ai",
                max_participants=3,
                spare_seats=1,
                release_lead_minutes=5,
                practice_groups=3,
                csrf=main.ADMIN_CSRF,
                paper=None,
                testdata=None,
                _="teacher",
            )
        stored = main.store.get_contest(tid)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(stored["files"], '["banana", "apple"]')
        self.assertEqual(
            main.json.loads(stored["pids"]), {"apple": "apple", "banana": "P1002"}
        )

    def test_manual_same_file_reregistration_creates_new_session_revision(self):
        tid = "7" * 24
        begin = datetime.now(timezone.utc) + timedelta(hours=1)
        document = {
            "title": "人工材料重新登记",
            "rule": "oi",
            "beginAt": begin,
            "endAt": begin + timedelta(hours=5),
            "pids": [101],
        }
        main.store.upsert_contest(
            tid,
            document["title"],
            ["apple"],
            {},
            materials_mode="manual",
            material_state="review",
            begin_at_ms=int(begin.timestamp() * 1000),
            end_at_ms=int((begin + timedelta(hours=5)).timestamp() * 1000),
            hydro_rule="oi",
        )
        payload = b"%PDF-1.4\nmanual fixture\n%%EOF\n"
        digest = main.hashlib.sha256(payload).hexdigest()
        main.save_material_paper(
            main.cfg["orchestrator"]["materials_dir"], tid, payload
        )
        main.store.set_paper(tid, "paper.pdf", digest, len(payload))
        testdata = b"fixture tar.gz"
        testdata_digest = main.hashlib.sha256(testdata).hexdigest()
        main.save_testdata_archive(
            main.cfg["orchestrator"]["materials_dir"], tid, testdata
        )
        main.store.set_testdata(
            tid,
            "testdata.tar.gz",
            testdata_digest,
            len(testdata),
            6,
            60,
        )
        publication_side_effect = lambda **kwargs: self._publication(
            kwargs["tid"],
            kwargs["revision"],
            paper_sha256=kwargs["paper_sha256"],
            paper_size=len(payload),
            testdata_sha256=kwargs["testdata_sha256"],
            testdata_size=len(testdata),
        )
        with mock.patch.object(
            main.material_publisher,
            "publish",
            side_effect=publication_side_effect,
        ):
            first = main._approve_manual_artifact(tid, "teacher")

        with mock.patch.object(
            main.hydro, "get_contest", return_value=document
        ), mock.patch.object(
            main.hydro, "get_problem", return_value={"docId": 101, "pid": "apple"}
        ), mock.patch.dict(main.cfg["hydro"], {"submit_enabled": True}), mock.patch.object(
            main.material_publisher,
            "publish",
            side_effect=publication_side_effect,
        ):
            response = main.admin_register(
                tid=tid,
                files="apple=101",
                submission_mode="folder",
                materials_mode="manual",
                max_participants=3,
                spare_seats=1,
                release_lead_minutes=5,
                practice_groups=3,
                csrf=main.ADMIN_CSRF,
                paper=None,
                testdata=None,
                _="teacher",
            )

        stored = main.store.get_contest(tid)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(stored["material_state"], "approved")
        self.assertNotEqual(stored["active_material_revision"], first["revision"])
        self.assertEqual(
            main.store.artifact_revision(tid, first["revision"])["state"],
            "superseded",
        )

    def test_ai_to_manual_without_legacy_pdf_is_rejected_without_state_change(self):
        tid = "8" * 24
        begin = datetime.now(timezone.utc) + timedelta(hours=1)
        document = {
            "title": "AI 转人工材料",
            "rule": "oi",
            "beginAt": begin,
            "endAt": begin + timedelta(hours=5),
            "pids": [101],
        }
        self._contest(tid)
        self._revision(tid, "ai-approved")
        main.store.approve_artifact_with_publication(
            tid,
            "ai-approved",
            "teacher",
            self._publication(tid, "ai-approved"),
        )
        before = main.store.get_contest(tid)

        with mock.patch.object(
            main.hydro, "get_contest", return_value=document
        ), mock.patch.object(
            main.hydro, "get_problem", return_value={"docId": 101, "pid": "apple"}
        ), mock.patch.dict(main.cfg["hydro"], {"submit_enabled": True}):
            with self.assertRaisesRegex(HTTPException, "重新上传") as raised:
                main.admin_register(
                    tid=tid,
                    files="apple=101",
                    submission_mode="folder",
                    materials_mode="manual",
                    max_participants=3,
                    spare_seats=1,
                    release_lead_minutes=5,
                    practice_groups=3,
                    csrf=main.ADMIN_CSRF,
                    paper=None,
                    testdata=None,
                    _="teacher",
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(main.store.get_contest(tid), before)

    def test_generate_route_only_queues_background_job_and_redirects_absolute(self):
        runner = mock.Mock()
        runner.start.return_value = {
            "job_id": "a" * 32,
            "details": {"stage": "queued"},
        }
        thread = mock.Mock()
        with mock.patch.object(main, "artifact_runner", runner), mock.patch.object(
            main.threading, "Thread", return_value=thread
        ) as thread_class:
            response = main.admin_material_generate(
                "b" * 24, main.ADMIN_CSRF, "teacher"
            )
        runner.start.assert_called_once_with("b" * 24, "teacher")
        thread_class.assert_called_once_with(
            target=runner.run,
            args=("a" * 32,),
            name="artifact-aaaaaaaa",
            daemon=True,
        )
        thread.start.assert_called_once_with()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin")

    def test_material_approval_redirect_is_absolute_admin(self):
        with mock.patch.object(main, "_activate_generated_artifact") as activate:
            response = main.admin_material_approve(
                "c" * 24,
                "ai-r1",
                main.ADMIN_CSRF,
                "teacher",
            )
        activate.assert_called_once_with("c" * 24, "ai-r1", "teacher")
        self.assertEqual(response.headers["location"], "/admin")

    def test_superseded_revision_is_rejected_before_any_legacy_file_write(self):
        tid = "d" * 24
        self._contest(tid)
        self._revision(tid, "ai-old")
        main.store.approve_artifact_with_publication(
            tid,
            "ai-old",
            "teacher",
            self._publication(tid, "ai-old"),
        )
        self._revision(tid, "ai-new")
        main.store.approve_artifact_with_publication(
            tid,
            "ai-new",
            "teacher",
            self._publication(tid, "ai-new"),
        )
        with mock.patch.object(main, "save_material_paper") as save_paper, mock.patch.object(
            main, "save_testdata_archive"
        ) as save_data:
            with self.assertRaisesRegex(Exception, "机器校验"):
                main._activate_generated_artifact(tid, "ai-old", "teacher")
        save_paper.assert_not_called()
        save_data.assert_not_called()

    def test_ready_contest_is_rejected_before_any_legacy_file_write(self):
        tid = "e" * 24
        self._contest(tid)
        self._revision(tid, "ai-ready")
        main.store.set_state(tid, "ready", "fixture")
        with mock.patch.object(main, "save_material_paper") as save_paper, mock.patch.object(
            main, "save_testdata_archive"
        ) as save_data:
            with self.assertRaisesRegex(Exception, "预热"):
                main._activate_generated_artifact(tid, "ai-ready", "teacher")
        save_paper.assert_not_called()
        save_data.assert_not_called()


class WebSubmitSessionTests(unittest.TestCase):
    def test_signed_cookie_authenticates_only_matching_token(self):
        token = "seat-submit-token"
        name = main._submit_cookie_name(token)
        value = main._submit_cookie_value(token)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [(b"cookie", f"{name}={value}".encode("ascii"))],
            }
        )
        self.assertTrue(main._submit_authenticated(request, token))
        self.assertFalse(main._submit_authenticated(request, "another-token"))

    def test_managed_pool_without_assignment_blocks_student_desktop(self):
        contest = {"tid": "a" * 24, "release_lead_minutes": 5}
        seat = {
            "uid": 7,
            "uname": "alice",
            "token": "must-not-leak",
            "submit_token": "must-not-leak-either",
            "vnc_pass": "secret",
        }
        with mock.patch.object(
            main.hydro, "verify_login", return_value=True
        ), mock.patch.object(
            main.store, "contests", return_value=[contest]
        ), mock.patch.object(
            main.store, "seat_by_uname", return_value=seat
        ), mock.patch.object(
            main.store, "seat_pool", return_value={"state": {"seats": []}}
        ), mock.patch.object(
            main.store, "seat_pool_assignment", return_value=None
        ), mock.patch.object(main.cvm, "status") as cloud_status:
            response = main.student_query("alice", "password")

        body = response.body.decode("utf-8")
        self.assertIn("座位分配校验失败", body)
        self.assertNotIn("must-not-leak", body)
        cloud_status.assert_not_called()

    def test_managed_pool_without_assignment_blocks_submit_context(self):
        seat = {"tid": "b" * 24, "uid": 7}
        with mock.patch.object(
            main.store, "seat_by_submit_token", return_value=seat
        ), mock.patch.object(
            main.store, "get_contest", return_value={"tid": seat["tid"]}
        ), mock.patch.object(
            main.store, "seat_pool", return_value={"state": {"seats": []}}
        ), mock.patch.object(
            main.store, "seat_pool_assignment", return_value=None
        ), mock.patch.object(
            main.store, "latest_web_submissions"
        ) as latest:
            with self.assertRaises(HTTPException) as raised:
                main._web_submit_context("token")

        self.assertEqual(raised.exception.status_code, 403)
        self.assertIn("座位分配校验失败", raised.exception.detail)
        latest.assert_not_called()

    def test_explicit_legacy_contest_without_pool_keeps_submit_compatibility(self):
        seat = {"tid": "c" * 24, "uid": 7}
        contest = {
            "tid": seat["tid"],
            "submission_mode": "web",
            "files": '["apple"]',
        }
        with mock.patch.object(
            main.store, "seat_by_submit_token", return_value=seat
        ), mock.patch.object(
            main.store, "get_contest", return_value=contest
        ), mock.patch.object(
            main.store, "seat_pool", return_value=None
        ), mock.patch.object(
            main.store, "seat_pool_assignment", return_value=None
        ), mock.patch.object(
            main.store, "latest_web_submissions", return_value={}
        ):
            context = main._web_submit_context("token")

        self.assertEqual(context, (seat, contest, ["apple"], {}))

    def test_login_cookie_secure_matches_configured_frontend_scheme(self):
        context = (
            {
                "candidate": "alice",
                "uname": "alice",
                "vnc_pass": "password",
            },
            {},
            [],
            {},
        )
        for public_base_url, secure in (
            ("https://exam.example.test", True),
            ("http://127.0.0.1:8600", False),
        ):
            configured = {
                "orchestrator": {
                    "admin_password": "1234567890123456",
                    "public_base_url": public_base_url,
                }
            }
            with self.subTest(public_base_url=public_base_url), mock.patch.object(
                main, "cfg", configured
            ), mock.patch.object(
                main, "_web_submit_context", return_value=context
            ):
                response = main.web_submit_login("token", "alice", "password")

            cookie = response.headers["set-cookie"].lower()
            self.assertEqual("secure" in cookie, secure)
            self.assertIn("httponly", cookie)
            self.assertIn("samesite=strict", cookie)

    def test_non_ascii_password_returns_visible_error_without_retaining_password(self):
        context = (
            {
                "candidate": "CSP001",
                "uname": "alice",
                "vnc_pass": "password",
                "submit_token": "token",
            },
            {"title": "登录回归赛", "state": "ready"},
            [],
            {},
        )
        with mock.patch.object(main, "_web_submit_context", return_value=context):
            response = main.web_submit_login("token", " CSP001 ", "中文错密")

        body = response.body.decode("utf-8")
        self.assertEqual(response.status_code, 401)
        self.assertIn("准考证号或密码错误", body)
        self.assertIn('role="alert" aria-live="assertive"', body)
        self.assertIn('value="CSP001"', body)
        self.assertNotIn("中文错密", body)
        self.assertIn("登录回归赛", body)
        self.assertIn("状态：进行中", body)

    def test_accepted_credentials_without_cookie_show_explicit_browser_error(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/submit/token",
                "headers": [],
            }
        )
        context = (
            {
                "candidate": "CSP001",
                "uname": "alice",
                "vnc_pass": "password",
                "submit_token": "token",
            },
            {"title": "fixture", "state": "ready"},
            ["apple"],
            {},
        )
        with mock.patch.object(
            main, "_web_submit_context", return_value=context
        ), mock.patch.object(main, "_submit_authenticated", return_value=False):
            response = main.web_submit_page(request, "token", login="accepted")

        body = response.body.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("账号密码正确，但浏览器未保存登录状态", body)
        self.assertIn('role="alert" aria-live="assertive"', body)

    def test_successful_login_redirect_marks_cookie_roundtrip(self):
        context = (
            {
                "candidate": "CSP001",
                "uname": "alice",
                "vnc_pass": "password",
            },
            {"title": "fixture", "state": "ready"},
            ["apple"],
            {},
        )
        with mock.patch.object(main, "_web_submit_context", return_value=context):
            response = main.web_submit_login("token", "CSP001", "password")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/submit/token?login=accepted")
        self.assertIn("set-cookie", response.headers)

    def test_completed_contest_login_page_explains_stale_link_and_submit_state(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/submit/token",
                "headers": [],
            }
        )
        context = (
            {
                "candidate": "CSP001",
                "uname": "alice",
                "vnc_pass": "password",
                "submit_token": "token",
            },
            {"title": "已经结束的旧赛", "state": "done"},
            [],
            {},
        )
        with mock.patch.object(
            main, "_web_submit_context", return_value=context
        ), mock.patch.object(main, "_submit_authenticated", return_value=False):
            response = main.web_submit_page(request, "token")

        body = response.body.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("已经结束的旧赛", body)
        self.assertIn("状态：已结束", body)
        self.assertIn("此入口已结束，请使用最新链接。", body)
        self.assertIn('role="status" aria-live="polite"', body)
        self.assertIn("正在验证…", body)
        self.assertIn('window.addEventListener("pageshow",reset)', body)
        self.assertIn("script-src 'nonce-", response.headers["content-security-policy"])
        self.assertIn('<script nonce="', body)

    def test_gateway_url_uses_shared_vnc_tuning(self):
        configured = {
            "contest_server": {
                "gateway_public_base_url": "https://exam.example.test",
                "no_vnc_quality": 3,
                "no_vnc_compression": 2,
            }
        }
        with mock.patch.object(main, "cfg", configured):
            url = main.gateway_url("198.51.100.10", "seat-token")
        self.assertIn("quality=3&compression=2", url)
        self.assertIn("path=s/seat-token/websockify", url)

    def test_gateway_url_defaults_to_high_quality_with_conservative_compression(self):
        configured = {
            "contest_server": {
                "gateway_public_base_url": "http://198.51.100.10",
            }
        }
        with mock.patch.object(main, "cfg", configured):
            url = main.gateway_url("198.51.100.10", "seat-token")
        self.assertIn("quality=9&compression=2", url)
        with mock.patch.object(main, "cfg", configured):
            notification_url = main.gateway_url("", "seat-token")
        self.assertTrue(notification_url.startswith("http://198.51.100.10/"))

    def test_http_gateway_notification_uses_https_one_hop_redirect(self):
        configured = {
            "orchestrator": {
                "public_base_url": "https://exam.example.test",
            },
            "contest_server": {
                "gateway_public_base_url": "http://198.51.100.10",
            },
        }
        with mock.patch.object(main, "cfg", configured):
            url = main.notification_desktop_url("", "seat-token-value-12345")
        self.assertEqual(
            url,
            "https://exam.example.test/desktop/seat-token-value-12345",
        )

    def test_https_gateway_notification_keeps_direct_url(self):
        configured = {
            "contest_server": {
                "gateway_public_base_url": "https://exam.example.test",
            },
        }
        with mock.patch.object(main, "cfg", configured):
            url = main.notification_desktop_url("", "seat-token-value-12345")
        self.assertTrue(
            url.startswith(
                "https://exam.example.test/s/seat-token-value-12345/vnc.html?"
            )
        )

    def test_notification_launch_page_offers_direct_and_https_fallback(self):
        # ``Pipeline`` generates gateway tokens with token_urlsafe(12), which
        # produces a 16-character base64url token in production.
        token = "AbCdEf012345_-xy"
        tid = "f" * 24
        configured = {
            "orchestrator": {
                "public_base_url": "https://exam.example.test",
            },
            "contest_server": {
                "gateway_public_base_url": "http://198.51.100.10",
            },
        }
        contest = {
            "tid": tid,
            "state": "ready",
            "end_at_ms": int(
                (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
                * 1000
            ),
        }
        with mock.patch.object(main, "cfg", configured), mock.patch.object(
            main.store,
            "seat_by_gateway_token",
            return_value={"tid": tid, "uid": 7, "token": token},
        ), mock.patch.object(
            main.store, "get_contest", return_value=contest
        ), mock.patch.object(
            main.store, "contests", return_value=[contest]
        ), mock.patch.object(
            main.store, "seat_pool", return_value={"state": {}}
        ), mock.patch.object(
            main.store,
            "seat_pool_assignment",
            return_value={"state": "released"},
        ), mock.patch.object(main.cvm, "status") as cloud_status:
            response = main.student_desktop_redirect(token)

        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn(
            f"http://198.51.100.10/s/{token}/vnc.html?", body
        )
        self.assertIn(
            f"https://exam.example.test/s/{token}/vnc.html?", body
        )
        self.assertIn("高速直连（推荐）", body)
        self.assertIn("兼容入口（较慢）", body)
        self.assertIn(f"path=s/{token}/websockify", body)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("default-src 'none'", response.headers["content-security-policy"])
        cloud_status.assert_not_called()

    def test_notification_redirect_fails_closed_before_release(self):
        token = "seat-token-value-1234567890"
        tid = "1" * 24
        contest = {
            "tid": tid,
            "state": "ready",
            "end_at_ms": int(
                (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
                * 1000
            ),
        }
        with mock.patch.object(
            main.store,
            "seat_by_gateway_token",
            return_value={"tid": tid, "uid": 7, "token": token},
        ), mock.patch.object(
            main.store, "get_contest", return_value=contest
        ), mock.patch.object(
            main.store, "contests", return_value=[contest]
        ), mock.patch.object(
            main.store, "seat_pool", return_value={"state": {}}
        ), mock.patch.object(
            main.store,
            "seat_pool_assignment",
            return_value={"state": "reserved"},
        ), mock.patch.object(main.cvm, "status") as cloud_status:
            with self.assertRaises(HTTPException) as raised:
                main.student_desktop_redirect(token)

        self.assertEqual(raised.exception.status_code, 404)
        cloud_status.assert_not_called()

    def test_notification_redirect_rejects_short_gateway_token_before_lookup(self):
        with mock.patch.object(
            main.store, "seat_by_gateway_token"
        ) as seat_lookup:
            with self.assertRaises(HTTPException) as raised:
                main.student_desktop_redirect("A" * 15)

        self.assertEqual(raised.exception.status_code, 404)
        seat_lookup.assert_not_called()

    def test_direct_reconcile_wrapper_keeps_admin_service_alive(self):
        with mock.patch.object(
            main.pipe,
            "reconcile_desktop_access",
            side_effect=RuntimeError("cloud unavailable"),
        ), mock.patch.object(main.log, "exception") as logged:
            main._reconcile_desktop_access()

        logged.assert_called_once_with(
            "desktop security-group reconciliation failed"
        )

    def test_submission_window_uses_hydro_begin_and_end_exclusively(self):
        now = datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc)
        document = {
            "beginAt": now,
            "endAt": now + timedelta(hours=5),
            "rule": "oi",
        }
        with mock.patch.object(main.hydro, "get_contest", return_value=document):
            self.assertTrue(main._contest_submission_open("a" * 24, now))
            self.assertTrue(
                main._contest_submission_open("a" * 24, now + timedelta(hours=1))
            )
            self.assertFalse(
                main._contest_submission_open(
                    "a" * 24, now - timedelta(microseconds=1)
                )
            )
            self.assertFalse(
                main._contest_submission_open("a" * 24, document["endAt"])
            )
        with mock.patch.object(main.hydro, "get_contest", return_value={}):
            self.assertFalse(main._contest_submission_open("a" * 24, now))

    def test_scheduler_follows_authoritative_oj_time_extension(self):
        tid = "c" * 24
        begin = datetime.now(timezone.utc) - timedelta(minutes=5)
        frozen_end = begin + timedelta(hours=5)
        contest = {
            "tid": tid,
            "state": "ready",
            "begin_at_ms": int(begin.timestamp() * 1000),
            "end_at_ms": int(frozen_end.timestamp() * 1000),
            "hydro_rule": "oi",
        }
        changed = {
            "beginAt": begin,
            "endAt": frozen_end + timedelta(minutes=1),
            "rule": "oi",
        }
        with mock.patch.object(
            main.store, "contests", return_value=[contest]
        ), mock.patch.object(
            main.hydro, "get_contest", return_value=changed
        ), mock.patch.object(
            main.pipe,
            "sync_contest_schedule",
            return_value={"changed": True, "deadline_reached": False},
        ) as sync_schedule, mock.patch.object(
            main.store, "transition"
        ) as transition, mock.patch.object(main, "_spawn") as spawn:
            main.tick()

        sync_schedule.assert_called_once_with(
            tid,
            begin_at_ms=int(begin.timestamp() * 1000),
            end_at_ms=int(changed["endAt"].timestamp() * 1000),
            hydro_rule="oi",
            observed_at_ms=mock.ANY,
        )
        transition.assert_not_called()
        spawn.assert_not_called()

    def test_scheduler_resumes_interrupted_collection_without_polling_oj(self):
        tid = "d" * 24
        contest = {"tid": tid, "state": "collecting"}
        with mock.patch.object(
            main.store, "contests", return_value=[contest]
        ), mock.patch.object(
            main.pipe, "is_active", return_value=False
        ), mock.patch.object(
            main.hydro, "get_contest"
        ) as get_contest, mock.patch.object(main, "_spawn") as spawn:
            main.tick()

        get_contest.assert_not_called()
        spawn.assert_called_once_with(main.pipe.collect, tid)

    def test_scheduler_finishes_due_safe_wait_without_polling_oj(self):
        tid = "e" * 24
        contest = {"tid": tid, "state": "safe_wait", "shutdown_after_ms": 1}
        with mock.patch.object(
            main.store, "contests", return_value=[contest]
        ), mock.patch.object(
            main.pipe, "is_active", return_value=False
        ), mock.patch.object(
            main.hydro, "get_contest"
        ) as get_contest, mock.patch.object(main, "_spawn") as spawn:
            main.tick()

        get_contest.assert_not_called()
        spawn.assert_called_once_with(main.pipe.finish_safe_wait, tid)


class WebSubmitTemplateTests(unittest.TestCase):
    def setUp(self):
        self.seat = {
            "submit_token": "token",
            "candidate": "adler",
            "uname": "adler",
        }

    def test_login_and_answer_pages_include_official_flow(self):
        login = main.WEB_LOGIN_PAGE.render(
            seat=self.seat,
            error=None,
            candidate="",
            contest_title="fixture",
            contest_state_label="进行中",
            contest_done=False,
            script_nonce="nonce",
        )
        self.assertIn("选手登录", login)
        self.assertIn("准考证号", login)
        self.assertIn("fixture", login)
        self.assertIn("状态：进行中", login)

        answer = main.WEB_SUBMIT_PAGE.render(
            seat=self.seat,
            problems=["apple", "banana"],
            latest={},
            opened=True,
            saved="",
            submit_nonces={"apple": "1" * 32, "banana": "2" * 32},
            now="09:00:00",
        )
        for text in (
            "考试须知",
            "试题下载",
            "答题",
            "提交时间",
            "内容长度",
            "递交正式文件",
        ):
            self.assertIn(text, answer)
        self.assertIn("/formal/apple", answer)
        self.assertIn("/formal/banana", answer)
        self.assertNotIn("/edit/", answer)

    def test_login_replaces_a_corrupted_upstream_contest_title(self):
        response = main._web_login_response(
            self.seat,
            {
                "title": "[????????] NOI Linux ?????? 20260809T171805",
                "state": "ready",
            },
        )
        body = response.body.decode("utf-8")
        self.assertIn("本场比赛（标题显示异常，不影响登录和提交）", body)
        self.assertNotIn("????????", body)
        self.assertNotIn("??????", body)

    def test_public_contest_title_preserves_valid_question_marks(self):
        self.assertEqual(
            main._public_contest_title("NOI Linux Quiz???? 2026"),
            "NOI Linux Quiz???? 2026",
        )
        self.assertEqual(
            main._public_contest_title("NOI Linux 模拟赛？"),
            "NOI Linux 模拟赛？",
        )

    def test_public_contest_title_rejects_unicode_replacement_character(self):
        self.assertEqual(
            main._public_contest_title("NOI Linux \ufffd\ufffd 模拟赛"),
            "本场比赛（标题显示异常，不影响登录和提交）",
        )

    def test_admin_shows_final_file_io_mapping_before_generation(self):
        page = main.ADMIN_V1_PAGE.render(
            contests=[
                {
                    "tid": "7" * 24,
                    "title": "fixture",
                    "file_io_preview": [{"slug": "apple", "pid": "P1001"}],
                    "material_state": "pending",
                    "material_state_label": "等待生成或上传",
                    "materials_mode": "ai",
                    "artifact_job": None,
                    "artifacts": [],
                }
            ],
            section="materials",
            section_label="比赛材料",
            sections=main.ADMIN_SECTIONS,
            csrf="csrf",
            oj_base_url="https://oj.example.test",
            defaults={
                "release_lead_minutes": 5,
                "practice_groups": 3,
            },
        )
        self.assertIn("apple.in / apple.out", page)
        self.assertNotIn("服务器", page)

    def test_teacher_ui_has_exactly_five_product_pages_without_infrastructure_controls(self):
        page = main.ADMIN_V1_PAGE.render(
            contests=[],
            section="overview",
            section_label="比赛总览",
            sections=main.ADMIN_SECTIONS,
            csrf="csrf",
            oj_base_url="https://oj.example.test",
            defaults={"release_lead_minutes": 5, "practice_groups": 3},
        )
        self.assertEqual(
            list(main.ADMIN_SECTIONS.values()),
            ["比赛总览", "比赛材料", "学生座位", "递交与评测", "结束与归档"],
        )
        for forbidden in (
            "手动开机",
            "手动关机",
            "Caddy",
            "PM2",
            "MongoDB",
            "连接密码",
        ):
            self.assertNotIn(forbidden, page)
        registered = {
            (route.path, method)
            for route in main.app.routes
            for method in getattr(route, "methods", set())
        }
        self.assertNotIn(("/admin/boot", "POST"), registered)
        self.assertNotIn(("/admin/shutdown", "POST"), registered)
        self.assertNotIn(("/admin/sync-roster", "POST"), registered)

    def test_answer_and_view_pages_offer_source_download(self):
        submission = {
            "source": "// 中文\nint main() {}\n",
            "created_at": "09:00:00",
            "size": 27,
            "sha256": "abc",
        }
        answer = main.WEB_SUBMIT_PAGE.render(
            seat=self.seat,
            problems=["apple"],
            latest={"apple": submission},
            opened=True,
            saved="",
            submit_nonces={"apple": "a" * 32},
            now="09:00:00",
        )
        view = main.WEB_VIEW_PAGE.render(
            seat=self.seat,
            problem="apple",
            submission=submission,
        )
        for page in (answer, view):
            self.assertIn("下载源码", page)
            self.assertIn("/download/apple", page)

    def test_legacy_edit_page_redirects_to_single_formal_source_flow(self):
        request = Request(
            {"type": "http", "method": "GET", "path": "/submit/token/edit/apple", "headers": []}
        )
        context = (
            {"submit_token": "token", "candidate": "alice", "uname": "alice"},
            {"tid": "a" * 24, "state": "ready", "submission_mode": "web"},
            ["apple"],
            {},
        )
        with mock.patch.object(
            main, "_web_submit_context", return_value=context
        ), mock.patch.object(
            main, "_submit_authenticated", return_value=True
        ):
            response = main.web_submit_edit(request, "token", "apple")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/submit/token")


class WebSubmitRealtimeTests(unittest.TestCase):
    def setUp(self):
        self.seat = {"uid": 7, "submit_token": "token", "uname": "alice"}
        self.contest = {
            "tid": "b" * 24,
            "submission_session": "session-1",
            "pids": '{"apple":"P1"}',
            "state": "ready",
            "begin_at_ms": int(
                (datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()
                * 1000
            ),
            "end_at_ms": int(
                (datetime.now(timezone.utc) + timedelta(minutes=1)).timestamp()
                * 1000
            ),
            "hydro_rule": "oi",
        }
        self.window = mock.patch.object(
            main, "_contest_submission_open", return_value=True
        )
        self.window.start()

    def tearDown(self):
        self.window.stop()

    def test_formal_submit_reads_canonical_seat_file_before_enqueue(self):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/submit/token/formal/apple",
                "headers": [],
            }
        )
        source = (
            b'#include <cstdio>\r\nint main(){freopen("apple.in","r",stdin);'
            b'freopen("apple.out","w",stdout);}\r\n'
        )
        context = (self.seat, self.contest, ["apple"], {})
        with mock.patch.object(
            main, "_web_submit_context", return_value=context
        ), mock.patch.object(
            main, "_submit_authenticated", return_value=True
        ), mock.patch.object(
            main, "_submission_window_open", return_value=True
        ), mock.patch.object(
            main.pipe, "read_formal_source", return_value=source
        ) as read_formal, mock.patch.object(
            main, "_enqueue_web_source"
        ) as enqueue:
            response = main.web_submit_formal_file(
                request, "token", "apple", "a" * 32
            )

        read_formal.assert_called_once_with(
            self.contest["tid"], 7, "apple", maximum_bytes=mock.ANY
        )
        enqueue.assert_called_once_with(
            self.seat,
            self.contest,
            "apple",
            source.decode().replace("\r\n", "\n"),
            "a" * 32,
        )
        self.assertEqual(response.status_code, 303)

    def test_enqueue_uses_atomic_store_gate_after_source_processing(self):
        judge = mock.Mock()
        judge.enqueue.side_effect = main.SubmissionClosedError("closed")
        closed = dict(
            self.contest,
            end_at_ms=int(
                (datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp()
                * 1000
            ),
        )
        with mock.patch.object(main, "realtime_judge", judge):
            with self.assertRaises(HTTPException) as raised:
                main._enqueue_web_source(
                    self.seat, closed, "apple", "int main(){}", "e" * 32
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse(judge.enqueue.call_args.kwargs["allow_new"])

    def test_each_submit_enqueues_exact_realtime_payload(self):
        judge = mock.Mock()
        judge.enqueue.return_value = {"id": 12, "judge_state": "pending"}
        source = (
            '#include <cstdio>\nint main(){freopen("apple.in","r",stdin);'
            'freopen("apple.out","w",stdout);}\n'
        )
        with mock.patch.object(main, "realtime_judge", judge):
            result = main._enqueue_web_source(
                self.seat, self.contest, "apple", source, "A" * 32
            )
        self.assertEqual(result["id"], 12)
        judge.enqueue.assert_called_once_with(
            submission_session="session-1",
            tid="b" * 24,
            uid=7,
            problem="apple",
            pid="P1",
            source=source,
            judge_source=source,
            issues=[],
            client_nonce="a" * 32,
            accepted_at_ms=mock.ANY,
            allow_new=True,
        )

    def test_same_nonce_can_replay_after_local_window_closes(self):
        judge = mock.Mock()
        judge.enqueue.return_value = {
            "id": 12,
            "judge_state": "submitted",
            "rid": "1" * 24,
            "replayed": True,
        }
        closed = dict(
            self.contest,
            end_at_ms=int(
                (datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp()
                * 1000
            ),
        )
        with mock.patch.object(main, "realtime_judge", judge):
            result = main._enqueue_web_source(
                self.seat, closed, "apple", "int main(){}", "a" * 32
            )
        self.assertTrue(result["replayed"])
        self.assertFalse(judge.enqueue.call_args.kwargs["allow_new"])

    def test_same_nonce_payload_conflict_becomes_http_409(self):
        judge = mock.Mock()
        judge.enqueue.side_effect = main.SubmissionConflictError("changed")
        with mock.patch.object(main, "realtime_judge", judge):
            with self.assertRaises(HTTPException) as raised:
                main._enqueue_web_source(
                    self.seat,
                    self.contest,
                    "apple",
                    "int main(){}",
                    "c" * 32,
                )
        self.assertEqual(raised.exception.status_code, 409)

    def test_rule_violation_persists_original_and_force_zero_judge_source(self):
        judge = mock.Mock()
        judge.enqueue.return_value = {"id": 13, "judge_state": "pending"}
        original = "int main(){return 0;}\n"
        with mock.patch.object(main, "realtime_judge", judge):
            main._enqueue_web_source(
                self.seat, self.contest, "apple", original, "d" * 32
            )
        payload = judge.enqueue.call_args.kwargs
        self.assertEqual(payload["source"], original)
        self.assertTrue(payload["issues"])
        self.assertTrue(payload["judge_source"].startswith('#error "NOI environment rule violation"'))
        self.assertTrue(payload["judge_source"].endswith(original))


class WebSubmitDownloadTests(unittest.TestCase):
    def setUp(self):
        self.request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/submit/token/download/apple",
                "headers": [],
            }
        )
        self.context = (
            {"submit_token": "token", "candidate": "adler", "uname": "adler"},
            {"submission_mode": "web"},
            ["apple"],
            {"apple": {"source": "// 中文\nint main() {}\n"}},
        )

    def test_download_returns_complete_utf8_source(self):
        with mock.patch.object(
            main, "_web_submit_context", return_value=self.context
        ), mock.patch.object(main, "_submit_authenticated", return_value=True):
            response = main.web_submit_download(self.request, "token", "apple")
        self.assertEqual(response.body, "// 中文\nint main() {}\n".encode("utf-8"))
        self.assertEqual(
            response.headers["content-disposition"],
            'attachment; filename="apple.cpp"',
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_download_requires_login(self):
        with mock.patch.object(
            main, "_web_submit_context", return_value=self.context
        ), mock.patch.object(main, "_submit_authenticated", return_value=False):
            response = main.web_submit_download(self.request, "token", "apple")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/submit/token")


if __name__ == "__main__":
    unittest.main()
