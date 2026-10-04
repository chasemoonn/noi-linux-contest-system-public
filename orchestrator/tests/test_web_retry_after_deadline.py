"""Real ASGI multipart requests plus SQLite; no OJ, cloud or live answers."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from services.realtime_judge import RealtimeJudge
from services.store import Store
from tests.test_realtime_judge import FakeSubmitter
from tests.test_workflow_ux import main, request


class WebReceiptReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / 'replay.db'))
        self.addCleanup(self.store.close)
        self.tid = '9' * 24
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.store.upsert_contest(self.tid, 'isolated retry fixture', ['apple'],
                                  {'apple': 'P1'}, 'both', begin_at_ms=now-60000,
                                  end_at_ms=now+600000, hydro_rule='oi')
        self.store.set_state(self.tid, 'ready')
        for uid, token in ((7, 'replay-seat'), (8, 'another-seat')):
            self.store.add_seat(self.tid, uid, f'user{uid}', f'desktop{uid}',
                                'fixture-only', token, f'CSP00{uid}', f'seat-{uid}',
                                f'172.18.0.{uid}')
        self.submitter = FakeSubmitter([{'ok': True, 'rid': '2' * 24}])
        self.judge = RealtimeJudge(self.store, self.submitter)
        for name, value in (('store', self.store), ('realtime_judge', self.judge)):
            patcher = mock.patch.object(main, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.source = 'int main(){return 0;}\n'

    def upload(self, nonce='a'*32, source=None, token='replay-seat'):
        text = self.source if source is None else source
        boundary = 'noi-replay-test-boundary'
        body = ''.join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            for key, value in (('problem', 'apple'), ('client_nonce', nonce))
        )
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="source"; '
                 f'filename="apple.cpp"\r\nContent-Type: text/plain\r\n\r\n{text}\r\n'
                 f'--{boundary}--\r\n')
        cookie = f'{main._submit_cookie_name(token)}={main._submit_cookie_value(token)}'
        return request(f'/submit/{token}', 'POST', body.encode(),
                       [(b'content-type', f'multipart/form-data; boundary={boundary}'.encode()),
                        (b'cookie', cookie.encode())])

    def close_contest(self):
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        with self.store._tx() as connection:
            connection.execute('UPDATE contests SET state=?,end_at_ms=? WHERE tid=?',
                               ('safe_wait', now-1000, self.tid))

    def test_accepted_multipart_retry_after_deadline_returns_original_receipt(self):
        self.assertEqual(self.upload()[0], 303)
        before = self.store.get_web_submission_by_nonce(self.tid, 7, 'apple', 'a'*32)
        self.close_contest()
        status, headers, _ = self.upload()
        self.assertEqual(status, 303)
        self.assertEqual(headers[b'location'], b'/submit/replay-seat?saved=apple')
        after = self.store.get_web_submission_by_nonce(self.tid, 7, 'apple', 'a'*32)
        self.assertEqual(before, after)

    def test_replaying_old_receipt_does_not_replace_newer_web_version(self):
        self.assertEqual(self.upload()[0], 303)
        self.assertEqual(self.upload('b'*32, 'int main(){return 1;}\n')[0], 303)
        latest = self.store.latest_web_submissions(self.tid, 7)['apple']
        self.close_contest()
        self.assertEqual(self.upload()[0], 303)
        self.assertEqual(self.store.latest_web_submissions(self.tid, 7)['apple'], latest)

    def test_new_nonce_after_deadline_is_rejected_without_creating_a_row(self):
        self.assertEqual(self.upload()[0], 303)
        self.close_contest()
        self.assertEqual(self.upload('b'*32)[0], 409)
        self.assertIsNone(self.store.get_web_submission_by_nonce(self.tid, 7, 'apple', 'b'*32))

    def test_changed_source_cannot_reuse_an_accepted_nonce_after_deadline(self):
        self.assertEqual(self.upload()[0], 303)
        before = self.store.latest_web_submissions(self.tid, 7)['apple']
        self.close_contest()
        self.assertEqual(self.upload(source='int main(){return 1;}\n')[0], 409)
        self.assertEqual(self.store.latest_web_submissions(self.tid, 7)['apple'], before)

    def test_other_student_cannot_reuse_a_receipt_nonce_after_deadline(self):
        self.assertEqual(self.upload()[0], 303)
        self.close_contest()
        self.assertEqual(self.upload(token='another-seat')[0], 409)
        self.assertEqual(self.store.latest_web_submissions(self.tid, 8), {})

    def test_delivered_retry_never_requeues_or_creates_another_oj_record(self):
        self.assertEqual(self.upload()[0], 303)
        self.judge.process_once()
        self.close_contest()
        self.assertEqual(self.upload()[0], 303)
        self.assertIsNone(self.judge.process_once())
        self.assertEqual(len(self.submitter.calls), 1)
