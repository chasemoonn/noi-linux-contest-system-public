"""Persistent, ordered delivery of explicit web submissions to Hydro."""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from typing import Callable, Iterable

from services.hydro_submit import HydroSubmitter
from services.store import Store, SubmissionLeaseLostError


log = logging.getLogger("orchestrator.realtime_judge")


class RealtimeJudgeError(RuntimeError):
    """Base class for real-time delivery failures."""


class RealtimeJudgePermanentError(RealtimeJudgeError):
    """Hydro permanently rejected a submission."""


class RealtimeJudgeTimeout(TimeoutError, RealtimeJudgeError):
    """A synchronous caller could not obtain a Hydro rid before its deadline."""


class RealtimeJudge:
    """Transactional outbox dispatcher for web submissions.

    Source persistence and queue creation happen in one SQLite transaction.
    Delivery is at-least-once; the Hydro plugin's 64-hex idempotency key turns
    retries into one record. Claims are FIFO within each contest/user/problem
    lane and use renewable-by-expiry leases so a crashed worker cannot strand a
    row forever.
    """

    def __init__(
        self,
        store: Store,
        submitter: HydroSubmitter,
        *,
        lease_seconds: float = 45.0,
        retry_delays: tuple[float, ...] = (1, 2, 5, 10, 30, 60),
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not retry_delays or any(delay < 0 for delay in retry_delays):
            raise ValueError("retry_delays must contain non-negative values")
        self.store = store
        self.submitter = submitter
        self.lease_seconds = float(lease_seconds)
        self.retry_delays = tuple(float(delay) for delay in retry_delays)
        self.clock = clock
        self.sleeper = sleeper
        self._worker_running = False
        self._worker_last_ok_at = 0.0
        self._worker_last_error = ""
        self._worker_error_count = 0

    def worker_health(self) -> dict:
        return {
            "running": bool(self._worker_running),
            "last_ok_at": float(self._worker_last_ok_at),
            "last_error": str(self._worker_last_error),
            "error_count": int(self._worker_error_count),
        }

    @staticmethod
    def new_client_nonce() -> str:
        """Return a nonce suitable for one rendered submit form."""
        return secrets.token_hex(16)

    def enqueue(
        self,
        *,
        submission_session: str,
        tid: str,
        uid: int,
        problem: str,
        pid: str,
        source: str,
        judge_source: str,
        issues: Iterable[str],
        client_nonce: str,
        accepted_at_ms: int,
        allow_new: bool = True,
        lang: str | None = None,
    ) -> dict:
        """Persist one logical submit action and its immutable judge payload."""
        selected_lang = self.submitter.lang if lang is None else str(lang)
        if not selected_lang:
            raise ValueError("an explicit Hydro language is required for real-time retry")
        submission_id = self.submitter.realtime_submission_id(
            submission_session,
            tid,
            int(uid),
            pid,
            client_nonce,
        )
        return self.store.enqueue_web_submission(
            tid,
            int(uid),
            problem,
            source,
            client_nonce=client_nonce,
            submission_id=submission_id,
            submission_session=submission_session,
            judge_pid=pid,
            judge_lang=selected_lang,
            judge_source=judge_source,
            issues=tuple(str(item) for item in issues),
            accepted_at_ms=int(accepted_at_ms),
            allow_new=bool(allow_new),
        )

    @staticmethod
    def _error_text(result: dict) -> str:
        value = result.get("error", "Hydro submission failed")
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(value)

    def _retry_delay(self, attempts: int) -> float:
        index = max(0, min(int(attempts) - 1, len(self.retry_delays) - 1))
        return self.retry_delays[index]

    def process_once(
        self,
        *,
        tid: str | None = None,
        uid: int | None = None,
        problem: str | None = None,
        submission_kind: str = "realtime",
    ) -> dict | None:
        """Deliver at most one due row and return its latest persisted state."""
        if submission_kind not in {"final", "realtime"}:
            raise ValueError("submission_kind must be final or realtime")
        row = self.store.claim_next_web_submission(
            now=self.clock(),
            lease_seconds=self.lease_seconds,
            tid=tid,
            uid=uid,
            problem=problem,
        )
        if row is None:
            return None
        lease_token = str(row["lease_token"])
        effective_kind = (
            "final"
            if submission_kind == "final" or row.get("judge_kind") == "final"
            else "realtime"
        )
        try:
            result = self.submitter.submit_one(
                str(row["tid"]),
                int(row["uid"]),
                str(row["judge_pid"]),
                str(row["judge_source"]),
                str(row["submission_id"]),
                lang=str(row["judge_lang"]),
                submission_kind=effective_kind,
                accepted_at_ms=int(row.get("accepted_at_ms") or 0),
            )
        except Exception as exc:  # a worker must persist unexpected transport failure
            log.exception("unexpected Hydro submit error for web submission %s", row["id"])
            result = {"ok": False, "retryable": True, "error": str(exc)}

        try:
            if result.get("ok") and result.get("rid"):
                return self.store.mark_web_submission_submitted(
                    int(row["id"]),
                    lease_token,
                    str(result["rid"]),
                    now=self.clock(),
                )
            error = self._error_text(result)
            if result.get("retryable", True):
                retry_at = self.clock() + self._retry_delay(int(row["attempts"]))
            else:
                retry_at = None
            return self.store.mark_web_submission_failed(
                int(row["id"]),
                lease_token,
                error,
                retry_at=retry_at,
            )
        except SubmissionLeaseLostError:
            # The HTTP request outlived its lease. A replacement worker uses the
            # same idempotency key, so returning the current row is safe.
            log.warning("delivery lease expired for web submission %s", row["id"])
            return self.store.get_web_submission(int(row["id"]))

    def ensure_submitted(
        self,
        submission_row_id: int,
        *,
        timeout_seconds: float = 120.0,
        poll_interval: float = 0.1,
    ) -> dict:
        """Synchronously obtain a rid, processing older rows in the same lane.

        This is intended for collection: a latest web source already delivered
        can be reused without a duplicate record, while a pending source is
        driven through the exact same idempotent delivery path.
        """
        if timeout_seconds <= 0 or poll_interval <= 0:
            raise ValueError("timeout_seconds and poll_interval must be positive")
        deadline = self.clock() + float(timeout_seconds)
        # Contest collection is allowed after endAt. Reset an expired realtime
        # failure (and unfinished predecessors in its FIFO lane) so every retry
        # below is explicitly marked final while keeping the same idempotency id.
        self.store.requeue_web_submission_for_final(int(submission_row_id))
        while True:
            row = self.store.get_web_submission(int(submission_row_id))
            if row is None:
                raise KeyError(f"web submission does not exist: {submission_row_id}")
            state = str(row.get("judge_state") or "local")
            if state == "submitted" and row.get("rid"):
                return row
            if state == "permanent_failed":
                raise RealtimeJudgePermanentError(
                    str(row.get("last_error") or "Hydro permanently rejected submission")
                )
            if state == "local" or not row.get("submission_id"):
                raise RealtimeJudgePermanentError(
                    "legacy local-only submission has no real-time Hydro payload"
                )

            self.process_once(
                tid=str(row["tid"]),
                uid=int(row["uid"]),
                problem=str(row["problem"]),
                submission_kind="final",
            )
            latest = self.store.get_web_submission(int(submission_row_id)) or row
            if latest.get("judge_state") == "submitted" and latest.get("rid"):
                return latest
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise RealtimeJudgeTimeout(
                    f"Hydro rid not available before timeout; "
                    f"state={latest.get('judge_state')} error={latest.get('last_error', '')}"
                )
            self.sleeper(min(float(poll_interval), remaining))

    def ensure(
        self,
        submission_row_id: int,
        *,
        timeout_seconds: float = 120.0,
        poll_interval: float = 0.1,
    ) -> dict:
        """Compatibility-friendly short name for collection call sites."""
        return self.ensure_submitted(
            submission_row_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )

    def drain(self, limit: int = 100) -> list[dict]:
        """Process up to ``limit`` currently due rows."""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        processed: list[dict] = []
        for _ in range(limit):
            row = self.process_once()
            if row is None:
                break
            processed.append(row)
        return processed

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        idle_seconds: float = 0.5,
    ) -> None:
        """Run a lightweight background dispatcher until ``stop_event`` is set."""
        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive")
        self._worker_running = True
        try:
            while not stop_event.is_set():
                try:
                    row = self.process_once()
                    self._worker_last_ok_at = time.time()
                    self._worker_last_error = ""
                except Exception as exc:
                    self._worker_error_count += 1
                    self._worker_last_error = str(exc)
                    log.exception("real-time judge worker iteration failed")
                    stop_event.wait(idle_seconds)
                    continue
                if row is None:
                    stop_event.wait(idle_seconds)
        finally:
            self._worker_running = False
