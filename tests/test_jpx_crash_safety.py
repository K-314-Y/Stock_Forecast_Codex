"""Offline, production-entry reproductions of the second regression review."""
from datetime import timedelta
from io import BytesIO
import http.client
import multiprocessing
from pathlib import Path
import unittest
from unittest.mock import patch

import test_jpx_policy_v2 as fixtures
import test_jpx_policy_v2_regression as regression
from test_jpx_policy_v2 import Transport, response, html, SESSION, OTHER, NOW, PDF_URL
from providers.jp.jpx_manual import historical_direct as d
from providers.jp.jpx_manual import historical_policy_v2 as policy


class CrashSafetyTests(unittest.TestCase):
    setUp = regression.ReviewRegressionTests.setUp
    assert_hard = regression.ReviewRegressionTests.assert_hard

    def test_restriction_heading_and_verification_button_stop_before_pdf(self):
        challenge = (b'<div role="alert">Access restricted</div>'
                     b'<h2>Verification required</h2><button>Verify</button>')
        transport = Transport([response(html() + challenge)])
        with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assert_hard('SECURITY_CHALLENGE', 'host')
        self.assertEqual(transport.calls, [d.LISTING])
        self.f.sleep.assert_not_called()

    def test_valid_pdf_late_widget_is_not_hidden_by_magic_or_prefix(self):
        writer, buffer = fixtures.PdfWriter(), BytesIO()
        writer.add_blank_page(width=200, height=200)
        writer.add_metadata({'/Title': 'ordinary information ' * 5000})
        writer.write(buffer)
        body = buffer.getvalue() + b'\n<div class="g-recaptcha" data-sitekey="test"></div>'
        self.assertGreater(body.index(b'g-recaptcha'), 65536)
        self.assertEqual(len(d.PdfReader(BytesIO(body), strict=True).pages), 1)
        transport = Transport([response(), response(body, PDF_URL, kind='application/pdf')])
        with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assert_hard('SECURITY_CHALLENGE', 'host')
        self.assertEqual(transport.calls, [d.LISTING, PDF_URL])

    def test_normal_pdf_html_and_security_prose_are_not_challenges(self):
        prose = (b'<h2>CAPTCHA support information</h2><p>Verification and CAPTCHA are '
                 b'described in this security document.</p><button>Continue reading</button>')
        transport = Transport([response(html() + prose),
                               response(self.f.pdf, PDF_URL, kind='application/pdf')])
        self.assertEqual(d.acquire_one(self.f.root, SESSION, transport=transport)['state'], 'PASS')
        self.assertEqual(transport.calls, [d.LISTING, PDF_URL])
        self.assertEqual(policy.Stops(self.f.store).index['active'], [])
        for description in (b'<h1>CAPTCHA support information</h1>',
                            b'<title>Security challenge documentation</title>',
                            b'<form>This form describes CAPTCHA verification.</form>'):
            self.assertFalse(d._challenge(response(html() + description)))

    def test_unreserved_url_encodings_cannot_reserve_a_fourth_same_operation_attempt(self):
        urls = [d.LISTING, d.LISTING.replace('/daily/', '/%64aily/'),
                d.LISTING.replace('/daily/', '/%64a%69ly/'),
                d.LISTING.replace('/daily/', '/%64a%69l%79/')]
        transport = Transport([response(b'', urls[index], status=302, Location=urls[index + 1])
                               for index in range(3)] + [response(html(OTHER), urls[-1])])
        result = d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assertEqual(result['status'], 'SKIP_RETRY_EXHAUSTED')
        self.assertEqual(transport.calls, urls[:3])
        budget = policy.Stops(self.f.store).index['retry_targets'][SESSION]
        self.assertEqual(len(budget['requests']), 1)
        self.assertEqual(next(iter(budget['requests'].values()))['attempts_started'], 3)
        self.assertEqual(policy.Stops(self.f.store).index['active'], [])

    def test_diagnostic_403_is_durable_before_any_response_logging(self):
        transport = Transport([response(status=403)])
        seal = fixtures.frozen._seal
        def interrupt(path, record):
            if Path(path).name == 'listing.http1.json':
                raise KeyboardInterrupt('diagnostic wrapper log interrupted')
            return seal(path, record)
        with patch.object(fixtures.frozen, '_seal', side_effect=interrupt), \
                patch.object(d.RequestContext, 'capture', side_effect=KeyboardInterrupt('detail log interrupted')):
            with self.assertRaises(KeyboardInterrupt):
                d.discover_json_one(self.f.root, SESSION, transport=transport,
                                    evidence_folder=self.f.root / 'diagnostic', run_state=d.RunState('first'))
        hard = self.assert_hard('HTTP_403', 'host')
        self.assertEqual(policy.read_reference(hard['reference'])['evidence']['response']['status'], 403)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE:HTTP_403'):
            d.discover_json_one(self.f.root, OTHER, transport=transport,
                                evidence_folder=self.f.root / 'restart', run_state=d.RunState('restart'))
        self.assertEqual(transport.calls, [d.LISTING])

    def test_external_redirect_stop_precedes_optional_response_logging(self):
        transport = Transport([response(b'', status=302, Location='https://external.invalid/blocked')])
        with patch.object(d.RequestContext, 'capture', side_effect=KeyboardInterrupt('logging interrupted')):
            with self.assertRaises(KeyboardInterrupt):
                d.discover_json_one(self.f.root, SESSION, transport=transport,
                                    evidence_folder=self.f.root / 'redirect')
        self.assert_hard('NON_OFFICIAL_HTTPS_URL', 'host')
        other = Transport([])
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.f.root, OTHER, transport=other, run_state=d.RunState('restart'))
        self.assertEqual(other.calls, [])
        self.assertEqual(transport.calls, [d.LISTING])

    def interrupted_integrity(self, code, scope, change_parser=None):
        transport = self.f.acquisition_transport()
        seal = fixtures.frozen._seal
        def interrupt(path, record):
            if Path(path).name == 'failure.json':
                self.assertEqual(record['reason'], code)
                raise KeyboardInterrupt('failure diagnostics interrupted')
            return seal(path, record)
        parse = self.f.parser.side_effect
        def corrupt(*args, **kwargs):
            parsed = parse(*args, **kwargs)
            change_parser(parsed)
            return parsed
        fault = (patch.object(d.parser, 'parse', side_effect=corrupt) if change_parser else
                 patch.object(d.gap, 'stage_existing_pdf', side_effect=ValueError(code)))
        with fault, patch.object(fixtures.frozen, '_seal', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                d.acquire_one(self.f.root, SESSION, transport=transport)
        hard = self.assert_hard(code, scope)
        record = policy.read_reference(hard['reference'])
        self.assertTrue(record['evidence_refs'])
        self.assertEqual(record['evidence']['reason'], code)
        integrity = record['evidence']['integrity']
        self.assertEqual(integrity['source_sha256'], fixtures.core.sha256(self.f.store / SESSION / 'stq_20261005.pdf'))
        self.assertEqual(integrity['expected_session'], SESSION)
        self.assertIn(integrity['parsed_reference'], record['evidence_refs'])
        self.assertEqual(policy.read_reference(integrity['parsed_reference'])['session_date'], SESSION)
        if code == 'RAW_PDF_HASH_CONFLICT':
            self.assertEqual(integrity['parsed_source_sha256'], '0' * 64)
            self.assertNotEqual(integrity['source_sha256'], integrity['parsed_source_sha256'])
        self.assertTrue((self.f.store / SESSION / 'stq_20261005.pdf').is_file())
        self.assertTrue(all(p.stat().st_size for p in self.f.store.rglob('*.json')))
        before = len(transport.calls)
        applicable = OTHER if scope == 'global' else SESSION
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE:' + code):
            d.acquire_one(self.f.root, applicable, transport=transport, run_state=d.RunState('restart'))
        self.assertEqual(len(transport.calls), before)

    def test_raw_hash_conflict_is_durable_before_failure_log(self):
        self.interrupted_integrity('RAW_PDF_HASH_CONFLICT', 'global',
                                   lambda parsed: parsed.update(source_file_hash='0' * 64))

    def test_provenance_conflict_is_durable_before_failure_log(self):
        self.interrupted_integrity('SOURCE_IDENTITY_MISMATCH', 'global')

    def test_semantic_hard_error_is_durable_before_failure_log(self):
        self.interrupted_integrity('OHLCV_INVALID', 'session',
                                   lambda parsed: parsed['records'][0].update(high=1))

    def test_diagnostic_integrity_is_durable_before_failure_log(self):
        transport = Transport([response()])
        seal = fixtures.frozen._seal
        def interrupt(path, record):
            if Path(path).name == 'failure.json':
                raise KeyboardInterrupt('discovery failure diagnostics interrupted')
            return seal(path, record)
        with patch.object(d, 'json_endpoints', side_effect=ValueError('SOURCE_IDENTITY_MISMATCH')), \
                patch.object(fixtures.frozen, '_seal', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                d.discover_json_one(self.f.root, SESSION, transport=transport,
                                    evidence_folder=self.f.root / 'diagnostic')
        self.assert_hard('SOURCE_IDENTITY_MISMATCH', 'global')
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.f.root, OTHER, transport=transport)
        self.assertEqual(transport.calls, [d.LISTING])

    def test_429_deadline_is_durable_before_diagnostic_logging(self):
        transport = Transport([response(status=429, **{'Retry-After': '3600'})])
        seal = fixtures.frozen._seal
        def interrupt(path, record):
            if Path(path).name == 'listing.http1.json':
                raise KeyboardInterrupt('wrapper diagnostic interrupted')
            return seal(path, record)
        with patch.object(fixtures.frozen, '_seal', side_effect=interrupt), \
                patch.object(d.RequestContext, 'capture', side_effect=KeyboardInterrupt('logging interrupted')):
            with self.assertRaises(KeyboardInterrupt):
                d.discover_json_one(self.f.root, SESSION, transport=transport,
                                    evidence_folder=self.f.root / 'rate-limit', run_state=d.RunState('first'))
        state = policy.Stops(self.f.store).index
        self.assertEqual(state['host_retry_not_before'], (NOW + timedelta(seconds=3600)).isoformat())
        self.assertEqual(state['retry_targets'][SESSION]['retry_count'], 1)
        self.assertEqual(state['active'], [])
        other = Transport([])
        # Interrupt waiting rather than pretending a one-hour wait has elapsed.
        with patch.object(d.time, 'sleep', side_effect=KeyboardInterrupt('still waiting')) as sleep:
            with self.assertRaises(KeyboardInterrupt):
                d.acquire_one(self.f.root, OTHER, transport=other, run_state=d.RunState('restart'))
        sleep.assert_called_once_with(3600)
        self.assertEqual(other.calls, [])
        self.assertEqual(transport.calls, [d.LISTING])

    def test_timeout_success_interruption_cannot_create_a_fourth_attempt(self):
        total = []
        for name, responses in [('first', [TimeoutError('transient'), response()]),
                                ('second', [response()])]:
            transport = Transport(responses)
            with patch.object(d, 'json_endpoints', side_effect=KeyboardInterrupt('operation not finalized')):
                with self.assertRaises(KeyboardInterrupt):
                    d.discover_json_one(self.f.root, SESSION, transport=transport,
                                        evidence_folder=self.f.root / name, run_state=d.RunState(name))
            total.extend(transport.calls)
        fourth = Transport([response()])
        with self.assertRaisesRegex(ValueError, 'RETRY_EXHAUSTED'):
            d.discover_json_one(self.f.root, SESSION, transport=fourth,
                                evidence_folder=self.f.root / 'third', run_state=d.RunState('third'))
        self.assertEqual(total, [d.LISTING] * 3)
        self.assertEqual(fourth.calls, [])
        self.assertEqual(policy.Stops(self.f.store).index['active'], [])
        other = Transport([response(html(SESSION))])
        self.assertEqual(d.acquire_one(self.f.root, OTHER, transport=other)['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(other.calls, [d.LISTING])

    def test_attempt_is_reserved_durably_before_transport_and_finalized_after_operation(self):
        testcase = self
        class Reserved(Transport):
            def get(self, url, *, limit):
                budget = policy.Stops(testcase.f.store).index['retry_targets'][SESSION]
                self_state = [item for item in budget['requests'].values() if item['url'] == url][0]
                testcase.assertTrue(budget['in_flight'])
                testcase.assertEqual(self_state['attempts_started'], 1)
                testcase.assertEqual(self_state['state'], 'IN_FLIGHT')
                return super().get(url, limit=limit)
        transport = Reserved([response(), response(self.f.pdf, PDF_URL, kind='application/pdf')])
        self.assertEqual(d.acquire_one(self.f.root, SESSION, transport=transport)['state'], 'PASS')
        budget = policy.Stops(self.f.store).index['retry_targets'][SESSION]
        self.assertFalse(budget['in_flight'])
        self.assertEqual({item['role'] for item in budget['requests'].values()}, {'listing', 'pdf'})
        for item in budget['requests'].values():
            self.assertEqual(item['state'], 'COMPLETED')
            self.assertEqual(item['attempts_started'], 1)
            self.assertEqual(policy.read_reference(item['result_reference'])['state'], 'PASS')
        self.assertEqual(d.acquire_one(self.f.root, SESSION, transport=transport)['web_requests'], 0)
        self.assertEqual(len(transport.calls), 2)

    def test_canonical_url_budget_does_not_cap_distinct_resources(self):
        # Exercise the production request boundary under the real store lock.
        with fixtures.frozen._locked(self.f.store):
            context = d.RequestContext(d.RunState('direct'), SESSION, stops=policy.Stops(self.f.store))
            for _ in range(3):
                d._request(Transport([response()]), d.LISTING, 1000000, 3, context=context)
            alias = d.LISTING.replace('www.jpx.co.jp/', 'www.jpx.co.jp:443/')
            fourth = Transport([response(url=alias)])
            with self.assertRaisesRegex(ValueError, 'RETRY_EXHAUSTED'):
                d._request(fourth, alias, 1000000, 3, context=context)
            self.assertEqual(fourth.calls, [])
            fresh = Transport([response(self.f.pdf, PDF_URL, kind='application/pdf')])
            d._request(fresh, PDF_URL, 1000000, 3, context=context, role='pdf')
            self.assertEqual(fresh.calls, [PDF_URL])

    def test_running_operation_keeps_its_ledger_across_midnight(self):
        current = [NOW]
        class Midnight(Transport):
            def get(self, url, *, limit):
                value = super().get(url, limit=limit)
                current[0] = NOW + timedelta(days=1)
                return value
        with fixtures.frozen._locked(self.f.store), patch.object(d, '_now', side_effect=lambda: current[0]):
            context = d.RequestContext(d.RunState('midnight'), SESSION, stops=policy.Stops(self.f.store))
            d._request(Midnight([response()]), d.LISTING, 1000000, 3, context=context)
            d._request(Transport([response(self.f.pdf, PDF_URL, kind='application/pdf')]),
                       PDF_URL, 1000000, 3, context=context, role='pdf')
            result = self.f.root / 'completed.json'
            policy.seal(result, dict(state='PASS'))
            context.finalize_requests(result)
        budget = policy.Stops(self.f.store).index['retry_targets'][SESSION]
        self.assertEqual(budget['day'], NOW.date().isoformat())
        self.assertEqual(len(budget['requests']), 2)
        self.assertTrue(all(item['state'] == 'COMPLETED' for item in budget['requests'].values()))

    def test_duplicate_attributes_cannot_hide_strong_challenge_widget(self):
        transport = Transport([response(html() + b'<div class="g-recaptcha" class="ordinary"></div>')])
        with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assert_hard('SECURITY_CHALLENGE', 'host')
        self.assertEqual(transport.calls, [d.LISTING])

    def test_inline_script_widget_control_is_detected_but_descriptive_script_is_safe(self):
        for control in (b'<script>document.write(\'<div class="g-recaptcha"></div>\');</script>',
                        b'<script>grecaptcha.render("verification");</script>'):
            with self.subTest(control=control):
                self.assertTrue(d._challenge(response(html() + control)))
        descriptive = b'<script>const article = "CAPTCHA security verification documentation";</script>'
        self.assertFalse(d._challenge(response(html() + descriptive)))

    def test_request_key_canonicalization_preserves_distinct_path_query_and_reserved_separator(self):
        canonical = d._canonical_request_url
        self.assertEqual(canonical(d.LISTING), canonical(d.LISTING.replace('/daily/', '/%64a%69ly/')))
        self.assertEqual(canonical(d.LISTING), canonical(d.LISTING.replace('/daily/', '/placeholder/%2e%2e/daily/')))
        self.assertNotEqual(canonical(d.LISTING), canonical(d.LISTING + '?version=1'))
        self.assertNotEqual(canonical(d.LISTING), canonical(d.LISTING.replace('/daily/', '/different/')))
        self.assertNotEqual(canonical(d.LISTING), canonical(d.LISTING.replace('/daily/', '/%2Fdaily/')))
        self.assertEqual(canonical(d.LISTING + '?key=%41'), canonical(d.LISTING + '?key=A'))

    def test_exhausted_attempt_keeps_its_actual_error_evidence(self):
        transport = Transport([TimeoutError('first'), TimeoutError('second'), TimeoutError('last')])
        result = d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assertEqual(result['status'], 'SKIP_RETRY_EXHAUSTED')
        self.assertEqual(transport.calls, [d.LISTING] * 3)
        folder = self.f.store / SESSION
        metadata = fixtures.core.read_frozen(folder / 'request-0003.json')
        self.assertEqual(metadata['reason'], 'last')
        self.assertEqual(metadata['result'], 'TimeoutError')
        requests = policy.Stops(self.f.store).index['retry_targets'][SESSION]['requests']
        operation = next(iter(requests.values()))
        self.assertEqual(operation['attempts_started'], 3)
        self.assertEqual(operation['state'], 'RETRY_EXHAUSTED')
        self.assertEqual(operation['last_result'], metadata)

    def test_concurrent_restarts_cannot_reserve_a_fourth_same_key_attempt(self):
        first = Transport([TimeoutError('transient'), response()])
        with patch.object(d, 'json_endpoints', side_effect=KeyboardInterrupt('not finalized')):
            with self.assertRaises(KeyboardInterrupt):
                d.discover_json_one(self.f.root, SESSION, transport=first,
                                    evidence_folder=self.f.root / 'first', run_state=d.RunState('first'))
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
                with patch.object(d, 'json_endpoints', side_effect=KeyboardInterrupt('not finalized')):
                    d.discover_json_one(self.f.root, SESSION, transport=Counted([response()]),
                        evidence_folder=self.f.root / ('worker-' + str(number)),
                        run_state=d.RunState('worker-' + str(number)))
            except (Exception, KeyboardInterrupt) as error:
                results.put(str(error))
        processes = [ctx.Process(target=worker, args=(number,)) for number in range(2)]
        try:
            for process in processes:
                process.start()
            ready.set()
            outcomes = [results.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            self.assertCountEqual(outcomes, ['not finalized', 'RETRY_EXHAUSTED'])
            self.assertEqual(len(first.calls) + count.value, 3)
            ledger = policy.Stops(self.f.store).index['retry_targets'][SESSION]['requests']
            self.assertEqual(next(iter(ledger.values()))['attempts_started'], 3)
            self.assertEqual(policy.Stops(self.f.store).index['active'], [])
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()

    def test_partial_transport_read_cannot_hide_late_challenge_control(self):
        partial = b'%PDF-1.4\n' + b' ' * 70000 + b'<div class="g-recaptcha"></div>'
        connection = fixtures.Mock()
        incoming = connection.getresponse.return_value
        incoming.status = 200
        incoming.getheaders.return_value = [('Content-Type', 'application/pdf')]
        incoming.read.side_effect = http.client.IncompleteRead(partial)
        with patch.object(d.http.client, 'HTTPSConnection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
                d.discover_json_one(self.f.root, SESSION, transport=d.HTTPS(),
                                    evidence_folder=self.f.root / 'partial')
        self.assert_hard('SECURITY_CHALLENGE', 'host')
        self.assertEqual(connection.request.call_count, 1)
        self.f.sleep.assert_not_called()

    def test_invalid_retry_after_is_rejected_without_unbounded_wait_or_more_requests(self):
        for number, value in enumerate(('invalid', '-1', '999999999999999999999999999',
                                        '2147483648', 'Fri, 31 Dec 9999 23:59:59 GMT')):
            with self.subTest(value=value):
                # Each value has its own isolated provider state.
                if number:
                    self.f.doCleanups()
                    self.setUp()
                transport = Transport([response(status=429, **{'Retry-After': value})])
                with self.assertRaisesRegex(ValueError, 'RETRY_AFTER_INVALID'):
                    d.discover_json_one(self.f.root, SESSION, transport=transport,
                                        evidence_folder=self.f.root / 'invalid-wait')
                self.assert_hard('RETRY_AFTER_INVALID', 'global')
                self.assertEqual(transport.calls, [d.LISTING])
                self.f.sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
