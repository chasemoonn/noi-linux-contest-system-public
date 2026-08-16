"""SQLite state store for contests, transitions, and seats."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time


_FILE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PID_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SESSION = re.compile(r"^[0-9a-f]{32}$")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MATERIAL_ATTACHMENTS = ("01_比赛题面.pdf", "02_辅助自测数据.tar.gz")


class SubmissionConflictError(ValueError):
    """A transport nonce or idempotency id was reused with different content."""


class SubmissionClosedError(SubmissionConflictError):
    """A new submission crossed the locally frozen contest window."""


class SubmissionLeaseLostError(RuntimeError):
    """A delivery worker attempted to finish a lease it no longer owns."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS contests(
    tid TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    files TEXT NOT NULL,
    pids TEXT NOT NULL DEFAULT '{}',
    submission_mode TEXT NOT NULL DEFAULT 'folder',
    submission_session TEXT NOT NULL DEFAULT '',
    paper_name TEXT NOT NULL DEFAULT '',
    paper_sha256 TEXT NOT NULL DEFAULT '',
    paper_size INTEGER NOT NULL DEFAULT 0,
    testdata_name TEXT NOT NULL DEFAULT '',
    testdata_sha256 TEXT NOT NULL DEFAULT '',
    testdata_size INTEGER NOT NULL DEFAULT 0,
    testdata_files INTEGER NOT NULL DEFAULT 0,
    testdata_expanded_size INTEGER NOT NULL DEFAULT 0,
    begin_at_ms INTEGER NOT NULL DEFAULT 0,
    end_at_ms INTEGER NOT NULL DEFAULT 0,
    hydro_rule TEXT NOT NULL DEFAULT '',
    materials_mode TEXT NOT NULL DEFAULT 'manual',
    material_state TEXT NOT NULL DEFAULT 'pending',
    active_material_revision TEXT NOT NULL DEFAULT '',
    material_manifest_sha256 TEXT NOT NULL DEFAULT '',
    max_participants INTEGER NOT NULL DEFAULT 15,
    spare_seats INTEGER NOT NULL DEFAULT 2,
    release_lead_minutes INTEGER NOT NULL DEFAULT 5,
    practice_groups INTEGER NOT NULL DEFAULT 3,
    time_sync_at_ms INTEGER NOT NULL DEFAULT 0,
    time_sync_checked_at_ms INTEGER NOT NULL DEFAULT 0,
    time_sync_error TEXT NOT NULL DEFAULT '',
    collection_run_id TEXT NOT NULL DEFAULT '',
    collection_dir TEXT NOT NULL DEFAULT '',
    collection_receipt_sha256 TEXT NOT NULL DEFAULT '',
    collection_completed_at_ms INTEGER NOT NULL DEFAULT 0,
    shutdown_after_ms INTEGER NOT NULL DEFAULT 0,
    shutdown_verified_at_ms INTEGER NOT NULL DEFAULT 0,
    workspace_purged_at_ms INTEGER NOT NULL DEFAULT 0,
    evidence_purged_at_ms INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'registered',
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS seats(
    tid TEXT NOT NULL,
    uid INTEGER NOT NULL,
    uname TEXT NOT NULL,
    token TEXT NOT NULL,
    vnc_pass TEXT NOT NULL,
    submit_token TEXT NOT NULL,
    candidate TEXT NOT NULL,
    container TEXT NOT NULL,
    cip TEXT NOT NULL,
    PRIMARY KEY(tid, uid),
    UNIQUE(token)
);
CREATE TABLE IF NOT EXISTS web_submissions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tid TEXT NOT NULL,
    uid INTEGER NOT NULL,
    problem TEXT NOT NULL,
    source TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    client_nonce TEXT NOT NULL DEFAULT '',
    submission_id TEXT NOT NULL DEFAULT '',
    submission_session TEXT NOT NULL DEFAULT '',
    judge_pid TEXT NOT NULL DEFAULT '',
    judge_lang TEXT NOT NULL DEFAULT '',
    judge_source TEXT NOT NULL DEFAULT '',
    judge_sha256 TEXT NOT NULL DEFAULT '',
    judge_issues TEXT NOT NULL DEFAULT '[]',
    judge_state TEXT NOT NULL DEFAULT 'local',
    judge_kind TEXT NOT NULL DEFAULT 'realtime',
    accepted_at_ms INTEGER NOT NULL DEFAULT 0,
    rid TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    lease_token TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    resolution_attempts INTEGER NOT NULL DEFAULT 0,
    resolution_after REAL NOT NULL DEFAULT 0,
    delivered_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS web_submissions_lookup
