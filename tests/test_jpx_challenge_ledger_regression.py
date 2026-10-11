"""F1--F4 production-entry regressions; synthetic fixtures and temporary state only."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import multiprocessing
from pathlib import Path
import unittest
from unittest.mock import patch

import test_jpx_policy_v2 as fixtures
from test_jpx_policy_v2 import Transport, response, html, SESSION, OTHER, NOW, PDF_URL
from providers.jp.jpx_manual import historical_direct as d
from providers.jp.jpx_manual import historical_policy_v2 as policy


class FinalAdversarialTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        fixture = fixtures.HistoricalPolicyTests()
        fixture.setUp()
        try:
            yield fixture
        finally:
            fixture.doCleanups()

    def assert_challenge(self, fragment, *, pdf=False):
        with self.fixture() as fixture:
            body = fixture.pdf + fragment if pdf else html().replace(b'</html>', fragment + b'</html>')
            values = ([response(), response(body, PDF_URL, kind='application/pdf')] if pdf
                      else [response(body)])
            transport = Transport(values)
            with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
                d.acquire_one(fixture.root, SESSION, transport=transport)
            self.assertEqual(transport.calls, [d.LISTING, PDF_URL] if pdf else [d.LISTING])
            active = policy.Stops(fixture.store).index['active']
            self.assertEqual(len(active), 1)
            record = policy.read_reference(active[0]['reference'])
            self.assertEqual((record['stop_class'], record['stop_code'], record['scope']),
                             ('HARD_STOP', 'SECURITY_CHALLENGE', 'host'))
            self.assertEqual(record['retry_count'], 0)
            self.assertEqual(record['evidence']['body_reference']['sha256'], hashlib.sha256(body).hexdigest())
            restart = Transport([])
            with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
                d.acquire_one(fixture.root, OTHER, transport=restart, run_state=d.RunState('restart'))
            self.assertEqual(restart.calls, [])

    def assert_normal(self, fragment):
        with self.fixture() as fixture:
            body = html().replace(b'</html>', fragment + b'</html>')
            transport = Transport([response(body), response(fixture.pdf, PDF_URL, kind='application/pdf')])
            result = d.acquire_one(fixture.root, SESSION, transport=transport)
            self.assertEqual(result['state'], 'PASS')
            self.assertEqual(transport.calls, [d.LISTING, PDF_URL])
            self.assertEqual(policy.Stops(fixture.store).index['active'], [])
            self.assertEqual(d.acquire_one(fixture.root, SESSION, transport=Transport([]))['web_requests'], 0)

    def test_f1_br_fragmented_challenge_stops_before_pdf(self):
        self.assert_challenge(b'<div role="alert">Access<br>restricted</div>'
                              b'<h2>Verification<br>required</h2><button>Verify</button>')

    def test_f1_nested_and_whitespace_challenge_matrix(self):
        for fragment in (
            b'<div role="alert"><span>Access</span> restricted</div>'
            b'<h2><strong>Verification</strong> required</h2><button><span>Verify</span></button>',
            b'<div ROLE=" AlErT ">Access\n restricted</div>'
            b'<h2>Verification\n\t required</h2><input TYPE="SuBmIt" value="Verify">',
            b'<div role="alert">Access<br/>restricted</div><h2>Verification required</h2>'
            b'<button><span>Ver</span><strong>ify</strong></button>',
        ):
            with self.subTest(fragment=fragment):
                self.assert_challenge(fragment)

    def test_f1_fragmented_explanation_is_normal(self):
        self.assert_normal(b'<h2><span>CAPTCHA</span><br><strong>support information</strong></h2>'
                           b'<p>Verification\n documentation</p><button>Continue reading</button>')

    def test_f4_descriptive_identifier_matrix_is_normal(self):
        for identifier in ('captcha-explanation', 'captcha-help', 'captcha-documentation',
                           'security-information', 'verification-guide', 'challenge-description',
                           'g-recaptcha-documentation'):
            with self.subTest(identifier=identifier):
                self.assert_normal(('<h2 id="' + identifier + '" class="' + identifier
                                    + '">CAPTCHA support information</h2>').encode())

    def test_f4_description_heading_and_normal_continue_is_normal(self):
        self.assert_normal(b'<h2>CAPTCHA support information</h2><button>Continue</button>')

    def test_f3_production_listing_description_matrix_is_normal(self):
        for fragment in (
            b'<h1>CAPTCHA explanation</h1>',
            b'<title>Security challenge documentation</title>',
            b'<form>This form describes CAPTCHA verification.</form>',
            b'<h1>Security challenge documentation</h1>',
            b'<h2>Verification documentation</h2><button>Continue reading</button>',
            b'<h2>CAPTCHA documentation</h2><p>CAPTCHA support and security guide.</p>',
            b'<form id="captcha-help"><input type="submit" value="Continue reading"></form>',
            b'<h2>Verification guide</h2><p>Challenge description.</p>',
            b'<form action="/docs/captcha-explanation">CAPTCHA help.</form>'
            b'<iframe src="/docs/captcha-help"></iframe>'
            b'<script src="/assets/captcha-documentation.js"></script>',
        ):
            with self.subTest(fragment=fragment):
                self.assert_normal(fragment)

    def test_strong_widget_positive_matrix(self):
        for fragment in (
            b'<div class="g-recaptcha"></div>', b'<div class="h-captcha"></div>',
            b'<div CLASS="ordinary CF-Turnstile"></div>',
            b'<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>',
            b'<script src="https://js.hcaptcha.com/1/api.js"></script>',
            b'<iframe src="https://challenges.cloudflare.com/turnstile/v0/api.js"></iframe>',
            b'<form id="captcha"><input name="captcha-response"></form>',
            b'<form action="/security-challenge"><input type="submit" value="Continue"></form>',
            b'<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>',
            b' ' * 70000 + b'<div class="g-recaptcha"></div>',
        ):
            with self.subTest(fragment=fragment[:100]):
                self.assert_challenge(fragment)

    def test_pdf_magic_with_strong_challenge_tail_stops(self):
        self.assert_challenge(b' ' * 70000 + b'<div class="h-captcha"></div>', pdf=True)

    def test_normal_jpx_html_and_pdf(self):
        self.assert_normal(b'')

    def interrupt_discovery(self, fixture, name, values):
        transport = Transport(values)
        with patch.object(d, 'json_endpoints', side_effect=KeyboardInterrupt('before result completion')):
            with self.assertRaises(KeyboardInterrupt):
                d.discover_json_one(fixture.root, SESSION, transport=transport,
                    evidence_folder=fixture.root / name, run_state=d.RunState(name))
        return transport.calls

    def test_f2_timeout_200_crash_restart_next_day_has_no_fourth_attempt(self):
        with self.fixture() as fixture:
            calls = self.interrupt_discovery(fixture, 'first', [TimeoutError('transient'), response()])
            calls += self.interrupt_discovery(fixture, 'second', [response()])
            before = policy.Stops(fixture.store).index['retry_targets'][SESSION]
            self.assertEqual(next(iter(before['requests'].values()))['state'], 'RESPONSE_RECEIVED')
            self.assertEqual(len(calls), 3)
            fourth = Transport([response()])
            with patch.object(d, '_now', return_value=NOW + timedelta(days=1)):
                with self.assertRaisesRegex(ValueError, 'RETRY_EXHAUSTED'):
                    d.discover_json_one(fixture.root, SESSION, transport=fourth,
                        evidence_folder=fixture.root / 'third', run_state=d.RunState('new-day'))
                fresh = Transport([response(html(SESSION))])
                result = d.acquire_one(fixture.root, OTHER, transport=fresh, run_state=d.RunState('fresh-session'))
                self.assertEqual(result['status'], 'NOT_YET_AVAILABLE')
                self.assertEqual(fresh.calls, [d.LISTING])
            self.assertEqual(fourth.calls, [])
            after = policy.Stops(fixture.store).index['retry_targets'][SESSION]
            self.assertEqual(after['requests'], before['requests'])
            self.assertEqual(next(iter(after['requests'].values()))['attempts_started'], 3)

    def test_all_unfinished_and_unknown_states_survive_rollover(self):
        for state in ('PENDING', 'IN_FLIGHT', 'RESPONSE_RECEIVED', 'RETRY', 'RETRY_EXHAUSTED', None, 'UNKNOWN'):
            with self.subTest(state=state), self.fixture() as fixture:
                self.interrupt_discovery(fixture, 'first', [response()])
                with fixtures.frozen._locked(fixture.store):
                    stops = policy.Stops(fixture.store)
                    budget = stops.index['retry_targets'][SESSION]
                    operation = next(iter(budget['requests'].values()))
                    operation['attempts_started'] = 3
                    if state is None:
                        operation.pop('state')
                    else:
                        operation['state'] = state
                    budget['in_flight'] = state == 'IN_FLIGHT'
                    budget['exhausted'] = state == 'RETRY_EXHAUSTED'
                    stops.save()
                transport = Transport([])
                with patch.object(d, '_now', return_value=NOW + timedelta(days=1)):
                    with self.assertRaisesRegex(ValueError, 'RETRY_(EXHAUSTED|INTERRUPTED_REVIEW_REQUIRED)'):
                        d.discover_json_one(fixture.root, SESSION, transport=transport,
                            evidence_folder=fixture.root / 'restart', run_state=d.RunState('restart'))
                self.assertEqual(transport.calls, [])
                ledger = policy.Stops(fixture.store).index['retry_targets'][SESSION]
                self.assertEqual(next(iter(ledger['requests'].values()))['attempts_started'], 3)

    def test_legacy_summary_without_request_ledger_is_not_fresh_next_day(self):
        with self.fixture() as fixture:
            self.interrupt_discovery(fixture, 'first', [TimeoutError('transient'), response()])
            with fixtures.frozen._locked(fixture.store):
                stops = policy.Stops(fixture.store)
                stops.index['retry_targets'][SESSION].pop('requests')
                stops.save()
            transport = Transport([])
            with patch.object(d, '_now', return_value=NOW + timedelta(days=1)):
                with self.assertRaisesRegex(ValueError, 'RETRY_INTERRUPTED_REVIEW_REQUIRED'):
                    d.discover_json_one(fixture.root, SESSION, transport=transport,
                        evidence_folder=fixture.root / 'restart', run_state=d.RunState('restart'))
            self.assertEqual(transport.calls, [])

    def test_completed_operation_can_have_a_later_normal_observation(self):
        with self.fixture() as fixture:
            first = Transport([response(html(OTHER))])
            self.assertEqual(d.acquire_one(fixture.root, SESSION, transport=first)['status'], 'NOT_YET_AVAILABLE')
            old = policy.Stops(fixture.store).index['retry_targets'][SESSION]
            self.assertTrue(all(item['state'] == 'COMPLETED' for item in old['requests'].values()))
            with patch.object(d, '_now', return_value=NOW + timedelta(days=1)):
                later = Transport([response(html(OTHER))])
                self.assertEqual(d.acquire_one(fixture.root, SESSION, transport=later)['status'], 'NOT_YET_AVAILABLE')
            self.assertEqual(later.calls, [d.LISTING])
            new = policy.Stops(fixture.store).index['retry_targets'][SESSION]
            self.assertEqual(new['day'], (NOW + timedelta(days=1)).date().isoformat())
            self.assertEqual(next(iter(new['requests'].values()))['attempts_started'], 1)

    def test_completed_label_without_result_reference_is_not_fresh_next_day(self):
        with self.fixture() as fixture:
            self.interrupt_discovery(fixture, 'first', [response()])
            with fixtures.frozen._locked(fixture.store):
                stops = policy.Stops(fixture.store)
                operation = next(iter(stops.index['retry_targets'][SESSION]['requests'].values()))
                operation.update(state='COMPLETED', attempts_started=3)
                stops.save()
            transport = Transport([])
            with patch.object(d, '_now', return_value=NOW + timedelta(days=1)):
                with self.assertRaisesRegex(ValueError, 'RETRY_INTERRUPTED_REVIEW_REQUIRED'):
                    d.discover_json_one(fixture.root, SESSION, transport=transport,
                        evidence_folder=fixture.root / 'restart', run_state=d.RunState('restart'))
            self.assertEqual(transport.calls, [])
            ledger = policy.Stops(fixture.store).index['retry_targets'][SESSION]
            self.assertEqual(next(iter(ledger['requests'].values()))['attempts_started'], 3)

    def test_corrupt_completion_evidence_cannot_reset_next_day_budget(self):
        with self.fixture() as fixture:
            d.acquire_one(fixture.root, SESSION, transport=Transport([response(html(OTHER))]))
            ledger = policy.Stops(fixture.store).index['retry_targets'][SESSION]
            operation = next(iter(ledger['requests'].values()))
            reference = operation['result_reference']
            Path(reference['path']).write_bytes(b'corrupt completion evidence')
            transport = Transport([])
            with patch.object(d, '_now', return_value=NOW + timedelta(days=1)):
                with self.assertRaises(ValueError):
                    d.discover_json_one(fixture.root, SESSION, transport=transport,
                        evidence_folder=fixture.root / 'restart', run_state=d.RunState('restart'))
            self.assertEqual(transport.calls, [])
            self.assertEqual(policy.Stops(fixture.store).index['retry_targets'][SESSION], ledger)

    def test_concurrent_day_boundary_restarts_share_three_attempt_limit(self):
        with self.fixture() as fixture:
            first = self.interrupt_discovery(fixture, 'first', [TimeoutError('transient'), response()])
            ctx = multiprocessing.get_context('fork')
            ready, count, results = ctx.Event(), ctx.Value('i', 0), ctx.Queue()
            def worker(number):
                class Counted(Transport):
                    def get(self, url, *, limit):
                        with count.get_lock():
                            count.value += 1
                        return super().get(url, limit=limit)
                ready.wait(10)
                try:
                    with patch.object(d, '_now', return_value=NOW + timedelta(days=number)), \
                            patch.object(d, 'json_endpoints', side_effect=KeyboardInterrupt('not finalized')):
                        d.discover_json_one(fixture.root, SESSION, transport=Counted([response()]),
                            evidence_folder=fixture.root / ('worker-' + str(number)),
                            run_state=d.RunState('worker-' + str(number)))
                except (Exception, KeyboardInterrupt) as error:
                    results.put(str(error))
            processes = [ctx.Process(target=worker, args=(number,)) for number in (0, 1)]
            try:
                for process in processes:
                    process.start()
                ready.set()
                outcomes = [results.get(timeout=15) for _ in processes]
                for process in processes:
                    process.join(15)
                    self.assertEqual(process.exitcode, 0)
                self.assertCountEqual(outcomes, ['not finalized', 'RETRY_EXHAUSTED'])
                self.assertEqual(len(first) + count.value, 3)
                budget = policy.Stops(fixture.store).index['retry_targets'][SESSION]
                self.assertEqual(budget['day'], NOW.date().isoformat())
                self.assertEqual(next(iter(budget['requests'].values()))['attempts_started'], 3)
                self.assertEqual(policy.Stops(fixture.store).index['active'], [])
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
                results.close()


if __name__ == '__main__':
    unittest.main()
