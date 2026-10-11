"""Public-tree offline policy tests; all state and synthetic bytes are temporary."""
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from pypdf import PdfWriter
from engine import minimal_trial as core
from engine import one_session_dry_run as frozen
from providers.jp.jpx_manual import historical_direct as d
from providers.jp.jpx_manual import historical_policy_v2 as policy

PROJECT = Path(__file__).resolve().parents[1]
SESSION = '2026-10-05'
OTHER = '2026-10-06'
NOW = datetime(2026, 10, 11, 5, tzinfo=timezone.utc)
PDF_URL = 'https://www.jpx.co.jp/published/arbitrary-A17.pdf'


def html(session=SESSION):
    return ('<html><title>東京証券取引所日報</title><table><tr><th>日付</th>'
            '<th>概算・精算表</th><th>株式相場表</th><th>債券相場表</th></tr><tr><td>'
            + session.replace('-', '/') + '</td><td>-</td><td><a href="' + PDF_URL
            + '">PDF</a></td><td>-</td></tr></table></html>').encode()


def response(body=None, url=d.LISTING, status=200, kind='text/html', **headers):
    return d.Response(url, status, {'Content-Type': kind, **headers}, html() if body is None else body)


class Transport:
    def __init__(self, values):
        self.values, self.calls = list(values), []

    def get(self, url, *, limit):
        self.calls.append(url)
        if not self.values:
            raise AssertionError('UNPLANNED_REQUEST:' + url)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        if value.url != url:
            raise AssertionError('UNPLANNED_URL:' + url)
        return value


class HistoricalPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'config').mkdir()
        (self.root / 'analysis').mkdir()
        config = PROJECT / 'config/jpx_daily_acquisition_config.json'
        (self.root / 'config' / config.name).write_bytes(config.read_bytes())
        sessions = ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09']
        (self.root / 'analysis/calendar_validation_2026-09-12.json').write_text(json.dumps(
            dict(jp_sessions=sessions, validation_range=dict(start=sessions[0], end=sessions[-1]))))
        self.store = self.root / 'data/jpx_historical_direct'
        self.addCleanup(patch.stopall)
        patch.object(d, '_now', return_value=NOW).start()
        patch.object(socket.socket, 'connect', side_effect=AssertionError('LIVE_REQUEST_FORBIDDEN')).start()
        self.sleep = patch.object(d.time, 'sleep').start()
        writer, buffer = PdfWriter(), BytesIO()
        writer.add_blank_page(width=200, height=200)
        writer.write(buffer)
        self.pdf = buffer.getvalue()
        # Acquisition state tests use a synthetic parser result. The real parser
        # and local JPX fixture are exercised separately in acquisition regression.
        def synthetic(raw, session, acquisition_method):
            digest = core.sha256(raw)
            return dict(session_date=session, source_file_hash=digest, source_file_hash_after=digest,
                        normalized_schema_validation='PASS', manual_file_integrity='PASS', duplicate_count=0,
                        retrieved_at=NOW.isoformat(), core6_extraction='6/6', records=[
                            dict(ticker=t, session_date=session, open=10, high=12, low=9, close=11, volume=100)
                            for t in d.parser.CORE])
        self.parser = patch.object(d.parser, 'parse', side_effect=synthetic).start()

    def acquisition_transport(self):
        return Transport([response(), response(self.pdf, PDF_URL, kind='application/pdf')])

    def legacy(self, record):
        self.store.mkdir(parents=True, exist_ok=True)
        path = self.store / 'STOP.json'
        path.write_text(json.dumps(record))
        return path

    def soft_failure(self):
        transport = self.acquisition_transport()
        with patch.object(d.gap, 'stage_existing_pdf', side_effect=ValueError('MANIFEST_NOT_VALIDATED')):
            with self.assertRaisesRegex(ValueError, 'MANIFEST_NOT_VALIDATED'):
                d.acquire_one(self.root, SESSION, transport=transport)
        stops = policy.Stops(self.store)
        return stops.index['active'][0]

    def resolution(self, item, **overrides):
        fixes = [policy.reference(self.root / 'config/jpx_daily_acquisition_config.json')]
        report = self.root / ('tests-' + str(len(list(self.root.glob('tests-*')))) + '.json')
        frozen._seal(report, dict(status='PASS', checks=dict(fixture='PASS', regression='PASS'), fix_refs=fixes))
        record = dict(stop_reference=item['reference'], scope=item['record']['scope'], target=item['record']['target'],
                      root_cause='Known local manifest metadata bug corrected', scope_isolation='PASS',
                      fix_refs=fixes, validation_refs=[policy.reference(report)], evidence_refs=item['record']['evidence_refs'])
        record.update(overrides)
        path = self.root / ('resolution-' + str(len(list(self.root.glob('resolution-*')))) + '.json')
        frozen._seal(path, record)
        return path

    def test_existing_hard_stop_refuses_resume_without_request(self):
        old = self.legacy(dict(reason='HTTP_403', session=SESSION))
        before = old.read_bytes()
        transport = Transport([])
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE:HTTP_403'):
            d.acquire_one(self.root, SESSION, transport=transport, resolution=self.root / 'fake.json')
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse(transport.calls)

    def test_soft_stop_validated_resume_preserves_evidence_and_reuses_raw(self):
        item = self.soft_failure()
        previous = self.store / SESSION
        before = {p.name: p.read_bytes() for p in previous.iterdir() if p.is_file()}
        raw_stop = (self.store / 'STOP.json').read_bytes()
        transport = Transport([])
        result = d.acquire_one(self.root, SESSION, transport=transport, resolution=self.resolution(item))
        self.assertEqual(result['state'], 'PASS')
        event = policy.read_reference(result['resumed_reference'])
        self.assertEqual(event['state'], 'RESUMED')
        self.assertEqual(event['previous_stop_reference'], item['reference'])
        self.assertIsNotNone(datetime.fromisoformat(event['resumed_at']).tzinfo)
        self.assertEqual((self.store / 'STOP.json').read_bytes(), raw_stop)
        self.assertEqual({p.name: p.read_bytes() for p in previous.iterdir() if p.is_file()}, before)
        self.assertFalse(policy.Stops(self.store).index['active'])
        self.assertFalse(transport.calls)
        self.assertEqual(d.acquire_one(self.root, SESSION, transport=transport)['status'], 'SKIP_EXISTING_LOCAL')

    def test_soft_stop_without_resolution_remains_active(self):
        self.soft_failure()
        with self.assertRaisesRegex(ValueError, 'SOFT_STOP_RESOLUTION_REQUIRED'):
            d.acquire_one(self.root, SESSION, transport=Transport([]))

    def test_resolution_requires_scope_fix_tests_and_unchanged_evidence(self):
        item = self.soft_failure()
        for override in (dict(target=OTHER), dict(root_cause=''), dict(scope_isolation='FAIL'),
                         dict(fix_refs=[]), dict(validation_refs=[]), dict(evidence_refs=[])):
            with self.subTest(override=override), self.assertRaises(ValueError):
                d.acquire_one(self.root, SESSION, transport=Transport([]), resolution=self.resolution(item, **override))
        self.assertEqual(len(policy.Stops(self.store).index['active']), 1)

    def test_resume_required_evidence_revalidation_cannot_be_claimed(self):
        item = self.soft_failure()
        resolution = self.resolution(item)
        (self.store / SESSION / 'listing.html').write_bytes(b'changed source')
        with self.assertRaisesRegex(ValueError, 'PROVENANCE_OR_HASH_CONFLICT'):
            d.acquire_one(self.root, SESSION, transport=Transport([]), resolution=resolution)
        items = policy.Stops(self.store).index['active']
        self.assertIn(item, items)
        hard = [x for x in items if x['record']['stop_class'] == 'HARD_STOP']
        self.assertEqual(len(hard), 1)
        self.assertEqual(hard[0]['record']['stop_code'], 'ACQUISITION_PROVENANCE_OR_HASH_CONFLICT')
        self.assertEqual(hard[0]['record']['scope'], 'global')
        self.assertEqual(hard[0]['record']['previous_stop_reference'], item['reference'])
        transport = Transport([])
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.root, OTHER, transport=transport)
        self.assertEqual(transport.calls, [])
        self.assertFalse(list((self.store / 'stops_v2').glob('resume-*')))

    def test_legacy_unknown_stop_fails_closed_and_is_not_overwritten(self):
        old = self.legacy(dict(reason='PARSER_FAILURE', session=SESSION))
        before = old.read_bytes()
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_LEGACY_UNCLASSIFIED'):
            d.acquire_one(self.root, OTHER, transport=Transport([]))
        self.assertEqual(old.read_bytes(), before)

    def test_malformed_legacy_stop_fails_closed_on_every_run(self):
        old = self.legacy({})
        old.write_text('{malformed')
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'HARD_STOP_LEGACY_UNCLASSIFIED'):
                d.acquire_one(self.root, SESSION, transport=Transport([]))
        self.assertEqual(old.read_text(), '{malformed')

    def test_known_legacy_soft_stop_can_resume_after_full_validation(self):
        acquired = d.acquire_one(self.root, SESSION, transport=self.acquisition_transport())
        entry = acquired['manifest_entry_candidate']
        inbox = self.root / 'data/outcome_input/manual_jpx'
        inbox.mkdir(parents=True)
        (inbox / entry['filename']).write_bytes(self.pdf)
        (inbox / 'retention_manifest.json').write_text(json.dumps(dict(entries=[entry])))
        old = self.legacy(dict(reason='MANIFEST_NOT_VALIDATED', session=SESSION, failed_at=NOW.isoformat()))
        before = old.read_bytes()
        item = policy.Stops(self.store).index['active'][0]
        self.assertEqual(item['record']['stop_class'], 'SOFT_STOP')
        result = d.acquire_one(self.root, SESSION, transport=Transport([]), resolution=self.resolution(item))
        self.assertEqual(result['state'], 'PASS')
        self.assertIsNotNone(result['resumed_reference'])
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(len(policy.Stops(self.store).index['resumed']), 1)

    def test_deleted_stop_evidence_is_not_a_release(self):
        self.soft_failure()
        (self.store / 'STOP.json').unlink()
        with self.assertRaisesRegex(ValueError, 'STOP_EVIDENCE_HASH_CONFLICT'):
            d.acquire_one(self.root, OTHER, transport=Transport([]))

    def test_failed_validation_report_and_changed_fix_block_resume(self):
        item = self.soft_failure()
        resolution = self.resolution(item)
        report_ref = json.loads(resolution.read_bytes())['validation_refs'][0]
        report = policy.read_reference(report_ref)
        report['checks']['regression'] = 'FAIL'
        failing = self.root / 'failed-test.json'
        frozen._seal(failing, report)
        with self.assertRaisesRegex(ValueError, 'REQUIRED_TESTS_FAILED'):
            d.acquire_one(self.root, SESSION, transport=Transport([]),
                          resolution=self.resolution(item, validation_refs=[policy.reference(failing)]))
        fix = self.root / 'config/jpx_daily_acquisition_config.json'
        config = json.loads(fix.read_bytes())
        config['config_version'] += '-changed'
        fix.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'CURRENT_FIX_HASH_INVALID'):
            d.acquire_one(self.root, SESSION, transport=Transport([]), resolution=resolution)

    def test_resume_event_must_be_durable_before_stop_is_released(self):
        item = self.soft_failure()
        resolution = self.resolution(item)
        seal = policy.seal
        def fail_event(path, record):
            if Path(path).name.startswith('resume-'):
                raise OSError('EVENT_WRITE_FAILED')
            return seal(path, record)
        with patch.object(policy, 'seal', side_effect=fail_event):
            with self.assertRaisesRegex(OSError, 'EVENT_WRITE_FAILED'):
                d.acquire_one(self.root, SESSION, transport=Transport([]), resolution=resolution)
        self.assertEqual(policy.Stops(self.store).index['active'], [item])

    def test_403_zero_retry_and_global_same_host_block_in_next_session_and_diagnostic(self):
        transport = Transport([response(status=403)])
        with self.assertRaisesRegex(ValueError, 'HTTP_403'):
            d.acquire_one(self.root, SESSION, transport=transport)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.root, OTHER, transport=transport)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.discover_json_one(self.root, OTHER, transport=transport, evidence_folder=self.root / 'diagnostic')
        self.assertEqual(transport.calls, [d.LISTING])
        record = core.read_frozen(self.store / 'STOP.json')
        self.assertEqual(record['scope'], 'host')
        self.assertEqual(record['retry_count'], 0)
        self.assertEqual((self.store / SESSION / 'request-0001.body').read_bytes(), html())

    def test_captcha_bot_and_auth_challenges_have_zero_retry_even_on_429(self):
        for text, status in (('CAPTCHA', 200), ('Bot challenge', 200), ('Authentication challenge', 200),
                             ('Authentication required', 200), ('CAPTCHA', 429)):
            with self.subTest(text=text, status=status):
                context = d.RequestContext()
                transport = Transport([response(('<title>' + text + '</title>').encode(), status=status)])
                with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
                    d._request(transport, d.LISTING, 4096, 3, context=context)
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(context.run.retries, {})

    def test_known_403_or_challenge_survives_transport_body_read_failure_without_retry(self):
        import http.client
        for status, error, expected in ((403, TimeoutError('body timeout'), 'HTTP_403'),
                                        (401, TimeoutError('body timeout'), 'AUTHENTICATION_CHALLENGE'),
                                        (429, http.client.IncompleteRead(b'CAPTCHA'), 'SECURITY_CHALLENGE'),
                                        (200, http.client.IncompleteRead(b'CAPTCHA'), 'SECURITY_CHALLENGE')):
            with self.subTest(status=status):
                connection = Mock()
                incoming = connection.getresponse.return_value
                incoming.status = status
                incoming.getheaders.return_value = [('Content-Type', 'text/html')]
                incoming.read.side_effect = error
                with patch.object(d.http.client, 'HTTPSConnection', return_value=connection):
                    with self.assertRaisesRegex(ValueError, expected):
                        d._request(d.HTTPS(), d.LISTING, 4096, 3)
                self.assertEqual(connection.request.call_count, 1)

    def test_429_then_success_warns_no_extra_retry_and_respects_retry_after(self):
        transport = Transport([response(status=429, **{'Retry-After': '7'}), response(html(OTHER))])
        result = d.acquire_one(self.root, SESSION, transport=transport)
        self.assertEqual(result['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(result['retry_count'], 1)
        self.assertEqual(len(transport.calls), 2)
        self.sleep.assert_called_once_with(7)

    def test_persistent_429_stops_host_early_and_next_session_has_zero_requests(self):
        transport = Transport([response(status=429, **{'Retry-After': '0'})] * 2)
        run = d.RunState('persistent-test')
        with self.assertRaisesRegex(ValueError, 'HOST_PERSISTENT_429'):
            d.acquire_one(self.root, SESSION, transport=transport, run_state=run)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.root, OTHER, transport=transport, run_state=d.RunState('different-run'))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(core.read_frozen(self.store / 'STOP.json')['scope'], 'host')

    def test_transient_network_max_two_retries_then_skip_independent_session(self):
        transport = Transport([TimeoutError('temporary')] * 3 + [response(html(SESSION))])
        run = d.RunState('transient-test')
        result = d.acquire_one(self.root, SESSION, transport=transport, run_state=run)
        self.assertEqual(result['status'], 'SKIP_RETRY_EXHAUSTED')
        self.assertEqual(result['retry_count'], 2)
        other = d.acquire_one(self.root, OTHER, transport=transport, run_state=run)
        self.assertEqual(other['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(len(transport.calls), 4)
        self.assertFalse((self.store / 'STOP.json').exists())
        # Same-day / same-run repetition cannot reset the exhausted budget.
        d.acquire_one(self.root, SESSION, transport=transport, run_state=d.RunState(run.run_id))
        self.assertEqual(len(transport.calls), 4)

    def test_retry_budget_is_shared_by_listing_and_pdf_not_reset_by_discovery(self):
        transport = Transport([TimeoutError('temporary'), response(), TimeoutError('temporary'),
                               response(self.pdf, PDF_URL, kind='application/pdf')])
        result = d.acquire_one(self.root, SESSION, transport=transport)
        self.assertEqual(result['state'], 'PASS')
        self.assertEqual(result['retry_count'], 2)
        self.assertEqual(len(transport.calls), 4)

    def test_pdf_retry_cannot_exceed_budget_already_used_on_listing(self):
        transport = Transport([TimeoutError('temporary')] * 2 + [response(), TimeoutError('temporary')])
        result = d.acquire_one(self.root, SESSION, transport=transport)
        self.assertEqual(result['status'], 'SKIP_RETRY_EXHAUSTED')
        self.assertEqual(len(transport.calls), 4)

    def test_soft_resume_retains_retry_budget_even_with_new_run_id(self):
        transport = Transport([TimeoutError('temporary')] * 2 + self.acquisition_transport().values)
        with patch.object(d.gap, 'stage_existing_pdf', side_effect=ValueError('MANIFEST_NOT_VALIDATED')):
            with self.assertRaises(ValueError):
                d.acquire_one(self.root, SESSION, transport=transport)
        item = policy.Stops(self.store).index['active'][0]
        result = d.acquire_one(self.root, SESSION, transport=Transport([]), resolution=self.resolution(item))
        self.assertEqual(result['retry_count'], 2)
        self.assertEqual(policy.read_reference(result['resumed_reference'])['retry_count'], 2)

    def test_run_retry_budget_survives_reconstruction(self):
        self.store.mkdir(parents=True)
        run = d.RunState('reconstructed')
        run.bind(self.store)
        run.retries[SESSION] = 1
        run.save()
        context = d.RequestContext(d.RunState(run.run_id), SESSION)
        context.run.bind(self.store)
        transport = Transport([TimeoutError('temporary')] * 2)
        with self.assertRaisesRegex(ValueError, 'RETRY_EXHAUSTED'):
            d._request(transport, d.LISTING, 4096, 3, context=context)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(context.run.retries[SESSION], 2)

    def test_invalid_persisted_retry_budget_is_fail_closed_before_requests(self):
        transport = Transport([])
        with self.assertRaisesRegex(ValueError, 'INVALID_RETRY_BUDGET'):
            d.acquire_one(self.root, SESSION, transport=transport, run_state=d.RunState(retries={SESSION: -1}))
        self.assertFalse(transport.calls)

    def test_official_json_octet_stream_schema_pass_records_warn_and_continues(self):
        markup = ('<html><title>東京証券取引所日報</title><table><tr><th>日付</th>'
                  '<th>概算・精算表</th><th>株式相場表</th><th>債券相場表</th></tr></table><script>'
                  "const BASE_PATH = '/published/index/'; const BUCKNUMBER_JSON = BASE_PATH + 'months.json';"
                  'const jsonUrl = `${BASE_PATH}listed_${month}.json`; '
                  "item.Month; data.TableDatas; row.TradeDate; 'Stocks'; row[key];"
                  '</script></html>').encode()
        list_url = 'https://www.jpx.co.jp/published/index/months.json'
        month_url = 'https://www.jpx.co.jp/published/index/listed_202610.json'
        transport = Transport([response(markup), response(b'{"TableDatas":[{"Month":"202610"}]}', list_url, kind='binary/octet-stream'),
                               response(json.dumps(dict(TableDatas=[dict(TradeDate='20261005', Stocks=PDF_URL)])).encode(),
                                        month_url, kind='application/octet-stream'),
                               response(self.pdf, PDF_URL, kind='application/pdf')])
        result = d.acquire_one(self.root, SESSION, transport=transport)
        self.assertEqual(result['state'], 'WARN')
        self.assertEqual([w['code'] for w in result['warnings']], ['CONTENT_TYPE_MISMATCH'] * 2)
        self.assertEqual(transport.calls, [d.LISTING, list_url, month_url, PDF_URL])
        self.assertEqual(d.acquire_one(self.root, SESSION, transport=Transport([]))['status'], 'SKIP_EXISTING_LOCAL')

    def test_malformed_json_or_schema_never_continues_as_warn(self):
        for body in (b'{', b'{}', b'{"TableDatas":{}}', b'{"TableDatas":[],"TableDatas":[]}', b'<title>CAPTCHA</title>'):
            warnings = []
            with self.subTest(body=body), self.assertRaises(ValueError):
                d._json_response(response(body, 'https://www.jpx.co.jp/x.json', kind='binary/octet-stream'), warnings)
            self.assertEqual(warnings, [])

    def test_external_redirect_hard_stop_preserves_body_never_contacts_external_host(self):
        transport = Transport([response(b'', status=302, Location='https://example.test/elsewhere')])
        with self.assertRaisesRegex(ValueError, 'NON_OFFICIAL'):
            d.acquire_one(self.root, SESSION, transport=transport)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.root, OTHER, transport=transport)
        self.assertEqual(transport.calls, [d.LISTING])

    def test_official_discovery_absent_link_has_no_filename_or_date_guessing(self):
        transport = Transport([response(html(OTHER))])
        self.assertEqual(d.acquire_one(self.root, SESSION, transport=transport)['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(transport.calls, [d.LISTING])

    def test_valid_existing_candidate_data_is_revalidated_without_refetch(self):
        d.acquire_one(self.root, SESSION, transport=self.acquisition_transport())
        transport = Transport([])
        self.assertEqual(d.acquire_one(self.root, SESSION, transport=transport)['status'], 'SKIP_EXISTING_LOCAL')
        self.assertFalse(transport.calls)
        raw = self.store / SESSION / 'stq_20261005.pdf'
        raw.write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError, 'HASH_CONFLICT'):
            d.acquire_one(self.root, SESSION, transport=transport)
        self.assertFalse(transport.calls)

    def test_valid_registered_manifest_raw_and_parsed_have_zero_refetch(self):
        result = d.acquire_one(self.root, SESSION, transport=self.acquisition_transport())
        entry = result['manifest_entry_candidate']
        inbox = self.root / 'data/outcome_input/manual_jpx'
        inbox.mkdir(parents=True)
        (inbox / entry['filename']).write_bytes(self.pdf)
        (inbox / 'retention_manifest.json').write_text(json.dumps(dict(entries=[entry])))
        transport = Transport([])
        skip = d.acquire_one(self.root, SESSION, transport=transport)
        self.assertEqual(skip['local_validation'], 'PASS')
        self.assertFalse(transport.calls)
        raw = self.store / SESSION / 'stq_20261005.pdf'
        raw.write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError, 'HASH_CONFLICT'):
            d.acquire_one(self.root, SESSION, transport=transport)
        self.assertFalse(transport.calls)

    def test_session_local_soft_failure_allows_unrelated_session(self):
        self.soft_failure()
        transport = Transport([response(html(SESSION))])
        self.assertEqual(d.acquire_one(self.root, OTHER, transport=transport)['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(transport.calls, [d.LISTING])

    def test_session_specific_hard_mismatch_blocks_only_affected_session(self):
        with patch.object(d.parser, 'parse', side_effect=ValueError('DATA_CONFLICT: session date 2026-10-04')):
            with self.assertRaisesRegex(ValueError, 'DATA_CONFLICT'):
                d.acquire_one(self.root, SESSION, transport=self.acquisition_transport())
        transport = Transport([response(html(SESSION))])
        self.assertEqual(d.acquire_one(self.root, OTHER, transport=transport)['status'], 'NOT_YET_AVAILABLE')
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.root, SESSION, transport=transport)
        self.assertEqual(len(transport.calls), 1)

    def test_retry_after_http_date_is_respected(self):
        transport = Transport([response(status=429, **{'Retry-After': 'Sun, 11 Oct 2026 05:00:10 GMT'}), response(html(OTHER))])
        d.acquire_one(self.root, SESSION, transport=transport)
        self.sleep.assert_called_once_with(10)

    def test_same_run_host_alias_cannot_bypass_persistent_429(self):
        context = d.RequestContext()
        transport = Transport([response(status=429, **{'Retry-After': '0'})] * 2)
        with self.assertRaisesRegex(ValueError, 'HOST_PERSISTENT_429'):
            d._request(transport, d.LISTING, 4096, 3, context=context)
        context.session = OTHER
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d._request(transport, 'https://jpx.co.jp/alternate/', 4096, 3, context=context)
        self.assertEqual(len(transport.calls), 2)

    def test_systemic_semantic_parser_failure_blocks_subsequent_session(self):
        with patch.object(d.parser, 'parse', side_effect=ValueError('UNKNOWN_PRICE_UNIT')):
            with self.assertRaisesRegex(ValueError, 'UNKNOWN_PRICE_UNIT'):
                d.acquire_one(self.root, SESSION, transport=self.acquisition_transport())
        transport = Transport([])
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.root, OTHER, transport=transport)
        self.assertFalse(transport.calls)


class ParserContractTests(unittest.TestCase):
    def test_actual_parser_rejects_session_ticker_and_ohlc_conflicts(self):
        with self.assertRaisesRegex(ValueError, 'DATA_CONFLICT'):
            d.parser.source_date('2026年10月6日', SESSION)
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_TICKER'):
            d.parser.unique([dict(ticker=t) for t in d.parser.CORE] + [dict(ticker='1605')])
        with self.assertRaisesRegex(ValueError, 'DATA_CONFLICT'):
            d.parser.validate_prices(dict(open=20, high=12, low=9, close=11))


if __name__ == '__main__':
    unittest.main()
