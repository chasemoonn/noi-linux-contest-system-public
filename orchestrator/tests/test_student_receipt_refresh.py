"""Receipt refresh must stay private and never discard a selected code file."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock

from starlette.requests import Request
from tests.test_workflow_ux import main, request


class StudentReceiptRefreshTests(unittest.TestCase):
    def context(self, latest=None):
        return ({'tid':'a'*24, 'uid':7, 'submit_token':'test-token', 'candidate':'CSP007'},
                {'tid':'a'*24, 'title':'fixture', 'state':'safe_wait',
                 'begin_at_ms':100, 'end_at_ms':200, 'hydro_rule':'oi',
                 'submission_mode':'both'}, ['apple'], latest or {})

    def test_status_revision_changes_without_exposing_source_or_scores(self):
        latest = {'apple': {'id':1, 'judge_state':'pending', 'sha256':'a'*64,
                            'size':20, 'source':'PRIVATE SOURCE', 'score':100,
                            'rid':'private-record', 'vnc_pass':'private-pass'}}
        with mock.patch.object(main, '_web_submit_context', side_effect=lambda token:self.context(latest)), \
             mock.patch.object(main, '_submit_authenticated', return_value=True):
            first = json.loads(request('/submit/test-token/status')[2])
            latest['apple']['judge_state'] = 'submitted'
            second = json.loads(request('/submit/test-token/status')[2])
        self.assertRegex(first['receipt_revision'], r'^[0-9a-f]{64}$')
        self.assertNotEqual(first['receipt_revision'], second['receipt_revision'])
        self.assertEqual(set(second), {'server_ms','begin_ms','end_ms','opened','receipt_revision'})
        for private in ('PRIVATE SOURCE', 'private-record', 'private-pass', 'score'):
            self.assertNotIn(private, json.dumps(second))

    def test_saved_query_does_not_fabricate_a_receipt_for_an_unsubmitted_problem(self):
        req = Request({'type':'http','method':'GET','path':'/submit/test-token','headers':[]})
        with mock.patch.object(main, '_web_submit_context', return_value=self.context()), \
             mock.patch.object(main, '_submit_authenticated', return_value=True):
            response = main.web_submit_page(req, 'test-token', saved='apple')
        self.assertNotIn('apple.cpp 已被程序回收系统接收', response.body.decode())
        self.assertIn('data-receipt-revision=', response.body.decode())

    def test_upload_form_does_not_opt_into_receipt_page_refresh(self):
        page = main.WEB_EDIT_PAGE.render(seat=self.context()[0], problem='apple',
                                         client_nonce='a'*32, now='12:00:00')
        self.assertNotIn('data-receipt-revision=', page)
        self.assertIn('type="file"', page)

    def test_ambiguous_receipt_explains_that_it_will_not_be_resent(self):
        submission = {'created_at':1000, 'size':20, 'sha256':'a'*64,
                      'source':'int main(){}', 'judge_state':'ambiguous'}
        page = main.WEB_VIEW_PAGE.render(seat=self.context()[0], problem='apple',
                                         submission=submission)
        self.assertIn('系统不会自动重发不确定递交', page)
        self.assertIn('请联系老师核对 OJ 记录', page)

    @unittest.skipUnless(shutil.which('node'), 'Node required for rendered JavaScript behavior')
    def test_rendered_clock_script_preserves_upload_and_hidden_pages(self):
        page = main.templates.get_template('collection_clock.html').render(
            clock={'server_ms':1000,'begin_ms':0,'end_ms':60000,'opened':True},
            status_url='/submit/test-token/status', script_nonce='test-nonce')
        script = page.split('>',1)[1].rsplit('</script>',1)[0]
        runner = Path(__file__).parent / 'js' / 'collection-clock-behavior.cjs'
        result = subprocess.run([shutil.which('node'), str(runner)],
                                input=json.dumps({'script':script}), text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['cases_passed'], 8)
