"""Offline HTML/HTTP fixtures and reduced local JPX PDF; never a live request."""
import copy
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from engine import minimal_trial as core
from engine import one_session_dry_run as probe
from providers.jp.jpx_manual import historical_direct as direct
from providers.jp.jpx_manual import parser

PROJECT = Path(__file__).resolve().parents[1]
SESSION = '2026-10-05'
HREF = '/documents/published/stock-table-A17.pdf?download=1'
PDF_URL = 'https://www.jpx.co.jp' + HREF
NOW = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)


def listing(session=SESSION, href=HREF, extra=''):
    return ('<!doctype html><html><head><title>東京証券取引所日報 | JPX</title></head>'
            '<body><h1>東京証券取引所日報</h1>' + extra + '<table><thead><tr>'
            '<th>日付</th><th>概算・精算表</th><th>株式相場表</th><th>債券相場表</th>'
            '</tr></thead><tbody><tr><td>' + session.replace('-', '/') + '</td>'
            '<td><a href="/other/summary.pdf">PDF</a></td><td><a href="' + href + '">'
            '<img src="/pdf.png" alt="PDF"></a></td><td><a href="/other/bonds.pdf">PDF</a></td>'
            '</tr></tbody></table></body></html>').encode()


def response(body=None, url=direct.LISTING, status=200, content_type='text/html', **headers):
    return direct.Response(url, status, {'Content-Type': content_type, **headers},
                           listing() if body is None else body)


class MockTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, limit):
        self.calls.append(url)
        if not self.responses:
            raise AssertionError('Unexpected retry/request')
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if value.url != url:
            raise AssertionError('Unexpected URL: ' + url)
        return value


