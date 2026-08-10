"""Submit collected code to the Hydro orchestrator plugin."""
from __future__ import annotations

import hashlib
import json
import logging

import requests

log = logging.getLogger("orchestrator.submit")


class HydroSubmitter:
    def __init__(self, base_url: str, token: str, lang: str = ""):
        self.endpoint = f"{base_url.rstrip('/')}/orchestrator/submit"
        self.token = token
        self.lang = lang

    @staticmethod
    def submission_id(session: str, tid: str, uid: int, pid: str) -> str:
        """Return a stable id for one logical contest submission slot."""
        identity = json.dumps(
            [str(session), str(tid), int(uid), str(pid)],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def realtime_submission_id(
        session: str,
        tid: str,
        uid: int,
        pid: str,
        client_nonce: str,
    ) -> str:
        """Return a stable id for one explicit real-time submit action.

        The legacy :meth:`submission_id` deliberately represents one final
        problem slot. Including a browser nonce here lets later source versions
        create distinct Hydro records while a transport replay gets the same
        record.
        """
        identity = json.dumps(
            [
                "web-v1",
                str(session),
                str(tid),
                int(uid),
                str(pid),
                str(client_nonce),
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def submit_one(
        self,
        tid: str,
        uid: int,
        pid: str,
        code: str,
        submission_id: str,
        *,
        lang: str | None = None,
        submission_kind: str = "final",
        accepted_at_ms: int | None = None,
    ) -> dict:
        if submission_kind not in {"final", "realtime"}:
            raise ValueError("submission_kind must be final or realtime")
        data = {
            "tid": tid,
            "uid": uid,
            "pid": pid,
            "code": code,
            "submission_id": submission_id,
            "submission_kind": submission_kind,
        }
        selected_lang = self.lang if lang is None else str(lang)
        if selected_lang:
            data["lang"] = selected_lang
        if accepted_at_ms is not None:
            data["accepted_at_ms"] = int(accepted_at_ms)
        try:
            response = requests.post(
                self.endpoint,
                data=data,
                headers={"X-Orchestrator-Token": self.token},
                timeout=(5, 30),
            )
        except requests.RequestException as exc:
            return {"ok": False, "retryable": True, "error": str(exc)}
        try:
            payload = response.json()
        except ValueError:
            payload = {"text": response.text[:300]}
        if response.ok and payload.get("rid"):
            result = {
                "ok": True,
                "retryable": False,
                "rid": str(payload["rid"]),
            }
        else:
            # A nominally successful response without a rid is an incomplete
            # acknowledgement. Retrying is safe because submission_id is
            # stable and the plugin returns the original record on replay.
            retryable = (
                response.ok
                # A rolling deployment can briefly expose a mismatched
                # orchestrator token. Keep the durable outbox item retryable
                # so fixing the shared secret recovers it automatically.
                or response.status_code in {401, 403, 408, 425, 429}
                or response.status_code >= 500
            )
            result = {
                "ok": False,
                "retryable": retryable,
                "status_code": response.status_code,
                "error": payload,
            }
        log.info("submit uid=%s pid=%s result=%s", uid, pid, result)
        return result
