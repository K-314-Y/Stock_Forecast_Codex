"""Synthetic offline publication/state/clock tests; every network call is injected."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from datetime import datetime as RealDatetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import monitor_jpx_publication as m

URL = 'https://www.jpx.co.jp/observed/listed-month.json'
NOW = m.DEADLINE - timedelta(hours=5)


def response(rows=(), status=200, body=None, **headers):
    return status, {'content-type': 'binary/octet-stream', **headers}, (
        json.dumps({'TableDatas': list(rows)}).encode() if body is None else body)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'state/jpx_publication_monitor' / m.SESSION
        (self.root / 'config').mkdir()
        self.policy = {'permanently_not_run_sessions': [m.SESSION], 'schedules_enabled': False,
                       'raw_provider_persistence': 'DISABLED', 'historical_snapshot': 'data/history.json'}
        (self.root / 'config/forward_trial_execution_policy.json').write_bytes(m.canonical(self.policy))
        self.calls = []

    def run_check(self, answer=None, now=NOW, **kwargs):
        def fetch(url):
            self.calls.append(url)
            self.assertEqual(url, URL)
            self.assertTrue(url.endswith('.json'))
            if isinstance(answer, Exception):
                raise answer
            return answer or response()
        return m.check_once(self.root, now=now, fetch=fetch,
                            context_check=lambda *_: (URL, 'a' * 40), **kwargs)

    def test_absent_normal_and_only_lightweight_state(self):
        result = self.run_check()
        self.assertEqual(result['result_state'], m.ABSENT)
        self.assertEqual(result['http_status'], 200)
        self.assertFalse(result['publication_found'])
        self.assertEqual(self.calls, [URL])
        self.assertEqual(set(p.name for p in self.folder.iterdir()), {'.lock', 'latest.json'})
        self.assertFalse((self.root / 'data').exists())
        self.assertEqual(json.loads((self.root / 'config/forward_trial_execution_policy.json').read_bytes()), self.policy)

    def test_present_exact_observed_href_no_pdf_request(self):
        row = {'TradeDate': '20261013', 'Stocks': '/published/unrelated-name.pdf'}
        result = self.run_check(response([row]))
        self.assertEqual(result['result_state'], m.DETECTED)
        self.assertEqual(result['source_identity']['publication_metadata']['exact_stocks_href'], row['Stocks'])
        self.assertEqual(self.calls, [URL])
        self.run_check(response(), now=NOW + timedelta(hours=1))
        self.assertEqual(self.calls, [URL])

    def test_empty_stock_and_other_date_absent(self):
        for value in ('', '-', None):
            self.assertIsNone(m.publication(*response([{'TradeDate': '20261013', 'Stocks': value}]), URL))
        self.assertIsNone(m.publication(*response([{'TradeDate': '20261009', 'Stocks': '/old.pdf'}]), URL))

    def test_before_cutoff_checks_publication(self):
        self.assertEqual(self.run_check(now=m.DEADLINE - timedelta(microseconds=1))['result_state'], m.ABSENT)
        self.assertEqual(len(self.calls), 1)

    def test_exact_cutoff_zero_network_and_permanent_exclusion(self):
        result = self.run_check(now=m.DEADLINE)
        self.assertEqual(result['result_state'], m.EXPIRED)
        self.assertTrue(result['deadline_expired'])
        self.assertEqual(self.calls, [])
        self.assertEqual(json.loads((self.root / 'config/forward_trial_execution_policy.json').read_bytes()), self.policy)
        self.assertEqual(m.not_run.sessions(self.root, self.policy), {m.SESSION, m.TARGET})

    def test_after_cutoff_zero_network(self):
        self.assertEqual(self.run_check(now=m.DEADLINE + timedelta(seconds=1))['result_state'], m.EXPIRED)
        self.assertEqual(self.calls, [])

    def test_expired_state_idempotent_no_policy_or_state_noise(self):
        self.run_check(now=m.DEADLINE)
        before = m.digest(self.folder / 'latest.json')
        policy = m.digest(self.root / 'config/forward_trial_execution_policy.json')
        self.run_check(now=m.DEADLINE + timedelta(days=1))
        self.assertEqual(before, m.digest(self.folder / 'latest.json'))
        self.assertEqual(policy, m.digest(self.root / 'config/forward_trial_execution_policy.json'))
        self.assertEqual(self.calls, [])

    def test_detection_still_requires_completed_history_at_deadline(self):
        self.run_check(response([{'TradeDate': '20261013', 'Stocks': '/observed.pdf'}]))
        result = self.run_check(now=m.DEADLINE)
        self.assertEqual(result['result_state'], m.EXPIRED)
        self.assertTrue(result['publication_found'])
        self.assertEqual(self.calls, [URL])

    def test_completed_history_deadline_does_not_exclude_target(self):
        result = self.run_check(now=m.DEADLINE, history_check=lambda *_: True)
        self.assertEqual(result['result_state'], m.DETECTED)
        self.assertEqual(json.loads((self.root / 'config/forward_trial_execution_policy.json').read_bytes()), self.policy)
        self.assertEqual(self.calls, [])

    def test_malformed_completion_receipt_fails_closed(self):
        self.folder.mkdir(parents=True)
        (self.folder / 'task10c6c_completion.json').write_text('{')
        self.assertEqual(self.run_check(now=m.DEADLINE)['result_state'], m.EXPIRED)

    def test_security_statuses_stop_once(self):
        for status, reason in [(401, 'AUTHENTICATION_CHALLENGE'), (403, 'HTTP_403'), (429, 'RATE_LIMIT')]:
            with self.subTest(status=status):
                (self.folder / 'latest.json').unlink(missing_ok=True)
                (self.folder / 'STOP.json').unlink(missing_ok=True)
                result = self.run_check(response(status=status))
                self.assertEqual(result['result_state'], m.STOP)
                self.assertEqual(result['stop_reason'], reason)
                count = len(self.calls)
                self.run_check(now=NOW + timedelta(hours=1))
                self.assertEqual(len(self.calls), count)

    def test_challenge_stop(self):
        result = self.run_check(response(body=b'<title>Security challenge CAPTCHA</title>'))
        self.assertEqual(result['result_state'], m.STOP)
        self.assertEqual(result['stop_reason'], 'SECURITY_CHALLENGE')

    def test_external_redirect_is_not_followed(self):
        result = self.run_check(response(status=302, location='https://evil.test/a.json'))
        self.assertEqual(result['stop_reason'], 'UNEXPECTED_REDIRECT')
        self.assertEqual(self.calls, [URL])

    def test_malformed_json_duplicate_keys_and_schema_stop(self):
        for body in (b'{', b'{}', b'{"TableDatas":[],"TableDatas":[]}', b'{"TableDatas":{}}',
                     b'{"TableDatas":[{"TradeDate":"20260230","Stocks":"/x.pdf"}]}'):
            with self.subTest(body=body):
                (self.folder / 'latest.json').unlink(missing_ok=True)
                (self.folder / 'STOP.json').unlink(missing_ok=True)
                self.assertEqual(self.run_check(response(body=body))['result_state'], m.STOP)

    def test_ambiguous_or_external_publication_is_stop(self):
        row = {'TradeDate': '20261013', 'Stocks': '/observed.pdf'}
        with self.assertRaisesRegex(ValueError, 'AMBIGUOUS'):
            m.publication(*response([row, row]), URL)
        with self.assertRaisesRegex(ValueError, 'IDENTITY'):
            m.publication(*response([dict(row, Stocks='https://evil.test/a.pdf')]), URL)

    def test_duplicate_invocation_and_hourly_guard(self):
        self.run_check()
        before = m.digest(self.folder / 'latest.json')
        self.run_check(now=NOW + timedelta(minutes=59, seconds=59))
        self.assertEqual(self.calls, [URL])
        self.assertEqual(before, m.digest(self.folder / 'latest.json'))
        self.run_check(now=NOW + timedelta(hours=1))
        self.assertEqual(self.calls, [URL, URL])

    def test_concurrent_invocations_make_one_get(self):
        gate = threading.Barrier(2)
        def invoke():
            gate.wait()
            return self.run_check()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: invoke(), range(2)))
        self.assertEqual(self.calls, [URL])
        self.assertTrue(all(r['result_state'] == m.ABSENT for r in results))

    def test_interrupted_get_is_not_retried(self):
        self.run_check()
        record = m.read_state(self.folder / 'latest.json')
        record['result_state'] = m.PENDING
        m.write_state(self.folder / 'latest.json', record)
        result = self.run_check(now=NOW + timedelta(hours=1))
        self.assertEqual(result['stop_reason'], 'INTERRUPTED_CHECK_REVIEW_REQUIRED')
        self.assertEqual(self.calls, [URL])

    def test_corrupt_state_permanent_stop_without_network(self):
        self.folder.mkdir(parents=True)
        (self.folder / 'latest.json').write_text('{')
        self.assertEqual(self.run_check()['result_state'], m.STOP)
        self.assertEqual(self.run_check(now=NOW + timedelta(hours=1))['result_state'], m.STOP)
        self.assertEqual(self.calls, [])

    def test_security_stop_then_expiry_idempotent(self):
        self.run_check(response(status=403))
        self.assertEqual(self.run_check(now=m.DEADLINE)['result_state'], m.EXPIRED)
        before = m.digest(self.folder / 'latest.json')
        self.run_check(now=m.DEADLINE + timedelta(hours=1))
        self.assertEqual(before, m.digest(self.folder / 'latest.json'))
        self.assertEqual(len(self.calls), 1)

    def test_transport_error_stops_automatic_requests(self):
        self.assertEqual(self.run_check(TimeoutError())['result_state'], m.STOP)
        self.run_check(now=NOW + timedelta(hours=1))
        self.assertEqual(len(self.calls), 1)

    def test_clock_rollback_stop(self):
        self.run_check()
        self.assertEqual(self.run_check(now=NOW - timedelta(seconds=1))['stop_reason'], 'MONITOR_CLOCK_ROLLBACK')
        self.assertEqual(len(self.calls), 1)

    def test_naive_clock_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'TIMEZONE_REQUIRED'):
            self.run_check(now=NOW.replace(tzinfo=None))

    def test_policy_failure_does_not_allow_late_network(self):
        (self.root / 'config/forward_trial_execution_policy.json').write_text('{')
        result = self.run_check(now=m.DEADLINE)
        self.assertEqual(result['result_state'], m.STOP)
        self.assertFalse(result['deadline_expired'])
        self.assertEqual(self.calls, [])

    def test_no_execution_imports_or_data_writes_in_fresh_process(self):
        code = '''
import sys
def audit(event, args):
    if event == 'import' and args[0].startswith(('engine.', 'providers.', 'models.')):
        raise RuntimeError('Execution/provider import forbidden: ' + args[0])
sys.addaudithook(audit)
from scripts import monitor_jpx_publication as m
from pathlib import Path
from datetime import timedelta
root=Path(sys.argv[1])
r=m.check_once(root, now=m.DEADLINE-timedelta(hours=3),
    context_check=lambda *_: ('https://www.jpx.co.jp/only.json', 'a'*40),
    fetch=lambda *_: (200, {'content-type':'application/json'}, b'{"TableDatas":[]}'))
assert r['result_state']==m.ABSENT
assert not (root/'data').exists()
'''
        completed = subprocess.run([sys.executable, '-c', code, str(self.root)],
                                   cwd=m.ROOT, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_source_identity_rejected_before_get(self):
        with self.assertRaises(ValueError):
            m.get_monthly('https://evil.test/a.json')
        with self.assertRaises(ValueError):
            m.get_monthly('https://www.jpx.co.jp/a.pdf')

    def test_context_conflict_never_gets(self):
        def bad(*_):
            raise ValueError('MONITOR_PROTECTED_CONTEXT_CHANGED')
        def forbidden(*_):
            self.fail('GET must not happen after context conflict')
        result = m.check_once(self.root, now=NOW, context_check=bad, fetch=forbidden)
        self.assertEqual(result['result_state'], m.STOP)

    def test_cutoff_crossed_during_context_validation_no_get(self):
        before = m.DEADLINE - timedelta(microseconds=1)
        class Clock(RealDatetime):
            samples = iter([before, m.DEADLINE])
            @classmethod
            def now(cls, tz=None):
                return next(cls.samples)
        with patch.object(m, 'datetime', Clock):
            result = m.check_once(self.root, context_check=lambda *_: (URL, 'a'*40),
                                  fetch=lambda *_: self.fail('No GET at cutoff'))
        self.assertEqual(result['result_state'], m.EXPIRED)
        entry = m.not_run.strict_json((self.root / 'state/forecast_not_run_sessions' / (m.TARGET+'.json')).read_bytes())
        self.assertEqual(entry['record']['recorded_at'],m.DEADLINE.isoformat())

    def test_response_completes_at_cutoff_no_readiness(self):
        before = m.DEADLINE - timedelta(seconds=1)
        class Clock(RealDatetime):
            samples = iter([before, before, m.DEADLINE])
            @classmethod
            def now(cls, tz=None):
                return next(cls.samples)
        with patch.object(m, 'datetime', Clock):
            result = m.check_once(self.root, context_check=lambda *_: (URL, 'a'*40),
                fetch=lambda *_: response([{'TradeDate':'20261013','Stocks':'/exact.pdf'}]))
        self.assertEqual(result['result_state'], m.EXPIRED)
        entry = m.not_run.strict_json((self.root / 'state/forecast_not_run_sessions' / (m.TARGET+'.json')).read_bytes())
        self.assertEqual(entry['record']['recorded_at'],m.DEADLINE.isoformat())

    def test_terminal_cli_disables_poll_and_retains_only_deadline_guard(self):
        for state, expired in [(m.DETECTED, False), (m.STOP, False), (m.EXPIRED, True)]:
            with self.subTest(state=state), patch.object(sys, 'argv', ['monitor', '--session', m.SESSION,
                 '--deadline', m.DEADLINE.isoformat(), '--once', '--scheduled']), \
                 patch.object(m, 'check_once', return_value={'result_state':state,'deadline_expired':expired}), \
                 patch.object(m, 'disable_timers') as disable, patch.object(m.subprocess, 'run'), \
                 patch('builtins.print'):
                m.main()
                disable.assert_called_once_with(all_timers=expired)

    def test_verified_official_context_and_changed_protection(self):
        source = self.root / 'discovery.json'
        source.write_bytes(m.canonical({'session':m.SESSION,'pdf_requests':0,
            'schema_version':'jpx-json-discovery-v1','monthly_json_reference_url':URL}))
        calendar = self.root / 'calendar.json'
        calendar.write_bytes(m.canonical({'jp_sessions':[m.SESSION,m.TARGET]}))
        self.policy.update(calendar='calendar.json',calendar_sha256=m.digest(calendar))
        policy_path=self.root / 'config/forward_trial_execution_policy.json'
        policy_path.write_bytes(m.canonical(self.policy))
        context={'target_session':m.SESSION,'deadline':m.DEADLINE.isoformat(),'monthly_url':URL,
                 'discovery_reference':'discovery.json','protected_references':{
                     'discovery.json':m.digest(source),'config/forward_trial_execution_policy.json':m.digest(policy_path)}}
        m.write_state(self.folder/'context.json',context)
        for relative in m.CODE_PATHS:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# synthetic monitor code\n')
        m.write_state(self.folder/'armed.json',{'code_commit':'a'*40,'target_session':m.SESSION,
            'deadline':m.DEADLINE.isoformat(),
            'code_hashes':{relative:m.digest(self.root / relative) for relative in m.CODE_PATHS}})
        def git(command, **kwargs):
            return m.BRANCH+'\n' if command[-1]=='--show-current' else 'a'*40+'\n'
        with patch.object(m.subprocess,'check_output',side_effect=git):
            self.assertEqual(m.verify_context(self.root,self.folder),(URL,'a'*40))
            source.write_text('{}')
            with self.assertRaisesRegex(ValueError,'PROTECTED_CONTEXT_CHANGED'):
                m.verify_context(self.root,self.folder)


if __name__ == '__main__':
    unittest.main()
