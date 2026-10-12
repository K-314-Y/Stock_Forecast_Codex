"""Public-tree, offline manual-review tests; every artifact is synthetic/temporary."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from engine import minimal_trial as core
from providers.jp.jpx_manual import historical_direct as direct
from providers.jp.jpx_manual import historical_policy_v2 as policy
from providers.jp.jpx_manual import legacy_stop_review as review

PROJECT = Path(__file__).resolve().parents[1]
SESSION = '2026-10-05'
BASE = '/automation/markets/statistics-equities/daily/json/'
HREF = '/automation/markets/statistics-equities/daily/files/202610/stq_20261005.pdf'
HTML = ('''<html><title>東京証券取引所日報</title><table><tr><th>日付</th>
<th>概算・精算表</th><th>株式相場表</th><th>債券相場表</th></tr><tbody></tbody></table>
<script>const BASE_PATH = "''' + BASE + '''";
const BUCKNUMBER_JSON = BASE_PATH + "tsedaily_report_monthlylist.json";
const jsonUrl = `${BASE_PATH}tsedaily_report_${month}.json`;
item.Month; data.TableDatas; row.TradeDate; 'Stocks'; row[key];</script></html>''').encode()


def response(body, url=direct.LISTING, kind='text/html'):
    return direct.Response(url, 200, {'Content-Type': kind}, body)


def jr(rows, url='https://www.jpx.co.jp/fixture.json'):
    return response(json.dumps(dict(TableDatas=rows)).encode(), url, 'application/json')


class JSONReviewRegressionTests(unittest.TestCase):
    def test_old_empty_html_reason_is_reproduced(self):
        with self.assertRaisesRegex(ValueError, '^LISTING_EMPTY_OR_AMBIGUOUS$'):
            direct.listing_link(response(HTML), SESSION)

    def test_json_endpoint_extraction_uses_observed_program(self):
        discovery = direct.json_endpoints(response(HTML))
        self.assertEqual(discovery['monthly_list_endpoint'], 'https://www.jpx.co.jp' + BASE + 'tsedaily_report_monthlylist.json')
        changed = HTML.replace(BASE.encode(), b'/explicit/').replace(b'tsedaily_report_', b'published_')
        observed = direct.json_endpoints(response(changed))
        self.assertEqual(observed['monthly_list_endpoint'], 'https://www.jpx.co.jp/explicit/published_monthlylist.json')

    def test_monthly_list_requires_published_month(self):
        discovery = direct.json_endpoints(response(HTML))
        self.assertEqual(direct.monthly_json_url(jr([dict(Month='202610')]), discovery, SESSION),
                         'https://www.jpx.co.jp' + BASE + 'tsedaily_report_202610.json')
        self.assertIsNone(direct.monthly_json_url(jr([dict(Month='202609')]), discovery, SESSION))
        for rows in ([{}], [dict(Month='202610')] * 2):
            with self.assertRaises(ValueError):
                direct.monthly_json_url(jr(rows), discovery, SESSION)

    def test_monthly_json_exact_oct5_href(self):
        link = direct.json_stock_link(jr([dict(TradeDate='20261005', Stocks=HREF)]), SESSION)
        self.assertEqual(link['exact_href'], HREF)
        self.assertEqual(link['resolved_pdf_url'], 'https://www.jpx.co.jp' + HREF)

    def test_no_guessing_when_href_or_program_absent(self):
        self.assertIsNone(direct.json_stock_link(jr([dict(TradeDate='20261005', Stocks=None)]), SESSION))
        self.assertIsNone(direct.json_stock_link(jr([]), SESSION))
        with self.assertRaisesRegex(ValueError, 'JSON_DISCOVERY'):
            direct.json_endpoints(response(HTML.replace(b'BASE_PATH', b'OTHER_PATH')))
        link = direct.json_stock_link(jr([dict(TradeDate='20261005', Stocks='/published/arbitrary-X.pdf')]), SESSION)
        self.assertEqual(link['resolved_pdf_url'], 'https://www.jpx.co.jp/published/arbitrary-X.pdf')

    def test_official_host_and_schema_security_are_enforced(self):
        for href in ('https://evil.test/a.pdf', '/not-pdf.html'):
            with self.assertRaises(ValueError):
                direct.json_stock_link(jr([dict(TradeDate='20261005', Stocks=href)]), SESSION)
        with self.assertRaisesRegex(ValueError, 'NON_OFFICIAL'):
            direct.json_endpoints(response(HTML.replace(BASE.encode(), b'https://evil.test/')))
        with self.assertRaises(ValueError):
            direct.json_stock_link(jr([dict(TradeDate='20261005')]), SESSION)


class LegacyReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = self.root / 'data/jpx_historical_direct'
        self.store.mkdir(parents=True)
        self.analysis = self.root / 'analysis'
        self.analysis.mkdir()
        for name in review.FIX_PATHS:
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((PROJECT / name).read_bytes())
        self.fixes = [policy.reference(self.root / name) for name in review.FIX_PATHS]
        self.old = dict(acquisition_method=direct.METHOD, automatic_retry=False,
                        failed_at='2026-10-09T12:01:24.562885+00:00', reason=review.REASON,
                        session=SESSION, status='REVIEW_REQUIRED')
        policy.seal(self.store / 'STOP.json', self.old)
        self.stop_ref = policy.reference(self.store / 'STOP.json')
        self.before = (self.store / 'STOP.json').read_bytes()
        discovery = direct.json_endpoints(response(HTML))
        monthly_url = direct.monthly_json_url(jr([dict(Month='202610')]), discovery, SESSION)
        responses = dict(listing=response(HTML),
                         monthly_list=jr([dict(Month='202610')], discovery['monthly_list_endpoint']),
                         monthly=jr([dict(TradeDate='20261005', Stocks=HREF)], monthly_url))
        self.inputs, chains = {}, {}
        for label, value in responses.items():
            path = self.analysis / (label + '.body')
            path.write_bytes(value.body)
            self.inputs[label] = policy.reference(path)
            # Known legacy schema alias, independently hash-bound by the review.
            chains[label] = [dict(url=value.url, status=200, headers=value.headers,
                                 sha256=core.sha256(path))]
        evidence = dict(schema_version='jpx-json-discovery-v1', status='HREF_DISCOVERED',
                        session=SESSION, acquisition_method=direct.METHOD, pdf_requests=0,
                        listing_url=direct.LISTING, listing_sha256=self.inputs['listing']['sha256'],
                        discovery=discovery, monthly_list_url=discovery['monthly_list_endpoint'],
                        monthly_list_sha256=self.inputs['monthly_list']['sha256'],
                        monthly_json_reference_url=monthly_url, monthly_json_sha256=self.inputs['monthly']['sha256'],
                        redirect_chains=chains, link=direct.json_stock_link(responses['monthly'], SESSION))
        path = self.analysis / 'discovery.json'
        policy.seal(path, evidence)
        self.evidence = policy.reference(path)
        self.report_path = self.store / 'offline-pass.json'
        # Synthetic PASS reports are test inputs, not production execution evidence.
        report = dict(schema_version=review.VALIDATION_SCHEMA, status='PASS', fix_refs=self.fixes,
                      previous_stop_reference=self.stop_ref, review_evidence=self.evidence,
                      discovery_inputs=self.inputs, live_jpx_requests=0,
                      checks={key: 'PASS' for key in review.CHECKS}, suites={name: dict(
                          status='PASS', tests_run=1, failures=0, errors=0) for name in review.SUITES})
        policy.seal(self.report_path, report)
        self.arguments = dict(expected_stop_sha256=self.stop_ref['sha256'], review_decision='APPROVED',
                              root_cause='Listing rows are populated by the observed official JS/JSON program.',
                              resolution='Use the current structured official JSON discovery.',
                              review_evidence=self.evidence, discovery_inputs=self.inputs,
                              fix_refs=self.fixes, validation_refs=[policy.reference(self.report_path)])
        self.guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('LIVE_REQUEST_FORBIDDEN')).start()
        self.addCleanup(patch.stopall)

    def release(self, **changes):
        return review.manual_review_release(self.root, **{**self.arguments, **changes})

    def test_valid_exact_manual_release_passes_gate_and_preserves_stop(self):
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE:HARD_STOP_LEGACY_UNCLASSIFIED'):
            policy.Stops(self.store).gate(SESSION)
        ref = self.release()
        receipt = core.read_frozen(ref['path'])
        self.assertEqual(receipt['state'], 'REVIEWED_RELEASE')
        self.assertEqual(receipt['review_action'], 'EXPLICIT_MANUAL_FUNCTION')
        self.assertNotIn('reviewer_identity', receipt)
        self.assertNotIn('signature', receipt)
        stops = policy.Stops(self.store)
        self.assertIsNone(stops.gate(SESSION))
        self.assertIsNone(stops.gate('2026-10-08'))
        self.assertEqual(len(stops.index['active']), 1)
        self.assertEqual((self.store / 'STOP.json').read_bytes(), self.before)
        self.assertEqual(policy.reference(self.store / 'STOP.json'), self.stop_ref)
        self.guard.assert_not_called()

    def test_wrong_stop_sha_fails_closed(self):
        with self.assertRaises(ValueError):
            self.release(expected_stop_sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            policy.Stops(self.store).gate(SESSION)

    def test_stop_modified_or_missing_after_release_fails_closed(self):
        self.release()
        path = self.store / 'STOP.json'
        path.write_bytes(self.before + b' ')
        with self.assertRaises(ValueError):
            policy.Stops(self.store)
        path.unlink()
        with self.assertRaises(ValueError):
            policy.Stops(self.store)

    def test_receipt_corrupted_or_missing_fails_closed(self):
        ref = self.release()
        stops = policy.Stops(self.store)
        path = Path(ref['path'])
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'REVIEWED_LEGACY_RELEASE_INVALID'):
            stops.gate(SESSION)  # Also checks an already-loaded object.
        path.unlink()
        with self.assertRaises(ValueError):
            policy.Stops(self.store)

    def test_receipt_sidecar_missing_fails_closed(self):
        ref = self.release()
        Path(ref['path']).with_suffix('.sha256').unlink()
        with self.assertRaisesRegex(ValueError, 'REVIEWED_LEGACY_RELEASE_INVALID'):
            policy.Stops(self.store)

    def test_wrong_legacy_reason_and_all_forbidden_reasons_are_rejected(self):
        for reason in ('OTHER_UNKNOWN', 'HTTP_403', 'SECURITY_CHALLENGE', 'CAPTCHA', 'ACCESS_DENIED',
                       'BOT_CHALLENGE', 'HASH_CONFLICT', 'PROVENANCE_CONFLICT', 'OHLCV_INVALID', 'SESSION_CONFLICT'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = root / 'data/jpx_historical_direct'
                store.mkdir(parents=True)
                policy.seal(store / 'STOP.json', {**self.old, 'reason': reason})
                with self.assertRaises(ValueError):
                    review.manual_review_release(root, **{**self.arguments,
                        'expected_stop_sha256': core.sha256(store / 'STOP.json')})
                self.assertFalse((store / review.FOLDER).exists())

    def test_current_v2_hard_stop_cannot_be_reviewed(self):
        for reason in ('HTTP_403', 'SECURITY_CHALLENGE', 'HASH_CONFLICT', 'PROVENANCE_CONFLICT', review.REASON):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = root / 'data/jpx_historical_direct'
                store.mkdir(parents=True)
                stops = policy.Stops(store)
                ref = stops.stop(reason, SESSION, evidence_refs=[], last_success=None, run_id='test', retry_count=0)
                with self.assertRaises(ValueError):
                    review.manual_review_release(root, **{**self.arguments, 'expected_stop_sha256': ref['sha256']})

    def test_other_active_hard_stops_remain_effective(self):
        self.release()
        for reason in ('HTTP_403', 'SECURITY_CHALLENGE', 'HASH_CONFLICT', 'PROVENANCE_CONFLICT'):
            stops = policy.Stops(self.store)
            stops.stop(reason, SESSION, evidence_refs=[], last_success=None, run_id='test', retry_count=0)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE:HTTP_403'):
            policy.Stops(self.store).gate('2026-10-08')

    def test_restart_in_fresh_process_revalidates_release(self):
        self.release()
        code = ('from providers.jp.jpx_manual.historical_policy_v2 import Stops; '
                'import sys; assert Stops(sys.argv[1]).gate("2026-10-05") is None')
        result = subprocess.run([sys.executable, '-c', code, str(self.store)], cwd=PROJECT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_approval_is_required_and_release_is_create_only(self):
        with self.assertRaisesRegex(ValueError, 'EXPLICIT_MANUAL_APPROVAL_REQUIRED'):
            self.release(review_decision='PENDING')
        ref = self.release()
        before = Path(ref['path']).read_bytes()
        with self.assertRaises(FileExistsError):
            self.release()
        self.assertEqual(Path(ref['path']).read_bytes(), before)

    def test_fix_and_validation_and_review_and_input_damage_fail_closed(self):
        self.release()
        for path in (self.root / review.FIX_PATHS[0], self.report_path,
                     Path(self.evidence['path']), Path(self.inputs['monthly']['path'])):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                path.write_bytes(before + b' ')
                with self.assertRaises(ValueError):
                    policy.Stops(self.store)
                path.write_bytes(before)

    def test_validation_must_be_sealed_and_include_all_pass_suites(self):
        self.report_path.with_suffix('.sha256').unlink()
        with self.assertRaises(ValueError):
            self.release()
        with self.assertRaises(ValueError):
            self.release(validation_refs=[])

    def test_validation_failed_or_missing_suite_or_discovery_check_is_rejected(self):
        original = core.read_frozen(self.report_path)
        for change in ({'status': 'FAIL'}, {'suites': {}}, {'checks': {}}, {'live_jpx_requests': 1},
                       {'suites': {'full_offline': dict(status='PASS', tests_run=1, failures=1, errors=0)}}):
            with self.subTest(change=change):
                self.report_path.write_bytes(core.canonical_bytes({**original, **change}))
                self.report_path.with_suffix('.sha256').write_text(core.sha256(self.report_path) + '\n')
                with self.assertRaises(ValueError):
                    self.release(validation_refs=[policy.reference(self.report_path)])

    def test_replay_cannot_accept_external_redirect_or_fabricated_link(self):
        path = Path(self.evidence['path'])
        original = core.read_frozen(path)
        for change in ({'link': {**original['link'], 'resolved_pdf_url': 'https://evil.test/a.pdf'}},
                       {'redirect_chains': {**original['redirect_chains'], 'monthly': [
                           {**original['redirect_chains']['monthly'][0], 'url': 'https://evil.test/a.json'}]}}):
            with self.subTest(change=change):
                path.write_bytes(core.canonical_bytes({**original, **change}))
                path.with_suffix('.sha256').write_text(core.sha256(path) + '\n')
                with self.assertRaises(ValueError):
                    review.replay_discovery(self.root, policy.reference(path), self.inputs, SESSION)

    def test_body_hash_is_not_confused_with_metadata_sidecar(self):
        path = Path(self.inputs['monthly']['path'])
        policy.seal(path.with_suffix('.json'), dict(sha256=self.inputs['monthly']['sha256']))
        self.assertNotEqual(path.with_suffix('.sha256').read_text().strip(), self.inputs['monthly']['sha256'])
        self.release()
        self.assertIsNone(policy.Stops(self.store).gate(SESSION))

    def test_receipt_resealed_with_wrong_state_scope_or_previous_content_is_rejected(self):
        ref = self.release()
        original = core.read_frozen(ref['path'])
        path = Path(ref['path'])
        for change in ({'state': 'ACTIVE'}, {'scope': 'session'}, {'previous_stop_contents': {}},
                       {'review_decision': 'PENDING'}, {'schema_version': 'unknown'}, {'fix_refs': []}):
            with self.subTest(change=change):
                path.write_bytes(core.canonical_bytes({**original, **change}))
                path.with_suffix('.sha256').write_text(core.sha256(path) + '\n')
                envelope = json.loads((self.store / 'current_stops_v2.json').read_bytes())
                envelope['index']['reviewed_legacy_releases'][0]['receipt_reference'] = policy.reference(path)
                policy.write_current(self.store / 'current_stops_v2.json', envelope['index'])
                with self.assertRaisesRegex(ValueError, 'REVIEWED_LEGACY_RELEASE_INVALID'):
                    policy.Stops(self.store)

    def test_lost_release_index_fails_closed(self):
        self.release()
        index = json.loads((self.store / 'current_stops_v2.json').read_bytes())['index']
        index.pop('reviewed_legacy_releases')
        policy.write_current(self.store / 'current_stops_v2.json', index)
        with self.assertRaisesRegex(ValueError, 'REVIEWED_LEGACY_RELEASE_INVALID'):
            policy.Stops(self.store)

    def test_crash_after_receipt_before_index_registration_fails_closed(self):
        policy.Stops(self.store)  # Complete the old legacy migration first.
        with patch.object(policy.Stops, 'save', side_effect=KeyboardInterrupt('index interrupted')):
            with self.assertRaises(KeyboardInterrupt):
                self.release()
        self.assertTrue(list((self.store / review.FOLDER).glob('*.json')))
        with self.assertRaisesRegex(ValueError, 'REVIEWED_LEGACY_RELEASE_INVALID'):
            policy.Stops(self.store)
        self.assertEqual((self.store / 'STOP.json').read_bytes(), self.before)

    def test_crash_between_receipt_and_sidecar_fails_closed(self):
        link = policy.os.link
        def interrupt(source, target):
            if Path(target).suffix == '.sha256':
                raise KeyboardInterrupt('sidecar interrupted')
            return link(source, target)
        with patch.object(policy.os, 'link', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.release()
        with self.assertRaisesRegex(ValueError, 'REVIEWED_LEGACY_RELEASE_INVALID'):
            policy.Stops(self.store)

    def test_legacy_stop_with_damaged_sidecar_cannot_be_released(self):
        (self.store / 'STOP.sha256').write_text('0' * 64)
        with self.assertRaises(ValueError):
            self.release()

    def test_automatic_soft_resume_still_refuses_legacy_hard(self):
        stops = policy.Stops(self.store)
        with self.assertRaisesRegex(ValueError, 'AUTOMATIC_RESUME_FORBIDDEN'):
            stops.resume(stops.index['active'][0], None, evidence_validation='PASS', run_id='test', retry_count=0, attempt='test')


if __name__ == '__main__':
    unittest.main()
