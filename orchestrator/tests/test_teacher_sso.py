import base64
import hashlib
import hmac
import json
import unittest

from services.teacher_sso import (
    issue_session,
    verify_hydro_ticket,
    verify_session,
)


KEY = "shared-orchestrator-token-that-is-long-enough"
TID = "0123456789abcdef01234567"


def make_ticket(*, uid=42, tid=TID, issued_at=1_800_000_000, expires_at=1_800_000_060):
    payload = {
        "exp": expires_at,
        "iat": issued_at,
        "nonce": "abcdefghijklmnop",
        "tid": tid,
        "uid": uid,
        "uname": "coach",
        "v": 1,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(
        hmac.new(
            KEY.encode(),
            f"noi-teacher-ticket-v1:{encoded}".encode("ascii"),
            hashlib.sha256,
        ).digest()
    ).rstrip(b"=").decode()
    return f"{encoded}.{signature}"


class TeacherSsoTests(unittest.TestCase):
    def test_hydro_ticket_becomes_scoped_session(self):
        identity = verify_hydro_ticket(make_ticket(), KEY, now=1_800_000_010)
        self.assertEqual(identity.uid, 42)
        self.assertEqual(identity.uname, "coach")
        self.assertEqual(identity.tid, TID)
        session = issue_session(identity, KEY, now=1_800_000_010, lifetime=3600)
        restored = verify_session(session, KEY, now=1_800_000_100)
        self.assertEqual(restored, identity.__class__(42, "coach", TID, 1_800_003_610))

    def test_ticket_rejects_tampering_expiry_and_wrong_contest(self):
        ticket = make_ticket()
        with self.assertRaisesRegex(ValueError, "signature"):
            verify_hydro_ticket(ticket[:-1] + ("A" if ticket[-1] != "A" else "B"), KEY, now=1_800_000_010)
        with self.assertRaisesRegex(ValueError, "expired"):
            verify_hydro_ticket(ticket, KEY, now=1_800_000_061)
        with self.assertRaisesRegex(ValueError, "expired|invalid"):
            verify_hydro_ticket(make_ticket(tid="not-a-tid"), KEY, now=1_800_000_010)

    def test_session_rejects_wrong_key(self):
        identity = verify_hydro_ticket(make_ticket(), KEY, now=1_800_000_010)
        session = issue_session(identity, KEY, now=1_800_000_010)
        with self.assertRaisesRegex(ValueError, "signature"):
            verify_session(session, "different-shared-key-that-is-long-enough", now=1_800_000_020)


if __name__ == "__main__":
    unittest.main()
