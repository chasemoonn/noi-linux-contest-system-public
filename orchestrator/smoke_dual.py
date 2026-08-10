"""End-to-end smoke exercise using a synthetic roster, not real Hydro users."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from services.cloud import make_cvm
from services.config import load_config
from services.pipeline import Pipeline
from services.store import Store

TID = "ffffffffffffffffffffff01"
UNAME = "noi-smoke"
UID = 99999999


class SmokeHydro:
    def roster(self, tid):
        if tid != TID:
            return []
        return [{"uid": UID, "uname": UNAME}]


def build():
    cfg = load_config(os.environ.get("ORCHESTRATOR_CONFIG", "/app/config.yaml"))
    # The synthetic contest intentionally has no Hydro document. Exercise all
    # collection logic but do not create records for a fake contest id.
    cfg["hydro"]["submit_enabled"] = False
    store = Store(cfg["orchestrator"]["db"])
    pipe = Pipeline(cfg, make_cvm(cfg["cloud"]), SmokeHydro(), store, logging.getLogger("smoke"))
    return cfg, store, pipe


def prepare():
    cfg, store, pipe = build()
    try:
        store.upsert_contest(
            TID,
            "NOI dual submission smoke test",
            ["apple", "banana"],
            {},
            "both",
        )
        ip = pipe.prepare(TID)
        seat = store.seats(TID)[0]
        print(json.dumps({
            "tid": TID,
            "ip": ip,
            "container": seat["container"],
            "desktop_token": seat["token"],
            "submit_token": seat["submit_token"],
            "candidate": seat["candidate"],
        }))
    finally:
        store.close()


def collect():
    cfg, store, pipe = build()
    try:
        report = pipe.collect(TID)
        contest = store.get_contest(TID)
        message = contest["message"]
        marker = "报告位于 "
        output = message.split(marker, 1)[1].split("；", 1)[0] if marker in message else ""
        print(json.dumps({
            "state": contest["state"],
            "report": report,
            "output": output,
        }, ensure_ascii=False))
    finally:
        store.close()


def status():
    cfg, store, _ = build()
    try:
        contest = store.get_contest(TID)
        print(json.dumps({
            "contest": contest,
            "seats": store.seats(TID),
            "web": store.latest_web_submissions(TID, UID),
        }, ensure_ascii=False, default=str))
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "collect", "status"))
    globals()[parser.parse_args().action]()
