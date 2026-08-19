"""NOI Linux contest orchestrator web service."""
from __future__ import annotations

from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, select_autoescape

from services.cloud import make_cvm
from services.config import load_config
from services.hydro import Hydro
from services.hydro_notify import HydroNotifier
from services.hydro_materials import HydroMaterialPublisher
from services.materials import (
    MaterialError,
    approved_material_paths,
    paper_path as material_paper_path,
    read_pdf_upload,
    read_testdata_upload,
    save_paper as save_material_paper,
    save_testdata_archive,
    sha256_file,
    testdata_archive_path as material_testdata_archive_path,
)
from services.hydro_submit import HydroSubmitter
from services.pipeline import Pipeline, gateway_base_url, novnc_path
from services.problem_mapping import ProblemMappingError, auto_problem_mapping
from services.realtime_judge import RealtimeJudge
from services.retention import RetentionManager
from services.static_check import check_code, force_zero_code
from services.store import Store, SubmissionClosedError, SubmissionConflictError
from services.teacher_sso import (
    TeacherIdentity,
    issue_session as issue_teacher_session,
    verify_hydro_ticket,
    verify_session as verify_teacher_session,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("orchestrator")

cfg = load_config(os.environ.get("ORCHESTRATOR_CONFIG", "config.yaml"))
cvm = make_cvm(cfg["cloud"])
hydro = Hydro(
    cfg["hydro"]["public_base_url"],
    cfg["hydro"]["mongo_uri"],
    cfg["hydro"]["domain_id"],
)
store = Store(cfg["orchestrator"]["db"])
material_publisher = HydroMaterialPublisher(
    cfg["hydro"]["internal_base_url"],
    cfg["hydro"]["orchestrator_token"],
    maximum_bytes=int(
        cfg["orchestrator"].get(
            "material_publish_max_bytes",
            128 * 1024 * 1024,
        )
    ),
)
recovered_artifact_jobs = store.recover_interrupted_artifact_jobs()
if recovered_artifact_jobs:
    log.warning(
        "marked %s material jobs interrupted after service restart",
        recovered_artifact_jobs,
    )
recovered_contests = store.recover_interrupted_contests()
if recovered_contests:
    log.warning(
        "marked %s interrupted prepare operations fail-closed",
        recovered_contests,
    )
artifact_runner = None
if (cfg.get("artifact_generation") or {}).get("enabled", False):
    from services.artifact_adapters import (
        OpenAICompatibleArtifactProvider,
        TrustedExecutableAdapterRegistry,
    )
    from services.artifact_generation import ArtifactGenerationService
    from services.artifact_orchestration import ArtifactJobRunner
    from services.hydro_problem_draft import HydroProblemDraftClient

    artifact_cfg = cfg["artifact_generation"]
    artifact_runner = ArtifactJobRunner(
        store=store,
        problem_client=HydroProblemDraftClient(
            cfg["hydro"]["internal_base_url"],
            cfg["hydro"]["orchestrator_token"],
        ),
        ai_provider=OpenAICompatibleArtifactProvider.from_config(
            artifact_cfg["ai"]
        ),
        tool_registry=TrustedExecutableAdapterRegistry.from_config(
            artifact_cfg["tools"]
        ),
        generation_service=ArtifactGenerationService(
            cfg["orchestrator"].get("artifact_root", "/app/data/artifacts")
        ),
        logger=log,
    )
realtime_judge = None
if cfg["hydro"].get("submit_enabled"):
    realtime_judge = RealtimeJudge(
        store,
        HydroSubmitter(
            cfg["hydro"]["internal_base_url"],
            cfg["hydro"]["orchestrator_token"],
            cfg["hydro"].get("submit_lang", ""),
            qualification_failure_marker_path=cfg["hydro"].get(
                "qualification_failure_marker_path", ""
            ),
            qualification_marker=cfg["hydro"].get(
                "qualification_marker", ""
            ),
        ),
        lease_seconds=float(
            cfg["orchestrator"].get("realtime_judge_lease_seconds", 45)
        ),
    )
notifier = None
if cfg["hydro"].get("notify_enabled", False):
    notify_hosts = cfg["hydro"].get("notify_allowed_https_hosts") or []
    if isinstance(notify_hosts, str):
        notify_hosts = [
            value.strip() for value in notify_hosts.split(",") if value.strip()
        ]
    notifier = HydroNotifier(
        cfg["hydro"]["internal_base_url"],
        cfg["hydro"]["orchestrator_token"],
        notify_hosts,
    )
pipe = Pipeline(cfg, cvm, hydro, store, log, realtime_judge=realtime_judge)
retention = RetentionManager(
    store,
    artifact_root=cfg["orchestrator"].get("artifact_root", "/app/data/artifacts"),
    materials_dir=cfg["orchestrator"].get("materials_dir", "/app/data/materials"),
    collected_dir=cfg["orchestrator"]["collected_dir"],
    workspace_retention_days=int(
        cfg["orchestrator"].get("workspace_retention_days", 30)
    ),
    evidence_retention_days=int(
        cfg["orchestrator"].get("evidence_retention_days", 180)
    ),
)
templates = Environment(autoescape=select_autoescape(default=True))
security = HTTPBasic(auto_error=False)
ADMIN_CSRF = secrets.token_urlsafe(32)
TEACHER_COOKIE = "__Host-noi_teacher"
_USED_TEACHER_TICKETS: dict[str, int] = {}
_TEACHER_TICKET_LOCK = threading.Lock()
REALTIME_JUDGE_STOP = threading.Event()
REALTIME_JUDGE_THREAD: threading.Thread | None = None
NOTIFICATION_RUN_LOCK = threading.Lock()
NOTIFICATION_BATCH_SIZE = 3
# Pipeline resources use ``secrets.token_urlsafe(12)``, whose unpadded
# base64url representation is exactly 16 characters.  Keep the route strict,
# but accept the tokens the production allocator actually emits.
_GATEWAY_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_CORRUPTED_TITLE_RUN = re.compile(
    r"(?:^|[\s\[（【])\?{4,}(?=$|[\s\]）】])"
)
_CORRUPTED_TITLE_FALLBACK = "本场比赛（标题显示异常，不影响登录和提交）"
DESKTOP_LAUNCH_PAGE = templates.from_string(
    """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NOI Linux 桌面入口</title><style>
body{font-family:system-ui,sans-serif;background:#f4f7fb;color:#172033;margin:0;padding:32px}
main{max-width:680px;margin:auto;background:#fff;border-radius:14px;padding:28px;box-shadow:0 8px 30px #18304a18}
h1{font-size:24px;margin:0 0 12px}.hint{color:#526176;line-height:1.7}
.actions{display:grid;gap:14px;margin:24px 0}.button{display:block;padding:15px 18px;border-radius:10px;text-decoration:none;font-weight:700}
.primary{background:#1769e0;color:#fff}.secondary{background:#edf2f8;color:#24364d}
.warning{background:#fff6dd;border-left:4px solid #e7a600;padding:12px 14px;line-height:1.6}
</style></head><body><main><h1>NOI Linux 桌面入口</h1>
<p class="hint">高速入口直接连接杭州比赛服务器，画质为 1366×768、30 fps、quality=9；兼容入口经过 OJ 中转，速度较慢，但可用于学校网络拦截裸 IP 时应急。</p>
<div class="actions"><a class="button primary" href="{{ direct_url }}" rel="noreferrer">高速直连（推荐）</a>
<a class="button secondary" href="{{ fallback_url }}" rel="noreferrer">兼容入口（较慢）</a></div>
<p class="warning">高速入口使用临时 HTTP，浏览器显示“不安全”属于已知提示。比赛结束后两个入口都会关闭。</p>
</main></body></html>"""
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _contest_submission_open(tid: str, now: datetime | None = None) -> bool:
    """Fail closed unless Hydro says the fixed contest window is open."""
    try:
        document = hydro.get_contest(tid)
        if (
            not document
            or str(document.get("rule") or "") != "oi"
            or not document.get("beginAt")
            or not document.get("endAt")
        ):
            return False
        current = _utc(now or datetime.now(timezone.utc))
        return _utc(document["beginAt"]) <= current < _utc(document["endAt"])
    except Exception:
        log.exception("cannot verify Hydro contest time for %s", tid)
        return False


def _spawn(target, tid: str) -> None:
    threading.Thread(target=target, args=(tid, True), daemon=True).start()


def _spawn_pool_operation(target, tid: str, **kwargs) -> None:
    """Run a live pool operation without tying it to the admin HTTP timeout."""

    def run() -> None:
        try:
            target(tid, **kwargs)
        except Exception:
            # Pipeline records a safe, user-facing status while retaining the
            # old pool; this log preserves the traceback for the operator.
            log.exception("background seat-pool operation failed for %s", tid)

    threading.Thread(target=run, daemon=True).start()


def _spawn_notification_retry(tid: str) -> None:
    """Wake one notification batch; the scheduler drains the durable remainder."""

    def run() -> None:
        try:
            contest = store.get_contest(tid)
            if contest and str(contest.get("state") or "") == "ready":
                _notify_released_seats(contest)
        except Exception:
            log.exception("background seat notification retry failed for %s", tid)

    threading.Thread(target=run, daemon=True).start()


def _public_contest_title(value: object, *, missing: str = "未命名比赛") -> str:
    """Keep corrupt upstream titles away from student-facing pages/messages."""
    title = str(value or "").strip()
    if not title:
        return missing
    if "\ufffd" in title or _CORRUPTED_TITLE_RUN.search(title):
        return _CORRUPTED_TITLE_FALLBACK
    return title


def _notify_released_seats(contest: dict) -> None:
    if notifier is None:
        return
    # The scheduler and the teacher's manual roster sync can overlap.  Hydro's
    # endpoint is idempotent, but avoiding a second local sender also keeps the
    # SQLite attempt counters and the scheduler latency deterministic.
    if not NOTIFICATION_RUN_LOCK.acquire(blocking=False):
        return
    try:
        release_lead_minutes = int(contest["release_lead_minutes"])
        tid = str(contest["tid"])
        stored = store.seat_pool(tid)
        if not stored:
            return

        jobs: list[dict] = []
        # Persist every currently released seat before doing any network I/O.
        # This makes an interrupted batch visible to /healthz, while pending
        # rows are ordered ahead of retries so a common outage cannot starve a
        # student who has never had an attempt.
        for pooled in stored["state"].get("seats", []):
            if pooled.get("state") != "released" or not pooled.get("uid"):
                continue
            uid = int(pooled["uid"])
            stage = "resource_lookup"
            try:
                resource = store.seat_pool_resource(tid, int(pooled["slot_no"]))
                if not resource:
                    raise RuntimeError("seat resource unavailable")
                credential_revision = int(
                    resource.get("credential_revision") or 1
                )
                stage = "notification_queue"
                notification_id = notifier.notification_id(
                    tid, uid, credential_revision
                )
                record = store.queue_seat_notification(
                    tid,
                    uid,
                    "seat_ready",
                    credential_revision,
                    notification_id,
                )
                if record.get("state") in {"sent", "permanent_failed"}:
                    continue
                jobs.append(
                    {
                        "uid": uid,
                        "notification_id": notification_id,
                        "resource": resource,
                        "state": str(record.get("state") or "pending"),
                        "attempts": int(record.get("attempts") or 0),
                        "updated_at": str(record.get("updated_at") or ""),
                    }
                )
            except Exception as exc:
                # A pre-queue failure has no durable row yet; the derived
                # notification health view reports it as untracked or as a
                # missing resource.  Do not persist or log exception text.
                log.error(
                    "cannot prepare released seat notification %s/%s "
                    "stage=%s error_type=%s",
                    tid,
                    uid,
                    stage,
                    type(exc).__name__,
                )

        jobs.sort(
            key=lambda job: (
                0 if job["state"] == "pending" else 1,
                job["attempts"],
                job["updated_at"],
                job["uid"],
            )
        )
        jobs = jobs[:NOTIFICATION_BATCH_SIZE]
        if not jobs:
            return

        gateway_ip = ""
        configured_gateway = str(
            cfg["contest_server"].get("gateway_public_base_url") or ""
        ).strip()
        if not configured_gateway:
            try:
                _, gateway_ip = cvm.status()
                if not gateway_ip:
                    raise RuntimeError("gateway address unavailable")
            except Exception as exc:
                error_type = type(exc).__name__
                for job in jobs:
                    try:
                        store.mark_seat_notification(
                            job["notification_id"],
                            sent=False,
                            retryable=True,
                            error=f"gateway_lookup:{error_type}"[:120],
                        )
                    except Exception as mark_exc:
                        log.error(
                            "cannot persist seat notification failure %s/%s "
                            "error_type=%s",
                            tid,
                            job["uid"],
                            type(mark_exc).__name__,
                        )
                log.error(
                    "cannot resolve notification gateway %s error_type=%s",
                    tid,
                    error_type,
                )
                return

        def deliver(job: dict) -> dict:
            try:
                result = notifier.send_seat_ready(
                    uid=job["uid"],
                    notification_id=job["notification_id"],
                    contest_title=_public_contest_title(
                        contest.get("title"), missing=str(contest["tid"])
                    ),
                    desktop_url=notification_desktop_url(
                        gateway_ip, job["resource"]["token"]
                    ),
                    candidate=job["resource"]["candidate"],
                    student_password=job["resource"]["vnc_pass"],
                    available_at=(
                        f"比赛开始前 {release_lead_minutes} 分钟起可登录"
                    ),
                )
                sent = bool(result.get("ok"))
                retryable = bool(result.get("retryable", True))
                return {
                    **job,
                    "sent": sent,
                    "retryable": retryable,
                    "error": (
                        ""
                        if sent
                        else (
                            "provider_rejected"
                            if retryable
                            else "provider_permanent_reject"
                        )
                    ),
                    "error_type": "ProviderRejected" if not sent else "",
                }
            except Exception as exc:
                # Exception strings may contain the submitted URL or password.
                return {
                    **job,
                    "sent": False,
                    "retryable": True,
                    "error": f"provider_send:{type(exc).__name__}"[:120],
                    "error_type": type(exc).__name__,
                }

        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            outcomes = list(executor.map(deliver, jobs))

        for outcome in outcomes:
            if not outcome["sent"]:
                log.error(
                    "cannot notify released seat %s/%s stage=provider_send "
                    "error_type=%s",
                    tid,
                    outcome["uid"],
                    outcome["error_type"],
                )
            try:
                store.mark_seat_notification(
                    outcome["notification_id"],
                    sent=outcome["sent"],
                    retryable=outcome["retryable"],
                    error=outcome["error"],
                )
            except Exception as mark_exc:
                log.error(
                    "cannot persist seat notification result %s/%s "
                    "error_type=%s",
                    tid,
                    outcome["uid"],
                    type(mark_exc).__name__,
                )
    finally:
        NOTIFICATION_RUN_LOCK.release()


def tick() -> None:
    now = datetime.now(timezone.utc)
    observed_at_ms = int(now.timestamp() * 1000)
    before = int(cfg["orchestrator"]["prepare_before_minutes"]) * 60
    late = int(cfg["orchestrator"].get("prepare_late_grace_minutes", 30)) * 60
    for contest in store.contests():
        tid = contest["tid"]
        contest_state = str(contest.get("state") or "")
        if contest_state == "collecting":
            if not pipe.is_active(tid):
                _spawn(pipe.collect, tid)
            continue
        if contest_state == "safe_wait":
            if (
                observed_at_ms >= int(contest.get("shutdown_after_ms") or 0)
                and not pipe.is_active(tid)
            ):
                _spawn(pipe.finish_safe_wait, tid)
            continue
        if contest_state not in {"registered", "ready"}:
            continue
        time_source_read = False
        try:
            document = hydro.get_contest(tid)
            time_source_read = True
            if (
                not document
                or not document.get("beginAt")
                or not document.get("endAt")
                or not document.get("rule")
            ):
                if contest["state"] == "ready":
                    store.mark_time_sync(
                        tid,
                        observed_at_ms=observed_at_ms,
                        error="OJ 比赛时间暂时不可读",
                    )
                continue
            begin = _utc(document["beginAt"])
            end = _utc(document["endAt"]) if document.get("endAt") else None
            seconds = (begin - now).total_seconds()
            if contest["state"] == "registered" and -late <= seconds <= before:
                if store.transition(
                    tid, {"registered"}, "preparing", "定时任务已触发备赛"
                ):
                    _spawn(pipe.prepare, tid)
            elif contest["state"] == "ready" and end:
                begin_ms = int(begin.timestamp() * 1000)
                end_ms = int(end.timestamp() * 1000)
                current_rule = str(document.get("rule") or "")
                schedule_drift = bool(
                    begin_ms != int(contest.get("begin_at_ms") or 0)
                    or end_ms != int(contest.get("end_at_ms") or 0)
                )
                if current_rule != "oi":
                    if store.transition(
                        tid,
                        {"ready"},
                        "collecting",
                        "OJ 赛制不再是 OI，已触发保护性收卷",
                    ):
                        _spawn(pipe.collect, tid)
                elif schedule_drift:
                    synced = pipe.sync_contest_schedule(
                        tid,
                        begin_at_ms=begin_ms,
                        end_at_ms=end_ms,
                        hydro_rule=current_rule,
                        observed_at_ms=observed_at_ms,
                    )
                    if synced["deadline_reached"] and store.transition(
                        tid,
                        {"ready"},
                        "collecting",
                        "OJ 已提前结束，正在按新截止时间收卷",
                    ):
                        _spawn(pipe.collect, tid)
                elif now >= end and store.transition(
                    tid, {"ready"}, "collecting", "到点自动收卷"
                ):
                    _spawn(pipe.collect, tid)
                elif now < end:
                    last_sync = int(contest.get("time_sync_at_ms") or 0)
                    if (
                        contest.get("time_sync_error")
                        or observed_at_ms - last_sync >= 30_000
                    ):
                        store.mark_time_sync(
                            tid, observed_at_ms=observed_at_ms, error=""
                        )
                    try:
                        pipe.sync_roster(tid)
                        _notify_released_seats(contest)
                    except Exception as exc:
                        log.exception("seat pool roster sync failed for %s", tid)
                        store.set_state(
                            tid,
                            "ready",
                            f"自动扩座或名单同步失败，系统将重试：{exc}",
                        )
        except Exception as exc:
            if contest.get("state") == "ready":
                try:
                    store.mark_time_sync(
                        tid,
                        observed_at_ms=observed_at_ms,
                        error=(
                            "OJ 时间读取失败"
                            if not time_source_read
                            else f"OJ 时间同步失败：{type(exc).__name__}"
                        ),
                    )
                except Exception:
                    log.exception("cannot persist time sync error for %s", tid)
            log.exception("scheduler tick failed for %s", tid)


def _reconcile_frontend(*, force: bool = False) -> bool:
    """Keep desktop routing fail-closed without taking down the web service."""
    try:
        pipe.reconcile_frontend(force=force)
        return True
    except Exception:
        # In direct mode a stale OJ /s/* proxy bypasses the temporary student
        # SG rule through the permanent OJ /32 rule.  Revoking the student rule
        # is insufficient, so suppress reopening and stop only the dedicated
        # contest VM.  The ordinary OJ/admin process remains available.
        if pipe._direct_access_enabled():
            try:
                pipe.shutdown_server()
            except Exception:
                log.exception(
                    "cannot stop contest VM after direct fallback close failure"
                )
        log.exception("frontend/cloud state reconciliation failed")
        return False


def _reconcile_desktop_access() -> None:
    """Converge the temporary public TCP rule from persisted contest state."""
    try:
        pipe.reconcile_desktop_access()
    except Exception:
        # Keep the admin/OJ process alive so the operator can see /healthz and
        # retry. Pipeline cleanup/VM stop remains the final fail-closed path.
        log.exception("desktop security-group reconciliation failed")


def _retention_sweep() -> None:
    """Apply local-only retention; OJ authority data is never in scope."""
    try:
        result = retention.sweep()
        if result["workspace"] or result["evidence"]:
            log.info("local retention sweep completed %s", result)
    except Exception:
        # A cleanup failure is deliberately non-destructive and retryable. It
        # must never affect the OJ, the active desktop route, or contest state.
        log.exception("local retention sweep failed closed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global REALTIME_JUDGE_THREAD
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    # The ECS timer is the exact folder cutoff. This short control-plane poll
    # detects forbidden Hydro edits quickly and starts archival/score finalizing.
    scheduler.add_job(tick, "interval", seconds=5, id="tick", max_instances=1)
    scheduler.add_job(
        _reconcile_frontend,
        "interval",
        seconds=30,
        id="frontend-reconcile",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _reconcile_desktop_access,
        "interval",
        seconds=int(
            (cfg.get("cloud", {}).get("aliyun", {}).get("desktop_access") or {}).get(
                "reconcile_seconds", 5
            )
        ),
        id="desktop-access-reconcile",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _retention_sweep,
        "interval",
        hours=6,
        id="local-retention",
        max_instances=1,
        coalesce=True,
    )
    scheduler_started = False
    realtime_thread_started = False
    try:
        # Force one reload at process startup.  This repairs a stale in-memory
        # Caddy route left by a previous process. Direct SG publication is
        # skipped if that closed route cannot be confirmed.
        frontend_safe = _reconcile_frontend(force=True)
        try:
            # A stale rule from a crash is closed unless the database proves
            # there is exactly one unexpired ready contest.
            if frontend_safe or not pipe._direct_access_enabled():
                pipe.reconcile_desktop_access()
            else:
                log.error(
                    "initial desktop reconciliation skipped because direct OJ fallback is unsafe"
                )
        except Exception:
            log.exception("initial desktop security-group reconciliation failed")
        scheduler.start()
        scheduler_started = True
        if realtime_judge is not None:
            REALTIME_JUDGE_STOP.clear()
            REALTIME_JUDGE_THREAD = threading.Thread(
                target=realtime_judge.run_forever,
                args=(REALTIME_JUDGE_STOP,),
                kwargs={
                    "idle_seconds": float(
                        cfg["orchestrator"].get(
                            "realtime_judge_idle_seconds", 0.5
                        )
                    )
                },
                daemon=True,
                name="noi-realtime-judge",
            )
            REALTIME_JUDGE_THREAD.start()
            realtime_thread_started = True
        yield
    finally:
        # Close-only latch comes before thread joins or APScheduler shutdown.
        # A queued reconcile job that acquires the SG lock later must revoke,
        # never re-authorize from still-persisted ready state.
        pipe.begin_shutdown()
        REALTIME_JUDGE_STOP.set()
        if realtime_thread_started and REALTIME_JUDGE_THREAD is not None:
            # Let an in-flight 5s/30s Hydro request persist its RID or retry
            # state before closing SQLite during an orderly restart.
            try:
                REALTIME_JUDGE_THREAD.join(timeout=40)
            except Exception:
                log.exception("realtime judge thread join failed during shutdown")
        REALTIME_JUDGE_THREAD = None
        if scheduler_started:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                # The close-only latch is already set. Continue to the final
                # cloud revoke even if APScheduler itself cannot drain.
                log.exception("scheduler shutdown failed during service cleanup")
        try:
            pipe.fail_closed_desktop_cleanup()
        except Exception:
            # The helper has already attempted the independent VM-stop safety
            # barrier.  Keep closing local resources even when cloud cleanup
            # could not be confirmed.
            log.exception(
                "fail-closed desktop cleanup failed during service shutdown"
            )
        hydro.close()
        store.close()


app = FastAPI(title="NOI Linux 模拟赛编排服务", lifespan=lifespan)


def require_admin(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> TeacherIdentity:
    session = request.cookies.get(TEACHER_COOKIE, "")
    if session:
        try:
            return verify_teacher_session(
                session, str(cfg["hydro"]["orchestrator_token"])
            )
        except ValueError:
            pass
    expected_user = str(cfg["orchestrator"].get("admin_username", "teacher"))
    expected_password = str(cfg["orchestrator"]["admin_password"])
    valid = credentials is not None
    if credentials is not None:
        valid &= secrets.compare_digest(
            credentials.username.encode(), expected_user.encode()
        )
        valid &= secrets.compare_digest(
            credentials.password.encode(), expected_password.encode()
        )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证失败",
            headers={"WWW-Authenticate": "Basic"},
        )
    return TeacherIdentity(0, expected_user, None, 0, technical=True)


def require_teacher_contest(teacher: TeacherIdentity | str, tid: str) -> str:
    normalized = tid.strip().lower()
    scope = teacher.tid if isinstance(teacher, TeacherIdentity) else None
    if scope is not None and not secrets.compare_digest(scope, normalized):
        raise HTTPException(403, "当前 OJ 教师登录只允许管理所选比赛")
    return normalized


def _teacher_actor(teacher: TeacherIdentity | str) -> str:
    return teacher.actor if isinstance(teacher, TeacherIdentity) else str(teacher)


def require_csrf(value: str) -> None:
    if not secrets.compare_digest(value, ADMIN_CSRF):
        raise HTTPException(403, "CSRF 校验失败")


def _audit_teacher(
    teacher: TeacherIdentity | str,
    tid: str,
    action: str,
    *,
    outcome: str = "requested",
    details: dict | None = None,
) -> None:
    """Persist a non-sensitive teacher action before accepting a mutation."""
    actor = _teacher_actor(teacher)
    store.append_audit(
        actor=f"teacher:{actor}",
        action=action,
        outcome=outcome,
        tid=tid,
        details=details,
    )


def gateway_url(ip: str, token: str) -> str:
    server = cfg["contest_server"]
    base = gateway_base_url(server, ip)
    quality = int(server.get("no_vnc_quality", 9))
    compression = int(server.get("no_vnc_compression", 2))
    return f"{base}{novnc_path(token, quality, compression)}"


def fallback_gateway_url(token: str) -> str:
    """Return the stable HTTPS OJ proxy for an already validated seat."""
    server = cfg["contest_server"]
    base = str(cfg.get("orchestrator", {}).get("public_base_url") or "").rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise HTTPException(503, "HTTPS 兼容入口未配置")
    quality = int(server.get("no_vnc_quality", 9))
    compression = int(server.get("no_vnc_compression", 2))
    return f"{base}{novnc_path(token, quality, compression)}"


def notification_desktop_url(ip: str, token: str) -> str:
    """Return a Hydro-safe one-click URL without proxying desktop traffic.

    The existing Hydro notification boundary intentionally accepts only an
    allowlisted HTTPS hostname.  A raw HTTP EIP therefore uses this service as
    a small control-plane redirect; the noVNC page and WebSocket still travel
    directly between the student's browser and the contest EIP.
    """
    direct = gateway_url(ip, token)
    if urlsplit(direct).scheme.lower() == "https":
        return direct
    base = str(cfg["orchestrator"].get("public_base_url") or "").rstrip("/")
    return f"{base}/desktop/{token}"


def web_submit_url(token: str) -> str:
    base = str(cfg["orchestrator"].get("public_base_url", "")).rstrip("/")
    return f"{base}/submit/{token}"


def paper_path(tid: str):
    contest = store.get_contest(tid)
    if not contest:
        return material_paper_path(
            cfg["orchestrator"].get("materials_dir", "/app/data/materials"), tid
        )
    revision = str(contest.get("active_material_revision") or "")
    artifact = store.artifact_revision(tid, revision) if revision else None
    paper, _ = approved_material_paths(
        materials_root=cfg["orchestrator"].get(
            "materials_dir", "/app/data/materials"
        ),
        artifact_root=cfg["orchestrator"].get(
            "artifact_root", "/app/data/artifacts"
        ),
        contest=contest,
        artifact=artifact,
    )
    return paper


STUDENT_PAGE = templates.from_string(
    """<!doctype html><html><head><meta charset="utf-8"><title>模拟赛座位查询</title>
<style>body{font-family:sans-serif;max-width:560px;margin:60px auto;padding:0 16px}
input,button{width:100%;padding:10px;margin:6px 0;font-size:16px;box-sizing:border-box}
.seat{background:#f4f8ff;border:1px solid #bcd;padding:16px;border-radius:8px;line-height:2}
code{background:#eee;padding:2px 6px;border-radius:4px}</style></head><body>
<h2>NOI Linux 模拟赛 · 座位查询</h2>
{% if error %}<p style="color:red">{{ error }}</p>{% endif %}
{% if seat %}<div class="seat"><b>{{ seat.uname }}</b> 同学，你的座位：<br>
桌面入口：<a href="{{ seat.url }}" target="_blank" rel="noopener">{{ seat.url }}</a><br>
连接密码：<code>{{ seat.vnc_pass }}</code><br>
交卷规则：<b>{{ seat.mode_label }}</b><br>
程序回收：<a href="{{ seat.web_submit_url }}" target="_blank" rel="noopener">打开 CSP 程序回收系统</a><br>
打开桌面链接 → Connect → 输入密码。代码始终保存在桌面【03_答案文件夹】规定目录；每次递交和截止收卷都读取这里的正式文件。<br>
{% if notice %}<a href="{{ notice }}" target="_blank" rel="noopener">选手操作文档</a>{% endif %}
</div>{% else %}<form method="post" action="query">
<input name="uname" autocomplete="username" placeholder="OJ 用户名" required>
<input name="password" type="password" autocomplete="current-password" placeholder="OJ 密码" required>
<button type="submit">查询我的座位</button></form>{% endif %}</body></html>"""
)

LEGACY_WEB_UPLOAD_PAGE = templates.from_string(
    """<!doctype html><html><head><meta charset="utf-8"><title>CSP 程序回收系统</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#eef3f7;color:#34495e;font:14px Arial,"Microsoft YaHei",sans-serif}
.top{height:64px;background:#18b8c7;color:#fff;display:flex;align-items:center;padding:0 28px;font-size:24px;box-shadow:0 1px 4px #789}
.layout{display:flex;min-height:calc(100vh - 64px)}.side{width:230px;background:#2d6c9f;color:#fff;padding-top:22px;flex:none}
.identity{padding:0 24px 20px;line-height:1.9;border-bottom:1px solid #4783b4}.nav{display:block;color:#eaf7ff;text-decoration:none;padding:15px 25px;border-bottom:1px solid #407dab}
.nav.active,.nav:hover{background:#17527e;color:#fff}.badge{float:right;background:#ef7d32;border-radius:10px;padding:1px 7px;font-size:12px}
.main{padding:28px 34px;flex:1;max-width:1100px}.panel{background:#fff;border:1px solid #d6e0e7;box-shadow:0 1px 3px #ccd5db;padding:24px}
h2{margin:0 0 8px;color:#2d5874;font-weight:normal}.sub{color:#7b8d99;margin-bottom:20px}.notice{background:#fff8dd;border-left:4px solid #e0b73b;padding:12px 16px;line-height:1.8}
.ok{background:#e7f7ec;border:1px solid #9fd0ad;color:#18733b;padding:10px 14px}.closed{background:#fee;color:#a00;padding:10px 14px}
table{width:100%;border-collapse:collapse;margin-top:20px}th{background:#edf4f8;color:#3f6178}th,td{border:1px solid #cddbe4;padding:12px;text-align:left}.problem{font:600 16px Consolas,monospace}
.upload{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.file{max-width:260px}.button,.view{display:inline-block;border:0;border-radius:3px;padding:7px 16px;text-decoration:none;font-size:14px;cursor:pointer}
.button{background:#20a7bd;color:#fff}.button:disabled{background:#aaa}.view{background:#4f87b2;color:#fff}.none{color:#9aa8b1}.hash{font:11px Consolas,monospace;color:#81909a}
</style></head><body><div class="top">CSP 程序回收系统</div><div class="layout">
<aside class="side"><div class="identity">比赛：{{ contest.title }}<br>准考证号：{{ seat.candidate }}<br>选手：{{ seat.uname }}</div>
<a class="nav" href="/submit/{{ seat.submit_token }}#notice">考试须知</a>
<a class="nav" href="/submit/{{ seat.submit_token }}/paper">试题下载</a>
<a class="nav active" href="/submit/{{ seat.submit_token }}">答题</a>
<a class="nav" href="/submit/{{ seat.submit_token }}#messages">消息 <span class="badge">0</span></a></aside>
<main class="main"><section class="panel"><h2>程序提交</h2><div class="sub">提交方式：{{ mode_label }}　每题以最后一次成功提交为准</div>
<div id="notice" class="notice">请先在 NOI Linux 中完成编写、编译和自测，再选择对应的 <b>.cpp</b> 文件提交。允许多次提交，务必核对文件名、时间和字节数。</div>
{% if saved %}<p class="ok">{{ saved }}.cpp 已成功提交，请在下表核对最后提交记录。</p>{% endif %}
{% if not opened %}<p class="closed">当前不在可提交状态，提交入口已经关闭。</p>{% endif %}
<table><thead><tr><th>题目</th><th>最后提交时间</th><th>文件大小</th><th>操作</th></tr></thead><tbody>
{% for problem in problems %}<tr><td class="problem">{{ problem }}.cpp</td>
<td>{% if latest.get(problem) %}{{ latest[problem].created_at }}{% else %}<span class="none">尚未提交</span>{% endif %}</td>
<td>{% if latest.get(problem) %}{{ latest[problem].size }} 字节<br><span class="hash">{{ latest[problem].sha256[:12] }}…</span>{% else %}—{% endif %}</td>
<td><div class="upload">{% if latest.get(problem) %}<a class="view" href="/submit/{{ seat.submit_token }}/view/{{ problem }}">查看</a>{% else %}<span class="none">查看</span>{% endif %}
<form method="post" enctype="multipart/form-data" action="/submit/{{ seat.submit_token }}"><input type="hidden" name="problem" value="{{ problem }}">
<input class="file" type="file" name="source" accept=".cpp,text/plain,text/x-c++src" required{% if not opened %} disabled{% endif %}>
<button class="button" type="submit"{% if not opened %} disabled{% endif %}>提交</button></form></div></td></tr>{% endfor %}
</tbody></table><p id="messages" class="sub">当前没有未读消息。</p></section></main></div></body></html>"""
)

LEGACY_WEB_VIEW_PAGE = templates.from_string(
    """<!doctype html><html><head><meta charset="utf-8"><title>查看提交 - CSP 程序回收系统</title>
<style>body{font:14px Arial,"Microsoft YaHei",sans-serif;margin:0;background:#eef3f7;color:#34495e}.top{background:#18b8c7;color:#fff;padding:18px 28px;font-size:24px}.main{max-width:1050px;margin:28px auto;background:#fff;border:1px solid #d6e0e7;padding:24px}.back{color:#16759a;text-decoration:none}pre{background:#17212b;color:#edf5f8;padding:18px;overflow:auto;white-space:pre;line-height:1.5}</style>
</head><body><div class="top">CSP 程序回收系统</div><main class="main"><a class="back" href="/submit/{{ seat.submit_token }}">← 返回答题页面</a><h2>{{ problem }}.cpp</h2><p>提交时间：{{ submission.created_at }}　大小：{{ submission.size }} 字节　SHA256：{{ submission.sha256 }}</p><pre>{{ submission.source }}</pre></main></body></html>"""
)

PROGRAM_COLLECTION_CSS = """
*{box-sizing:border-box}html,body{min-height:100%}body{margin:0;background:#f1f3f5;color:#333;font:14px Arial,"Microsoft YaHei",sans-serif}
a{color:inherit}.frame{width:min(1120px,calc(100% - 28px));margin:24px auto;background:#fff;box-shadow:0 2px 12px rgba(24,55,79,.18)}
.brand{height:72px;background:#3aa0b8;color:#fff;display:flex;align-items:center;justify-content:space-between;padding:0 28px 0 58px}
.brand strong{font-size:25px;letter-spacing:.5px}.logout{background:#fff;color:#555;border:1px solid #d8e0e4;padding:7px 13px;text-decoration:none;border-radius:2px;font-size:12px}
.body{display:flex;min-height:650px}.side{width:220px;flex:none;background:linear-gradient(#299eb7 0,#257dad 43%,#244d94 100%);color:#fff}
.seat-meta{padding:11px 24px;border-bottom:1px solid rgba(255,255,255,.2);min-height:44px;line-height:22px}.seat-meta span{display:inline-block;width:20px}
.nav{display:block;background:#f7f7f7;color:#3f4548;text-decoration:none;padding:14px 24px;border-bottom:1px solid #e7e7e7}.nav:hover,.nav.active{background:#59bd5e;color:#fff}.nav .ico{display:inline-block;width:22px;color:#283943}.nav.active .ico{color:#fff}
.stage{position:relative;flex:1;padding:18px 20px 46px;overflow:hidden;background:#fff}.stage:before{content:"";position:absolute;inset:0;pointer-events:none;opacity:.12;background:radial-gradient(circle at 34% 11%,#19b9ee 0 12px,transparent 13px),radial-gradient(circle at 79% 81%,#a859d5 0 15px,transparent 16px),linear-gradient(132deg,transparent 35%,#5ac7e8 35.2%,transparent 35.5%)}
.card{position:relative;border:1px solid #d8dde1;background:rgba(255,255,255,.94);min-height:530px}.card-title{padding:18px 20px;border-bottom:1px solid #ddd;font-weight:bold}.content{padding:22px 26px}
.notice{background:#edf4ff;border:1px solid #cbdcf4;padding:14px 16px;line-height:1.8;margin-bottom:18px}.success{background:#e8f7e9;border:1px solid #9fd1a3;color:#237a2b;padding:10px 14px;margin-bottom:16px}.closed{background:#fff0f0;border:1px solid #e8b4b4;color:#a22;padding:10px 14px;margin-bottom:16px}
.candidate{margin:0 0 12px;font-weight:bold}.answer-table{width:100%;border-collapse:collapse;background:#fff}.answer-table th,.answer-table td{border-bottom:1px solid #d9dfe3;padding:12px 10px;text-align:center}.answer-table th{background:#eef3f5;color:#45545d;font-weight:normal}.answer-table td:nth-child(2){text-align:left}.empty{color:#999}
.btn{display:inline-block;border:0;border-radius:2px;padding:6px 12px;text-decoration:none;cursor:pointer;font:13px Arial,"Microsoft YaHei",sans-serif}.btn-view{background:#f3f3f3;color:#444;border:1px solid #cfcfcf}.btn-submit{background:#288fc3;color:#fff;border:1px solid #237da9}.btn-primary{background:#55b95b;color:#fff;padding:9px 25px}.btn-disabled{opacity:.45;pointer-events:none}.actions{display:flex;justify-content:center;gap:7px}
.edit-title{font-size:18px;margin:0 0 12px}.warning{color:#a64925;line-height:1.8;margin-bottom:12px}.upload-box{display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:#eef7ff;border:1px solid #bdd7eb;padding:12px;margin-bottom:18px}.upload-box input[type=file]{max-width:420px}.or-divider{text-align:center;color:#77838a;margin:2px 0 12px}.codebox{width:100%;height:320px;padding:12px;border:1px solid #aebcc5;resize:vertical;font:14px/1.5 Consolas,"Courier New",monospace;tab-size:4}.form-actions{display:flex;gap:10px;margin-top:12px}.back{background:#f4f4f4;border:1px solid #ccc;color:#444}
.code-view{background:#17212b;color:#edf5f8;padding:16px;overflow:auto;white-space:pre;line-height:1.5;min-height:360px}.meta{color:#68757d;margin:12px 0}.small{font-size:12px;color:#73828a}
.judge-ok{color:#287a32}.judge-wait{color:#8a641c}.judge-error{color:#a22}.judge-local{color:#68757d}
.login-frame{width:min(1060px,calc(100% - 28px));margin:55px auto;background:#063a91;box-shadow:0 3px 18px rgba(7,43,82,.28)}.login-brand{background:#3aa0b8;color:#fff;padding:18px 42px;font-size:25px;font-weight:bold}.login-body{display:flex;min-height:500px}.login-panel{width:43%;padding:105px 68px;background:radial-gradient(circle at 54% 24%,rgba(75,219,255,.65),transparent 18%),#073a90}.login-panel h2{color:#fff;font-weight:normal;margin:0 0 22px}.login-panel input{width:100%;height:44px;border:1px solid #d0d8df;padding:0 12px;font-size:15px}.login-panel input+input{border-top:0}.login-panel button{width:100%;margin-top:14px;height:42px;border:0;background:#57bd5d;color:#fff;font-size:16px;cursor:pointer}.login-error{background:#fff1f1;color:#a22;padding:9px 11px;margin-bottom:10px}.login-hero{position:relative;flex:1;display:flex;align-items:center;justify-content:center;text-align:center;color:#fff;overflow:hidden;background:linear-gradient(135deg,#245be1,#0d8bf1)}.login-hero:before{content:"";position:absolute;inset:0;opacity:.7;background:linear-gradient(45deg,transparent 25%,#ffd000 25% 34%,transparent 34% 58%,#27bfc8 58% 68%,transparent 68%),radial-gradient(circle at 72% 34%,#5742dc 0 45px,transparent 46px),radial-gradient(circle at 28% 76%,#082b91 0 55px,transparent 56px)}.login-hero h1{position:relative;font-size:29px;max-width:430px;line-height:1.4;text-shadow:0 2px 4px #17448d}
.login-panel button:disabled{background:#8ab98e;cursor:wait}.login-context{background:#eaf4ff;border:1px solid #9fc7ef;color:#173f71;padding:11px 12px;margin-bottom:12px;line-height:1.6}.login-context strong{display:block;font-size:15px}.login-state{font-weight:bold}.login-ended{background:#fff2d9;border:2px solid #d97a00;color:#7a3c00;font-weight:bold;padding:10px 12px;margin-bottom:12px}.login-error{background:#fff1f1;border:2px solid #d33;color:#8c1010;font-size:15px;font-weight:bold;padding:11px 12px;margin-bottom:12px}.login-status{min-height:22px;margin:10px 0 0;color:#fff;font-weight:bold}
@media(max-width:760px){.frame{width:100%;margin:0}.body{display:block}.side{width:100%}.stage{padding:12px}.brand{padding:0 16px}.login-frame{width:100%;margin:0}.login-body{display:block}.login-panel{width:100%;padding:40px}.login-hero{min-height:240px}.answer-table{font-size:12px}}
"""

WEB_LOGIN_PAGE = templates.from_string(
    """<!doctype html><html><head><meta charset="utf-8"><title>CSP 程序回收系统</title><style>"""
    + PROGRAM_COLLECTION_CSS
    + """</style></head><body><main class="login-frame"><div class="login-brand">CSP 程序回收系统</div><div class="login-body">
<section class="login-panel"><h2>选手登录</h2><div class="login-context"><strong>{{ contest_title }}</strong><span class="login-state">状态：{{ contest_state_label }}</span></div>
{% if contest_done %}<div class="login-ended" role="alert">此入口已结束，请使用最新链接。</div>{% endif %}{% if error %}<div class="login-error" role="alert" aria-live="assertive">{{ error }}</div>{% endif %}
<form id="submit-login-form" method="post" action="/submit/{{ seat.submit_token }}/login"><input name="candidate" autocomplete="username" autocapitalize="characters" spellcheck="false" placeholder="准考证号" value="{{ candidate }}" required><input name="password" type="password" autocomplete="current-password" autocapitalize="none" spellcheck="false" placeholder="密码" required><button id="submit-login-button" type="submit">登录</button><div id="submit-login-status" class="login-status" role="status" aria-live="polite"></div></form></section>
<script nonce="{{ script_nonce }}">(()=>{const form=document.getElementById("submit-login-form");const button=document.getElementById("submit-login-button");const status=document.getElementById("submit-login-status");if(!form||!button||!status)return;const reset=()=>{button.disabled=false;button.textContent="登录";if(status.textContent==="正在验证…")status.textContent=""};form.addEventListener("submit",()=>{if(!form.checkValidity())return;button.disabled=true;button.textContent="正在验证…";status.textContent="正在验证…"});window.addEventListener("pageshow",reset)})();</script>
<section class="login-hero"><h1>CSP 程序回收系统</h1></section></div></main></body></html>"""
)

WEB_SUBMIT_PAGE = templates.from_string(
    """<!doctype html><html><head><meta charset="utf-8"><title>答题 - CSP 程序回收系统</title><style>"""
    + PROGRAM_COLLECTION_CSS
    + """</style></head><body><main class="frame"><header class="brand"><strong>CSP 程序回收系统</strong><a class="logout" href="/submit/{{ seat.submit_token }}/logout">退出登录</a></header><div class="body">
<aside class="side"><div class="seat-meta"><span>◷</span>{{ now }}</div><div class="seat-meta"><span>♟</span>{{ seat.candidate }}</div><a class="nav" href="#notice"><span class="ico">●</span>考试须知</a><a class="nav" href="/submit/{{ seat.submit_token }}/paper"><span class="ico">⬇</span>试题下载</a><a class="nav active" href="/submit/{{ seat.submit_token }}"><span class="ico">✎</span>答题</a><a class="nav" href="#messages"><span class="ico">✉</span>0条未读消息</a></aside>
<section class="stage"><div class="card"><div class="card-title">答题</div><div class="content"><div id="notice" class="notice">请先在 NOI Linux 的正式答案目录中完成代码编写、保存、编译和自测，再点击对应题目的“递交正式文件”。系统会直接读取规定目录里的 <b>题目名.cpp</b>，可靠保存后立即以你的身份送入本场 OJ 评测；不需要再次选择文件或粘贴代码。比赛期间不会显示分数、测试点或编译结果，教师可以在 OJ 后台实时观察。允许多次递交，以截止时正式答案目录中的最终版本为准。</div>
{% if saved %}<div class="success">{{ saved }}.cpp 已被程序回收系统接收，并将自动送入 OJ 评测；请使用“查看”核对完整内容、时间和字节数。</div>{% endif %}{% if not opened %}<div class="closed">当前不在 Hydro 比赛提交时间内，提交通道已经关闭。</div>{% endif %}
<p class="candidate">准考证号：{{ seat.candidate }}</p><table class="answer-table"><thead><tr><th>序号</th><th>试题名称</th><th>提交时间</th><th>内容长度</th><th>OJ送评状态</th><th>操作</th></tr></thead><tbody>
{% for problem in problems %}{% set item = latest.get(problem) %}<tr><td>{{ loop.index }}</td><td>第 {{ loop.index }} 题：{{ problem }}</td><td>{% if item %}{{ item.created_at }}{% else %}<span class="empty">尚未递交</span>{% endif %}</td><td>{% if item %}{{ item.size }}B{% else %}0B{% endif %}</td><td>{% if not item %}<span class="empty">—</span>{% elif item.judge_state == 'submitted' %}<span class="judge-ok">已送入 OJ</span>{% elif item.judge_state in ('pending','sending') %}<span class="judge-wait">等待送评</span>{% elif item.judge_state == 'retry' %}<span class="judge-wait">送评重试中</span>{% elif item.judge_state == 'ambiguous' %}<span class="judge-error">送评结果待核对，请勿重复递交同一请求</span>{% elif item.judge_state == 'permanent_failed' %}<span class="judge-error">送评异常，教师处理中</span>{% else %}<span class="judge-local">已在本地保存</span>{% endif %}</td><td><div class="actions">{% if item %}<a class="btn btn-view" href="/submit/{{ seat.submit_token }}/view/{{ problem }}">查看</a><a class="btn btn-view" href="/submit/{{ seat.submit_token }}/download/{{ problem }}">下载源码</a>{% else %}<span class="btn btn-view btn-disabled">查看</span><span class="btn btn-view btn-disabled">下载源码</span>{% endif %}<form method="post" action="/submit/{{ seat.submit_token }}/formal/{{ problem }}"><input type="hidden" name="client_nonce" value="{{ submit_nonces[problem] }}"><button class="btn btn-submit{% if not opened %} btn-disabled{% endif %}"{% if not opened %} disabled{% endif %}>递交正式文件</button></form></div></td></tr>{% endfor %}
</tbody></table><p id="messages" class="small">“已送入 OJ”表示 OJ 已创建评测记录并进入判题队列，不代表判题已经结束。比赛期间不会向选手显示评测结果。</p></div></div></section></div></main></body></html>"""
)

WEB_EDIT_PAGE = templates.from_string(
    """<!doctype html><html><head><meta charset="utf-8"><title>提交代码 - CSP 程序回收系统</title><style>"""
    + PROGRAM_COLLECTION_CSS
    + """</style></head><body><main class="frame"><header class="brand"><strong>CSP 程序回收系统</strong><a class="logout" href="/submit/{{ seat.submit_token }}/logout">退出登录</a></header><div class="body"><aside class="side"><div class="seat-meta"><span>◷</span>{{ now }}</div><div class="seat-meta"><span>♟</span>{{ seat.candidate }}</div><a class="nav" href="/submit/{{ seat.submit_token }}#notice"><span class="ico">●</span>考试须知</a><a class="nav" href="/submit/{{ seat.submit_token }}/paper"><span class="ico">⬇</span>试题下载</a><a class="nav active" href="/submit/{{ seat.submit_token }}"><span class="ico">✎</span>答题</a><a class="nav" href="#"><span class="ico">✉</span>0条未读消息</a></aside>
<section class="stage"><div class="card"><div class="card-title">提交代码</div><div class="content"><h2 class="edit-title">第 {{ problem_index }} 题：{{ problem }}</h2><div class="warning">从 Windows 传送代码时，优先直接上传本地 .cpp 文件；也可以粘贴完整源代码。不要使用远程桌面剪贴板传长代码。提交前检查文件开头、结尾和文件读写名称是否完整。</div>
<form class="upload-box" method="post" enctype="multipart/form-data" action="/submit/{{ seat.submit_token }}"><input type="hidden" name="problem" value="{{ problem }}"><input type="hidden" name="client_nonce" value="{{ client_nonce }}"><input type="file" name="source" accept=".cpp,text/plain,text/x-c++src" required><button class="btn btn-primary" type="submit">上传 .cpp 文件</button></form><div class="or-divider">— 或粘贴完整源代码 —</div>
<form method="post" action="/submit/{{ seat.submit_token }}/paste"><input type="hidden" name="problem" value="{{ problem }}"><input type="hidden" name="client_nonce" value="{{ client_nonce }}"><textarea class="codebox" name="code" spellcheck="false" required></textarea><div class="form-actions"><button class="btn btn-primary" type="submit">确认提交</button><a class="btn back" href="/submit/{{ seat.submit_token }}">取消并返回</a></div></form></div></div></section></div></main></body></html>"""
)

WEB_VIEW_PAGE = templates.from_string(
    """<!doctype html><html><head><meta charset="utf-8"><title>查看提交 - CSP 程序回收系统</title><style>"""
    + PROGRAM_COLLECTION_CSS
    + """</style></head><body><main class="frame"><header class="brand"><strong>CSP 程序回收系统</strong><a class="logout" href="/submit/{{ seat.submit_token }}/logout">退出登录</a></header><div class="body"><aside class="side"><div class="seat-meta"><span>♟</span>{{ seat.candidate }}</div><a class="nav active" href="/submit/{{ seat.submit_token }}"><span class="ico">✎</span>答题</a></aside><section class="stage"><div class="card"><div class="card-title">查看提交</div><div class="content"><div class="actions"><a class="btn back" href="/submit/{{ seat.submit_token }}">返回答题页面</a><a class="btn btn-submit" href="/submit/{{ seat.submit_token }}/download/{{ problem }}">下载源码</a></div><h2>{{ problem }}.cpp</h2><p class="meta">提交时间：{{ submission.created_at }}　内容长度：{{ submission.size }}B　SHA256：{{ submission.sha256 }}　送评状态：{% if submission.judge_state == 'submitted' %}已送入 OJ{% elif submission.judge_state in ('pending','sending','retry') %}处理中{% elif submission.judge_state == 'ambiguous' %}结果待只读核对，系统不会自动重发{% elif submission.judge_state == 'permanent_failed' %}异常，教师处理中{% else %}本地保存{% endif %}</p><pre class="code-view">{{ submission.source }}</pre></div></div></section></div></main></body></html>"""
)

MODE_LABELS = {
    # Legacy database values remain readable during the V1 migration, but the
    # product exposes one submission contract only.
    "folder": "正式答案目录 + 每次递交到 OJ",
    "web": "正式答案目录 + 每次递交到 OJ",
    "both": "正式答案目录 + 每次递交到 OJ",
}

CONTEST_STATE_LABELS = {
    "registered": "等待备赛",
    "preparing": "正在备赛",
    "ready": "进行中",
    "collecting": "正在收卷",
    "safe_wait": "安全等待",
    "safe_ended": "已安全结束",
    "done": "已结束",
    "error": "需要教师处理",
}

ADMIN_PAGE = templates.from_string(
    """<!doctype html><html><head><meta charset="utf-8"><title>编排后台</title>
<style>*{box-sizing:border-box}body{font-family:Arial,"Microsoft YaHei",sans-serif;max-width:1440px;margin:28px auto;padding:0 18px;color:#243447;background:#f5f7fa}
.card{background:#fff;border:1px solid #dce3ea;border-radius:10px;padding:18px 20px;margin:16px 0;box-shadow:0 2px 8px #dfe5eb}
table{border-collapse:collapse;width:100%;background:#fff}td,th{border:1px solid #d6dee6;padding:9px 10px;vertical-align:top}th{background:#edf3f8}
input,button,select{padding:8px;margin:4px;font-size:14px}input[type=number]{width:86px}input[type=file]{max-width:300px}button{cursor:pointer}button:disabled{cursor:not-allowed;color:#888}
form.inline{display:inline}code{background:#eef1f4;padding:2px 6px}.hint{color:#667;font-size:13px;line-height:1.6}.ok{color:#087b35}.warn{color:#ad6300}.block{color:#b00020;font-weight:bold}.actions{line-height:2.5}.grid{display:grid;grid-template-columns:180px minmax(260px,1fr);gap:5px 10px;align-items:center}.grid label{text-align:right;color:#405366}</style>
</head><body><h2>NOI Linux 模拟赛 · 编排后台</h2>
<div class="card"><p>比赛服务器：<b>{{ cloud_state }}</b> {{ cloud_ip }}</p>
<form class="inline" method="post" action="/admin/boot"><input type="hidden" name="csrf" value="{{ csrf }}"><button>手动开机</button></form>
<form class="inline" method="post" action="/admin/shutdown"><input type="hidden" name="csrf" value="{{ csrf }}"><button>手动关机</button></form></div>
<div class="card"><h3>登记比赛</h3><form method="post" enctype="multipart/form-data" action="/admin/register"><div class="grid">
<input type="hidden" name="csrf" value="{{ csrf }}">
<label>Hydro 比赛 tid</label><input name="tid" size="28" required>
<label>题目映射（AI 可留空）</label><input name="files" size="64" placeholder="AI 留空自动读取；人工可填 apple=P1001,banana=P1002">
<label>交卷规则</label><span class="hint">正式答案目录是唯一代码源；每次点击递交都会进入 OJ，截止时自动核对最终文件。</span>
<label>备赛材料</label><select name="materials_mode"><option value="ai">AI 生成草稿，教师审核后发布</option><option value="manual">老师上传 PDF / 自测数据</option></select>
<label>座位容量</label><span class="hint">按 OJ 已报名人数自动增加，并维持至少 2 个或报名人数 10% 的备用座位。</span>
<label>提前发放分钟</label><input type="number" name="release_lead_minutes" min="1" max="60" value="{{ defaults.release_lead_minutes }}" required>
<label>每题自测组数</label><input type="number" name="practice_groups" min="2" max="4" value="{{ defaults.practice_groups }}" required>
<label>人工试题 PDF</label><input type="file" name="paper" accept="application/pdf,.pdf">
<label>人工自测数据 ZIP</label><input type="file" name="testdata" accept="application/zip,.zip">
</div><p class="hint">AI 模式可不填题目映射：系统按 Hydro 比赛题目顺序读取，优先采用题目 config.filename，其次安全的公开题号，最后使用 problem1、problem2；登记后会先显示最终 slug.in/out，教师确认后才克隆。AI 无需上传文件，机器校验通过后仍需教师另点批准。人工模式继续填写映射并上传 PDF；ZIP 只读下发，不收卷、不计分。</p><button>登记比赛</button></form></div>
<div class="card"><h3>已登记比赛</h3><table><tr><th>比赛</th><th>材料</th><th>座位池</th><th>运行状态</th><th>操作</th></tr>
{% for c in contests %}<tr><td><code>{{ c.tid }}</code><br><b>{{ c.title }}</b><br><span class="hint">正式答案目录 + 每次递交到 OJ<br>座位按报名自动增加，当前目标 {{ c.max_participants }} 正式 + {{ c.spare_seats }} 备用<br>提前 {{ c.release_lead_minutes }} 分钟发放</span></td>
<td><span class="hint"><b>当前文件读写（生成前请确认）</b>{% for item in c.file_io_preview %}<br><code>{{ item.slug }}.in / {{ item.slug }}.out</code>{% if item.pid %} → Hydro {{ item.pid }}{% endif %}{% endfor %}</span><br>{% if c.material_state == 'approved' %}<span class="ok">已批准并冻结</span><br><code>{{ c.active_material_revision }}</code><br><span class="hint">PDF {{ c.paper_sha256[:12] }}…<br>{% if c.testdata_sha256 %}自测 {{ c.testdata_files }} 个文件{% else %}无自测数据{% endif %}</span>
{% elif c.material_state == 'review' %}<span class="warn">已克隆原题为本场私有题，等待教师审核 PDF</span>{% elif c.material_state == 'draft' %}<span class="block">机器校验尚未完成，草稿不可批准</span>{% else %}<span class="block">未就绪：{{ c.material_state }}</span>{% endif %}
{% if c.artifact_job %}<br><span class="hint">生成任务 {{ c.artifact_job.state }} / {{ c.artifact_job.progress }}%<br>{{ c.artifact_job.message or c.artifact_job.error }}{% if c.artifact_job.details.file_io_plan %}<br>已验证 {{ c.artifact_job.details.file_io_plan|length }} 道私有克隆题的文件读写{% endif %}</span>{% endif %}</td>
<td>{% if c.pool_counts %}<span class="ok">已建立 r{{ c.pool_revision }}</span><br><span class="hint">待建 {{ c.pool_counts.get('planned',0) }} / 预热中 {{ c.pool_counts.get('warming',0) }} / 已验收 {{ c.pool_counts.get('verified',0) }} / 已预留 {{ c.pool_counts.get('reserved',0) }} / 已发放 {{ c.pool_counts.get('released',0) }}</span>{% else %}<span class="hint">尚未预热</span>{% endif %}</td>
<td><b>{{ c.state }}</b><br><span class="hint">{{ c.message }}<br>OJ 时间最近同步：{{ c.time_sync_label or '尚未同步' }}{% if c.time_sync_error %}<br><span class="block">时间同步异常：{{ c.time_sync_error }}</span>{% endif %}</span></td><td class="actions">
{% if c.materials_mode == 'ai' %}{% if not c.artifact_job or c.artifact_job.state in ('error','interrupted') %}<form class="inline" method="post" action="/admin/materials/generate"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><button onclick="return confirm('点击即表示批准：为本场创建或复用私有题副本，并改为 slug.in/out。共享原题不会修改；AI 只生成待审核草稿，仍须另行预览 PDF 和报告后批准发放。')">批准私有克隆并生成 AI 审核草稿</button></form><br><span class="hint">共享原题不改；AI 结果不会自动发放，须预览 PDF 与机器报告后另点批准。</span>{% elif c.artifact_job.state == 'done' %}<span class="hint">本场已生成过审核材料。为防止复用陈旧题面或数据指纹，不允许直接重新生成；如题目已修改，请在 Hydro 新建比赛后重新登记。</span>{% endif %}{% endif %}
{% for a in c.artifacts %}{% if a.state == 'review' %}<br><span class="hint">{{ a.revision }}：已克隆/验证 {{ a.file_io_plan|length }} 道题，待审核 PDF</span> <form class="inline" method="post" action="/admin/materials/approve"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><input type="hidden" name="revision" value="{{ a.revision }}"><button>批准 {{ a.revision }}</button></form> <a href="/admin/materials/{{ c.tid }}/{{ a.revision }}/paper" target="_blank">预览 PDF</a> <a href="/admin/materials/{{ c.tid }}/{{ a.revision }}/manifest" target="_blank">下载 manifest</a> <a href="/admin/materials/{{ c.tid }}/{{ a.revision }}/validation" target="_blank">下载机器校验报告</a>{% elif a.state == 'draft' %}<br><span class="block">{{ a.revision }} 仍是机器草稿，不可批准</span>{% endif %}{% endfor %}
<form class="inline" method="post" action="/admin/prepare"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><button {% if c.material_state != 'approved' %}disabled title="请先批准材料"{% endif %}>提前预热全部座位</button></form>
<form class="inline" method="post" action="/admin/sync-roster"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><button>同步报名名单</button></form>
{% if c.pool_revision is not none and c.state == 'ready' %}<br><span class="hint">正式座位按 OJ 报名自动增加，系统会持续维持备用座位。</span>
<br><form class="inline" method="post" action="/admin/pool/replace"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><input type="hidden" name="expected_revision" value="{{ c.pool_revision }}"><select name="slot_no" required><option value="">选择故障座位</option>{% for s in c.pool_seats %}{% if s.state not in ('planned','frozen','collected') %}<option value="{{ s.slot_no }}">{{ '%03d'|format(s.slot_no) }} · {{ s.state }}{% if s.uname %} · {{ s.uname }}{% endif %}</option>{% endif %}{% endfor %}</select><input name="reason" maxlength="200" placeholder="故障原因（必填）" required><label><input type="checkbox" name="teacher_approved" value="yes" required>教师确认切换</label><button onclick="return confirm('确认隔离该座位，并在有学生时切换到已验收备用位？')">替换故障座位</button></form>{% for s in c.pool_seats %}{% if s.state == 'planned' and s.failure_count|int > 0 %}<form class="inline" method="post" action="/admin/pool/repair"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><input type="hidden" name="expected_revision" value="{{ c.pool_revision }}"><input type="hidden" name="slot_no" value="{{ s.slot_no }}"><button onclick="return confirm('重新创建并验收座位 {{ '%03d'|format(s.slot_no) }}，恢复备用容量？')">修复备用容量 {{ '%03d'|format(s.slot_no) }}</button></form>{% endif %}{% endfor %}{% endif %}
<form class="inline" method="post" action="/admin/collect"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><button>收卷 / 失败重试</button></form>
<a href="/admin/export/{{ c.tid }}">导出已发放座位</a>　<a target="_blank" rel="noopener" href="{{ hydro_public_base_url }}/contest/{{ c.tid }}/scoreboard?realtime=1">OJ 实时监控</a></td></tr>{% endfor %}</table>
<p class="hint">预热阶段按 OJ 当前报名人数自动启动并逐个验收；新增报名会自动补充座位。学生每次递交都形成 OJ 记录，截止时系统核对正式答案目录并补齐最终版本。OJ 实时监控仅教师可看，OI 赛制下学生保持盲测。</p></div></body></html>"""
)

ADMIN_SECTIONS = {
    "overview": "比赛总览",
    "materials": "比赛材料",
    "seats": "学生座位",
    "submissions": "递交与评测",
    "finish": "结束与归档",
}

ADMIN_V1_PAGE = templates.from_string(
    """<!doctype html><html><head><meta charset="utf-8"><title>{{ section_label }} · NOI Linux 考试系统</title>
<style>*{box-sizing:border-box}body{margin:0;font-family:Arial,"Microsoft YaHei",sans-serif;background:#f4f6f9;color:#203044}.shell{max-width:1400px;margin:0 auto;padding:24px}.top{display:flex;align-items:center;justify-content:space-between}.brand{font-size:24px;font-weight:700}.sub{color:#65758b;font-size:13px}.nav{display:flex;gap:8px;margin:22px 0;flex-wrap:wrap}.nav a{padding:10px 15px;border-radius:8px;background:#fff;border:1px solid #d8e0e8;color:#29445f;text-decoration:none}.nav a.active{background:#1769aa;color:#fff;border-color:#1769aa}.card{background:#fff;border:1px solid #dce3ea;border-radius:10px;padding:18px 20px;margin:14px 0;box-shadow:0 2px 8px #dfe5eb}.grid{display:grid;grid-template-columns:180px minmax(260px,1fr);gap:7px 10px;align-items:center}.grid label{text-align:right;color:#405366}input,button,select{padding:8px;margin:3px;font-size:14px}input[type=file]{max-width:330px}button,.button{display:inline-block;border:1px solid #1769aa;background:#1769aa;color:#fff;border-radius:6px;padding:8px 12px;text-decoration:none;cursor:pointer}button.secondary,.button.secondary{background:#fff;color:#1769aa}button:disabled{border-color:#aaa;background:#ddd;color:#777;cursor:not-allowed}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #e0e6ec;padding:11px 9px;text-align:left;vertical-align:top}th{background:#f4f7fa}.hint{color:#65758b;font-size:13px;line-height:1.55}.ok{color:#087b35;font-weight:700}.warn{color:#ad6300;font-weight:700}.block{color:#b00020;font-weight:700}.pill{display:inline-block;padding:3px 8px;border-radius:12px;background:#eef3f8;margin:2px}.actions{line-height:2.8}code{background:#eef1f4;padding:2px 5px;border-radius:4px}.empty{padding:36px;text-align:center;color:#65758b}</style></head><body><main class="shell"><div class="top"><div><div class="brand">NOI Linux 考试系统</div><div class="sub">OJ 系统负责比赛与评测；这里负责标准考试桌面、程序回收和安全归档。</div></div><a class="button secondary" target="_blank" rel="noopener" href="{{ oj_base_url }}">打开 OJ 系统</a></div>
<nav class="nav">{% for key,label in sections.items() %}<a class="{% if key == section %}active{% endif %}" href="/admin/{{ key }}">{{ label }}</a>{% endfor %}</nav>
{% if section == 'overview' %}<section class="card"><h2>创建一场考试</h2><form method="post" enctype="multipart/form-data" action="/admin/register"><input type="hidden" name="csrf" value="{{ csrf }}"><div class="grid"><label>OJ 比赛编号</label>{% if teacher_tid %}<input type="hidden" name="tid" value="{{ teacher_tid }}"><code>{{ teacher_tid }}</code>{% else %}<input name="tid" size="30" required>{% endif %}<label>程序名映射</label><input name="files" size="64" placeholder="自动生成可留空；人工材料示例 apple=P1001"><label>材料方式</label><select name="materials_mode"><option value="ai">自动生成，老师审核后发布</option><option value="manual">老师上传 PDF 和辅助数据</option></select><label>提前发放</label><input type="number" name="release_lead_minutes" min="1" max="60" value="{{ defaults.release_lead_minutes }}" required><label>每题辅助数据</label><select name="practice_groups">{% for value in (2,3,4) %}<option value="{{ value }}"{% if value == defaults.practice_groups %} selected{% endif %}>{{ value }} 组 .in/.out</option>{% endfor %}</select><label>人工试题 PDF</label><input type="file" name="paper" accept="application/pdf,.pdf"><label>人工辅助数据 ZIP</label><input type="file" name="testdata" accept="application/zip,.zip"></div><p class="hint">报名人数、正式座位和备用座位由系统自动计算。每次递交都会进入 OJ；截止时以正式答案目录中的最后版本为准。</p><button>读取比赛并创建</button></form></section>
<section class="card"><h2>当前比赛</h2>{% if not contests %}<div class="empty">尚未创建比赛</div>{% else %}<table><tr><th>比赛</th><th>阶段</th><th>进度</th><th>下一步</th></tr>{% for c in contests %}<tr><td><b>{{ c.title }}</b><br><code>{{ c.tid }}</code><br><span class="hint">{{ c.begin_label }} — {{ c.end_label }}</span></td><td><span class="pill">{{ c.state_label }}</span><br><span class="hint">{{ c.message }}</span></td><td>材料：{% if c.material_state == 'approved' %}<span class="ok">已发布</span>{% else %}<span class="warn">{{ c.material_state_label }}</span>{% endif %}<br>座位：{{ c.pool_bound }} 已绑定 / {{ c.pool_total }} 总数<br>递交：{{ c.delivery.counts.submitted }} 已送评</td><td>{% if c.material_state != 'approved' %}<a class="button" href="/admin/materials">审核材料</a>{% elif c.state in ('registered','error') %}<a class="button" href="/admin/seats">准备座位</a>{% elif c.state == 'ready' %}<a class="button" href="/admin/submissions">查看比赛</a>{% else %}<a class="button" href="/admin/finish">查看收卷</a>{% endif %}</td></tr>{% endfor %}</table>{% endif %}</section>
{% elif section == 'materials' %}<section class="card"><h2>比赛材料</h2><p class="hint">系统固定生成 CSP 风格 PDF 与每题 2～4 组不计分的 .in/.out。只有 OJ 附件与学生桌面字节完全一致时，才允许进入备赛。</p>{% for c in contests %}<article class="card"><h3>{{ c.title }}</h3><p>{% for item in c.file_io_preview %}<span class="pill">{{ item.slug }}.in / {{ item.slug }}.out</span>{% endfor %}</p>{% if c.material_state == 'approved' %}<p class="ok">已发布并冻结：{{ c.active_material_revision }}</p><p class="hint">PDF {{ c.paper_sha256[:12] }}…；辅助数据 {{ c.testdata_files }} 个文件；OJ 回执 {% if c.material_publication %}{{ c.material_publication.receipt_sha256[:12] }}…{% else %}<span class="block">缺失</span>{% endif %}</p>{% else %}<p class="warn">{{ c.material_state_label }}</p>{% endif %}<div class="actions">{% if c.materials_mode == 'ai' and (not c.artifact_job or c.artifact_job.state in ('error','interrupted')) %}<form style="display:inline" method="post" action="/admin/materials/generate"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><button>生成待审核材料</button></form>{% endif %}{% for a in c.artifacts %}{% if a.state == 'review' %}<a class="button secondary" target="_blank" href="/admin/materials/{{ c.tid }}/{{ a.revision }}/paper">预览 PDF</a><a class="button secondary" target="_blank" href="/admin/materials/{{ c.tid }}/{{ a.revision }}/validation">查看校验报告</a><form style="display:inline" method="post" action="/admin/materials/approve"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><input type="hidden" name="revision" value="{{ a.revision }}"><button onclick="return confirm('确认 PDF、程序名和辅助数据无误，并发布到 OJ 与学生桌面？')">批准并发布</button></form>{% endif %}{% endfor %}</div></article>{% else %}<div class="empty">请先在比赛总览创建比赛</div>{% endfor %}</section>
{% elif section == 'seats' %}<section class="card"><h2>学生座位</h2><p class="hint">报名后自动增加新座位；系统持续保留至少 2 个或报名人数 10% 的备用座位，不需要老师审核扩容。</p>{% for c in contests %}<article class="card"><h3>{{ c.title }} · {{ c.state_label }}</h3><p><span class="pill">总座位 {{ c.pool_total }}</span><span class="pill">已绑定 {{ c.pool_bound }}</span><span class="pill">已发放 {{ c.pool_counts.get('released',0) }}</span><span class="pill">已验收备用 {{ c.pool_counts.get('verified',0) }}</span></p><div class="actions">{% if c.state in ('registered','error') %}<form style="display:inline" method="post" action="/admin/prepare"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><button {% if c.material_state != 'approved' %}disabled{% endif %}>运行教师测试并准备座位</button></form>{% endif %}{% if c.pool_revision is not none and c.state == 'ready' %}<form style="display:inline" method="post" action="/admin/pool/replace"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><input type="hidden" name="expected_revision" value="{{ c.pool_revision }}"><select name="slot_no" required><option value="">选择故障座位</option>{% for s in c.pool_seats %}{% if s.state not in ('planned','frozen','collected') %}<option value="{{ s.slot_no }}">{{ '%03d'|format(s.slot_no) }} · {{ s.state }}{% if s.uname %} · {{ s.uname }}{% endif %}</option>{% endif %}{% endfor %}</select><input name="reason" maxlength="200" placeholder="故障原因" required><input type="hidden" name="teacher_approved" value="yes"><button onclick="return confirm('确认隔离故障座位并自动切换到备用位？')">替换故障座位</button></form>{% for s in c.pool_seats %}{% if s.state == 'planned' and s.failure_count|int > 0 %}<form style="display:inline" method="post" action="/admin/pool/repair"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><input type="hidden" name="expected_revision" value="{{ c.pool_revision }}"><input type="hidden" name="slot_no" value="{{ s.slot_no }}"><button onclick="return confirm('重新创建并验收座位 {{ '%03d'|format(s.slot_no) }}，恢复备用容量？')">修复备用容量 {{ '%03d'|format(s.slot_no) }}</button></form>{% endif %}{% endfor %}{% endif %}<a class="button secondary" href="/admin/export/{{ c.tid }}">下载座位状态</a></div></article>{% else %}<div class="empty">暂无座位</div>{% endfor %}</section>
{% elif section == 'submissions' %}<section class="card"><h2>递交与评测</h2><p class="hint">老师在 OJ 系统查看源码、每次评测与实时分数。本页只显示送达状态，不展示或导出学生密码。</p>{% for c in contests %}<article class="card"><h3>{{ c.title }}</h3><p><span class="pill">已送评 {{ c.delivery.counts.submitted }}</span><span class="pill">等待 {{ c.delivery.counts.pending + c.delivery.counts.sending }}</span><span class="pill">重试 {{ c.delivery.counts.retry }}</span><span class="pill">待核对 {{ c.delivery.counts.ambiguous }}</span><span class="pill">异常 {{ c.delivery.counts.permanent_failed }}</span></p><p><span class="pill">入口通知已送达 {{ c.notifications.counts.sent }}</span><span class="pill">通知待处理 {{ c.notifications.counts.pending + c.notifications.counts.retry }}</span><span class="pill">通知异常 {{ c.notifications.counts.permanent_failed }}</span></p>{% if c.delivery.safe and c.notifications.safe %}<p class="ok">递交与通知队列正常</p>{% else %}<p class="block">存在待处理、待核对或异常项目，系统不会自动重发不确定递交，也不会静默结束比赛。</p>{% endif %}{% if c.notifications.counts.permanent_failed %}<form class="inline" method="post" action="/admin/notifications/retry"><input type="hidden" name="tid" value="{{ c.tid }}"><input type="hidden" name="csrf" value="{{ csrf }}"><button>重试失败通知</button></form>{% endif %}<a class="button" target="_blank" rel="noopener" href="{{ oj_base_url }}/contest/{{ c.tid }}/scoreboard?realtime=1">在 OJ 系统查看比赛</a></article>{% else %}<div class="empty">暂无比赛</div>{% endfor %}</section>
{% elif section == 'finish' %}<section class="card"><h2>结束与归档</h2><p class="hint">OJ 时间是唯一比赛时间。截止后系统冻结正式答案目录、补齐最后版本、等待评测队列清空并生成归档；只有证据完整才会安全关机。</p>{% for c in contests %}<article class="card"><h3>{{ c.title }} · {{ c.state_label }}</h3><p>收卷证据：{% if c.collection_receipt_sha256 %}<span class="ok">{{ c.collection_receipt_sha256[:12] }}…</span>{% else %}<span class="warn">尚未生成</span>{% endif %}<br>安全关机：{% if c.shutdown_verified_at_ms %}<span class="ok">已核验</span>{% elif c.shutdown_after_ms %}<span class="warn">等待保护期结束</span>{% else %}<span class="hint">尚未进入关机阶段</span>{% endif %}</p><form method="post" action="/admin/collect"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="tid" value="{{ c.tid }}"><button onclick="return confirm('只在需要提前结束或自动收卷失败时使用。确认现在开始收卷？')">提前结束 / 重试收卷</button></form>{% if not c.delivery.safe %}<p class="block">递交队列尚未清空，禁止关机。</p>{% endif %}</article>{% else %}<div class="empty">暂无归档</div>{% endfor %}</section>{% endif %}
</main></body></html>"""
)


def _html(content: str) -> HTMLResponse:
    return HTMLResponse(content, headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
def student_home():
    return _html(
        STUDENT_PAGE.render(
            seat=None,
            error=None,
            notice=cfg["orchestrator"].get("student_notice_url"),
        )
    )


@app.get("/desktop/{token}")
def student_desktop_redirect(token: str):
    """Validate a released seat, then leave the OJ path for the contest EIP."""
    unavailable = HTTPException(404, "桌面入口无效、尚未开放或已经结束")
    if not _GATEWAY_TOKEN.fullmatch(str(token)):
        raise unavailable
    seat = store.seat_by_gateway_token(token)
    if not seat:
        raise unavailable
    contest = store.get_contest(str(seat["tid"]))
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if (
        not contest
        or contest.get("state") != "ready"
        or int(contest.get("end_at_ms") or 0) <= now_ms
        or len(store.contests("ready")) != 1
    ):
        raise unavailable
    pool = store.seat_pool(str(seat["tid"]))
    assignment = store.seat_pool_assignment(
        str(seat["tid"]), int(seat["uid"])
    )
    if pool is not None and (
        assignment is None or assignment.get("state") != "released"
    ):
        raise unavailable

    gateway_ip = ""
    if not str(
        cfg["contest_server"].get("gateway_public_base_url") or ""
    ).strip():
        state, gateway_ip = cvm.status()
        if str(state).upper() != "RUNNING" or not gateway_ip:
            raise unavailable
    response = HTMLResponse(
        DESKTOP_LAUNCH_PAGE.render(
            direct_url=gateway_url(gateway_ip, token),
            fallback_url=fallback_gateway_url(token),
        )
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return response


@app.post("/query", response_class=HTMLResponse)
def student_query(uname: str = Form(...), password: str = Form(...)):
    if not hydro.verify_login(uname, password):
        return _html(
            STUDENT_PAGE.render(
                seat=None,
                error="用户名或密码不正确",
                notice=cfg["orchestrator"].get("student_notice_url"),
            )
        )
    pending_message = ""
    for contest in store.contests("ready"):
        seat = store.seat_by_uname(contest["tid"], uname)
        if seat:
            pool = store.seat_pool(contest["tid"])
            assignment = store.seat_pool_assignment(
                contest["tid"], int(seat["uid"])
            )
            if pool is not None and assignment is None:
                pending_message = "座位分配校验失败，请联系教师"
                continue
            if assignment and assignment.get("state") != "released":
                release_lead_minutes = int(contest["release_lead_minutes"])
                pending_message = (
                    "座位已预留并通过检查，将在比赛开始前 "
                    f"{release_lead_minutes} 分钟开放"
                )
                continue
            _, ip = cvm.status()
            url = gateway_url(ip, seat["token"])
            return _html(
                STUDENT_PAGE.render(
                    seat={
                        "uname": uname,
                        "url": url,
                        "vnc_pass": seat["vnc_pass"],
                        "mode_label": "正式答案目录 + 每次递交到 OJ",
                        "web_submit_url": web_submit_url(seat["submit_token"]),
                    },
                    error=None,
                    notice=cfg["orchestrator"].get("student_notice_url"),
                )
            )
    return _html(
        STUDENT_PAGE.render(
            seat=None,
            error=pending_message
            or "当前没有为你分配的座位（比赛未准备或未报名）",
            notice=cfg["orchestrator"].get("student_notice_url"),
        )
    )


def _web_submit_response(
    content: str, status_code: int = 200, script_nonce: str = ""
) -> HTMLResponse:
    script_policy = f" script-src 'nonce-{script_nonce}';" if script_nonce else ""
    return HTMLResponse(
        content,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline';"
                + script_policy
                + " "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'self'"
            ),
        },
    )


def _web_login_response(
    seat: dict,
    contest: dict,
    *,
    error: str = "",
    candidate: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    script_nonce = secrets.token_hex(16)
    contest_state = str(contest.get("state") or "unknown")
    return _web_submit_response(
        WEB_LOGIN_PAGE.render(
            seat=seat,
            error=error,
            candidate=candidate,
            contest_title=_public_contest_title(contest.get("title")),
            contest_state_label=CONTEST_STATE_LABELS.get(
                contest_state, contest_state
            ),
            contest_done=contest_state in {
                "collecting",
                "safe_wait",
                "safe_ended",
                "done",
            },
            script_nonce=script_nonce,
        ),
        status_code=status_code,
        script_nonce=script_nonce,
    )


def _web_submit_context(token: str) -> tuple[dict, dict, list[str], dict]:
    seat = store.seat_by_submit_token(token)
    if not seat:
        raise HTTPException(404, "提交入口不存在")
    contest = store.get_contest(seat["tid"])
    if not contest:
        raise HTTPException(404, "比赛不存在")
    pool = store.seat_pool(seat["tid"])
    assignment = store.seat_pool_assignment(seat["tid"], int(seat["uid"]))
    if pool is not None and assignment is None:
        raise HTTPException(403, "座位分配校验失败")
    if assignment and assignment.get("state") != "released":
        raise HTTPException(403, "座位尚未到发放时间")
    problems = json.loads(contest["files"])
    latest = store.latest_web_submissions(seat["tid"], int(seat["uid"]))
    return seat, contest, problems, latest


def _submit_cookie_name(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return f"noi_submit_{digest}"


def _submit_cookie_value(token: str) -> str:
    key = str(cfg["orchestrator"]["admin_password"]).encode("utf-8")
    message = f"submit:{token}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _submit_cookie_secure() -> bool:
    public_base_url = str(
        cfg["orchestrator"].get("public_base_url", "")
    ).strip()
    return urlsplit(public_base_url).scheme.lower() == "https"


def _submit_authenticated(request: Request, token: str) -> bool:
    actual = request.cookies.get(_submit_cookie_name(token), "")
    return secrets.compare_digest(actual, _submit_cookie_value(token))


def _submit_login_redirect(
    token: str, *, credentials_accepted: bool = False
) -> RedirectResponse:
    suffix = "?login=accepted" if credentials_accepted else ""
    return RedirectResponse(f"/submit/{token}{suffix}", status_code=303)


def _normalize_uploaded_source(payload: bytes, maximum: int) -> str:
    if len(payload) > maximum:
        raise HTTPException(413, f"源代码超过 {maximum} 字节限制")
    try:
        code = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            code = payload.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "源代码必须使用 UTF-8 或 GB18030 编码") from exc
    return _normalize_pasted_source(code, maximum)


def _normalize_pasted_source(code: str, maximum: int) -> str:
    normalized = code.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip() or "\x00" in normalized:
        raise HTTPException(400, "源代码为空或包含非法字符")
    if len(normalized.encode("utf-8")) > maximum:
        raise HTTPException(413, f"源代码超过 {maximum} 字节限制")
    return normalized


_CLIENT_NONCE = re.compile(r"^[0-9a-f]{32}$")


def _snapshot_submission_open(contest: dict, at_ms: int) -> bool | None:
    begin_at = int(contest.get("begin_at_ms") or 0)
    end_at = int(contest.get("end_at_ms") or 0)
    if not begin_at or not end_at:
        return None
    rule = str(contest.get("hydro_rule") or "")
    return (
        contest.get("state") == "ready"
        and rule == "oi"
        and begin_at <= int(at_ms) < end_at
    )


def _submission_window_open(contest: dict) -> bool:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    snapshot = _snapshot_submission_open(contest, now_ms)
    if snapshot is not None:
        return snapshot
    return contest.get("state") == "ready" and _contest_submission_open(str(contest["tid"]))


def _enqueue_web_source(
    seat: dict,
    contest: dict,
    problem: str,
    source: str,
    client_nonce: str,
) -> dict:
    """Persist one click and enqueue its exact Hydro judge payload."""
    if realtime_judge is None:
        raise HTTPException(503, "实时评测服务未启用，本次提交未被接收")
    accepted_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    snapshot = _snapshot_submission_open(contest, accepted_at_ms)
    allow_new = bool(snapshot)
    if snapshot is None:
        try:
            document = hydro.get_contest(str(contest["tid"]))
        except Exception as exc:
            raise HTTPException(503, "暂时无法核验比赛时间，请立即重试") from exc
        if not document or str(document.get("rule") or "") != "oi":
            raise HTTPException(409, "本场当前不是可实时递交的 OI 比赛")
        try:
            begin_at_ms = int(_utc(document["beginAt"]).timestamp() * 1000)
            end_at_ms = int(_utc(document["endAt"]).timestamp() * 1000)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(503, "Hydro 比赛时间配置无效") from exc
        allow_new = begin_at_ms <= accepted_at_ms < end_at_ms
    nonce = str(client_nonce).strip().lower()
    if not _CLIENT_NONCE.fullmatch(nonce):
        raise HTTPException(400, "递交页面已失效，请返回题目列表后重新提交")
    try:
        pid_map = json.loads(contest.get("pids") or "{}")
    except (TypeError, ValueError) as exc:
        raise HTTPException(503, "本场题目映射无效，请联系教师") from exc
    pid = str(pid_map.get(problem, "")).strip()
    if not pid:
        raise HTTPException(503, f"题目 {problem} 尚未配置 OJ 映射")
    issues = check_code(source, problem)
    judge_source = force_zero_code(source, issues) if issues else source
    try:
        return realtime_judge.enqueue(
            submission_session=str(contest["submission_session"]),
            tid=str(contest["tid"]),
            uid=int(seat["uid"]),
            problem=problem,
            pid=pid,
            source=source,
            judge_source=judge_source,
            issues=issues,
            client_nonce=nonce,
            accepted_at_ms=accepted_at_ms,
            allow_new=allow_new,
        )
    except SubmissionClosedError as exc:
        raise HTTPException(
            409, "提交处理完成时已超过 Hydro 比赛截止时间"
        ) from exc
    except SubmissionConflictError as exc:
        raise HTTPException(
            409, "同一递交请求的内容发生变化，请返回题目列表后重新提交"
        ) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(503, f"无法建立 OJ 送评任务：{exc}") from exc


@app.get("/submit/{token}", response_class=HTMLResponse)
def web_submit_page(
    request: Request, token: str, saved: str = "", login: str = ""
):
    seat, contest, problems, latest = _web_submit_context(token)
    if not _submit_authenticated(request, token):
        error = ""
        if login == "accepted":
            error = (
                "账号密码正确，但浏览器未保存登录状态。"
                "请关闭隐私浏览后重试，仍无法登录请立即联系老师。"
            )
        return _web_login_response(seat, contest, error=error)
    return _web_submit_response(
        WEB_SUBMIT_PAGE.render(
            seat=seat,
            contest=contest,
            problems=problems,
            latest=latest,
            opened=_submission_window_open(contest),
            saved=saved if saved in problems else "",
            mode_label=MODE_LABELS.get(contest["submission_mode"], ""),
            submit_nonces={
                problem: RealtimeJudge.new_client_nonce() for problem in problems
            },
            now=datetime.now().strftime("%H:%M:%S"),
        )
    )


@app.post("/submit/{token}/formal/{problem}")
def web_submit_formal_file(
    request: Request,
    token: str,
    problem: str,
    client_nonce: str = Form(...),
):
    seat, contest, problems, _ = _web_submit_context(token)
    if not _submit_authenticated(request, token):
        return _submit_login_redirect(token)
    if problem not in problems:
        raise HTTPException(400, "题目名称不属于本场比赛")
    if not _submission_window_open(contest):
        raise HTTPException(409, "当前不在 OJ 比赛递交时间内")
    maximum = int(cfg["orchestrator"].get("web_submit_max_bytes", 102400))
    try:
        payload = pipe.read_formal_source(
            str(contest["tid"]),
            int(seat["uid"]),
            problem,
            maximum_bytes=maximum,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, f"无法读取正式文件：{exc}") from exc
    normalized = _normalize_uploaded_source(payload, maximum)
    _enqueue_web_source(seat, contest, problem, normalized, client_nonce)
    return RedirectResponse(f"/submit/{token}?saved={problem}", status_code=303)


@app.post("/submit/{token}/login", response_class=HTMLResponse)
def web_submit_login(
    token: str,
    candidate: str = Form(...),
    password: str = Form(...),
):
    seat, contest, _, _ = _web_submit_context(token)
    login_name = candidate.strip()
    valid_name = login_name in {str(seat["candidate"]), str(seat["uname"])}
    valid_password = secrets.compare_digest(
        password.encode("utf-8"), str(seat["vnc_pass"]).encode("utf-8")
    )
    if not (valid_name and valid_password):
        return _web_login_response(
            seat,
            contest,
            error="准考证号或密码错误",
            candidate=login_name,
            status_code=401,
        )
    response = _submit_login_redirect(token, credentials_accepted=True)
    response.set_cookie(
        _submit_cookie_name(token),
        _submit_cookie_value(token),
        path=f"/submit/{token}",
        httponly=True,
        samesite="strict",
        secure=_submit_cookie_secure(),
    )
    return response


@app.get("/submit/{token}/logout")
def web_submit_logout(token: str):
    _web_submit_context(token)
    response = _submit_login_redirect(token)
    response.delete_cookie(_submit_cookie_name(token), path=f"/submit/{token}")
    return response


@app.get("/submit/{token}/paper")
def web_submit_paper(request: Request, token: str):
    seat, contest, _, _ = _web_submit_context(token)
    if not _submit_authenticated(request, token):
        return _submit_login_redirect(token)
    path = paper_path(contest["tid"])
    if not contest.get("paper_sha256") or not path.is_file():
        raise HTTPException(404, "试题 PDF 尚未上传")
    if sha256_file(path) != contest["paper_sha256"]:
        raise HTTPException(409, "试题 PDF 哈希校验失败")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=str(contest.get("paper_name") or "试题.pdf"),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/submit/{token}/edit/{problem}", response_class=HTMLResponse)
def web_submit_edit(request: Request, token: str, problem: str):
    seat, contest, problems, _ = _web_submit_context(token)
    if not _submit_authenticated(request, token):
        return _submit_login_redirect(token)
    if problem not in problems:
        raise HTTPException(404, "题目名称不属于本场比赛")
    return RedirectResponse(f"/submit/{token}", status_code=303)


@app.get("/submit/{token}/view/{problem}", response_class=HTMLResponse)
def web_submit_view(request: Request, token: str, problem: str):
    seat, _, problems, latest = _web_submit_context(token)
    if not _submit_authenticated(request, token):
        return _submit_login_redirect(token)
    if problem not in problems or problem not in latest:
        raise HTTPException(404, "尚无该题提交记录")
    return _web_submit_response(
        WEB_VIEW_PAGE.render(
            seat=seat,
            problem=problem,
            submission=latest[problem],
        )
    )


@app.get("/submit/{token}/download/{problem}")
def web_submit_download(request: Request, token: str, problem: str):
    _, _, problems, latest = _web_submit_context(token)
    if not _submit_authenticated(request, token):
        return _submit_login_redirect(token)
    if problem not in problems or problem not in latest:
        raise HTTPException(404, "尚无该题提交记录")
    source = str(latest[problem]["source"])
    return Response(
        content=source.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{problem}.cpp"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/submit/{token}")
def web_submit_code(
    request: Request,
    token: str,
    problem: str = Form(...),
    client_nonce: str = Form(...),
    source: UploadFile = File(...),
):
    _, _, _, _ = _web_submit_context(token)
    if not _submit_authenticated(request, token):
        return _submit_login_redirect(token)
    raise HTTPException(
        410,
        "旧上传入口已停用；请返回程序回收系统，点击“递交正式文件”",
    )


@app.post("/submit/{token}/paste")
def web_submit_paste(
    request: Request,
    token: str,
    problem: str = Form(...),
    client_nonce: str = Form(...),
    code: str = Form(...),
):
    _, _, _, _ = _web_submit_context(token)
    if not _submit_authenticated(request, token):
        return _submit_login_redirect(token)
    raise HTTPException(
        410,
        "旧粘贴入口已停用；请返回程序回收系统，点击“递交正式文件”",
    )


@app.get("/admin/sso")
def admin_sso(ticket: str):
    try:
        identity = verify_hydro_ticket(
            ticket, str(cfg["hydro"]["orchestrator_token"])
        )
    except ValueError as exc:
        raise HTTPException(401, "OJ 教师登录票据无效或已过期") from exc
    digest = hashlib.sha256(ticket.encode("ascii", "strict")).hexdigest()
    now = int(datetime.now(timezone.utc).timestamp())
    with _TEACHER_TICKET_LOCK:
        for key, expires_at in list(_USED_TEACHER_TICKETS.items()):
            if expires_at < now:
                _USED_TEACHER_TICKETS.pop(key, None)
        if digest in _USED_TEACHER_TICKETS:
            raise HTTPException(409, "OJ 教师登录票据已使用，请从 OJ 比赛页重新进入")
        _USED_TEACHER_TICKETS[digest] = identity.expires_at
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        TEACHER_COOKIE,
        issue_teacher_session(identity, str(cfg["hydro"]["orchestrator_token"])),
        max_age=12 * 60 * 60,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@app.get("/admin", response_class=HTMLResponse)
def admin(teacher: TeacherIdentity = Depends(require_admin)):
    return _admin_page("overview", teacher)


@app.get("/admin/{section}", response_class=HTMLResponse)
def admin_section(section: str, teacher: TeacherIdentity = Depends(require_admin)):
    if section not in ADMIN_SECTIONS:
        raise HTTPException(404, "教师页面不存在")
    return _admin_page(section, teacher)


def _admin_time_label(value_ms: int) -> str:
    if int(value_ms or 0) <= 0:
        return "未设置"
    return (
        datetime.fromtimestamp(int(value_ms) / 1000, timezone.utc)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .strftime("%Y-%m-%d %H:%M")
    )


def _admin_page(section: str, teacher: TeacherIdentity) -> HTMLResponse:
    contests = []
    for item in store.contests():
        if teacher.tid is not None and item.get("tid") != teacher.tid:
            continue
        contest = dict(item)
        try:
            registered_files = json.loads(contest.get("files") or "[]")
            registered_pids = json.loads(contest.get("pids") or "{}")
        except json.JSONDecodeError:
            registered_files, registered_pids = [], {}
        contest["file_io_preview"] = [
            {"slug": slug, "pid": str(registered_pids.get(slug) or "")}
            for slug in registered_files
            if isinstance(slug, str) and _FILE_NAME.fullmatch(slug)
        ]
        pool = store.seat_pool(contest["tid"])
        if pool:
            counts = {}
            for seat in pool["state"].get("seats", []):
                key = str(seat.get("state") or "unknown")
                counts[key] = counts.get(key, 0) + 1
            contest["pool_counts"] = counts
            contest["pool_revision"] = pool["revision"]
            contest["pool_seats"] = list(pool["state"].get("seats", []))
        else:
            contest["pool_counts"] = {}
            contest["pool_revision"] = None
            contest["pool_seats"] = []
        contest["artifact_job"] = store.latest_artifact_job(contest["tid"])
        contest["artifacts"] = store.artifact_revisions(contest["tid"])
        active_revision = str(contest.get("active_material_revision") or "")
        contest["material_publication"] = (
            store.material_publication(contest["tid"], active_revision)
            if active_revision
            else None
        )
        contest["delivery"] = store.contest_delivery_health(contest["tid"])
        contest["notifications"] = store.contest_notification_health(contest["tid"])
        contest["audit_events"] = store.audit_events(contest["tid"], limit=8)
        contest["pool_total"] = len(contest["pool_seats"])
        contest["pool_bound"] = sum(
            1 for seat in contest["pool_seats"] if seat.get("uid") is not None
        )
        contest["state_label"] = CONTEST_STATE_LABELS.get(
            str(contest.get("state") or ""), str(contest.get("state") or "未知")
        )
        if contest.get("state") == "ready":
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            contest["state_label"] = (
                "等待开赛"
                if now_ms < int(contest.get("begin_at_ms") or 0)
                else "比赛进行中"
            )
        contest["material_state_label"] = {
            "pending": "等待生成或上传",
            "generating": "正在生成",
            "draft": "机器校验未完成",
            "review": "等待老师审核",
            "approved": "已发布",
        }.get(
            str(contest.get("material_state") or ""),
            str(contest.get("material_state") or "未知"),
        )
        contest["begin_label"] = _admin_time_label(contest.get("begin_at_ms") or 0)
        contest["end_label"] = _admin_time_label(contest.get("end_at_ms") or 0)
        sync_ms = int(contest.get("time_sync_at_ms") or 0)
        contest["time_sync_label"] = (
            datetime.fromtimestamp(sync_ms / 1000, timezone.utc)
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .strftime("%Y-%m-%d %H:%M:%S")
            if sync_ms
            else ""
        )
        contests.append(contest)
    return _html(
        ADMIN_V1_PAGE.render(
            contests=contests,
            section=section,
            section_label=ADMIN_SECTIONS[section],
            sections=ADMIN_SECTIONS,
            csrf=ADMIN_CSRF,
            oj_base_url=str(cfg["hydro"]["public_base_url"]).rstrip("/"),
            defaults={
                "max_participants": int(
                    cfg["orchestrator"].get("default_max_participants", 15)
                ),
                "spare_seats": int(
                    cfg["orchestrator"].get("default_spare_seats", 2)
                ),
                "release_lead_minutes": int(
                    cfg["orchestrator"].get("release_lead_minutes", 5)
                ),
                "practice_groups": int(
                    cfg["orchestrator"].get("practice_groups_per_problem", 3)
                ),
            },
            teacher_tid=teacher.tid,
        )
    )


def _approve_manual_artifact(tid: str, approved_by: str) -> dict:
    contest = store.get_contest(tid)
    if not contest or not contest.get("paper_sha256"):
        raise RuntimeError("人工材料缺少试题 PDF")
    if not contest.get("testdata_sha256"):
        raise RuntimeError("V1 人工材料缺少辅助自测数据")
    files = [
        {
            "path": "paper.pdf",
            "audience": "student",
            "size": int(contest.get("paper_size") or 0),
            "sha256": str(contest["paper_sha256"]),
        }
    ]
    if contest.get("testdata_sha256"):
        files.append(
            {
                "path": "testdata.tar.gz",
                "audience": "student",
                "size": int(contest.get("testdata_size") or 0),
                "sha256": str(contest["testdata_sha256"]),
            }
        )
    manifest = {
        "schema_version": 1,
        "tid": tid,
        "mode": "manual",
        "status": "teacher_uploaded",
        # Re-registration creates a new immutable review even when the bytes
        # are unchanged.  Binding the revision to the freshly rotated submit
        # session prevents an old approved row from being silently re-used
        # after the contest selection was cleared by upsert_contest().
        "submission_session": str(contest.get("submission_session") or ""),
        "files": files,
    }
    payload = (
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    revision = f"manual-{digest[:16]}"
    root = Path(cfg["orchestrator"].get("materials_dir", "/app/data/materials")) / tid
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f"manual-manifest-{secrets.token_hex(6)}.tmp"
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, root / "manifest.json")
    finally:
        temporary.unlink(missing_ok=True)
    store.put_artifact_revision(
        tid,
        revision,
        state="review",
        source_sha256=digest,
        root_path=str(root),
        manifest_sha256=digest,
        manifest=manifest,
        paper_name=str(contest.get("paper_name") or "试题.pdf"),
        paper_sha256=str(contest["paper_sha256"]),
        paper_size=int(contest.get("paper_size") or 0),
        testdata_name=str(contest.get("testdata_name") or ""),
        testdata_sha256=str(contest.get("testdata_sha256") or ""),
        testdata_size=int(contest.get("testdata_size") or 0),
        testdata_files=int(contest.get("testdata_files") or 0),
        testdata_expanded_size=int(contest.get("testdata_expanded_size") or 0),
    )
    paper_path = material_paper_path(
        cfg["orchestrator"].get("materials_dir", "/app/data/materials"), tid
    )
    testdata_path = material_testdata_archive_path(
        cfg["orchestrator"].get("materials_dir", "/app/data/materials"), tid
    )
    publication = material_publisher.publish(
        tid=tid,
        revision=revision,
        paper_path=paper_path,
        paper_sha256=str(contest["paper_sha256"]),
        testdata_path=testdata_path,
        testdata_sha256=str(contest["testdata_sha256"]),
    )
    if publication.get("ok") is not True:
        raise RuntimeError("OJ 未确认接收比赛 PDF 与辅助自测数据")
    return store.approve_artifact_with_publication(
        tid,
        revision,
        approved_by,
        publication,
    )


def _verify_reusable_manual_files(
    contest: dict,
    *,
    require_paper: bool,
    require_testdata: bool,
) -> None:
    """Fail closed before re-registration reuses legacy manual material.

    Generated revisions live below artifact_root and deliberately are not
    copied into the mutable legacy materials directory.  Therefore metadata
    alone is not evidence that an omitted manual upload can be re-used.
    """
    tid = str(contest.get("tid") or "")
    materials_root = cfg["orchestrator"].get(
        "materials_dir", "/app/data/materials"
    )
    candidates: list[tuple[str, Path, str, int]] = []
    if require_paper:
        candidates.append(
            (
                "试题 PDF",
                material_paper_path(materials_root, tid),
                str(contest.get("paper_sha256") or ""),
                int(contest.get("paper_size") or 0),
            )
        )
    if require_testdata and contest.get("testdata_sha256"):
        candidates.append(
            (
                "测试数据",
                material_testdata_archive_path(materials_root, tid),
                str(contest.get("testdata_sha256") or ""),
                int(contest.get("testdata_size") or 0),
            )
        )
    for label, path, expected_hash, expected_size in candidates:
        try:
            stat_result = path.stat()
        except OSError as exc:
            raise HTTPException(
                400, f"原人工{label}文件不存在，请重新上传"
            ) from exc
        if (
            not path.is_file()
            or not expected_hash
            or int(stat_result.st_size) != expected_size
            or sha256_file(path) != expected_hash
        ):
            raise HTTPException(
                400, f"原人工{label}文件与登记摘要不一致，请重新上传"
            )


def _activate_generated_artifact(tid: str, revision: str, approved_by: str) -> dict:
    # This DB-only read gate happens before any file operation. The final
    # approval repeats the same checks under BEGIN IMMEDIATE, closing races
    # with seat preparation or another revision approval.
    artifact = store.artifact_approval_candidate(tid, revision)
    root = Path(artifact["root_path"]).resolve()
    configured = Path(
        cfg["orchestrator"].get("artifact_root", "/app/data/artifacts")
    ).resolve()
    if configured != root and configured not in root.parents:
        raise RuntimeError("材料版本目录越过 artifact_root")
    paper = root / "student" / "paper.pdf"
    testdata = root / "student" / "testdata.tar.gz"
    manifest = root / "manifest.json"
    for path, digest in (
        (paper, artifact["paper_sha256"]),
        (manifest, artifact["manifest_sha256"]),
    ):
        if not path.is_file() or sha256_file(path) != digest:
            raise RuntimeError(f"材料文件缺失或哈希不符: {path.name}")
    if not artifact.get("testdata_sha256"):
        raise RuntimeError("V1 材料版本缺少辅助自测数据")
    if not testdata.is_file() or sha256_file(testdata) != artifact["testdata_sha256"]:
        raise RuntimeError("学生自测数据缺失或哈希不符")
    # Generated files remain in their immutable revision directory. Downstream
    # resolution follows active_material_revision, so approval has no mutable
    # legacy-file copy step that could partially fail or overwrite old files.
    publication = material_publisher.publish(
        tid=tid,
        revision=revision,
        paper_path=paper,
        paper_sha256=str(artifact["paper_sha256"]),
        testdata_path=testdata,
        testdata_sha256=str(artifact["testdata_sha256"]),
    )
    if publication.get("ok") is not True:
        raise RuntimeError("OJ 未确认接收比赛 PDF 与辅助自测数据")
    return store.approve_artifact_with_publication(
        tid,
        revision,
        approved_by,
        publication,
    )


_TID = re.compile(r"^[0-9a-fA-F]{24}$")
_FILE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@app.post("/admin/register")
def admin_register(
    tid: str = Form(...),
    files: str = Form(""),
    # Accepted only for compatibility with older automation; ignored below.
    submission_mode: str = Form("folder"),
    materials_mode: str = Form("ai"),
    # V1 capacity comes from the live OJ roster; legacy form values are ignored.
    max_participants: int = Form(15),
    spare_seats: int = Form(2),
    release_lead_minutes: int = Form(5),
    practice_groups: int = Form(3),
    csrf: str = Form(...),
    paper: UploadFile | None = File(None),
    testdata: UploadFile | None = File(None),
    _: TeacherIdentity = Depends(require_admin),
):
    teacher = _
    require_csrf(csrf)
    tid = require_teacher_contest(teacher, tid)
    if not _TID.fullmatch(tid):
        raise HTTPException(400, "比赛 tid 必须是 24 位 ObjectId")
    if materials_mode not in {"ai", "manual"}:
        raise HTTPException(400, "备赛材料方式无效")
    if not 1 <= int(release_lead_minutes) <= 60:
        raise HTTPException(400, "座位发放提前量必须在 1 到 60 分钟之间")
    if not 2 <= int(practice_groups) <= 4:
        raise HTTPException(400, "每题自测数据必须为 2 到 4 组")
    if materials_mode == "ai" and (
        (paper is not None and paper.filename)
        or (testdata is not None and testdata.filename)
    ):
        raise HTTPException(400, "AI 自动生成模式无需上传 PDF 或测试数据 ZIP")
    if not cfg["hydro"].get("submit_enabled"):
        raise HTTPException(503, "OJ 实时评测未启用，不能登记 V1 比赛")
    document = hydro.get_contest(tid)
    if not document:
        raise HTTPException(400, "Hydro 中找不到该比赛 tid")
    if str(document.get("rule", "")) != "oi":
        raise HTTPException(
            400, "NOI Linux V1 只允许 OJ 的 OI 赛制，以保证学生赛中盲测"
        )
    try:
        begin_at_ms = int(_utc(document["beginAt"]).timestamp() * 1000)
        end_at_ms = int(_utc(document["endAt"]).timestamp() * 1000)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, "Hydro 比赛缺少有效的开始或结束时间") from exc
    if end_at_ms <= begin_at_ms:
        raise HTTPException(400, "Hydro 比赛结束时间必须晚于开始时间")
    file_list: list[str] = []
    pid_map: dict[str, str] = {}
    seen_files: set[str] = set()
    seen_pids: set[str] = set()
    for raw in files.split(","):
        raw = raw.strip()
        if not raw:
            continue
        name, separator, pid = raw.partition("=")
        name, pid = name.strip(), pid.strip()
        if not _FILE_NAME.fullmatch(name):
            raise HTTPException(400, f"非法题目文件名: {name}")
        if name in seen_files:
            raise HTTPException(400, f"题目文件名重复: {name}")
        seen_files.add(name)
        if (
            cfg["hydro"].get("submit_enabled") or materials_mode == "ai"
        ) and (not separator or not pid):
            raise HTTPException(
                400, f"启用回传或 AI 材料时必须填写 {name}=Hydro题号"
            )
        pid_key = pid.casefold()
        if pid and pid_key in seen_pids:
            raise HTTPException(400, f"Hydro 题号重复映射: {pid}")
        file_list.append(name)
        if pid:
            seen_pids.add(pid_key)
            pid_map[name] = pid
    if not file_list and materials_mode == "ai":
        try:
            file_list, pid_map, _ = auto_problem_mapping(
                document, hydro.get_problem
            )
        except ProblemMappingError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, "暂时无法读取 Hydro 比赛题目") from exc
    if not file_list:
        raise HTTPException(400, "人工材料模式至少登记一道题")
    if cfg["hydro"].get("submit_enabled") or materials_mode == "ai":
        try:
            contest_pids = {int(value) for value in document.get("pids") or []}
            for name, pid in pid_map.items():
                problem = hydro.get_problem(pid)
                if not problem or int(problem["docId"]) not in contest_pids:
                    raise HTTPException(
                        400, f"{name} 映射的 Hydro 题目 {pid} 不属于本场比赛"
                    )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "暂时无法核验 Hydro 题目映射") from exc

    existing = store.get_contest(tid)
    if existing and existing.get("state") in {
        "preparing",
        "ready",
        "collecting",
        "safe_wait",
    }:
        raise HTTPException(
            409, "比赛正在备赛、进行或收卷，禁止重新登记以免改变实时送评会话"
        )
    if existing and (store.seats(tid) or store.web_submission_count(tid)):
        raise HTTPException(
            409,
            "本场已有座位、收卷证据或实时递交，禁止覆盖登记；"
            "请完成本场收卷，如需重开请在 Hydro 新建比赛",
        )
    paper_data: tuple[str, bytes, str] | None = None
    if paper is not None and paper.filename:
        maximum = int(
            cfg["orchestrator"].get("paper_max_bytes", 64 * 1024 * 1024)
        )
        try:
            paper_data = read_pdf_upload(paper.file, paper.filename, maximum)
        except MaterialError as exc:
            status_code = 413 if "超过" in str(exc) else 400
            raise HTTPException(status_code, str(exc)) from exc
    elif materials_mode == "manual" and (
        not existing or not existing.get("paper_sha256")
    ):
        raise HTTPException(400, "首次登记比赛必须上传试题 PDF")

    testdata_data: tuple[str, bytes, str, int, int] | None = None
    if testdata is not None and testdata.filename:
        try:
            testdata_data = read_testdata_upload(
                testdata.file,
                testdata.filename,
                int(cfg["orchestrator"].get("testdata_max_bytes", 64 * 1024 * 1024)),
                int(
                    cfg["orchestrator"].get(
                        "testdata_expanded_max_bytes", 256 * 1024 * 1024
                    )
                ),
                int(cfg["orchestrator"].get("testdata_max_files", 1000)),
                file_list,
            )
        except MaterialError as exc:
            status_code = 413 if "超过" in str(exc) else 400
            raise HTTPException(status_code, str(exc)) from exc
    elif existing and existing.get("testdata_sha256"):
        previous_files = json.loads(existing.get("files") or "[]")
        if previous_files != file_list:
            raise HTTPException(
                400, "题目文件名已变化，请重新选择与新题目匹配的测试数据 ZIP"
            )

    if materials_mode == "manual" and existing:
        _verify_reusable_manual_files(
            existing,
            require_paper=paper_data is None,
            require_testdata=testdata_data is None,
        )

    _audit_teacher(
        teacher,
        tid,
        "contest.register",
        details={
            "materials_mode": materials_mode,
            "practice_groups": int(practice_groups),
        },
    )
    store.upsert_contest(
        tid,
        str(document.get("title", tid)),
        file_list,
        pid_map,
        "both",
        begin_at_ms=begin_at_ms,
        end_at_ms=end_at_ms,
        hydro_rule=str(document.get("rule") or ""),
        materials_mode=materials_mode,
        material_state="pending" if materials_mode == "ai" else "review",
        # The authoritative values are replaced from the live roster during
        # prepare and every later synchronization.
        max_participants=0,
        spare_seats=2,
        release_lead_minutes=int(release_lead_minutes),
        practice_groups=int(practice_groups),
    )
    if materials_mode == "ai":
        store.clear_material_selection(tid)
    if paper_data:
        paper_name, payload, digest = paper_data
        save_material_paper(
            cfg["orchestrator"].get("materials_dir", "/app/data/materials"),
            tid,
            payload,
        )
        store.set_paper(tid, paper_name, digest, len(payload))
    if testdata_data:
        testdata_name, payload, digest, file_count, expanded_size = testdata_data
        save_testdata_archive(
            cfg["orchestrator"].get("materials_dir", "/app/data/materials"),
            tid,
            payload,
        )
        store.set_testdata(
            tid,
            testdata_name,
            digest,
            len(payload),
            file_count,
            expanded_size,
        )
    if materials_mode == "manual":
        _approve_manual_artifact(tid, _teacher_actor(teacher))
    return RedirectResponse("../admin", status_code=303)


@app.post("/admin/prepare")
def admin_prepare(
    tid: str = Form(...),
    csrf: str = Form(...),
    teacher: TeacherIdentity = Depends(require_admin),
):
    require_csrf(csrf)
    tid = require_teacher_contest(teacher, tid)
    contest = store.get_contest(tid)
    if not contest or contest.get("material_state") != "approved":
        raise HTTPException(409, "备赛材料尚未由教师批准并冻结")
    _audit_teacher(teacher, tid, "contest.prepare")
    if not store.transition(tid, {"registered", "error"}, "preparing", "手动触发备赛"):
        raise HTTPException(409, "当前状态不允许备赛")
    _spawn(pipe.prepare, tid)
    return RedirectResponse("../admin", status_code=303)


@app.post("/admin/notifications/retry")
def admin_retry_notifications(
    tid: str = Form(...),
    csrf: str = Form(...),
    teacher: TeacherIdentity = Depends(require_admin),
):
    require_csrf(csrf)
    tid = require_teacher_contest(teacher, tid)
    if not _TID.fullmatch(tid):
        raise HTTPException(400, "比赛 tid 必须是 24 位 ObjectId")
    if notifier is None:
        raise HTTPException(503, "OJ 入口通知未启用")
    try:
        changed = store.retry_failed_seat_notifications(tid)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except SubmissionConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    _audit_teacher(
        teacher,
        tid,
        "contest.notifications.retry",
        details={"requeued": int(changed)},
    )
    if changed:
        _spawn_notification_retry(tid)
    return RedirectResponse("/admin/submissions", status_code=303)


@app.post("/admin/materials/generate")
def admin_material_generate(
    tid: str = Form(...),
    csrf: str = Form(...),
    teacher: TeacherIdentity = Depends(require_admin),
):
    """Queue only after the teacher explicitly approves private problem clones."""
    require_csrf(csrf)
    tid = require_teacher_contest(teacher, tid)
    if artifact_runner is None:
        raise HTTPException(503, "AI 材料生成尚未安全配置")
    _audit_teacher(teacher, tid.strip(), "contest.materials.generate")
    try:
        job = artifact_runner.start(tid, _teacher_actor(teacher))
    except (KeyError, ValueError, RuntimeError, SubmissionConflictError) as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        worker = threading.Thread(
            target=artifact_runner.run,
            args=(job["job_id"],),
            name=f"artifact-{job['job_id'][:8]}",
            daemon=True,
        )
        worker.start()
    except Exception as exc:
        try:
            store.update_artifact_job(
                job["job_id"],
                "error",
                progress=0,
                message="材料后台任务未能启动，未发布",
                error=str(exc),
                details=job.get("details") or {},
            )
        except Exception:
            log.exception("cannot mark unstarted material job failed")
        raise HTTPException(500, "材料后台任务未能启动") from exc
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/materials/{tid}/{revision}/paper")
def admin_material_paper(
    tid: str, revision: str, teacher: TeacherIdentity = Depends(require_admin)
):
    tid = require_teacher_contest(teacher, tid)
    artifact = store.artifact_revision(tid, revision)
    if not artifact:
        raise HTTPException(404, "材料版本不存在")
    root = Path(artifact["root_path"]).resolve()
    allowed = Path(
        cfg["orchestrator"].get("artifact_root", "/app/data/artifacts")
    ).resolve()
    paper = (root / "student" / "paper.pdf").resolve()
    if (allowed != root and allowed not in root.parents) or root not in paper.parents:
        raise HTTPException(400, "材料路径无效")
    if not paper.is_file() or sha256_file(paper) != artifact["paper_sha256"]:
        raise HTTPException(409, "PDF 缺失或哈希校验失败")
    return FileResponse(
        paper,
        media_type="application/pdf",
        filename=f"{tid}-{revision}.pdf",
        headers={"Cache-Control": "no-store"},
    )


def _verified_artifact_json_file(
    tid: str, revision: str, relative_path: str
) -> tuple[Path, dict]:
    artifact = store.artifact_revision(tid, revision)
    if not artifact:
        raise HTTPException(404, "材料版本不存在")
    root = Path(artifact["root_path"]).resolve()
    allowed = Path(
        cfg["orchestrator"].get("artifact_root", "/app/data/artifacts")
    ).resolve()
    path = (root / relative_path).resolve()
    if (
        (allowed != root and allowed not in root.parents)
        or root not in path.parents
        or not path.is_file()
    ):
        raise HTTPException(409, "教师审阅文件缺失或路径无效")
    expected = ""
    if relative_path == "manifest.json":
        expected = str(artifact.get("manifest_sha256") or "")
    else:
        for item in (artifact.get("manifest") or {}).get("files", []):
            if isinstance(item, dict) and item.get("path") == relative_path:
                expected = str(item.get("sha256") or "")
                break
    if not expected or sha256_file(path) != expected:
        raise HTTPException(409, "教师审阅文件哈希校验失败")
    return path, artifact


@app.get("/admin/materials/{tid}/{revision}/validation")
def admin_material_validation(
    tid: str, revision: str, teacher: TeacherIdentity = Depends(require_admin)
):
    tid = require_teacher_contest(teacher, tid)
    path, _ = _verified_artifact_json_file(
        tid, revision, "teacher/validation-report.json"
    )
    return FileResponse(
        path,
        media_type="application/json",
        filename=f"{tid}-{revision}-validation-report.json",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/admin/materials/approve")
def admin_material_approve(
    tid: str = Form(...),
    revision: str = Form(...),
    csrf: str = Form(...),
    teacher: TeacherIdentity = Depends(require_admin),
):
    require_csrf(csrf)
    tid = require_teacher_contest(teacher, tid)
    _audit_teacher(
        teacher,
        tid,
        "contest.materials.approve",
        details={"revision": revision},
    )
    try:
        _activate_generated_artifact(tid, revision, _teacher_actor(teacher))
    except (KeyError, RuntimeError, SubmissionConflictError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/collect")
def admin_collect(
    tid: str = Form(...),
    csrf: str = Form(...),
    teacher: TeacherIdentity = Depends(require_admin),
):
    require_csrf(csrf)
    tid = require_teacher_contest(teacher, tid)
    if not store.seats(tid):
        raise HTTPException(409, "座位表为空，无法收卷")
    _audit_teacher(teacher, tid, "contest.collect")
    if not store.transition(tid, {"ready", "error"}, "collecting", "手动触发收卷"):
        raise HTTPException(409, "当前状态不允许收卷")
    _spawn(pipe.collect, tid)
    return RedirectResponse("../admin", status_code=303)


@app.post("/admin/pool/replace")
def admin_pool_replace(
    tid: str = Form(...),
    slot_no: int = Form(...),
    reason: str = Form(...),
    expected_revision: int = Form(...),
    teacher_approved: str = Form(""),
    csrf: str = Form(...),
    teacher: TeacherIdentity = Depends(require_admin),
):
    require_csrf(csrf)
    tid = require_teacher_contest(teacher, tid)
    reason = reason.strip()
    if not _TID.fullmatch(tid):
        raise HTTPException(400, "比赛 tid 无效")
    if teacher_approved != "yes":
        raise HTTPException(400, "故障切换需要教师明确勾选确认")
    if not reason or len(reason) > 200:
        raise HTTPException(400, "请填写 1～200 字的故障原因")
    contest = store.get_contest(tid)
    pool = store.seat_pool(tid)
    if not contest or contest.get("state") != "ready" or not pool:
        raise HTTPException(409, "比赛不在可替换故障座位状态")
    if int(pool["revision"]) != int(expected_revision):
        raise HTTPException(409, "座位池状态已变化，请刷新后台后重试")
    seat = next(
        (
            item
            for item in pool["state"].get("seats", [])
            if int(item.get("slot_no", -1)) == int(slot_no)
        ),
        None,
    )
    if not seat or seat.get("state") in {"planned", "frozen", "collected"}:
        raise HTTPException(409, "所选座位当前不能替换")
    _audit_teacher(
        teacher,
        tid,
        "contest.seat.replace",
        details={"slot_no": int(slot_no), "pool_revision": int(expected_revision)},
    )
    store.set_state(
        tid,
        "ready",
        f"教师已确认隔离故障座位 {slot_no:03d}；正在安全切换",
    )
    _spawn_pool_operation(
        pipe.replace_failed_seat,
        tid,
        slot_no=slot_no,
        reason=reason,
        expected_revision=expected_revision,
        teacher_approved=True,
    )
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/pool/repair")
def admin_pool_repair(
    tid: str = Form(...),
    slot_no: int = Form(...),
    expected_revision: int = Form(...),
    csrf: str = Form(...),
    teacher: TeacherIdentity = Depends(require_admin),
):
    require_csrf(csrf)
    tid = require_teacher_contest(teacher, tid)
    if not _TID.fullmatch(tid):
        raise HTTPException(400, "比赛 tid 无效")
    contest = store.get_contest(tid)
    pool = store.seat_pool(tid)
    if not contest or contest.get("state") != "ready" or not pool:
        raise HTTPException(409, "比赛不在可恢复备用容量状态")
    if int(pool["revision"]) != int(expected_revision):
        raise HTTPException(409, "座位池状态已变化，请刷新后台后重试")
    seat = next(
        (
            item
            for item in pool["state"].get("seats", [])
            if int(item.get("slot_no", -1)) == int(slot_no)
        ),
        None,
    )
    if not seat or seat.get("state") != "planned" or \
            seat.get("uid") is not None or int(seat.get("failure_count") or 0) < 1:
        raise HTTPException(409, "所选座位不需要容量修复")
    _audit_teacher(
        teacher,
        tid,
        "contest.seat.capacity_repair",
        details={"slot_no": int(slot_no), "pool_revision": int(expected_revision)},
    )
    store.set_state(
        tid, "ready", f"正在重建座位 {int(slot_no):03d}，恢复备用容量"
    )
    _spawn_pool_operation(
        pipe.repair_pool_capacity,
        tid,
        slot_no=int(slot_no),
        expected_revision=int(expected_revision),
    )
    return RedirectResponse("/admin/seats", status_code=303)


def _csv_cell(value: str) -> str:
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


@app.get("/admin/export/{tid}")
def admin_export(tid: str, teacher: TeacherIdentity = Depends(require_admin)):
    tid = require_teacher_contest(teacher, tid)
    _audit_teacher(teacher, tid, "contest.seats.export", outcome="completed")
    pool = store.seat_pool(tid)
    pool_by_uid = {
        int(item["uid"]): item
        for item in ((pool or {}).get("state") or {}).get("seats", [])
        if isinstance(item, dict) and isinstance(item.get("uid"), int)
    }
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["用户名", "考号", "座位号", "座位状态"])
    for seat in store.seats(tid):
        assignment = pool_by_uid.get(int(seat["uid"]), {})
        writer.writerow(
            [
                _csv_cell(seat["uname"]),
                _csv_cell(seat["candidate"]),
                assignment.get("slot_no", ""),
                assignment.get("state", "unknown"),
            ]
        )
    return PlainTextResponse(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="seats-{tid}.csv"'},
    )


def admin_boot(csrf: str = Form(...), _: TeacherIdentity = Depends(require_admin)):
    """Operator-only compatibility helper; deliberately has no HTTP route."""
    require_csrf(csrf)
    pipe.boot_server()
    return RedirectResponse("../admin", status_code=303)


def admin_shutdown(csrf: str = Form(...), _: TeacherIdentity = Depends(require_admin)):
    """Operator-only fail-closed helper; deliberately has no HTTP route."""
    require_csrf(csrf)
    pipe.shutdown_server()
    return RedirectResponse("../admin", status_code=303)


@app.get("/healthz")
def healthz():
    try:
        desktop_access = pipe.desktop_access_health()
    except Exception as exc:
        desktop_access = {
            "enabled": getattr(cvm, "desktop_access_enabled", False) is True,
            "healthy": False,
            "error": type(exc).__name__,
        }
    # /healthz is public. Counts and desired state are sufficient; cloud IDs
    # are intentionally withheld.
    desktop_access.pop("managed_rule_ids", None)
    desktop_access.pop("security_group_id", None)
    desktop_access.pop("eip", None)
    notification_metrics = store.seat_notification_health()
    notification_counts = notification_metrics["counts"]
    notification_healthy = (
        notifier is None
        or all(
            int(notification_counts.get(state, 0)) == 0
            for state in (
                "pending",
                "retry",
                "permanent_failed",
                "untracked",
                "missing_resource",
                "invalid_pool",
            )
        )
    )
    notification_payload = {
        "enabled": notifier is not None,
        "healthy": notification_healthy,
        "counts": notification_counts,
        "max_retry_attempts": notification_metrics["max_retry_attempts"],
        "oldest_retry_at": notification_metrics["oldest_retry_at"],
    }
    active_seats = store.active_seat_count()
    if realtime_judge is None:
        active_states = {
            "registered",
            "preparing",
            "ready",
            "collecting",
            "safe_wait",
        }
        active_realtime_contests = [
            {
                "tid": str(contest["tid"]),
                "state": str(contest.get("state") or ""),
                "submission_mode": str(
                    contest.get("submission_mode") or "folder"
                ),
            }
            for contest in store.contests()
            if str(contest.get("state") or "") in active_states
            and str(contest.get("submission_mode") or "folder")
            in {"web", "both"}
        ]
        payload = {
            "ok": (
                not active_realtime_contests
                and notification_healthy
                and bool(desktop_access.get("healthy"))
            ),
            "realtime_judge": "disabled",
            "active_realtime_contests": active_realtime_contests,
            "seat_notifications": notification_payload,
            "desktop_access": desktop_access,
            "active_seats": active_seats,
        }
        if not payload["ok"]:
            return JSONResponse(payload, status_code=503)
        return payload
    worker = realtime_judge.worker_health()
    queue = store.realtime_queue_health()
    alive = bool(
        REALTIME_JUDGE_THREAD is not None and REALTIME_JUDGE_THREAD.is_alive()
    )
    counts = queue["counts"]
    backlog_limit_ms = max(
        60_000,
        int(float(realtime_judge.lease_seconds) * 2 * 1000),
    )
    queue_healthy = (
        int(counts.get("retry", 0)) == 0
        and int(counts.get("permanent_failed", 0)) == 0
        and int(counts.get("ambiguous", 0)) == 0
        and int(queue["oldest_waiting_ms"]) <= backlog_limit_ms
    )
    healthy = (
        alive
        and bool(worker["running"])
        and not worker["last_error"]
        and queue_healthy
        and notification_healthy
        and bool(desktop_access.get("healthy"))
    )
    payload = {
        "ok": healthy,
        "realtime_judge": {
            "thread_alive": alive,
            "running": bool(worker["running"]),
            "last_ok_at": worker["last_ok_at"],
            "error_count": worker["error_count"],
            "last_error": str(worker["last_error"])[:200],
            "queue_counts": counts,
            "oldest_waiting_ms": queue["oldest_waiting_ms"],
            "backlog_limit_ms": backlog_limit_ms,
        },
        "seat_notifications": notification_payload,
        "desktop_access": desktop_access,
        "active_seats": active_seats,
    }
    return JSONResponse(payload, status_code=200 if healthy else 503)
