"""Read-only Hydro 5.0.1 Mongo access and login verification."""
from __future__ import annotations

from bson import ObjectId
from pymongo import MongoClient
import requests

CONTEST_DOCTYPE = 30
PROBLEM_DOCTYPE = 10


class Hydro:
    def __init__(self, base_url: str, mongo_uri: str, domain_id: str = "system"):
        self.base_url = base_url.rstrip("/")
        self.domain_id = domain_id
        self.client = MongoClient(
            mongo_uri,
            tz_aware=True,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
        self.db = self.client.get_default_database()

    @staticmethod
    def _oid(tid: str) -> ObjectId | None:
        try:
            return ObjectId(tid)
        except Exception:
            return None

    def ping(self) -> None:
        self.client.admin.command("ping")

    def get_contest(self, tid: str) -> dict | None:
        oid = self._oid(tid)
        if oid is None:
            return None
        return self.db["document"].find_one(
            {
                "domainId": self.domain_id,
                "docType": CONTEST_DOCTYPE,
                "docId": oid,
            }
        )

    def get_problem(self, pid: str | int) -> dict | None:
        text = str(pid).strip()
        query = {
            "domainId": self.domain_id,
            "docType": PROBLEM_DOCTYPE,
        }
        if text.isdigit():
            query["docId"] = int(text)
        else:
            query["pid"] = text
        return self.db["document"].find_one(
            query,
            {
                "docId": 1,
                "pid": 1,
                "title": 1,
                "content": 1,
                "config": 1,
                "data": 1,
                "additional_file": 1,
                "owner": 1,
            },
        )

    def roster(self, tid: str) -> list[dict]:
        """Return registered users from Hydro's document.status collection."""
        oid = self._oid(tid)
        if oid is None:
            return []
        statuses = self.db["document.status"].find(
            {
                "domainId": self.domain_id,
                "docType": CONTEST_DOCTYPE,
                "docId": oid,
                "uid": {"$type": "number", "$gt": 0},
            },
            {"uid": 1},
        )
        result = []
        seen = set()
        for status in statuses:
            uid = int(status["uid"])
            if uid in seen:
                continue
            seen.add(uid)
            user = self.db["user"].find_one({"_id": uid}, {"uname": 1}) or {}
            result.append({"uid": uid, "uname": user.get("uname", f"uid{uid}")})
        return sorted(result, key=lambda item: item["uname"].casefold())

    def verify_login(self, uname: str, password: str) -> bool:
        try:
            response = requests.post(
                f"{self.base_url}/login",
                data={"uname": uname, "password": password},
                allow_redirects=False,
                timeout=(5, 10),
            )
        except requests.RequestException:
            return False
        cookie = response.headers.get("set-cookie", "").lower()
        return response.status_code in (301, 302, 303) and "sid=" in cookie

    def close(self) -> None:
        self.client.close()