ON web_submissions(tid,uid,problem,id);
CREATE TABLE IF NOT EXISTS artifact_revisions(
    tid TEXT NOT NULL,
    revision TEXT NOT NULL,
    state TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    root_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL DEFAULT '',
    manifest_json TEXT NOT NULL DEFAULT '{}',
    file_io_plan_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    paper_name TEXT NOT NULL DEFAULT '',
    paper_sha256 TEXT NOT NULL DEFAULT '',
    paper_size INTEGER NOT NULL DEFAULT 0,
    testdata_name TEXT NOT NULL DEFAULT '',
    testdata_sha256 TEXT NOT NULL DEFAULT '',
    testdata_size INTEGER NOT NULL DEFAULT 0,
    testdata_files INTEGER NOT NULL DEFAULT 0,
    testdata_expanded_size INTEGER NOT NULL DEFAULT 0,
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY(tid,revision)
);
CREATE TABLE IF NOT EXISTS artifact_jobs(
    job_id TEXT PRIMARY KEY,
    tid TEXT NOT NULL,
    revision TEXT NOT NULL,
    state TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS artifact_jobs_tid
ON artifact_jobs(tid,created_at);
CREATE TABLE IF NOT EXISTS material_publications(
    tid TEXT NOT NULL,
    revision TEXT NOT NULL,
    publication_id TEXT NOT NULL UNIQUE,
    receipt_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY(tid,revision)
);
CREATE TABLE IF NOT EXISTS seat_pools(
    tid TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS seat_pool_resources(
    tid TEXT NOT NULL,
    slot_no INTEGER NOT NULL,
    token TEXT NOT NULL,
    vnc_pass TEXT NOT NULL,
    submit_token TEXT NOT NULL,
    candidate TEXT NOT NULL,
    container TEXT NOT NULL,
    cip TEXT NOT NULL,
    image_digest TEXT NOT NULL,
    material_digest TEXT NOT NULL,
    credential_revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY(tid,slot_no),
    UNIQUE(token),
    UNIQUE(submit_token),
    UNIQUE(container)
);
CREATE TABLE IF NOT EXISTS seat_notifications(
    tid TEXT NOT NULL,
    uid INTEGER NOT NULL,
    kind TEXT NOT NULL,
    credential_revision INTEGER NOT NULL,
    notification_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY(tid,uid,kind,credential_revision),
    UNIQUE(notification_id)
);
CREATE TABLE IF NOT EXISTS audit_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tid TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS audit_events_tid_time
ON audit_events(tid,created_at_ms,id);
"""


class Store:
    def __init__(self, path: str):
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(contests)")
        }
        if "pids" not in columns:
            self._conn.execute("ALTER TABLE contests ADD COLUMN pids TEXT DEFAULT '{}'")
        if "submission_mode" not in columns:
            self._conn.execute(
                "ALTER TABLE contests ADD COLUMN submission_mode TEXT DEFAULT 'folder'"
            )
        if "submission_session" not in columns:
            self._conn.execute(
                "ALTER TABLE contests ADD COLUMN submission_session TEXT DEFAULT ''"
            )
        if "paper_name" not in columns:
            self._conn.execute(
                "ALTER TABLE contests ADD COLUMN paper_name TEXT DEFAULT ''"
            )
        if "paper_sha256" not in columns:
            self._conn.execute(
                "ALTER TABLE contests ADD COLUMN paper_sha256 TEXT DEFAULT ''"
            )
        if "paper_size" not in columns:
            self._conn.execute(
                "ALTER TABLE contests ADD COLUMN paper_size INTEGER DEFAULT 0"
            )
        for name, definition in (
            ("testdata_name", "TEXT DEFAULT ''"),
            ("testdata_sha256", "TEXT DEFAULT ''"),
            ("testdata_size", "INTEGER DEFAULT 0"),
            ("testdata_files", "INTEGER DEFAULT 0"),
            ("testdata_expanded_size", "INTEGER DEFAULT 0"),
            ("begin_at_ms", "INTEGER DEFAULT 0"),
            ("end_at_ms", "INTEGER DEFAULT 0"),
            ("hydro_rule", "TEXT DEFAULT ''"),
            ("materials_mode", "TEXT NOT NULL DEFAULT 'manual'"),
            ("material_state", "TEXT NOT NULL DEFAULT 'pending'"),
            ("active_material_revision", "TEXT NOT NULL DEFAULT ''"),
            ("material_manifest_sha256", "TEXT NOT NULL DEFAULT ''"),
            ("max_participants", "INTEGER NOT NULL DEFAULT 15"),
            ("spare_seats", "INTEGER NOT NULL DEFAULT 2"),
            ("release_lead_minutes", "INTEGER NOT NULL DEFAULT 5"),
            ("practice_groups", "INTEGER NOT NULL DEFAULT 3"),
            ("time_sync_at_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("time_sync_checked_at_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("time_sync_error", "TEXT NOT NULL DEFAULT ''"),
            ("collection_run_id", "TEXT NOT NULL DEFAULT ''"),
            ("collection_dir", "TEXT NOT NULL DEFAULT ''"),
            ("collection_receipt_sha256", "TEXT NOT NULL DEFAULT ''"),
            ("collection_completed_at_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("shutdown_after_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("shutdown_verified_at_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("workspace_purged_at_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("evidence_purged_at_ms", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE contests ADD COLUMN {name} {definition}"
                )
        self._conn.execute(
            "UPDATE contests SET material_state='approved', "
            "active_material_revision=CASE WHEN active_material_revision='' "
            "THEN 'legacy-manual' ELSE active_material_revision END "
            "WHERE paper_sha256<>'' AND material_state='pending'"
        )
        for row in self._conn.execute(
            "SELECT tid,submission_session FROM contests"
        ).fetchall():
            if not row["submission_session"]:
                self._conn.execute(
                    "UPDATE contests SET submission_session=? WHERE tid=?",
                    (secrets.token_hex(16), row["tid"]),
                )
        seat_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(seats)")
        }
        if "submit_token" not in seat_columns:
            self._conn.execute(
                "ALTER TABLE seats ADD COLUMN submit_token TEXT DEFAULT ''"
            )
        if "candidate" not in seat_columns:
            self._conn.execute("ALTER TABLE seats ADD COLUMN candidate TEXT DEFAULT ''")
        for row in self._conn.execute(
            "SELECT tid,uid,submit_token,candidate,uname FROM seats"
        ).fetchall():
            submit_token = row["submit_token"] or secrets.token_urlsafe(24)
            candidate = row["candidate"] or f"U{row['uid']}"
            self._conn.execute(
                "UPDATE seats SET submit_token=?,candidate=? WHERE tid=? AND uid=?",
                (submit_token, candidate, row["tid"], row["uid"]),
            )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS seats_submit_token_unique "
            "ON seats(submit_token) WHERE submit_token <> ''"
        )
        web_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(web_submissions)")
        }
        for name, definition in (
            ("client_nonce", "TEXT NOT NULL DEFAULT ''"),
            ("submission_id", "TEXT NOT NULL DEFAULT ''"),
            ("submission_session", "TEXT NOT NULL DEFAULT ''"),
            ("judge_pid", "TEXT NOT NULL DEFAULT ''"),
            ("judge_lang", "TEXT NOT NULL DEFAULT ''"),
            ("judge_source", "TEXT NOT NULL DEFAULT ''"),
            ("judge_sha256", "TEXT NOT NULL DEFAULT ''"),
            ("judge_issues", "TEXT NOT NULL DEFAULT '[]'"),
            ("judge_state", "TEXT NOT NULL DEFAULT 'local'"),
            ("judge_kind", "TEXT NOT NULL DEFAULT 'realtime'"),
            ("accepted_at_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("rid", "TEXT NOT NULL DEFAULT ''"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("next_retry_at", "REAL NOT NULL DEFAULT 0"),
            ("lease_until", "REAL NOT NULL DEFAULT 0"),
            ("lease_token", "TEXT NOT NULL DEFAULT ''"),
            ("last_error", "TEXT NOT NULL DEFAULT ''"),
            ("resolution_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("resolution_after", "REAL NOT NULL DEFAULT 0"),
            ("delivered_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in web_columns:
                self._conn.execute(
                    f"ALTER TABLE web_submissions ADD COLUMN {name} {definition}"
                )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS web_submissions_nonce_unique "
            "ON web_submissions(tid,uid,problem,client_nonce) "
            "WHERE client_nonce <> ''"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS web_submissions_idempotency_unique "
            "ON web_submissions(submission_id) WHERE submission_id <> ''"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS web_submissions_delivery_queue "
            "ON web_submissions(judge_state,next_retry_at,id)"
        )
        job_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(artifact_jobs)")
        }
        if "details_json" not in job_columns:
            self._conn.execute(
                "ALTER TABLE artifact_jobs ADD COLUMN details_json "
                "TEXT NOT NULL DEFAULT '{}'"
            )
        # Old builds did not prevent two active rows. Preserve the newest row
        # and explicitly interrupt older duplicates before adding the durable
        # cross-process uniqueness gate.
        active = self._conn.execute(
            "SELECT rowid,tid FROM artifact_jobs "
            "WHERE state IN ('queued','running') "
            "ORDER BY tid,updated_at DESC,rowid DESC"
        ).fetchall()
        seen_active: set[str] = set()
        for row in active:
            if row["tid"] in seen_active:
                self._conn.execute(
                    "UPDATE artifact_jobs SET state='interrupted',progress=0,"
                    "error='旧版本存在重复活动任务，已在迁移时中断',"
                    "updated_at=datetime('now','localtime') WHERE rowid=?",
                    (int(row["rowid"]),),
                )
            else:
                seen_active.add(str(row["tid"]))
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS artifact_jobs_one_active "
            "ON artifact_jobs(tid) WHERE state IN ('queued','running')"
        )
        self._conn.commit()

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @contextmanager
    def _immediate_tx(self):
        """Serialize an outbox claim across processes using SQLite's write lock."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert_contest(
        self,
        tid,
        title,
        files,
        pids=None,
        submission_mode="folder",
        *,
        begin_at_ms: int = 0,
        end_at_ms: int = 0,
        hydro_rule: str = "",
        materials_mode: str = "manual",
        material_state: str = "pending",
        max_participants: int = 15,
        spare_seats: int = 2,
        release_lead_minutes: int = 5,
        practice_groups: int = 3,
    ) -> None:
        submission_session = secrets.token_hex(16)
        with self._tx() as conn:
            previous = conn.execute(
                "SELECT 1 FROM contests WHERE tid=?", (str(tid),)
            ).fetchone()
            if previous:
                has_seats = conn.execute(
                    "SELECT 1 FROM seats WHERE tid=? LIMIT 1", (str(tid),)
                ).fetchone()
                has_submissions = conn.execute(
                    "SELECT 1 FROM web_submissions WHERE tid=? LIMIT 1", (str(tid),)
                ).fetchone()
                if has_seats or has_submissions:
                    raise SubmissionConflictError(
                        "cannot re-register a contest that still has run evidence"
                    )
            conn.execute(
                "INSERT INTO contests(tid,title,files,pids,submission_mode,submission_session,"
                "begin_at_ms,end_at_ms,hydro_rule,materials_mode,material_state,"
                "max_participants,spare_seats,release_lead_minutes,practice_groups) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(tid) DO UPDATE SET title=excluded.title, "
                "files=excluded.files, pids=excluded.pids, "
                "submission_mode=excluded.submission_mode, "
                "submission_session=excluded.submission_session, "
                "begin_at_ms=excluded.begin_at_ms,end_at_ms=excluded.end_at_ms,"
                "hydro_rule=excluded.hydro_rule, "
                "materials_mode=excluded.materials_mode,"
                "material_state=excluded.material_state,"
                "active_material_revision='',material_manifest_sha256='',"
                "max_participants=excluded.max_participants,"
                "spare_seats=excluded.spare_seats,"
                "release_lead_minutes=excluded.release_lead_minutes,"
                "practice_groups=excluded.practice_groups,"
                "state='registered', message=''",
                (
                    tid,
                    title,
                    json.dumps(files, ensure_ascii=False),
                    json.dumps(pids or {}, ensure_ascii=False),
                    submission_mode,
                    submission_session,
                    int(begin_at_ms),
                    int(end_at_ms),
                    str(hydro_rule),
                    str(materials_mode),
                    str(material_state),
                    int(max_participants),
                    int(spare_seats),
                    int(release_lead_minutes),
                    int(practice_groups),
                ),
            )

    def get_contest(self, tid: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM contests WHERE tid=?", (tid,)
            ).fetchone()
        return dict(row) if row else None

    def mark_time_sync(
        self,
        tid: str,
        *,
        observed_at_ms: int,
        error: str = "",
    ) -> None:
        """Record time-source health without changing the confirmed schedule."""
        with self._tx() as conn:
            if error:
                cur = conn.execute(
                    "UPDATE contests SET time_sync_checked_at_ms=?,"
                    "time_sync_error=? WHERE tid=?",
                    (int(observed_at_ms), str(error)[:1000], str(tid)),
                )
            else:
                cur = conn.execute(
                    "UPDATE contests SET time_sync_at_ms=?,"
                    "time_sync_checked_at_ms=?,time_sync_error='' WHERE tid=?",
                    (int(observed_at_ms), int(observed_at_ms), str(tid)),
                )
        if cur.rowcount != 1:
            raise KeyError(f"比赛不存在: {tid}")

    def commit_schedule_sync(
        self,
        tid: str,
        *,
        expected_begin_at_ms: int,
        expected_end_at_ms: int,
        begin_at_ms: int,
        end_at_ms: int,
        hydro_rule: str,
        observed_at_ms: int,
        expected_pool_revision: int,
        pool_state: dict,
    ) -> dict:
        """Atomically publish an OJ schedule and matching seat release boundary."""
        payload = json.dumps(pool_state, ensure_ascii=False, sort_keys=True)
        new_revision = int(pool_state.get("revision", -1))
        with self._immediate_tx() as conn:
            contest = conn.execute(
                "SELECT state,begin_at_ms,end_at_ms FROM contests WHERE tid=?",
                (str(tid),),
            ).fetchone()
            if not contest or str(contest["state"]) != "ready":
                raise SubmissionConflictError("比赛已不在可同步时间的状态")
            if (
                int(contest["begin_at_ms"] or 0) != int(expected_begin_at_ms)
                or int(contest["end_at_ms"] or 0) != int(expected_end_at_ms)
            ):
                raise SubmissionConflictError("比赛时间已被其他流程更新")
            pool = conn.execute(
                "SELECT revision FROM seat_pools WHERE tid=?", (str(tid),)
            ).fetchone()
            if not pool or int(pool["revision"]) != int(expected_pool_revision):
                raise SubmissionConflictError("座位池已被其他流程更新")
            if new_revision < int(expected_pool_revision):
                raise SubmissionConflictError("座位池时间 revision 无效")
            conn.execute(
                "UPDATE seat_pools SET revision=?,state_json=?,"
                "updated_at=datetime('now','localtime') WHERE tid=?",
                (new_revision, payload, str(tid)),
            )
            conn.execute(
                "UPDATE contests SET begin_at_ms=?,end_at_ms=?,hydro_rule=?,"
                "time_sync_at_ms=?,time_sync_checked_at_ms=?,"
                "time_sync_error='',message=? WHERE tid=?",
                (
                    int(begin_at_ms),
                    int(end_at_ms),
                    str(hydro_rule),
                    int(observed_at_ms),
                    int(observed_at_ms),
                    "已同步 OJ 比赛时间",
                    str(tid),
                ),
            )
            updated = conn.execute(
                "SELECT * FROM contests WHERE tid=?", (str(tid),)
            ).fetchone()
        return dict(updated)

    def replace_contest_pid_map(
        self,
        tid: str,
        *,
        expected_submission_session: str,
        pid_map: dict[str, str],
    ) -> dict:
        """Atomically bind verified private clones to an untouched contest run.

        The submission session is an optimistic lock over re-registration. The
        update is refused after any seat or submission evidence exists and can
        never add, remove, or rename a registered student filename.
        """
        session = str(expected_submission_session)
        if not _SESSION.fullmatch(session):
            raise ValueError("expected_submission_session is invalid")
        if not isinstance(pid_map, dict) or not pid_map:
            raise ValueError("pid_map must be a non-empty object")
        normalized: dict[str, str] = {}
        for key, value in pid_map.items():
            if (
                not isinstance(key, str)
                or not _FILE_KEY.fullmatch(key)
                or not isinstance(value, str)
                or not _PID_VALUE.fullmatch(value)
            ):
                raise ValueError("pid_map contains an invalid filename or pid")
            normalized[key] = value
        if len(set(normalized.values())) != len(normalized):
            raise ValueError("pid_map values must be unique")
        with self._immediate_tx() as conn:
            contest = conn.execute(
                "SELECT * FROM contests WHERE tid=?", (str(tid),)
            ).fetchone()
            if not contest:
                raise KeyError(f"比赛不存在: {tid}")
            if contest["state"] not in {"registered", "error"}:
                raise SubmissionConflictError("比赛已进入备赛或运行阶段，不能替换题目映射")
            if str(contest["submission_session"]) != session:
                raise SubmissionConflictError("比赛已被重新登记，题目克隆结果已过期")
            try:
                files = json.loads(contest["files"] or "[]")
            except json.JSONDecodeError as exc:
                raise SubmissionConflictError("比赛文件列表损坏") from exc
            if (
                not isinstance(files, list)
                or any(
                    not isinstance(item, str) or not _FILE_KEY.fullmatch(item)
                    for item in files
                )
                or len(set(files)) != len(files)
                or set(files) != set(normalized)
            ):
                raise SubmissionConflictError("克隆映射必须与登记文件名完全一致")
            if conn.execute(
                "SELECT 1 FROM seats WHERE tid=? LIMIT 1", (str(tid),)
            ).fetchone() or conn.execute(
                "SELECT 1 FROM web_submissions WHERE tid=? LIMIT 1", (str(tid),)
            ).fetchone():
                raise SubmissionConflictError("本场已有座位或递交证据，不能替换题目映射")
            conn.execute(
                "UPDATE contests SET pids=? WHERE tid=?",
                (
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                    str(tid),
                ),
            )
            updated = conn.execute(
                "SELECT * FROM contests WHERE tid=?", (str(tid),)
            ).fetchone()
        return dict(updated)

    def set_paper(self, tid: str, name: str, sha256: str, size: int) -> None:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE contests SET paper_name=?,paper_sha256=?,paper_size=?,"
                "material_state=CASE WHEN materials_mode='manual' THEN 'approved' "
                "ELSE material_state END,active_material_revision=CASE "
                "WHEN materials_mode='manual' AND active_material_revision='' "
                "THEN 'legacy-manual' ELSE active_material_revision END "
                "WHERE tid=?",
                (name, sha256, int(size), tid),
            )
        if cur.rowcount != 1:
            raise KeyError(f"比赛不存在: {tid}")

    def clear_material_selection(self, tid: str) -> None:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE contests SET paper_name='',paper_sha256='',paper_size=0,"
                "testdata_name='',testdata_sha256='',testdata_size=0,"
                "testdata_files=0,testdata_expanded_size=0,material_state='pending',"
                "active_material_revision='',material_manifest_sha256='' WHERE tid=?",
                (str(tid),),
            )
        if cur.rowcount != 1:
            raise KeyError(f"比赛不存在: {tid}")

    def set_testdata(
        self,
        tid: str,
        name: str,
        sha256: str,
        size: int,
        files: int,
        expanded_size: int,
    ) -> None:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE contests SET testdata_name=?,testdata_sha256=?,"
                "testdata_size=?,testdata_files=?,testdata_expanded_size=? WHERE tid=?",
                (name, sha256, int(size), int(files), int(expanded_size), tid),
            )
        if cur.rowcount != 1:
            raise KeyError(f"比赛不存在: {tid}")

    def put_artifact_revision(
        self,
        tid: str,
        revision: str,
        *,
        state: str,
        source_sha256: str,
        root_path: str,
        manifest_sha256: str = "",
        manifest: dict | None = None,
        file_io_plan: list | None = None,
        warnings: list | None = None,
        paper_name: str = "",
        paper_sha256: str = "",
        paper_size: int = 0,
        testdata_name: str = "",
        testdata_sha256: str = "",
        testdata_size: int = 0,
        testdata_files: int = 0,
        testdata_expanded_size: int = 0,
        complete_job_id: str = "",
        complete_job_details: dict | None = None,
    ) -> None:
        """Persist a material review, optionally completing its job atomically."""
        manifest_json = json.dumps(
            manifest or {}, ensure_ascii=False, sort_keys=True
        )
        file_io_plan_json = json.dumps(
            file_io_plan or [], ensure_ascii=False, sort_keys=True
        )
        warnings_json = json.dumps(warnings or [], ensure_ascii=False)
        immutable = {
            "source_sha256": str(source_sha256),
            "root_path": str(root_path),
            "manifest_sha256": str(manifest_sha256),
            "manifest_json": manifest_json,
            "file_io_plan_json": file_io_plan_json,
            "warnings_json": warnings_json,
            "paper_name": str(paper_name),
            "paper_sha256": str(paper_sha256),
            "paper_size": int(paper_size),
            "testdata_name": str(testdata_name),
            "testdata_sha256": str(testdata_sha256),
            "testdata_size": int(testdata_size),
            "testdata_files": int(testdata_files),
            "testdata_expanded_size": int(testdata_expanded_size),
        }
        job_details_payload = (
            self._encode_artifact_job_details(complete_job_details)
            if complete_job_id
            else ""
        )
        transaction = self._immediate_tx if complete_job_id else self._tx
        with transaction() as conn:
            contest = conn.execute(
                "SELECT 1 FROM contests WHERE tid=?", (str(tid),)
            ).fetchone()
            if not contest:
                raise KeyError(f"比赛不存在: {tid}")
            if complete_job_id:
                job = conn.execute(
                    "SELECT state,revision FROM artifact_jobs WHERE job_id=? AND tid=?",
                    (str(complete_job_id), str(tid)),
                ).fetchone()
                if (
                    not job
                    or job["state"] not in {"queued", "running"}
                    or job["revision"] != str(revision)
                ):
                    raise SubmissionConflictError("材料任务已结束或版本不匹配")
                if conn.execute(
                    "SELECT 1 FROM artifact_revisions WHERE tid=? AND revision=?",
                    (str(tid), str(revision)),
                ).fetchone():
                    raise SubmissionConflictError("材料 review 版本已存在且不可覆盖")
            existing = conn.execute(
                "SELECT * FROM artifact_revisions WHERE tid=? AND revision=?",
                (str(tid), str(revision)),
            ).fetchone()
            if existing:
                changed = [
                    column
                    for column, expected in immutable.items()
                    if existing[column] != expected
                ]
                if changed:
                    raise SubmissionConflictError(
                        "材料版本不可覆盖，差异字段: " + ", ".join(changed)
                    )
                # An identical retry is a no-op. In particular, never demote
                # an approved/superseded row back to review or draft.
                return
            conn.execute(
                "INSERT INTO artifact_revisions("
                "tid,revision,state,source_sha256,root_path,manifest_sha256,"
                "manifest_json,file_io_plan_json,warnings_json,paper_name,"
                "paper_sha256,paper_size,testdata_name,testdata_sha256,"
                "testdata_size,testdata_files,testdata_expanded_size) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(tid),
                    str(revision),
                    str(state),
                    str(source_sha256),
                    str(root_path),
                    str(manifest_sha256),
                    manifest_json,
                    file_io_plan_json,
                    warnings_json,
                    str(paper_name),
                    str(paper_sha256),
                    int(paper_size),
                    str(testdata_name),
                    str(testdata_sha256),
                    int(testdata_size),
                    int(testdata_files),
                    int(testdata_expanded_size),
                ),
            )
            conn.execute(
                "UPDATE contests SET material_state=? WHERE tid=?",
                (str(state), str(tid)),
            )
            if complete_job_id:
                conn.execute(
                    "UPDATE artifact_jobs SET state='done',progress=100,"
                    "message='材料已生成，等待教师审核 PDF 与机器报告',error='',"
                    "details_json=?,updated_at=datetime('now','localtime') "
                    "WHERE job_id=?",
                    (job_details_payload, str(complete_job_id)),
                )

    @staticmethod
    def _decode_artifact_row(row: sqlite3.Row | None) -> dict | None:
        if not row:
            return None
        result = dict(row)
        for column in ("manifest_json", "file_io_plan_json", "warnings_json"):
            result[column.removesuffix("_json")] = json.loads(result[column] or "{}")
        return result

    def artifact_revision(self, tid: str, revision: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_revisions WHERE tid=? AND revision=?",
                (str(tid), str(revision)),
            ).fetchone()
        return self._decode_artifact_row(row)

    def artifact_revisions(self, tid: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM artifact_revisions WHERE tid=? "
                "ORDER BY created_at DESC,revision DESC",
                (str(tid),),
            ).fetchall()
        return [self._decode_artifact_row(row) for row in rows]

    def artifact_approval_candidate(self, tid: str, revision: str) -> dict:
        """Read-only preflight for a generated-material approval attempt."""
        with self._tx() as conn:
            contest = conn.execute(
                "SELECT state,material_state,active_material_revision "
                "FROM contests WHERE tid=?",
                (str(tid),),
            ).fetchone()
            if not contest:
                raise KeyError(f"比赛不存在: {tid}")
            if contest["state"] not in {"registered", "error"}:
                raise SubmissionConflictError("座位预热开始后不能更换材料版本")
            row = conn.execute(
                "SELECT * FROM artifact_revisions WHERE tid=? AND revision=?",
                (str(tid), str(revision)),
            ).fetchone()
            if not row or row["state"] not in {"review", "approved"}:
                raise SubmissionConflictError("材料版本尚未完成机器校验")
            if row["state"] == "approved" and (
                contest["material_state"] != "approved"
                or contest["active_material_revision"] != str(revision)
            ):
                raise SubmissionConflictError("已批准材料版本与比赛活动版本不一致")
            if not row["manifest_sha256"] or not row["paper_sha256"]:
                raise SubmissionConflictError("材料版本缺少 PDF 或完整清单")
        result = self._decode_artifact_row(row)
        assert result is not None
        return result

    @staticmethod
    def _canonical_json_sha256(value: object) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def _validate_material_publication(
        cls,
        publication: dict,
        *,
        tid: str,
        revision: str,
        artifact: sqlite3.Row,
    ) -> tuple[str, str, str]:
        if not isinstance(publication, dict) or set(publication) != {
            "ok",
            "publication_id",
            "tid",
            "revision",
            "attachments",
            "receipt_sha256",
        }:
            raise SubmissionConflictError("OJ 材料发布回执字段不完整")
        publication_id = str(publication.get("publication_id") or "")
        receipt_sha256 = str(publication.get("receipt_sha256") or "")
        if publication.get("ok") is not True:
            raise SubmissionConflictError("OJ 尚未确认材料发布成功")
        if not _SHA256.fullmatch(publication_id) or not _SHA256.fullmatch(
            receipt_sha256
        ):
            raise SubmissionConflictError("OJ 材料发布回执摘要无效")
        if str(publication.get("tid") or "").lower() != str(tid).lower():
            raise SubmissionConflictError("OJ 材料发布回执比赛不匹配")
        if str(publication.get("revision") or "") != str(revision):
            raise SubmissionConflictError("OJ 材料发布回执版本不匹配")
        attachments = publication.get("attachments")
        expected = [
            {
                "name": _MATERIAL_ATTACHMENTS[0],
                "sha256": str(artifact["paper_sha256"]),
                "size": int(artifact["paper_size"]),
            },
            {
                "name": _MATERIAL_ATTACHMENTS[1],
                "sha256": str(artifact["testdata_sha256"]),
                "size": int(artifact["testdata_size"]),
            },
        ]
        if attachments != expected:
            raise SubmissionConflictError("OJ 与桌面材料的字节摘要不一致")
        receipt = {
            "publication_id": publication_id,
            "tid": str(tid).lower(),
            "revision": str(revision),
            "attachments": expected,
        }
        if cls._canonical_json_sha256(receipt) != receipt_sha256:
            raise SubmissionConflictError("OJ 材料发布回执校验失败")
        receipt_json = json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return publication_id, receipt_sha256, receipt_json

    @staticmethod
    def _decode_material_publication(row: sqlite3.Row | None) -> dict | None:
        if not row:
            return None
        try:
            receipt = json.loads(row["receipt_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("OJ 材料发布回执已损坏") from exc
        if not isinstance(receipt, dict):
            raise RuntimeError("OJ 材料发布回执已损坏")
        if (
            receipt.get("publication_id") != row["publication_id"]
            or Store._canonical_json_sha256(receipt) != row["receipt_sha256"]
        ):
            raise RuntimeError("OJ 材料发布回执摘要不一致")
        return {
            **receipt,
            "ok": True,
            "receipt_sha256": row["receipt_sha256"],
            "published_at": row["published_at"],
        }

    def material_publication(self, tid: str, revision: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM material_publications WHERE tid=? AND revision=?",
                (str(tid), str(revision)),
            ).fetchone()
        return self._decode_material_publication(row)

    def _approve_artifact(
        self,
        tid: str,
        revision: str,
        approved_by: str,
        *,
        publication: dict | None,
    ) -> dict:
        """Atomically freeze the OJ receipt and promote one reviewed revision."""
        with self._immediate_tx() as conn:
            contest = conn.execute(
                "SELECT state,material_state,active_material_revision "
                "FROM contests WHERE tid=?",
                (str(tid),),
            ).fetchone()
            if not contest:
                raise KeyError(f"比赛不存在: {tid}")
            if contest["state"] not in {"registered", "error"}:
                raise SubmissionConflictError("座位预热开始后不能更换材料版本")
            row = conn.execute(
                "SELECT * FROM artifact_revisions WHERE tid=? AND revision=?",
                (str(tid), str(revision)),
            ).fetchone()
            if not row or row["state"] not in {"review", "approved"}:
                raise SubmissionConflictError("材料版本尚未完成机器校验")
            if row["state"] == "approved" and (
                contest["material_state"] != "approved"
                or contest["active_material_revision"] != str(revision)
            ):
                raise SubmissionConflictError("已批准材料版本与比赛活动版本不一致")
            if (
                not row["manifest_sha256"]
                or not row["paper_sha256"]
                or not row["testdata_sha256"]
                or int(row["paper_size"]) <= 0
                or int(row["testdata_size"]) <= 0
            ):
                raise SubmissionConflictError("材料版本缺少 PDF、自测数据或完整清单")
            if publication is not None:
                publication_id, receipt_sha256, receipt_json = (
                    self._validate_material_publication(
                        publication,
                        tid=str(tid),
                        revision=str(revision),
                        artifact=row,
                    )
                )
                existing = conn.execute(
                    "SELECT * FROM material_publications WHERE tid=? AND revision=?",
                    (str(tid), str(revision)),
                ).fetchone()
                if existing:
                    if (
                        existing["publication_id"] != publication_id
                        or existing["receipt_sha256"] != receipt_sha256
                        or existing["receipt_json"] != receipt_json
                    ):
                        raise SubmissionConflictError("OJ 材料发布回执发生冲突")
                else:
                    try:
                        conn.execute(
                            "INSERT INTO material_publications("
                            "tid,revision,publication_id,receipt_sha256,receipt_json) "
                            "VALUES(?,?,?,?,?)",
                            (
                                str(tid),
                                str(revision),
                                publication_id,
                                receipt_sha256,
                                receipt_json,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise SubmissionConflictError(
                            "OJ 材料发布编号已被其他版本占用"
                        ) from exc
            elif not conn.execute(
                "SELECT 1 FROM material_publications WHERE tid=? AND revision=?",
                (str(tid), str(revision)),
            ).fetchone():
                raise SubmissionConflictError("尚未保存 OJ 材料发布回执")
            if row["state"] == "approved":
                approved = row
                # Idempotent approval must not rewrite approved_at or teacher.
                result = self._decode_artifact_row(approved)
                assert result is not None
                return result
            conn.execute(
                "UPDATE artifact_revisions SET state='superseded',"
                "updated_at=datetime('now','localtime') "
                "WHERE tid=? AND revision<>? AND state='approved'",
                (str(tid), str(revision)),
            )
            conn.execute(
                "UPDATE artifact_revisions SET state='approved',approved_by=?,"
                "approved_at=datetime('now','localtime'),"
                "updated_at=datetime('now','localtime') WHERE tid=? AND revision=?",
                (str(approved_by), str(tid), str(revision)),
            )
            conn.execute(
                "UPDATE contests SET material_state='approved',"
                "active_material_revision=?,material_manifest_sha256=?,"
                "paper_name=?,paper_sha256=?,paper_size=?,testdata_name=?,"
                "testdata_sha256=?,testdata_size=?,testdata_files=?,"
                "testdata_expanded_size=? WHERE tid=?",
                (
                    str(revision),
                    row["manifest_sha256"],
                    row["paper_name"],
                    row["paper_sha256"],
                    int(row["paper_size"]),
                    row["testdata_name"],
                    row["testdata_sha256"],
                    int(row["testdata_size"]),
                    int(row["testdata_files"]),
                    int(row["testdata_expanded_size"]),
                    str(tid),
                ),
            )
            approved = conn.execute(
                "SELECT * FROM artifact_revisions WHERE tid=? AND revision=?",
                (str(tid), str(revision)),
            ).fetchone()
        result = self._decode_artifact_row(approved)
        assert result is not None
        return result

    def approve_artifact_with_publication(
        self,
        tid: str,
        revision: str,
        approved_by: str,
        publication: dict,
    ) -> dict:
        return self._approve_artifact(
            tid,
            revision,
            approved_by,
            publication=publication,
        )

    def approve_artifact(self, tid: str, revision: str, approved_by: str) -> dict:
        """Promote a revision only when an OJ publication receipt already exists."""
        return self._approve_artifact(
            tid,
            revision,
            approved_by,
            publication=None,
        )

    @staticmethod
    def _encode_artifact_job_details(details: dict | None) -> str:
        if details is None:
            details = {}
        if not isinstance(details, dict):
            raise ValueError("artifact job details must be an object")
        try:
            payload = json.dumps(
                details,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("artifact job details are not JSON serializable") from exc
        if len(payload.encode("utf-8")) > 32 * 1024 * 1024:
            raise ValueError("artifact job details exceed 32 MiB")
        return payload

    @staticmethod
    def _decode_artifact_job(row: sqlite3.Row | None) -> dict | None:
        if not row:
            return None
        result = dict(row)
        try:
            details = json.loads(result.get("details_json") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("artifact job details are corrupted") from exc
        if not isinstance(details, dict):
            raise RuntimeError("artifact job details must be an object")
        result["details"] = details
        return result

    def start_artifact_job(
        self,
        job_id: str,
        tid: str,
        revision: str,
        *,
        details: dict,
        message: str = "已排队",
    ) -> dict:
        """Create the only queued/running job for one contest."""
        if not _JOB_ID.fullmatch(str(job_id)):
            raise ValueError("artifact job_id must be 32 lowercase hexadecimal characters")
        if not _REVISION.fullmatch(str(revision)):
            raise ValueError("artifact revision is invalid")
        payload = self._encode_artifact_job_details(details)
        with self._immediate_tx() as conn:
            contest = conn.execute(
                "SELECT state,materials_mode,active_material_revision "
                "FROM contests WHERE tid=?",
                (str(tid),),
            ).fetchone()
            if not contest:
                raise KeyError(f"比赛不存在: {tid}")
            if contest["materials_mode"] != "ai":
                raise SubmissionConflictError("本场不是 AI 材料模式")
            if contest["state"] not in {"registered", "error"}:
                raise SubmissionConflictError("比赛已进入备赛或运行阶段")
            if conn.execute(
                "SELECT 1 FROM artifact_jobs WHERE tid=? "
                "AND state IN ('queued','running') LIMIT 1",
                (str(tid),),
            ).fetchone():
                raise SubmissionConflictError("本场已有材料生成任务正在执行")
            if conn.execute(
                "SELECT 1 FROM seats WHERE tid=? LIMIT 1", (str(tid),)
            ).fetchone() or conn.execute(
                "SELECT 1 FROM web_submissions WHERE tid=? LIMIT 1", (str(tid),)
            ).fetchone():
                raise SubmissionConflictError("本场已有座位或递交证据，不能生成新材料")
            try:
                conn.execute(
                    "INSERT INTO artifact_jobs("
                    "job_id,tid,revision,state,progress,message,error,details_json"
                    ") VALUES(?,?,?,'queued',0,?,'',?)",
                    (
                        str(job_id),
                        str(tid),
                        str(revision),
                        str(message)[:2000],
                        payload,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SubmissionConflictError(
                    "本场已有材料生成任务正在执行"
                ) from exc
            if not contest["active_material_revision"]:
                conn.execute(
                    "UPDATE contests SET material_state='generating' WHERE tid=?",
                    (str(tid),),
                )
            row = conn.execute(
                "SELECT * FROM artifact_jobs WHERE job_id=?", (str(job_id),)
            ).fetchone()
        result = self._decode_artifact_job(row)
        assert result is not None
        return result

    def update_artifact_job(
        self,
        job_id: str,
        state: str,
        *,
        progress: int = 0,
        message: str = "",
        error: str = "",
        details: dict | None = None,
    ) -> dict:
        allowed = {"queued", "running", "done", "error", "interrupted"}
        if state not in allowed:
            raise ValueError("artifact job state is invalid")
        with self._immediate_tx() as conn:
            current = conn.execute(
                "SELECT * FROM artifact_jobs WHERE job_id=?", (str(job_id),)
            ).fetchone()
            if not current:
                raise KeyError(f"材料任务不存在: {job_id}")
            if current["state"] in {"done", "error", "interrupted"} and state != current["state"]:
                raise SubmissionConflictError("已结束的材料任务不能重新激活")
            bounded_progress = max(0, min(100, int(progress)))
            if bounded_progress < int(current["progress"]):
                raise SubmissionConflictError("材料任务进度不能倒退")
            payload = (
                self._encode_artifact_job_details(details)
                if details is not None
                else current["details_json"]
            )
            conn.execute(
                "UPDATE artifact_jobs SET state=?,progress=?,message=?,error=?,"
                "details_json=?,updated_at=datetime('now','localtime') "
                "WHERE job_id=?",
                (
                    state,
                    bounded_progress,
                    str(message)[:2000],
                    str(error)[:8000],
                    payload,
                    str(job_id),
                ),
            )
            if state in {"error", "interrupted"}:
                conn.execute(
                    "UPDATE contests SET material_state='pending' WHERE tid=? "
                    "AND active_material_revision=''",
                    (current["tid"],),
                )
            row = conn.execute(
                "SELECT * FROM artifact_jobs WHERE job_id=?", (str(job_id),)
            ).fetchone()
        result = self._decode_artifact_job(row)
        assert result is not None
        return result

    def put_artifact_job(
        self,
        job_id: str,
        tid: str,
        revision: str,
        state: str,
        *,
        progress: int = 0,
        message: str = "",
        error: str = "",
        details: dict | None = None,
    ) -> None:
        """Compatibility wrapper; new callers should use start/update methods."""
        if self.artifact_job(job_id) is None:
            self.start_artifact_job(
                job_id,
                tid,
                revision,
                details=details or {},
                message=message or "已排队",
            )
        self.update_artifact_job(
            job_id,
            state,
            progress=progress,
            message=message,
            error=error,
            details=details,
        )

    def recover_interrupted_artifact_jobs(self) -> int:
        """Release active-job locks left by a previous service process."""
        with self._immediate_tx() as conn:
            rows = conn.execute(
                "SELECT job_id,tid FROM artifact_jobs "
                "WHERE state IN ('queued','running')"
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE artifact_jobs SET state='interrupted',"
                    "error='编排服务重启，原任务已安全中断；再次点击可从已保存预检继续',"
                    "updated_at=datetime('now','localtime') "
                    "WHERE state IN ('queued','running')"
                )
                for tid in {str(row["tid"]) for row in rows}:
                    conn.execute(
                        "UPDATE contests SET material_state='pending' WHERE tid=? "
                        "AND active_material_revision=''",
                        (tid,),
                    )
        return len(rows)

    def recover_interrupted_contests(self, *, observed_at_ms: int | None = None) -> int:
        """Fail closed prepare work that cannot be resumed after process loss.

        Collection and safe-wait are intentionally left intact: the scheduler
        has explicit idempotent resume paths for both states. A partially
        prepared contest has no equivalent completion proof, so it must return
        to a teacher-visible retry state and the startup frontend reconciler
        keeps it closed.
        """
        observed = (
            int(time.time() * 1000)
            if observed_at_ms is None
            else int(observed_at_ms)
        )
        if observed <= 0:
            raise ValueError("recovery timestamp is invalid")
        with self._immediate_tx() as conn:
            rows = conn.execute(
                "SELECT tid FROM contests WHERE state='preparing'"
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE contests SET state='error',"
                    "message='编排服务在备赛中重启；入口保持关闭，请重新运行教师测试和备赛' "
                    "WHERE state='preparing'"
                )
                for row in rows:
                    conn.execute(
                        "INSERT INTO audit_events(tid,actor,action,outcome,details_json,created_at_ms) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            str(row["tid"]),
                            "system",
                            "contest.recovery.prepare_interrupted",
                            "failed",
                            json.dumps({"state": "error"}, sort_keys=True),
                            observed,
                        ),
                    )
        return len(rows)

    def artifact_job(self, job_id: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_jobs WHERE job_id=?", (str(job_id),)
            ).fetchone()
        return self._decode_artifact_job(row)

    def latest_artifact_job(self, tid: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_jobs WHERE tid=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (str(tid),),
            ).fetchone()
        return self._decode_artifact_job(row)

    def put_seat_pool(self, tid: str, expected_revision: int | None, state: dict) -> int:
        """Compare-and-swap one serialized seat-pool state."""
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        new_revision = int(state.get("revision", 0))
        if new_revision < 0:
            raise ValueError("seat pool revision must be non-negative")
        with self._immediate_tx() as conn:
            current = conn.execute(
                "SELECT revision FROM seat_pools WHERE tid=?", (str(tid),)
            ).fetchone()
            if current is None:
                if expected_revision not in {None, -1}:
                    raise SubmissionConflictError("座位池已被其他操作更新")
                conn.execute(
                    "INSERT INTO seat_pools(tid,revision,state_json) VALUES(?,?,?)",
                    (str(tid), new_revision, payload),
                )
            else:
                if expected_revision is None or int(current["revision"]) != int(
                    expected_revision
                ):
                    raise SubmissionConflictError("座位池已被其他操作更新")
                conn.execute(
                    "UPDATE seat_pools SET revision=?,state_json=?,"
                    "updated_at=datetime('now','localtime') WHERE tid=?",
                    (new_revision, payload, str(tid)),
                )
        return new_revision

    def seat_pool(self, tid: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT revision,state_json,updated_at FROM seat_pools WHERE tid=?",
                (str(tid),),
            ).fetchone()
        if not row:
            return None
        return {
            "revision": int(row["revision"]),
            "state": json.loads(row["state_json"]),
            "updated_at": row["updated_at"],
        }

    def delete_seat_pool(self, tid: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM seat_pools WHERE tid=?", (str(tid),))
            conn.execute("DELETE FROM seat_pool_resources WHERE tid=?", (str(tid),))

    def put_seat_pool_resource(
        self,
        tid: str,
        slot_no: int,
        *,
        token: str,
        vnc_pass: str,
        submit_token: str,
        candidate: str,
        container: str,
        cip: str,
        image_digest: str,
        material_digest: str,
        credential_revision: int = 1,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO seat_pool_resources("
                "tid,slot_no,token,vnc_pass,submit_token,candidate,container,cip,"
                "image_digest,material_digest,credential_revision) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tid,slot_no) DO UPDATE SET "
                "token=excluded.token,vnc_pass=excluded.vnc_pass,"
                "submit_token=excluded.submit_token,candidate=excluded.candidate,"
                "container=excluded.container,cip=excluded.cip,"
                "image_digest=excluded.image_digest,"
                "material_digest=excluded.material_digest,"
                "credential_revision=excluded.credential_revision",
                (
                    str(tid),
                    int(slot_no),
                    str(token),
                    str(vnc_pass),
                    str(submit_token),
                    str(candidate),
                    str(container),
                    str(cip),
                    str(image_digest),
                    str(material_digest),
                    int(credential_revision),
                ),
            )

    @staticmethod
    def _validate_pool_resource(state: dict, resource: dict) -> dict:
        """Validate a runtime resource against one verified pool seat."""
        slot_no = int(resource["slot_no"])
        seat = next(
            (
                item
                for item in state.get("seats", [])
                if int(item.get("slot_no", -1)) == slot_no
            ),
            None,
        )
        if not seat or seat.get("state") != "verified":
            raise SubmissionConflictError("新增座位尚未完成验收")
        if (
            str(seat.get("container_ref") or "") != str(resource["container"])
            or str(seat.get("image_digest") or "")
            != str(resource["image_digest"])
            or str(seat.get("material_digest") or "")
            != str(resource["material_digest"])
        ):
            raise SubmissionConflictError("新增座位验收证据与连接资源不一致")
        return seat

    def commit_pool_expansion(
        self,
        tid: str,
        expected_revision: int,
        state: dict,
        resources: list[dict],
    ) -> int:
        """Atomically append verified runtime resources and CAS the pool.

        Existing resources are deliberately INSERT-only here: expansion must
        never rotate a student's credentials or replace an existing slot.
        """
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        new_revision = int(state.get("revision", 0))
        if str(state.get("tid") or "") != str(tid):
            raise ValueError("座位池比赛标识不一致")
        if not resources:
            raise ValueError("座位池扩容至少需要一个新增资源")
        validated = []
        seen_slots: set[int] = set()
        for resource in resources:
            seat = self._validate_pool_resource(state, resource)
            slot_no = int(resource["slot_no"])
            if slot_no in seen_slots:
                raise ValueError("新增座位编号重复")
            seen_slots.add(slot_no)
            validated.append((seat, resource))
        with self._immediate_tx() as conn:
            current = conn.execute(
                "SELECT revision,state_json FROM seat_pools WHERE tid=?",
                (str(tid),),
            ).fetchone()
            if not current or int(current["revision"]) != int(expected_revision):
                raise SubmissionConflictError("座位池已被其他操作更新")
            previous_state = json.loads(current["state_json"])
            previous_by_slot = {
                int(item.get("slot_no", -1)): item
                for item in previous_state.get("seats", [])
            }
            new_by_slot = {
                int(item.get("slot_no", -1)): item
                for item in state.get("seats", [])
            }
            previous_slots = set(previous_by_slot)
            if any(int(resource["slot_no"]) in previous_slots for _, resource in validated):
                raise SubmissionConflictError("扩容不得覆盖原有座位")
            if any(
                new_by_slot.get(slot) != seat
                for slot, seat in previous_by_slot.items()
            ):
                raise SubmissionConflictError("扩容不得修改原有座位或学生映射")
            if set(new_by_slot) - previous_slots != seen_slots:
                raise SubmissionConflictError("新增座位必须全部完成验收后一次发布")
            if new_revision <= int(expected_revision):
                raise SubmissionConflictError("扩容后的座位池 revision 无效")
            for _, resource in validated:
                conn.execute(
                    "INSERT INTO seat_pool_resources("
                    "tid,slot_no,token,vnc_pass,submit_token,candidate,container,cip,"
                    "image_digest,material_digest,credential_revision) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(tid),
                        int(resource["slot_no"]),
                        str(resource["token"]),
                        str(resource["vnc_pass"]),
                        str(resource["submit_token"]),
                        str(resource["candidate"]),
                        str(resource["container"]),
                        str(resource["cip"]),
                        str(resource["image_digest"]),
                        str(resource["material_digest"]),
                        int(resource.get("credential_revision", 1)),
                    ),
                )
            conn.execute(
                "UPDATE seat_pools SET revision=?,state_json=?,"
                "updated_at=datetime('now','localtime') WHERE tid=?",
                (new_revision, payload, str(tid)),
            )
            conn.execute(
                "UPDATE contests SET max_participants=?,spare_seats=? WHERE tid=?",
                (
                    int(state.get("max_participants", 0)),
                    int(state.get("spare_count", 0)),
                    str(tid),
                ),
            )
        return new_revision

    def commit_pool_replacement(
        self,
        tid: str,
        expected_revision: int,
        state: dict,
        *,
        failed_slot: int,
    ) -> dict | None:
        """Atomically invalidate a failed slot and rebind its user to a spare."""
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        new_revision = int(state.get("revision", 0))
        if str(state.get("tid") or "") != str(tid):
            raise ValueError("座位池比赛标识不一致")
        with self._immediate_tx() as conn:
            current = conn.execute(
                "SELECT revision,state_json FROM seat_pools WHERE tid=?",
                (str(tid),),
            ).fetchone()
            if not current or int(current["revision"]) != int(expected_revision):
                raise SubmissionConflictError("座位池已被其他操作更新")
            previous = json.loads(current["state_json"])
            if new_revision <= int(expected_revision):
                raise SubmissionConflictError("替换后的座位池 revision 无效")
            old_seat = next(
                (
                    item
                    for item in previous.get("seats", [])
                    if int(item.get("slot_no", -1)) == int(failed_slot)
                ),
                None,
            )
            failed = next(
                (
                    item
                    for item in state.get("seats", [])
                    if int(item.get("slot_no", -1)) == int(failed_slot)
                ),
                None,
            )
            if not old_seat or not failed or failed.get("state") != "planned":
                raise SubmissionConflictError("故障座位状态不允许替换")
            uid = int(old_seat.get("uid") or 0)
            replacement = None
            replacement_resource = None
            if uid:
                replacement = next(
                    (
                        item
                        for item in state.get("seats", [])
                        if int(item.get("uid") or 0) == uid
                    ),
                    None,
                )
                if not replacement or replacement.get("state") not in {
                    "reserved",
                    "released",
                }:
                    raise SubmissionConflictError("备用座位未正确接管学生")
                replacement_resource = conn.execute(
                    "SELECT * FROM seat_pool_resources WHERE tid=? AND slot_no=?",
                    (str(tid), int(replacement["slot_no"])),
                ).fetchone()
                if not replacement_resource:
                    raise SubmissionConflictError("备用座位连接资源不存在")
                if (
                    str(replacement.get("container_ref") or "")
                    != replacement_resource["container"]
                    or str(replacement.get("image_digest") or "")
                    != replacement_resource["image_digest"]
                    or str(replacement.get("material_digest") or "")
                    != replacement_resource["material_digest"]
                ):
                    raise SubmissionConflictError("备用座位验收证据不一致")
            previous_by_slot = {
                int(item.get("slot_no", -1)): item
                for item in previous.get("seats", [])
            }
            new_by_slot = {
                int(item.get("slot_no", -1)): item
                for item in state.get("seats", [])
            }
            if set(previous_by_slot) != set(new_by_slot):
                raise SubmissionConflictError("故障替换不得改变座位池容量")
            changed_slots = {int(failed_slot)}
            if replacement:
                changed_slots.add(int(replacement["slot_no"]))
            if any(
                new_by_slot[slot] != seat
                for slot, seat in previous_by_slot.items()
                if slot not in changed_slots
            ):
                raise SubmissionConflictError("故障替换不得修改其他学生座位")
            failed_resource = conn.execute(
                "SELECT * FROM seat_pool_resources WHERE tid=? AND slot_no=?",
                (str(tid), int(failed_slot)),
            ).fetchone()
            conn.execute(
                "DELETE FROM seat_pool_resources WHERE tid=? AND slot_no=?",
                (str(tid), int(failed_slot)),
            )
            bound = None
            if uid and replacement_resource is not None:
                next_credential_revision = max(
                    int(replacement_resource["credential_revision"]) + 1,
                    int(failed_resource["credential_revision"]) + 1
                    if failed_resource is not None
                    else 2,
                )
                conn.execute(
                    "UPDATE seat_pool_resources SET credential_revision=? "
                    "WHERE tid=? AND slot_no=?",
                    (
                        next_credential_revision,
                        str(tid),
                        int(replacement["slot_no"]),
                    ),
                )
                conn.execute(
                    "INSERT INTO seats("
                    "tid,uid,uname,token,vnc_pass,submit_token,candidate,container,cip) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tid,uid) DO UPDATE SET "
                    "uname=excluded.uname,token=excluded.token,"
                    "vnc_pass=excluded.vnc_pass,submit_token=excluded.submit_token,"
                    "candidate=excluded.candidate,container=excluded.container,"
                    "cip=excluded.cip",
                    (
                        str(tid),
                        uid,
                        str(replacement.get("uname") or old_seat.get("uname") or ""),
                        replacement_resource["token"],
                        replacement_resource["vnc_pass"],
                        replacement_resource["submit_token"],
                        replacement_resource["candidate"],
                        replacement_resource["container"],
                        replacement_resource["cip"],
                    ),
                )
                bound = dict(replacement_resource)
                bound["credential_revision"] = next_credential_revision
                bound["uid"] = uid
            conn.execute(
                "UPDATE seat_pools SET revision=?,state_json=?,"
                "updated_at=datetime('now','localtime') WHERE tid=?",
                (new_revision, payload, str(tid)),
            )
        return bound

    def commit_pool_repair(
        self,
        tid: str,
        expected_revision: int,
        state: dict,
        resource: dict,
        *,
        repaired_slot: int,
    ) -> int:
        """Atomically restore one isolated slot as a clean verified spare.

        A failed-seat cutover deliberately commits before the old container is
        removed.  Rebuilding that slot is therefore a second CAS transaction:
        every unrelated seat must remain byte-for-byte identical, the repaired
        slot must remain unassigned, and its new runtime resource is inserted
        only with the verified pool state.
        """
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        new_revision = int(state.get("revision", 0))
        slot_no = int(repaired_slot)
        if str(state.get("tid") or "") != str(tid):
            raise ValueError("座位池比赛标识不一致")
        if int(resource.get("slot_no", -1)) != slot_no:
            raise ValueError("修复资源与座位编号不一致")
        repaired = self._validate_pool_resource(state, resource)
        if repaired.get("uid") is not None or str(repaired.get("uname") or ""):
            raise SubmissionConflictError("修复后的备用座位不得绑定学生")
        with self._immediate_tx() as conn:
            current = conn.execute(
                "SELECT revision,state_json FROM seat_pools WHERE tid=?",
                (str(tid),),
            ).fetchone()
            if not current or int(current["revision"]) != int(expected_revision):
                raise SubmissionConflictError("座位池已被其他操作更新")
            previous = json.loads(current["state_json"])
            previous_by_slot = {
                int(item.get("slot_no", -1)): item
                for item in previous.get("seats", [])
            }
            new_by_slot = {
                int(item.get("slot_no", -1)): item
                for item in state.get("seats", [])
            }
            before = previous_by_slot.get(slot_no)
            if set(previous_by_slot) != set(new_by_slot) or not before or \
                    before.get("state") != "planned" or before.get("uid") is not None:
                raise SubmissionConflictError("只有已隔离且未绑定的座位可以恢复容量")
            if any(
                new_by_slot[number] != item
                for number, item in previous_by_slot.items()
                if number != slot_no
            ):
                raise SubmissionConflictError("容量恢复不得修改其他座位")
            if int(repaired.get("failure_count", -1)) != int(
                before.get("failure_count", -2)
            ) or str(repaired.get("role") or "") != "spare":
                raise SubmissionConflictError("容量恢复不得重置故障历史或备用角色")
            if new_revision <= int(expected_revision):
                raise SubmissionConflictError("容量恢复后的 revision 无效")
            exists = conn.execute(
                "SELECT 1 FROM seat_pool_resources WHERE tid=? AND slot_no=?",
                (str(tid), slot_no),
            ).fetchone()
            if exists:
                raise SubmissionConflictError("待恢复座位仍残留旧连接资源")
            conn.execute(
                "INSERT INTO seat_pool_resources("
                "tid,slot_no,token,vnc_pass,submit_token,candidate,container,cip,"
                "image_digest,material_digest,credential_revision) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(tid), slot_no, str(resource["token"]),
                    str(resource["vnc_pass"]), str(resource["submit_token"]),
                    str(resource["candidate"]), str(resource["container"]),
                    str(resource["cip"]), str(resource["image_digest"]),
                    str(resource["material_digest"]),
                    int(resource.get("credential_revision", 1)),
                ),
            )
            conn.execute(
                "UPDATE seat_pools SET revision=?,state_json=?,"
                "updated_at=datetime('now','localtime') WHERE tid=?",
                (new_revision, payload, str(tid)),
            )
        return new_revision

    def seat_pool_resource(self, tid: str, slot_no: int) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM seat_pool_resources WHERE tid=? AND slot_no=?",
                (str(tid), int(slot_no)),
            ).fetchone()
        return dict(row) if row else None

    def seat_pool_resources(self, tid: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM seat_pool_resources WHERE tid=? ORDER BY slot_no",
                (str(tid),),
            ).fetchall()
        return [dict(row) for row in rows]

    def queue_seat_notification(
        self,
        tid: str,
        uid: int,
        kind: str,
        credential_revision: int,
        notification_id: str,
    ) -> dict:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO seat_notifications("
                "tid,uid,kind,credential_revision,notification_id) VALUES(?,?,?,?,?) "
                "ON CONFLICT(tid,uid,kind,credential_revision) DO NOTHING",
                (
                    str(tid),
                    int(uid),
                    str(kind),
                    int(credential_revision),
                    str(notification_id),
                ),
            )
            row = conn.execute(
                "SELECT * FROM seat_notifications WHERE "
                "tid=? AND uid=? AND kind=? AND credential_revision=?",
                (str(tid), int(uid), str(kind), int(credential_revision)),
            ).fetchone()
        return dict(row)

    def mark_seat_notification(
        self,
        notification_id: str,
        *,
        sent: bool,
        retryable: bool = True,
        error: str = "",
    ) -> None:
        state = (
            "sent"
            if sent
            else ("retry" if retryable else "permanent_failed")
        )
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE seat_notifications SET state=?,attempts=attempts+1,"
                "last_error=?,sent_at=CASE WHEN ? THEN datetime('now','localtime') "
                "ELSE sent_at END,updated_at=datetime('now','localtime') "
                "WHERE notification_id=?",
                (
                    state,
                    str(error),
                    1 if sent else 0,
                    str(notification_id),
                ),
            )
        if cur.rowcount != 1:
            raise KeyError("通知记录不存在")

    def pending_seat_notifications(self, tid: str | None = None) -> list[dict]:
        sql = "SELECT * FROM seat_notifications WHERE state<>'sent'"
        args: tuple[object, ...] = ()
        if tid is not None:
            sql += " AND tid=?"
            args = (str(tid),)
        sql += " ORDER BY updated_at,uid"
        with self._tx() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def contest_notification_health(self, tid: str) -> dict:
        """Return non-sensitive notification counts for one contest."""
        counts = {
            "pending": 0,
            "retry": 0,
            "permanent_failed": 0,
            "sent": 0,
        }
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT state,COUNT(*) AS total FROM seat_notifications "
                "WHERE tid=? GROUP BY state",
                (str(tid),),
            ).fetchall()
        for row in rows:
            counts[str(row["state"])] = int(row["total"])
        return {
            "counts": counts,
            "safe": sum(
                counts.get(state, 0)
                for state in ("pending", "retry", "permanent_failed")
            )
            == 0,
        }

    def retry_failed_seat_notifications(self, tid: str) -> int:
        """Requeue only failed notifications for currently released credentials.

        Old credential revisions remain immutable audit evidence.  Repeated
        teacher clicks are idempotent because only ``permanent_failed`` rows
        transition back to ``pending``; Hydro still owns the final
        notification-id idempotency boundary.
        """

        with self._tx() as conn:
            contest = conn.execute(
                "SELECT state FROM contests WHERE tid=?", (str(tid),)
            ).fetchone()
            if not contest:
                raise KeyError("比赛不存在")
            if str(contest["state"]) != "ready":
                raise SubmissionConflictError("只有已就绪比赛可以重试入口通知")
            pool = conn.execute(
                "SELECT state_json FROM seat_pools WHERE tid=?", (str(tid),)
            ).fetchone()
            if not pool:
                raise SubmissionConflictError("比赛缺少可重试的座位池")
            try:
                state = json.loads(pool["state_json"])
                seats = state.get("seats") if isinstance(state, dict) else None
                if not isinstance(seats, list):
                    raise ValueError("invalid seats")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SubmissionConflictError("座位池状态无效，拒绝重试通知") from exc
            resources = {
                int(row["slot_no"]): int(row["credential_revision"])
                for row in conn.execute(
                    "SELECT slot_no,credential_revision FROM seat_pool_resources "
                    "WHERE tid=?",
                    (str(tid),),
                ).fetchall()
            }
            keys: set[tuple[int, int]] = set()
            for seat in seats:
                if not isinstance(seat, dict) or seat.get("state") != "released":
                    continue
                uid, slot_no = seat.get("uid"), seat.get("slot_no")
                if (
                    not isinstance(uid, int)
                    or isinstance(uid, bool)
                    or uid <= 1
                    or not isinstance(slot_no, int)
                    or isinstance(slot_no, bool)
                    or slot_no <= 0
                    or slot_no not in resources
                ):
                    raise SubmissionConflictError(
                        "已发放座位与当前凭据不一致，拒绝重试通知"
                    )
                keys.add((uid, resources[slot_no]))
            changed = 0
            for uid, credential_revision in sorted(keys):
                cur = conn.execute(
                    "UPDATE seat_notifications SET state='pending',last_error='',"
                    "updated_at=datetime('now','localtime') WHERE tid=? AND uid=? "
                    "AND kind='seat_ready' AND credential_revision=? "
                    "AND state='permanent_failed'",
                    (str(tid), int(uid), int(credential_revision)),
                )
                changed += int(cur.rowcount)
        return changed

    def seat_notification_health(self) -> dict:
        """Return non-sensitive health for each currently released credential.

        A seat replacement deliberately leaves the previous notification row
        for auditability.  Only the released seat's *current* credential
        revision participates in health, so an obsolete retry cannot keep the
        service unhealthy.  Conversely, a released seat without a resource or
        queue row is reported even when notification preparation failed before
        an outbox row could be written.
        """

        active = "('registered','preparing','ready','collecting')"
        with self._tx() as conn:
            pools = conn.execute(
                "SELECT p.tid,p.state_json FROM seat_pools p "
                "JOIN contests c ON c.tid=p.tid WHERE c.state IN " + active
            ).fetchall()
            resources = conn.execute(
                "SELECT r.tid,r.slot_no,r.credential_revision "
                "FROM seat_pool_resources r JOIN contests c ON c.tid=r.tid "
                "WHERE c.state IN " + active
            ).fetchall()
            notifications = conn.execute(
                "SELECT n.tid,n.uid,n.kind,n.credential_revision,n.state,"
                "n.attempts,n.updated_at FROM seat_notifications n "
                "JOIN contests c ON c.tid=n.tid WHERE c.state IN " + active
            ).fetchall()

        resource_revisions = {
            (str(row["tid"]), int(row["slot_no"])): int(
                row["credential_revision"]
            )
            for row in resources
        }
        notification_rows = {
            (
                str(row["tid"]),
                int(row["uid"]),
                str(row["kind"]),
                int(row["credential_revision"]),
            ): row
            for row in notifications
        }
        counts = {
            "pending": 0,
            "retry": 0,
            "permanent_failed": 0,
            "sent": 0,
            "untracked": 0,
            "missing_resource": 0,
            "invalid_pool": 0,
        }
        max_retry_attempts = 0
        retry_times: list[str] = []
        expected: set[tuple[str, int, str, int]] = set()

        for pool_row in pools:
            tid = str(pool_row["tid"])
            try:
                state = json.loads(pool_row["state_json"])
                seats = state.get("seats") if isinstance(state, dict) else None
                if not isinstance(seats, list):
                    raise ValueError("invalid seat list")
            except (TypeError, ValueError, json.JSONDecodeError):
                counts["invalid_pool"] += 1
                continue

            released_slots: set[int] = set()
            released_uids: set[int] = set()
            for seat in seats:
                if not isinstance(seat, dict):
                    counts["invalid_pool"] += 1
                    continue
                if seat.get("state") != "released":
                    continue
                uid = seat.get("uid")
                slot_no = seat.get("slot_no")
                if (
                    not isinstance(uid, int)
                    or isinstance(uid, bool)
                    or uid <= 1
                    or not isinstance(slot_no, int)
                    or isinstance(slot_no, bool)
                    or slot_no <= 0
                    or uid in released_uids
                    or slot_no in released_slots
                ):
                    counts["invalid_pool"] += 1
                    continue
                released_uids.add(uid)
                released_slots.add(slot_no)
                credential_revision = resource_revisions.get((tid, slot_no))
                if credential_revision is None:
                    counts["missing_resource"] += 1
                    continue
                if credential_revision < 1:
                    counts["invalid_pool"] += 1
                    continue
                key = (tid, uid, "seat_ready", credential_revision)
                if key in expected:
                    counts["invalid_pool"] += 1
                    continue
                expected.add(key)
                notification = notification_rows.get(key)
                if notification is None:
                    counts["untracked"] += 1
                    continue
                notification_state = str(notification["state"])
                if notification_state not in {
                    "pending",
                    "retry",
                    "permanent_failed",
                    "sent",
                }:
                    counts["invalid_pool"] += 1
                    continue
                counts[notification_state] += 1
                if notification_state == "retry":
                    max_retry_attempts = max(
                        max_retry_attempts, int(notification["attempts"])
                    )
                    retry_at = str(notification["updated_at"] or "")
                    if retry_at:
                        retry_times.append(retry_at)

        return {
            "counts": counts,
            "max_retry_attempts": max_retry_attempts,
            "oldest_retry_at": min(retry_times) if retry_times else "",
        }

    def contests(self, state: str | None = None) -> list[dict]:
        sql = "SELECT * FROM contests"
        args: tuple = ()
        if state:
            sql += " WHERE state=?"
            args = (state,)
        sql += " ORDER BY created_at DESC, tid"
        with self._tx() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def active_seat_count(self) -> int:
        """Count unique bound containers for contests not yet safely ended."""
        active = ("registered", "preparing", "ready", "collecting", "safe_wait")
        marks = ",".join("?" for _ in active)
        with self._tx() as conn:
            return int(
                conn.execute(
                    f"SELECT COUNT(DISTINCT s.container) FROM seats s "
                    f"JOIN contests c ON c.tid=s.tid WHERE c.state IN ({marks})",
                    active,
                ).fetchone()[0]
            )

    @staticmethod
    def _audit_details(details: dict | None) -> dict:
        """Accept a deliberately small, non-sensitive audit payload."""
        payload = dict(details or {})
        forbidden = (
            "password",
            "secret",
            "token",
            "cookie",
            "authorization",
            "source",
            "code",
        )
        cleaned: dict[str, str | int | bool | None] = {}
        for raw_key, raw_value in payload.items():
            key = str(raw_key)
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                raise ValueError("audit detail key is invalid")
            if any(word in key.casefold() for word in forbidden):
                raise ValueError("audit detail contains a forbidden key")
            if raw_value is None or isinstance(raw_value, (bool, int)):
                cleaned[key] = raw_value
            elif isinstance(raw_value, str) and len(raw_value) <= 256:
                cleaned[key] = raw_value
            else:
                raise ValueError("audit detail value is invalid")
        return cleaned

    def append_audit(
        self,
        *,
        actor: str,
        action: str,
        outcome: str,
        tid: str = "",
        details: dict | None = None,
        created_at_ms: int | None = None,
    ) -> int:
        actor_value = str(actor).strip()
        action_value = str(action).strip()
        outcome_value = str(outcome).strip()
        tid_value = str(tid).strip()
        if (
            not actor_value
            or len(actor_value) > 128
            or any(ord(character) < 32 for character in actor_value)
        ):
            raise ValueError("audit actor is invalid")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,127}", action_value):
            raise ValueError("audit action is invalid")
        if outcome_value not in {"requested", "accepted", "completed", "failed"}:
            raise ValueError("audit outcome is invalid")
        if tid_value and not re.fullmatch(r"[0-9a-f]{24}", tid_value):
            raise ValueError("audit tid is invalid")
        payload = self._audit_details(details)
        timestamp = int(time.time() * 1000) if created_at_ms is None else int(created_at_ms)
        if timestamp <= 0:
            raise ValueError("audit timestamp is invalid")
        with self._immediate_tx() as conn:
            cur = conn.execute(
                "INSERT INTO audit_events(tid,actor,action,outcome,details_json,created_at_ms) "
                "VALUES(?,?,?,?,?,?)",
                (
                    tid_value,
                    actor_value,
                    action_value,
                    outcome_value,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    timestamp,
                ),
            )
        return int(cur.lastrowid)

    def audit_events(self, tid: str = "", *, limit: int = 500) -> list[dict]:
        maximum = int(limit)
        if not 1 <= maximum <= 5000:
            raise ValueError("audit limit is invalid")
        sql = "SELECT * FROM audit_events"
        args: tuple[object, ...]
        if tid:
            sql += " WHERE tid=?"
            args = (str(tid), maximum)
        else:
            args = (maximum,)
        sql += " ORDER BY created_at_ms DESC,id DESC LIMIT ?"
        with self._tx() as conn:
            rows = conn.execute(sql, args).fetchall()
        events: list[dict] = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json") or "{}")
            events.append(item)
        return events

    def set_state(self, tid: str, state: str, message: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE contests SET state=?, message=? WHERE tid=?",
                (state, message, tid),
            )
            conn.execute(
                "INSERT INTO audit_events(tid,actor,action,outcome,details_json,created_at_ms) "
                "VALUES(?,?,?,?,?,?)",
                (
                    str(tid),
                    "system",
                    "contest.state.set",
                    "completed",
                    json.dumps({"state": str(state)}, sort_keys=True),
                    int(time.time() * 1000),
                ),
            )

    def transition(
        self,
        tid: str,
        from_states: set[str] | tuple[str, ...],
        to_state: str,
        message: str = "",
    ) -> bool:
        states = tuple(from_states)
        if not states:
            return False
        marks = ",".join("?" for _ in states)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE contests SET state=?, message=? "
                f"WHERE tid=? AND state IN ({marks})",
                (to_state, message, tid, *states),
            )
            if cur.rowcount == 1:
                conn.execute(
                    "INSERT INTO audit_events(tid,actor,action,outcome,details_json,created_at_ms) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        str(tid),
                        "system",
                        "contest.state.transition",
                        "completed",
                        json.dumps(
                            {"from": ",".join(sorted(states)), "to": str(to_state)},
                            sort_keys=True,
                        ),
                        int(time.time() * 1000),
                    ),
                )
        return cur.rowcount == 1

    def enter_safe_wait(
        self,
        tid: str,
        *,
        run_id: str,
        collection_dir: str,
        receipt_sha256: str,
        completed_at_ms: int,
        shutdown_after_ms: int,
        message: str,
    ) -> bool:
        """Commit durable collection evidence before the delayed shutdown."""
        if not _REVISION.fullmatch(str(run_id)):
            raise ValueError("collection run id is invalid")
        directory = str(collection_dir).strip()
        digest = str(receipt_sha256).lower()
        completed = int(completed_at_ms)
        shutdown_after = int(shutdown_after_ms)
        if not directory or not _SHA256.fullmatch(digest):
            raise ValueError("collection evidence is invalid")
        if completed <= 0 or shutdown_after < completed:
            raise ValueError("safe-wait shutdown boundary is invalid")
        with self._immediate_tx() as conn:
            cur = conn.execute(
                "UPDATE contests SET state='safe_wait',message=?,"
                "collection_run_id=?,collection_dir=?,"
                "collection_receipt_sha256=?,collection_completed_at_ms=?,"
                "shutdown_after_ms=?,shutdown_verified_at_ms=0 "
                "WHERE tid=? AND state='collecting'",
                (
                    str(message),
                    str(run_id),
                    directory,
                    digest,
                    completed,
                    shutdown_after,
                    str(tid),
                ),
            )
        return cur.rowcount == 1

    def mark_safe_ended(self, tid: str, *, observed_at_ms: int, message: str) -> bool:
        """Record a verified delayed shutdown; never bypass the time boundary."""
        observed = int(observed_at_ms)
        with self._immediate_tx() as conn:
            cur = conn.execute(
                "UPDATE contests SET state='safe_ended',message=?,"
                "shutdown_verified_at_ms=? WHERE tid=? AND state='safe_wait' "
                "AND shutdown_after_ms>0 AND shutdown_after_ms<=?",
                (str(message), observed, str(tid), observed),
            )
        return cur.rowcount == 1

    def retention_candidates(
        self,
        *,
        now_ms: int,
        workspace_retention_days: int,
        evidence_retention_days: int,
    ) -> list[dict]:
        """Return only safely ended contests whose local retention boundary passed."""
        now_value = int(now_ms)
        workspace_ms = int(workspace_retention_days) * 86_400_000
        evidence_ms = int(evidence_retention_days) * 86_400_000
        if workspace_ms <= 0 or evidence_ms < workspace_ms:
            raise ValueError("retention boundaries are invalid")
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM contests WHERE state IN ('safe_ended','done') "
                "AND shutdown_verified_at_ms>0 AND ("
                "(workspace_purged_at_ms=0 AND shutdown_verified_at_ms<=?) OR "
                "(evidence_purged_at_ms=0 AND shutdown_verified_at_ms<=?)) "
                "ORDER BY shutdown_verified_at_ms,tid",
                (now_value - workspace_ms, now_value - evidence_ms),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_retention_purged(
        self, tid: str, *, kind: str, observed_at_ms: int
    ) -> bool:
        column = {
            "workspace": "workspace_purged_at_ms",
            "evidence": "evidence_purged_at_ms",
        }.get(str(kind))
        if not column:
            raise ValueError("retention kind is invalid")
        observed = int(observed_at_ms)
        if observed <= 0:
            raise ValueError("retention timestamp is invalid")
        with self._immediate_tx() as conn:
            cur = conn.execute(
                f"UPDATE contests SET {column}=? WHERE tid=? "
                "AND state IN ('safe_ended','done') AND shutdown_verified_at_ms>0 "
                f"AND {column}=0",
                (observed, str(tid)),
            )
            if cur.rowcount == 1:
                conn.execute(
                    "INSERT INTO audit_events(tid,actor,action,outcome,details_json,created_at_ms) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        str(tid),
                        "system",
                        "contest.retention.purge",
                        "completed",
                        json.dumps({"kind": str(kind)}, sort_keys=True),
                        observed,
                    ),
                )
        return cur.rowcount == 1

    def reset_seats(self, tid: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM seats WHERE tid=?", (tid,))
            # Preparing a contest starts a fresh exam run. A retry or reused
            # contest id must never inherit web submissions from an older run.
            conn.execute("DELETE FROM web_submissions WHERE tid=?", (tid,))

    def web_submission_count(
        self, tid: str, submission_session: str | None = None
    ) -> int:
        sql = "SELECT COUNT(*) FROM web_submissions WHERE tid=?"
        args: tuple[object, ...] = (str(tid),)
        if submission_session is not None:
            sql += " AND submission_session=?"
            args += (str(submission_session),)
        with self._tx() as conn:
            return int(conn.execute(sql, args).fetchone()[0])

    def realtime_queue_health(self, now_ms: int | None = None) -> dict:
        """Return non-sensitive delivery backlog counters for health checks."""
        current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        counts = {
            "pending": 0,
            "sending": 0,
            "retry": 0,
            "permanent_failed": 0,
            "ambiguous": 0,
            "submitted": 0,
        }
        oldest_waiting_ms = 0
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT judge_state,COUNT(*) AS total,MIN(accepted_at_ms) AS oldest "
                "FROM web_submissions WHERE submission_id<>'' GROUP BY judge_state"
            ).fetchall()
        for row in rows:
            state = str(row["judge_state"])
            counts[state] = int(row["total"])
            if state in {"pending", "sending", "retry", "permanent_failed", "ambiguous"}:
                accepted = int(row["oldest"] or 0)
                if accepted > 0:
                    oldest_waiting_ms = max(
                        oldest_waiting_ms, max(0, current_ms - accepted)
                    )
        return {"counts": counts, "oldest_waiting_ms": oldest_waiting_ms}

    def contest_delivery_health(self, tid: str) -> dict:
        """Return exact delivery states for one contest without source text."""
        counts = {
            "pending": 0,
            "sending": 0,
            "retry": 0,
            "permanent_failed": 0,
            "ambiguous": 0,
            "submitted": 0,
        }
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT judge_state,COUNT(*) AS total FROM web_submissions "
                "WHERE tid=? AND submission_id<>'' GROUP BY judge_state",
                (str(tid),),
            ).fetchall()
        for row in rows:
            counts[str(row["judge_state"])] = int(row["total"])
        unfinished = sum(
            int(counts.get(state, 0))
            for state in ("pending", "sending", "retry", "permanent_failed", "ambiguous")
        )
        return {"counts": counts, "unfinished": unfinished, "safe": unfinished == 0}

    def add_seat(
        self,
        tid,
        uid,
        uname,
        token,
        vnc_pass,
        submit_token,
        candidate,
        container,
        cip,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO seats "
                "(tid,uid,uname,token,vnc_pass,submit_token,candidate,container,cip) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    uid,
                    uname,
                    token,
                    vnc_pass,
                    submit_token,
                    candidate,
                    container,
                    cip,
                ),
            )

    def bind_pool_seat(self, tid: str, uid: int, uname: str, slot_no: int) -> dict:
        """Expose one reserved pool resource through the legacy seat interface."""
        with self._immediate_tx() as conn:
            pool_row = conn.execute(
                "SELECT state_json FROM seat_pools WHERE tid=?", (str(tid),)
            ).fetchone()
            resource = conn.execute(
                "SELECT * FROM seat_pool_resources WHERE tid=? AND slot_no=?",
                (str(tid), int(slot_no)),
            ).fetchone()
            if not pool_row or not resource:
                raise KeyError("座位池资源不存在")
            pool = json.loads(pool_row["state_json"])
            seat_state = next(
                (
                    item
                    for item in pool.get("seats", [])
                    if int(item.get("slot_no", -1)) == int(slot_no)
                ),
                None,
            )
            if (
                not seat_state
                or int(seat_state.get("uid") or 0) != int(uid)
                or str(seat_state.get("uname") or "") != str(uname)
                or seat_state.get("state") not in {"reserved", "released"}
            ):
                raise SubmissionConflictError("座位池身份绑定与当前状态不一致")
            if (
                str(seat_state.get("container_ref") or "") != resource["container"]
                or str(seat_state.get("image_digest") or "")
                != resource["image_digest"]
                or str(seat_state.get("material_digest") or "")
                != resource["material_digest"]
            ):
                raise SubmissionConflictError("座位验收证据与连接资源不一致")
            conn.execute(
                "INSERT INTO seats("
                "tid,uid,uname,token,vnc_pass,submit_token,candidate,container,cip) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tid,uid) DO UPDATE SET "
                "uname=excluded.uname,token=excluded.token,vnc_pass=excluded.vnc_pass,"
                "submit_token=excluded.submit_token,candidate=excluded.candidate,"
                "container=excluded.container,cip=excluded.cip",
                (
                    str(tid),
                    int(uid),
                    str(uname),
                    resource["token"],
                    resource["vnc_pass"],
                    resource["submit_token"],
                    resource["candidate"],
                    resource["container"],
                    resource["cip"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM seats WHERE tid=? AND uid=?",
                (str(tid), int(uid)),
            ).fetchone()
        return dict(row)

    def seat_pool_assignment(self, tid: str, uid: int) -> dict | None:
        with self._tx() as conn:
            pool_row = conn.execute(
                "SELECT state_json FROM seat_pools WHERE tid=?", (str(tid),)
            ).fetchone()
            if not pool_row:
                return None
            pool = json.loads(pool_row["state_json"])
            state = next(
                (
                    item
                    for item in pool.get("seats", [])
                    if int(item.get("uid") or 0) == int(uid)
                ),
                None,
            )
            if not state:
                return None
            resource = conn.execute(
                "SELECT * FROM seat_pool_resources WHERE tid=? AND slot_no=?",
                (str(tid), int(state["slot_no"])),
            ).fetchone()
        result = dict(state)
        result["resource"] = dict(resource) if resource else None
        return result

    def seats(self, tid: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM seats WHERE tid=? ORDER BY uname", (tid,)
            ).fetchall()
        return [dict(row) for row in rows]

    def seat_by_uname(self, tid: str, uname: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM seats WHERE tid=? AND uname=?", (tid, uname)
            ).fetchone()
        return dict(row) if row else None

    def seat_by_gateway_token(self, token: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM seats WHERE token=?", (str(token),)
            ).fetchone()
        return dict(row) if row else None

    def seat_by_submit_token(self, token: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM seats WHERE submit_token=?", (token,)
            ).fetchone()
        return dict(row) if row else None

    def add_web_submission(
        self, tid: str, uid: int, problem: str, source: str
    ) -> dict:
        """Store a legacy local-only submission.

        New real-time call sites should use :meth:`enqueue_web_submission`.
        Keeping this method local-only preserves the collection behavior used
        by older deployments and tests.
        """
        encoded = source.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO web_submissions(tid,uid,problem,source,sha256,size) "
                "VALUES(?,?,?,?,?,?)",
                (tid, uid, problem, source, digest, len(encoded)),
            )
            row = conn.execute(
                "SELECT * FROM web_submissions WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        return dict(row)

    def enqueue_web_submission(
        self,
        tid: str,
        uid: int,
        problem: str,
        source: str,
        *,
        client_nonce: str,
        submission_id: str,
        submission_session: str,
        judge_pid: str,
        judge_lang: str,
        judge_source: str,
        issues: list[str] | tuple[str, ...],
        accepted_at_ms: int,
        allow_new: bool = True,
    ) -> dict:
        """Atomically save source and its exact Hydro delivery payload.

        ``client_nonce`` identifies one browser submission. Replaying the same
        nonce and immutable payload returns the existing row. Reusing it with
        different content is rejected rather than creating a second record.
        """
        nonce = str(client_nonce)
        idem = str(submission_id)
        session = str(submission_session)
        pid = str(judge_pid)
        lang = str(judge_lang)
        accepted = int(accepted_at_ms)
        if not nonce or len(nonce) > 200:
            raise ValueError("client_nonce must contain 1 to 200 characters")
        if len(idem) != 64 or any(char not in "0123456789abcdef" for char in idem):
            raise ValueError("submission_id must be 64 lowercase hexadecimal characters")
        if not session or not pid or not lang:
            raise ValueError("submission_session, judge_pid, and judge_lang are required")
        if not judge_source:
            raise ValueError("judge_source is required")
        if accepted <= 0:
            raise ValueError("accepted_at_ms must be a positive Unix timestamp")

        encoded = source.encode("utf-8")
        source_digest = hashlib.sha256(encoded).hexdigest()
        judge_encoded = judge_source.encode("utf-8")
        judge_digest = hashlib.sha256(judge_encoded).hexdigest()
        issue_list = [str(item) for item in issues]
        issues_json = json.dumps(
            issue_list, ensure_ascii=False, separators=(",", ":")
        )
        immutable = {
            "tid": str(tid),
            "uid": int(uid),
            "problem": str(problem),
            "source": source,
            "sha256": source_digest,
            "size": len(encoded),
            "client_nonce": nonce,
            "submission_id": idem,
            "submission_session": session,
            "judge_pid": pid,
            "judge_lang": lang,
            "judge_source": judge_source,
            "judge_sha256": judge_digest,
            "judge_issues": issues_json,
        }

        def checked_replay(row: sqlite3.Row) -> dict:
            current = dict(row)
            # accepted_at_ms is the receipt time of the first successful local
            # transaction. A browser retry necessarily arrives later, but the
            # same nonce and payload must replay the first receipt instead of
            # becoming a conflict (including after the contest closes).
            if any(current.get(key) != value for key, value in immutable.items()):
                raise SubmissionConflictError(
                    "submission nonce or idempotency id was reused with different payload"
                )
            current["replayed"] = True
            return current

        with self._immediate_tx() as conn:
            contest = conn.execute(
                "SELECT submission_session,pids,state,begin_at_ms,end_at_ms,hydro_rule "
                "FROM contests WHERE tid=?",
                (str(tid),),
            ).fetchone()
            if not contest:
                raise KeyError(f"比赛不存在: {tid}")
            if str(contest["submission_session"]) != session:
                raise SubmissionConflictError(
                    "contest submission session changed before enqueue"
                )
            try:
                pid_map = json.loads(contest["pids"] or "{}")
            except (TypeError, ValueError) as exc:
                raise ValueError("contest problem mapping is invalid") from exc
            if str(pid_map.get(str(problem), "")) != pid:
                raise SubmissionConflictError(
                    "contest problem mapping changed before enqueue"
                )
            existing = conn.execute(
                "SELECT * FROM web_submissions "
                "WHERE tid=? AND uid=? AND problem=? AND client_nonce=?",
                (str(tid), int(uid), str(problem), nonce),
            ).fetchone()
            if existing:
                return checked_replay(existing)
            existing = conn.execute(
                "SELECT * FROM web_submissions WHERE submission_id=?", (idem,)
            ).fetchone()
            if existing:
                return checked_replay(existing)
            if not allow_new:
                raise SubmissionClosedError(
                    "new submission is outside the authoritative contest window"
                )
            if str(contest["state"]) != "ready":
                raise SubmissionClosedError(
                    "contest stopped accepting new submissions before enqueue"
                )
            begin_at = int(contest["begin_at_ms"] or 0)
            end_at = int(contest["end_at_ms"] or 0)
            if begin_at and end_at:
                if (
                    str(contest["hydro_rule"] or "") != "oi"
                    or not begin_at <= accepted < end_at
                ):
                    raise SubmissionClosedError(
                        "new submission is outside the frozen OI contest window"
                    )
            insert_values = {**immutable, "accepted_at_ms": accepted}
            columns = ",".join((*insert_values.keys(), "judge_state"))
            placeholders = ",".join("?" for _ in range(len(insert_values) + 1))
            cur = conn.execute(
                f"INSERT INTO web_submissions({columns}) VALUES({placeholders})",
                (*insert_values.values(), "pending"),
            )
            row = conn.execute(
                "SELECT * FROM web_submissions WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        result = dict(row)
        result["replayed"] = False
        return result

    def get_web_submission(self, submission_row_id: int) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM web_submissions WHERE id=?",
                (int(submission_row_id),),
            ).fetchone()
        return dict(row) if row else None

    def get_web_submission_by_nonce(
        self, tid: str, uid: int, problem: str, client_nonce: str
    ) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM web_submissions "
                "WHERE tid=? AND uid=? AND problem=? AND client_nonce=?",
                (str(tid), int(uid), str(problem), str(client_nonce)),
            ).fetchone()
        return dict(row) if row else None

    def claim_next_web_submission(
        self,
        *,
        now: float | None = None,
        lease_seconds: float = 45.0,
        tid: str | None = None,
        uid: int | None = None,
        problem: str | None = None,
    ) -> dict | None:
        """Lease the next due outbox row while preserving per-problem FIFO.

        An expired ``sending`` row is eligible for a new lease. A later row in
        the same ``(tid, uid, problem)`` lane remains blocked until every older
        delivery is either submitted or permanently failed.
        """
        timestamp = time.time() if now is None else float(now)
        duration = float(lease_seconds)
        if duration <= 0:
            raise ValueError("lease_seconds must be positive")
        filters: list[str] = []
        args: list[object] = [timestamp, timestamp]
        if tid is not None:
            filters.append("q.tid=?")
            args.append(str(tid))
        if uid is not None:
            filters.append("q.uid=?")
            args.append(int(uid))
        if problem is not None:
            filters.append("q.problem=?")
            args.append(str(problem))
        filter_sql = "" if not filters else " AND " + " AND ".join(filters)
        query = (
            "SELECT q.* FROM web_submissions q WHERE "
            "((q.judge_state IN ('pending','retry') AND q.next_retry_at<=?) "
            "OR (q.judge_state='sending' AND q.lease_until<=?))"
            f"{filter_sql} "
            "AND NOT EXISTS (SELECT 1 FROM web_submissions older "
            "WHERE older.tid=q.tid AND older.uid=q.uid "
            "AND older.problem=q.problem AND older.id<q.id "
            "AND older.judge_state IN ('pending','retry','sending')) "
            "ORDER BY q.id LIMIT 1"
        )
        lease_token = secrets.token_hex(16)
        with self._immediate_tx() as conn:
            row = conn.execute(query, tuple(args)).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE web_submissions SET judge_state='sending', "
                "attempts=attempts+1,lease_until=?,lease_token=? WHERE id=?",
                (timestamp + duration, lease_token, int(row["id"])),
            )
            claimed = conn.execute(
                "SELECT * FROM web_submissions WHERE id=?", (int(row["id"]),)
            ).fetchone()
        return dict(claimed)

    def mark_web_submission_submitted(
        self,
        submission_row_id: int,
        lease_token: str,
        rid: str,
        *,
        now: float | None = None,
    ) -> dict:
        if not rid:
            raise ValueError("rid is required")
        timestamp = time.time() if now is None else float(now)
        delivered = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
        with self._immediate_tx() as conn:
            cur = conn.execute(
                "UPDATE web_submissions SET judge_state='submitted',rid=?,"
                "lease_until=0,lease_token='',next_retry_at=0,last_error='',"
                "delivered_at=? WHERE id=? AND judge_state='sending' "
                "AND lease_token=?",
                (str(rid), delivered, int(submission_row_id), str(lease_token)),
            )
            if cur.rowcount != 1:
                raise SubmissionLeaseLostError("submission delivery lease was lost")
            row = conn.execute(
                "SELECT * FROM web_submissions WHERE id=?",
                (int(submission_row_id),),
            ).fetchone()
        return dict(row)

    def mark_web_submission_failed(
        self,
        submission_row_id: int,
        lease_token: str,
        error: str,
        *,
        retry_at: float | None,
        ambiguous: bool = False,
        resolution_after: float | None = None,
    ) -> dict:
        if ambiguous and retry_at is not None:
            raise ValueError("an ambiguous submission cannot be scheduled for retry")
        state = "ambiguous" if ambiguous else (
            "permanent_failed" if retry_at is None else "retry"
        )
        if resolution_after is not None and not ambiguous:
            raise ValueError("only ambiguous submissions have a resolution schedule")
        next_retry = 0 if retry_at is None else float(retry_at)
        next_resolution = 0 if resolution_after is None else float(resolution_after)
        with self._immediate_tx() as conn:
            cur = conn.execute(
                "UPDATE web_submissions SET judge_state=?,next_retry_at=?,"
                "lease_until=0,lease_token='',last_error=?,resolution_after=? "
                "WHERE id=? AND judge_state='sending' AND lease_token=?",
                (
                    state,
                    next_retry,
                    str(error)[:4000],
                    next_resolution,
                    int(submission_row_id),
                    str(lease_token),
                ),
            )
            if cur.rowcount != 1:
                raise SubmissionLeaseLostError("submission delivery lease was lost")
            row = conn.execute(
                "SELECT * FROM web_submissions WHERE id=?",
                (int(submission_row_id),),
            ).fetchone()
        return dict(row)

    def claim_ambiguous_web_submission(
        self,
        *,
        now: float | None = None,
        check_seconds: float = 30.0,
    ) -> dict | None:
        """Claim one due row for a read-only OJ correlation check."""
        timestamp = time.time() if now is None else float(now)
        delay = float(check_seconds)
        if delay <= 0:
            raise ValueError("check_seconds must be positive")
        with self._immediate_tx() as conn:
            row = conn.execute(
                "SELECT * FROM web_submissions WHERE judge_state='ambiguous' "
                "AND resolution_after>0 AND resolution_after<=? ORDER BY id LIMIT 1",
                (timestamp,),
            ).fetchone()
            if not row:
                return None
            cur = conn.execute(
                "UPDATE web_submissions SET resolution_attempts=resolution_attempts+1,"
                "resolution_after=? WHERE id=? AND judge_state='ambiguous' "
                "AND resolution_after>0 AND resolution_after<=?",
                (timestamp + delay, int(row["id"]), timestamp),
            )
            if cur.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM web_submissions WHERE id=?", (int(row["id"]),)
            ).fetchone()
        return dict(claimed)

    def finish_ambiguous_web_submission(
        self,
        submission_row_id: int,
        submission_id: str,
        *,
        rid: str | None = None,
        resolution_status: str = "pending",
        now: float | None = None,
    ) -> dict:
        """Persist one exact read-only correlation result.

        Only a unique 24-hex RID may transition an ambiguous row to submitted.
        All other results remain fail-closed and cannot enter the delivery queue.
        """
        allowed = {"missing", "multiple", "pending", "unknown", "unsupported", "unavailable"}
        timestamp = time.time() if now is None else float(now)
        with self._immediate_tx() as conn:
            if rid is not None:
                rid_text = str(rid).lower()
                if not re.fullmatch(r"[0-9a-f]{24}", rid_text):
                    raise ValueError("resolved rid must be 24 lowercase hexadecimal characters")
                delivered = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
                cur = conn.execute(
                    "UPDATE web_submissions SET judge_state='submitted',rid=?,"
                    "last_error='',resolution_after=0,delivered_at=? "
                    "WHERE id=? AND submission_id=? AND judge_state='ambiguous'",
                    (rid_text, delivered, int(submission_row_id), str(submission_id)),
                )
            else:
                status = str(resolution_status)
                if status not in allowed:
                    raise ValueError("invalid ambiguous resolution status")
                cur = conn.execute(
                    "UPDATE web_submissions SET last_error=?,resolution_after=? "
                    "WHERE id=? AND submission_id=? AND judge_state='ambiguous'",
                    (
                        f"OJ 只读核对：{status}",
                        (-1 if status in {"missing", "multiple", "unknown", "unsupported"}
                         else timestamp + 30.0),
                        int(submission_row_id),
                        str(submission_id),
                    ),
                )
            if cur.rowcount != 1:
                raise SubmissionLeaseLostError("ambiguous submission state changed")
            row = conn.execute(
                "SELECT * FROM web_submissions WHERE id=?",
                (int(submission_row_id),),
            ).fetchone()
        return dict(row)

    def requeue_web_submission_for_final(self, submission_row_id: int) -> dict:
        """Make a row and unfinished predecessors immediately final-deliverable.

        Collection may run after Hydro has started rejecting ``realtime``
        submissions. Resetting the whole lane prefix preserves FIFO. A worker
        still holding an old lease cannot overwrite the new attempt because
        completion updates require the matching lease token; both attempts use
        the same Hydro idempotency id and exact payload.
        """
        with self._immediate_tx() as conn:
            target = conn.execute(
                "SELECT * FROM web_submissions WHERE id=?",
                (int(submission_row_id),),
            ).fetchone()
            if not target:
                raise KeyError(f"web submission does not exist: {submission_row_id}")
            if not target["submission_id"] or target["judge_state"] == "local":
                raise ValueError("legacy local-only submission has no Hydro payload")
            if target["judge_state"] == "submitted" and target["rid"]:
                return dict(target)
            if target["judge_state"] == "ambiguous":
                # A final collection pass cannot turn an unknown OJ insert
                # outcome into permission to create another record.
                return dict(target)
            conn.execute(
                "UPDATE web_submissions SET judge_state='pending',"
                "judge_kind='final',next_retry_at=0,lease_until=0,lease_token='' "
                "WHERE tid=? AND uid=? AND problem=? AND id<=? "
                "AND judge_state IN ('pending','retry','sending','permanent_failed') "
                "AND submission_id<>''",
                (
                    target["tid"],
                    int(target["uid"]),
                    target["problem"],
                    int(target["id"]),
                ),
            )
            row = conn.execute(
                "SELECT * FROM web_submissions WHERE id=?",
                (int(submission_row_id),),
            ).fetchone()
        return dict(row)

    def latest_web_submissions(self, tid: str, uid: int) -> dict[str, dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT current.* FROM web_submissions current "
                "JOIN (SELECT problem,MAX(id) AS id FROM web_submissions "
                "WHERE tid=? AND uid=? GROUP BY problem) latest "
                "ON current.id=latest.id ORDER BY current.problem",
                (tid, uid),
            ).fetchall()
        return {row["problem"]: dict(row) for row in rows}