class ListingTests(unittest.TestCase):
    def test_exact_stock_column_href_without_url_guessing(self):
        result = direct.listing_link(response(), SESSION)
        self.assertEqual(result['exact_href'], HREF)
        self.assertEqual(result['resolved_pdf_url'], PDF_URL)
        self.assertNotIn('20261005', result['resolved_pdf_url'])
        self.assertIn('2026/10/05', result['row_html'])
        self.assertIn(HREF, result['row_html'])

    def test_absent_target_is_unlisted_no_date_substitution(self):
        self.assertIsNone(direct.listing_link(response(listing('2026-10-06')), SESSION))

    def test_external_link_rejected(self):
        with self.assertRaisesRegex(ValueError, 'NON_OFFICIAL'):
            direct.listing_link(response(listing(href='https://example.com/a.pdf')), SESSION)

    def test_unsafe_urls_rejected(self):
        for url in ('http://www.jpx.co.jp/a.pdf', 'https://jpx.co.jp.evil.test/a.pdf',
                    'https://eviljpx.co.jp/a.pdf', 'https://u@www.jpx.co.jp/a.pdf',
                    'https://www.jpx.co.jp:444/a.pdf', 'https://www.jpx.co.jp/a.pdf#x',
                    'https://www.jpx.co.jp/a%0d%0a.pdf', 'file:///a.pdf'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                direct.official_url(url)

    def test_200_challenge_title_rejected_even_with_expected_table(self):
        html = listing().replace('東京証券取引所日報 | JPX'.encode(), b'Access Denied')
        with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            direct.listing_link(response(html), SESSION)

    def test_200_captcha_form_rejected(self):
        html = listing(extra='<form id="captcha"><input></form>')
        with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            direct.listing_link(response(html), SESSION)

    def test_200_unknown_html_rejected(self):
        with self.assertRaisesRegex(ValueError, 'UNEXPECTED_LISTING_HTML'):
            direct.listing_link(response(b'<html><title>Welcome</title><body>Hello</body></html>'), SESSION)

    def test_challenge_word_in_ordinary_text_is_not_enough(self):
        self.assertIsNotNone(direct.listing_link(response(listing(extra='<p>captchaという語の説明</p>')), SESSION))

    def test_wrong_content_type_rejected(self):
        with self.assertRaisesRegex(ValueError, 'UNEXPECTED_LISTING_RESPONSE'):
            direct.listing_link(response(content_type='text/plain'), SESSION)

    def test_wrong_table_header_rejected(self):
        html = listing().replace('株式相場表'.encode(), '株価'.encode())
        with self.assertRaisesRegex(ValueError, 'UNEXPECTED_LISTING_HTML'):
            direct.listing_link(response(html), SESSION)

    def test_duplicate_target_row_rejected(self):
        html = listing()
        row = html[html.index(b'<tbody>') + len(b'<tbody>'):html.index(b'</tbody>')]
        html = html.replace(b'</tbody>', row + b'</tbody>')
        with self.assertRaisesRegex(ValueError, 'AMBIGUOUS'):
            direct.listing_link(response(html), SESSION)

    def test_duplicate_href_attribute_rejected(self):
        html = listing().replace(('href="' + HREF + '"').encode(), b'href="/a.pdf" href="/b.pdf"')
        with self.assertRaisesRegex(ValueError, 'AMBIGUOUS_HTML_ATTRIBUTES'):
            direct.listing_link(response(html), SESSION)

    def test_missing_or_multiple_stock_links_rejected(self):
        for replacement in (b'', b'<a href="/one.pdf">one</a><a href="/two.pdf">two</a>'):
            html = listing()
            start = html.index((' <a href="' + HREF).strip().encode())
            end = html.index(b'</a>', start) + 4
            html = html[:start] + replacement + html[end:]
            with self.subTest(replacement=replacement), self.assertRaisesRegex(ValueError, 'PDF_LINK_UNKNOWN'):
                direct.listing_link(response(html), SESSION)

    def test_base_tag_cannot_change_href_resolution(self):
        with self.assertRaisesRegex(ValueError, 'UNEXPECTED_LISTING_HTML'):
            direct.listing_link(response(listing(extra='<base href="https://evil.test/">')), SESSION)

    def test_script_link_does_not_count_as_publication(self):
        html = b'<html><title>JPX</title><script>var html="stock-table.pdf";</script></html>'
        with self.assertRaises(ValueError):
            direct.listing_link(response(html), SESSION)


class RequestTests(unittest.TestCase):
    def request(self, transport, redirects=3):
        with patch.object(direct, '_now', return_value=NOW):
            return direct._request(transport, direct.LISTING, 4096, redirects)

    def test_403_stops_after_one_request(self):
        transport = MockTransport([response(status=403)])
        with self.assertRaisesRegex(ValueError, 'HTTP_403'):
            self.request(transport)
        self.assertEqual(len(transport.calls), 1)

    def test_persistent_429_stops_after_one_retry(self):
        transport = MockTransport([response(status=429, **{'Retry-After': '0'})] * 2)
        with self.assertRaisesRegex(ValueError, 'HOST_PERSISTENT_429'):
            self.request(transport)
        self.assertEqual(len(transport.calls), 2)

    def test_external_redirect_not_contacted(self):
        transport = MockTransport([response(status=302, Location='https://example.com/a.pdf')])
        with self.assertRaisesRegex(ValueError, 'NON_OFFICIAL'):
            self.request(transport)
        self.assertEqual(transport.calls, [direct.LISTING])

    def test_http_redirect_rejected_before_contact(self):
        transport = MockTransport([response(status=302, Location='http://www.jpx.co.jp/a.pdf')])
        with self.assertRaisesRegex(ValueError, 'NON_OFFICIAL'):
            self.request(transport)
        self.assertEqual(len(transport.calls), 1)

    def test_official_redirect_chain_preserved(self):
        url = 'https://www.jpx.co.jp/other/listing/'
        transport = MockTransport([response(status=302, Location=url, body=b''), response(url=url)])
        result, chain = self.request(transport)
        self.assertEqual(result.url, url)
        self.assertEqual([r['url'] for r in chain], [direct.LISTING, url])

    def test_redirect_cycle_rejected_without_repeating_request(self):
        transport = MockTransport([response(status=302, Location=direct.LISTING, body=b'')])
        with self.assertRaisesRegex(ValueError, 'REDIRECT_CYCLE'):
            self.request(transport)
        self.assertEqual(len(transport.calls), 1)

    def test_redirect_budget_no_extra_contact(self):
        transport = MockTransport([response(status=302, Location='/another/', body=b'')])
        with self.assertRaisesRegex(ValueError, 'REDIRECT_LIMIT'):
            self.request(transport, redirects=0)
        self.assertEqual(len(transport.calls), 1)

    def test_unknown_status_no_retry(self):
        transport = MockTransport([response(status=500)])
        with self.assertRaisesRegex(ValueError, 'UNEXPECTED_HTTP_STATUS'):
            self.request(transport)
        self.assertEqual(len(transport.calls), 1)

    def test_transport_timeout_retry_max_two(self):
        transport = MockTransport([TimeoutError('timeout')] * 3)
        with self.assertRaisesRegex(ValueError, 'RETRY_EXHAUSTED'):
            self.request(transport)
        self.assertEqual(len(transport.calls), 3)

    def test_redirect_challenge_body_stops(self):
        transport = MockTransport([response(body=b'<title>Security challenge</title>', status=302, Location='/other/')])
        with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            self.request(transport)
        self.assertEqual(len(transport.calls), 1)

    def test_response_size_limit(self):
        with self.assertRaisesRegex(ValueError, 'RESPONSE_TOO_LARGE'):
            self.request(MockTransport([response(b'x' * 4097)]))

    def test_clock_goes_back_rejected(self):
        with patch.object(direct, '_now', side_effect=[NOW, datetime(2026, 10, 7, tzinfo=timezone.utc)]):
            with self.assertRaisesRegex(ValueError, 'CLOCK_INVALID'):
                direct._request(MockTransport([response()]), direct.LISTING, 4096, 3)


class AcquisitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A compact LOCAL fixture uses the six already tested JPX pages, no fetch.
        reader = PdfReader(PROJECT / 'data/outcome_input/manual_jpx/stq_20260915.pdf')
        writer = PdfWriter()
        for index in (0, 25, 27, 39, 47, 51):
            writer.add_page(reader.pages[index])
        stream = BytesIO()
        writer.write(stream)
        cls.pdf = stream.getvalue()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for part in ('config', 'analysis', 'data/outcome_input/manual_jpx', 'data/outcome_parsed/jpx'):
            (self.root / part).mkdir(parents=True)
        self.policy = json.loads((PROJECT / 'config/jpx_daily_acquisition_config.json').read_bytes())
        self.save_policy()
        (self.root / 'config/normalized_market_schema.json').write_bytes((PROJECT / 'config/normalized_market_schema.json').read_bytes())
        (self.root / 'analysis/calendar_validation_2026-09-12.json').write_bytes((PROJECT / 'analysis/calendar_validation_2026-09-12.json').read_bytes())
        self.manifest = self.root / 'data/outcome_input/manual_jpx/retention_manifest.json'
        self.manifest.write_text('{"entries":[]}')
        self.addCleanup(patch.stopall)
        patch.object(direct, '_now', return_value=NOW).start()
        patch.object(socket.socket, 'connect', side_effect=AssertionError('TEST_NETWORK_FORBIDDEN')).start()

    def save_policy(self):
        (self.root / 'config/jpx_daily_acquisition_config.json').write_text(json.dumps(self.policy))

    def valid_acquisition(self):
        # Test-only policy permits an old local fixture; production is Oct5/6/7.
        self.policy['historical_direct']['allowed_sessions'] = ['2026-09-15']
        self.policy['historical_direct']['forecast_target'] = '2026-09-18'
        self.save_policy()
        transport = MockTransport([response(listing('2026-09-15')),
                                   response(self.pdf, url=PDF_URL, content_type='application/pdf')])
        result = direct.acquire_one(self.root, '2026-09-15', transport=transport)
        return result, transport, self.root / 'data/jpx_historical_direct/2026-09-15'

    def test_normal_pdf_accepted(self):
        direct.validate_pdf(response(self.pdf, url=PDF_URL, content_type='application/pdf'))

    def test_html_instead_of_pdf_rejected(self):
        with self.assertRaisesRegex(ValueError, 'NON_PDF_RESPONSE'):
            direct.validate_pdf(response(listing(), url=PDF_URL))

    def test_200_challenge_pdf_response_rejected(self):
        with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            direct.validate_pdf(response(b'<title>Verify you are human</title>', url=PDF_URL))

    def test_magic_mismatch_rejected_even_with_pdf_content_type(self):
        with self.assertRaisesRegex(ValueError, 'PDF_MAGIC_MISMATCH'):
            direct.validate_pdf(response(b'<html>bad</html>', url=PDF_URL, content_type='application/pdf'))

    def test_magic_alone_is_not_a_valid_pdf(self):
        with self.assertRaisesRegex(ValueError, 'PDF_CORRUPT'):
            direct.validate_pdf(response(b'%PDF-1.7\ninvalid', url=PDF_URL, content_type='application/pdf'))

    def test_existing_raw_is_excluded_before_listing(self):
        raw = self.root / 'data/outcome_input/manual_jpx/stq_20261005.pdf'
        raw.write_bytes(b'existing')
        transport = MockTransport([])
        result = direct.acquire_one(self.root, SESSION, transport=transport)
        self.assertEqual(result['status'], 'SKIP_EXISTING_LOCAL')
        self.assertFalse(transport.calls)
        self.assertEqual(raw.read_bytes(), b'existing')

    def test_registered_renamed_raw_is_excluded(self):
        raw = self.root / 'data/outcome_input/manual_jpx/renamed.pdf'
        raw.write_bytes(b'existing')
        self.manifest.write_text(json.dumps(dict(entries=[dict(session_date=SESSION, filename=raw.name, sha256=core.sha256(raw))])))
        transport = MockTransport([])
        self.assertEqual(direct.acquire_one(self.root, SESSION, transport=transport)['status'], 'SKIP_EXISTING_LOCAL')
        self.assertFalse(transport.calls)

    def test_existing_hash_conflict_does_not_redownload(self):
        raw = self.root / 'data/outcome_input/manual_jpx/renamed.pdf'
        raw.write_bytes(b'corrupt')
        self.manifest.write_text(json.dumps(dict(entries=[dict(session_date=SESSION, filename=raw.name, sha256='f' * 64)])))
        transport = MockTransport([])
        with self.assertRaisesRegex(ValueError, 'HASH_CONFLICT'):
            direct.acquire_one(self.root, SESSION, transport=transport)
        self.assertFalse(transport.calls)

    def test_unknown_local_raw_blocks_without_fetch(self):
        (self.root / 'data/outcome_input/manual_jpx/unknown.pdf').write_bytes(b'unknown')
        transport = MockTransport([])
        with self.assertRaisesRegex(ValueError, 'UNREGISTERED_LOCAL_RAW'):
            direct.acquire_one(self.root, SESSION, transport=transport)
        self.assertFalse(transport.calls)

    def test_unlisted_does_not_make_pdf_request(self):
        transport = MockTransport([response(listing('2026-10-06'))])
        self.assertEqual(direct.acquire_one(self.root, SESSION, transport=transport)['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(transport.calls, [direct.LISTING])

    def test_outside_allowlist_and_latest_fresh_session_rejected(self):
        for session in ('2026-10-08', '2026-10-09', '2026-09-15'):
            with self.subTest(session=session), self.assertRaisesRegex(ValueError, 'NOT_APPROVED'):
                direct.acquire_one(self.root, session, transport=MockTransport([]))

    def test_no_implicit_live_transport(self):
        with self.assertRaises(TypeError):
            direct.acquire_one(self.root, SESSION)

    def test_interrupted_attempt_cannot_be_retried(self):
        folder = self.root / 'data/jpx_historical_direct' / SESSION
        folder.mkdir(parents=True)
        (folder / 'start.json').write_text('{}')
        transport = MockTransport([])
        with self.assertRaises(FileExistsError):
            direct.acquire_one(self.root, SESSION, transport=transport)
        self.assertFalse(transport.calls)

    def test_restriction_stops_next_session_and_keeps_failure(self):
        transport = MockTransport([response(status=403)])
        with self.assertRaisesRegex(ValueError, 'HTTP_403'):
            direct.acquire_one(self.root, SESSION, transport=transport)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            direct.acquire_one(self.root, '2026-10-06', transport=transport)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(probe.core.read_frozen(self.root / 'data/jpx_historical_direct/STOP.json')['reason'], 'HTTP_403')

    def test_parser_wrong_session_keeps_raw_and_never_retries(self):
        transport = MockTransport([response(), response(self.pdf, url=PDF_URL, content_type='application/pdf')])
        with self.assertRaisesRegex(ValueError, 'DATA_CONFLICT'):
            direct.acquire_one(self.root, SESSION, transport=transport)
        folder = self.root / 'data/jpx_historical_direct' / SESSION
        self.assertEqual((folder / 'stq_20261005.pdf').read_bytes(), self.pdf)
        self.assertEqual(core.read_frozen(folder / 'acquisition.json')['acquisition_method'], direct.METHOD)
        self.assertTrue((self.root / 'data/jpx_historical_direct/STOP.json').is_file())
        self.assertFalse((folder / 'result.json').exists())

    def test_real_parser_validation_direct_metadata_and_create_only_evidence(self):
        manifest_before = self.manifest.read_bytes()
        result, transport, folder = self.valid_acquisition()
        self.assertEqual(result['status'], 'VALIDATED_PENDING_EXPLICIT_REGISTRATION')
        self.assertEqual(transport.calls, [direct.LISTING, PDF_URL])
        self.assertEqual(self.manifest.read_bytes(), manifest_before)
        receipt = core.read_frozen(folder / 'acquisition.json')
        parsed = core.read_frozen(folder / 'parsed.json')
        self.assertEqual(receipt['raw_sha256'], hashlib.sha256(self.pdf).hexdigest())
        self.assertEqual(receipt['listing_sha256'], core.sha256(folder / 'listing.html'))
        self.assertEqual(receipt['exact_href'], HREF)
        self.assertEqual(receipt['listing_observed_at'], NOW.isoformat())
        self.assertEqual(receipt['completed_at'], NOW.isoformat())
        self.assertEqual(receipt['source_role'], 'OUTCOME_ONLY')
        self.assertIsNone(receipt['browser_actor'])
        for item in [parsed, *parsed['records']]:
            self.assertEqual(item['acquisition_method'], direct.METHOD)
            self.assertIsNone(item['browser_actor'])
        self.assertTrue(all(r['acquisition'] == direct.METHOD and r['provider'] == 'JPX_DAILY_REPORT' for r in parsed['records']))
        self.assertEqual(parsed['core6_extraction'], '6/6')
        self.assertEqual(parsed['normalized_schema_validation'], 'PASS')
        self.assertEqual(parsed['source_file_hash'], receipt['raw_sha256'])
        self.assertEqual(core.read_frozen(folder / 'result.json')['manifest_entry_candidate']['acquisition_origin'], direct.METHOD)
        before = {p.name:p.read_bytes() for p in folder.iterdir()}
        with self.assertRaises(FileExistsError):
            probe._seal(folder / 'acquisition.json', {'overwrite': True})
        self.assertEqual(direct.acquire_one(self.root, '2026-09-15', transport=MockTransport([]))['status'], 'SKIP_EXISTING_LOCAL')
        self.assertEqual({p.name:p.read_bytes() for p in folder.iterdir()}, before)
        # Existing Manifest/Raw/Parsed validation can read the candidate after explicit registration.
        self.manifest.write_text(json.dumps(dict(entries=[result['manifest_entry_candidate']])))
        with patch.object(core, 'ROOT', self.root):
            validated, provenance = probe._validated_jpx(folder / 'parsed.json', '2026-09-15', direct._now().replace(hour=23))
        self.assertEqual(validated['core6_extraction'], '6/6')
        self.assertEqual(provenance['raw_sha256'], receipt['raw_sha256'])

    def test_direct_cannot_claim_browser_actor(self):
        with self.assertRaisesRegex(ValueError, 'BROWSER_ACTOR_CONFLICT'):
            parser.parse(Path('/unused.pdf'), SESSION, direct.METHOD, 'GOOGLE_CHROME_VISIBLE_RDC')

    def test_soft_resume_revalidates_real_parser_without_download(self):
        # Exercise a real metadata failure and its actual validator, rather than
        # making the validator throw or deriving PASS from a claimed string.
        fix = self.root / 'config/test_manifest_metadata.json'
        fix.write_text('{"parser_status":"FAIL"}')
        old_fix_hash = core.sha256(fix)
        validator = direct.gap.stage_existing_pdf
        def metadata_adapter(parsed, manifest, *args, **kwargs):
            entries = [dict(entry, parser_status=json.loads(fix.read_bytes())['parser_status'])
                       for entry in manifest['entries']]
            return validator(parsed, dict(entries=entries), *args, **kwargs)
        adapter = patch.object(direct.gap, 'stage_existing_pdf', side_effect=metadata_adapter)
        adapter.start()
        self.addCleanup(adapter.stop)
        with self.assertRaisesRegex(ValueError, 'MANIFEST_NOT_VALIDATED'):
            self.valid_acquisition()
        store = self.root / 'data/jpx_historical_direct'
        item = direct.lifecycle.Stops(store).index['active'][0]
        old_stop = Path(item['reference']['path']).read_bytes()
        fix.write_text('{"parser_status":"PASS"}')
        self.assertNotEqual(core.sha256(fix), old_fix_hash)
        fixes = [direct.lifecycle.reference(fix)]
        calendar = json.loads((self.root / 'analysis/calendar_validation_2026-09-12.json').read_bytes())
        receipt, raw = direct._verified_acquisition(store / '2026-09-15', '2026-09-15')
        checks = {}
        for check in ('fixture', 'regression'):
            folder = self.root / check
            folder.mkdir()
            entry = direct._candidate(folder, receipt, raw, '2026-09-15', calendar)
            parsed = core.read_frozen(folder / 'parsed.json')
            layer = json.loads((folder / 'validated_layer.json').read_bytes())
            self.assertEqual(layer['core6_count'], 6)
            self.assertEqual(parsed['normalized_schema_validation'], 'PASS')
            self.assertEqual(entry['sha256'], core.sha256(raw))
            checks[check] = parsed['normalized_schema_validation']
        report = self.root / 'regression.json'
        probe._seal(report, dict(status=parsed['normalized_schema_validation'], checks=checks, fix_refs=fixes,
                               validation_outputs=[direct.lifecycle.reference(self.root / check / 'parsed.json')
                                                   for check in checks]))
        resolution = self.root / 'resolution.json'
        probe._seal(resolution, dict(stop_reference=item['reference'], scope='session', target='2026-09-15',
            root_cause='Local manifest validation bug fixed', scope_isolation='PASS', fix_refs=fixes,
            validation_refs=[direct.lifecycle.reference(report)], evidence_refs=item['record']['evidence_refs']))
        transport = MockTransport([])
        result = direct.acquire_one(self.root, '2026-09-15', transport=transport, resolution=resolution)
        self.assertEqual(result['state'], 'PASS')
        self.assertEqual(result['aggregate']['validated_tickers'], 6)
        self.assertFalse(transport.calls)
        self.assertEqual(direct.lifecycle.read_reference(result['resumed_reference'])['state'], 'RESUMED')
        self.assertEqual(Path(item['reference']['path']).read_bytes(), old_stop)
        self.assertEqual(direct.lifecycle.Stops(store).index['resumed'][0]['resume_event_reference'], result['resumed_reference'])

    def test_real_pdf_parser_octet_stream_warns_after_full_validation(self):
        self.policy['historical_direct']['allowed_sessions'] = ['2026-09-15']
        self.policy['historical_direct']['forecast_target'] = '2026-09-18'
        self.save_policy()
        transport = MockTransport([response(listing('2026-09-15')),
                                   response(self.pdf, url=PDF_URL, content_type='binary/octet-stream')])
        result = direct.acquire_one(self.root, '2026-09-15', transport=transport)
        self.assertEqual(result['state'], 'WARN')
        self.assertEqual(result['aggregate']['validated_tickers'], 6)
        entry = result['manifest_entry_candidate']
        self.assertEqual(entry['source_metadata']['content_type'], 'binary/octet-stream')
        self.assertEqual(core.read_frozen(Path(entry['normalized_output_path']))['normalized_schema_validation'], 'PASS')
        self.assertEqual(direct.acquire_one(self.root, '2026-09-15', transport=transport)['local_validation'], 'PASS')
        self.assertEqual(len(transport.calls), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
