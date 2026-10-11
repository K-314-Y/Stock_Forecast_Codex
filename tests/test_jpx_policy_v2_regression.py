"""Offline reproductions of the v2 regression-review findings."""
from datetime import timedelta
import inspect
import multiprocessing
import json
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import call, patch

import test_jpx_policy_v2 as fixtures
from test_jpx_policy_v2 import Transport, response, html, SESSION, OTHER, NOW, PDF_URL
from providers.jp.jpx_manual import historical_direct as d
from providers.jp.jpx_manual import historical_policy_v2 as policy


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.HistoricalPolicyTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def assert_hard(self, code, scope, old=None):
        items = policy.Stops(self.f.store).index['active']
        hard = [x for x in items if x['record']['stop_class'] == 'HARD_STOP']
        self.assertEqual(len(hard), 1)
        self.assertEqual(hard[0]['record']['stop_code'], code)
        self.assertEqual(hard[0]['record']['scope'], scope)
        if old:
            self.assertIn(old, items)
            self.assertEqual(hard[0]['record']['previous_stop_reference'], old['reference'])
        return hard[0]

    def test_resume_hash_conflict_escalates_and_blocks_other_session(self):
        item = self.f.soft_failure()
        original = Path(item['reference']['path']).read_bytes()
        resolution = self.f.resolution(item)
        (self.f.store / SESSION / 'listing.html').write_bytes(b'altered provenance')
        transport = Transport([])
        with self.assertRaisesRegex(ValueError, 'PROVENANCE_OR_HASH_CONFLICT'):
            d.acquire_one(self.f.root, SESSION, transport=transport, resolution=resolution)
        hard = self.assert_hard('ACQUISITION_PROVENANCE_OR_HASH_CONFLICT', 'global', item)
        self.assertTrue(hard['record']['evidence_refs'])
        self.assertEqual(Path(item['reference']['path']).read_bytes(), original)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.f.root, OTHER, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_resume_source_identity_conflict_is_global_hard(self):
        d.acquire_one(self.f.root, SESSION, transport=self.f.acquisition_transport())
        self.f.legacy(dict(reason='MANIFEST_NOT_VALIDATED', session=SESSION))
        item = policy.Stops(self.f.store).index['active'][0]
        resolution = self.f.resolution(item)
        path = self.f.store / SESSION / 'acquisition.json'
        receipt = fixtures.core.read_frozen(path)
        receipt['resolved_pdf_url'] = 'https://www.jpx.co.jp/published/different.pdf'
        # Fault injection: valid byte digest cannot hide contradictory source identity.
        path.write_bytes(fixtures.core.canonical_bytes(receipt))
        path.with_suffix('.sha256').write_text(fixtures.core.sha256(path))
        transport = Transport([])
        with self.assertRaisesRegex(ValueError, 'SOURCE_IDENTITY_MISMATCH'):
            d.acquire_one(self.f.root, SESSION, transport=transport, resolution=resolution)
        self.assert_hard('SOURCE_IDENTITY_MISMATCH', 'global', item)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.f.root, OTHER, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_resume_integrity_stop_precedes_failure_log_interruption(self):
        item = self.f.soft_failure()
        resolution = self.f.resolution(item)
        (self.f.store / SESSION / 'listing.html').write_bytes(b'corrupt')
        seal = fixtures.frozen._seal
        def interrupt(path, record):
            if Path(path).name.startswith('local-failure-'):
                raise KeyboardInterrupt('failure log interruption')
            return seal(path, record)
        transport = Transport([])
        with patch.object(fixtures.frozen, '_seal', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            d.acquire_one(self.f.root, SESSION, transport=transport, resolution=resolution)
        self.assert_hard('ACQUISITION_PROVENANCE_OR_HASH_CONFLICT', 'global', item)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.f.root, OTHER, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_resume_ticker_mismatch_is_session_hard_and_other_session_is_independent(self):
        item = self.f.soft_failure()
        parse = self.f.parser.side_effect
        def wrong_ticker(*args, **kwargs):
            parsed = parse(*args, **kwargs)
            parsed['records'][0]['ticker'] = 'unknown'
            return parsed
        transport = Transport([])
        with patch.object(d.parser, 'parse', side_effect=wrong_ticker):
            with self.assertRaisesRegex(ValueError, 'CORE6_OR_DUPLICATE_CONFLICT'):
                d.acquire_one(self.f.root, SESSION, transport=transport, resolution=self.f.resolution(item))
        self.assert_hard('CORE6_OR_DUPLICATE_CONFLICT', 'session', item)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.f.root, SESSION, transport=transport, resolution=self.f.resolution(item))
        self.assertEqual(transport.calls, [])
        other = Transport([response(html(SESSION))])
        self.assertEqual(d.acquire_one(self.f.root, OTHER, transport=other)['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(len(other.calls), 1)

    def test_pdf_prefix_on_429_does_not_hide_captcha(self):
        transport = Transport([response(b'%PDF-1.4\n<title>CAPTCHA</title>', status=429,
                                        kind='application/pdf', **{'Retry-After': '0'})])
        with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assert_hard('SECURITY_CHALLENGE', 'host')
        self.assertEqual(transport.calls, [d.LISTING])
        self.f.sleep.assert_not_called()

    def test_html_recaptcha_widget_stops_before_pdf(self):
        transport = Transport([response(html() + b'<div class="g-recaptcha" data-sitekey="test"></div>')])
        with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assert_hard('SECURITY_CHALLENGE', 'host')
        self.assertEqual(transport.calls, [d.LISTING])
        self.f.sleep.assert_not_called()

    def test_normal_html_and_pdf_do_not_false_positive(self):
        self.assertFalse(d._challenge(response(html() + b'<p>CAPTCHA documentation</p>')))
        self.assertFalse(d._challenge(response(self.f.pdf, PDF_URL, kind='application/pdf')))
        result = d.acquire_one(self.f.root, SESSION, transport=self.f.acquisition_transport())
        self.assertEqual(result['state'], 'PASS')

    def test_challenge_widget_below_long_html_header_stops_before_pdf(self):
        transport = Transport([response(html() + b' ' * 70000 + b'<div class="g-recaptcha"></div>')])
        with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assert_hard('SECURITY_CHALLENGE', 'host')
        self.assertEqual(transport.calls, [d.LISTING])

    def test_all_widget_attributes_are_inspected_and_plain_pdf_words_are_safe(self):
        self.assertTrue(d._challenge(response(b'<div class="g-recaptcha" id="ordinary"></div>')))
        writer, buffer = fixtures.PdfWriter(), BytesIO()
        writer.add_blank_page(width=200, height=200)
        writer.add_metadata({'/Title': 'CAPTCHA documentation'})
        writer.write(buffer)
        self.assertFalse(d._challenge(response(buffer.getvalue(), PDF_URL, kind='application/pdf')))

    def test_diagnostic_has_no_context_gate_bypass(self):
        self.f.legacy(dict(reason='HTTP_403', session=SESSION))
        transport = Transport([])
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE:HTTP_403'):
            d.discover_json_one(self.f.root, OTHER, transport=transport,
                                evidence_folder=self.f.root / 'new', run_state=d.RunState('new-run'))
        self.assertNotIn('_context', inspect.signature(d.discover_json_one).parameters)
        with self.assertRaises(TypeError):
            d.discover_json_one(self.f.root, OTHER, transport=transport,
                                evidence_folder=self.f.root / 'custom', _context=d.RequestContext())
        self.assertEqual(transport.calls, [])

    def test_security_stop_is_durable_before_run_save_interruption(self):
        save = d.RunState.save
        def interrupt(run):
            save(run)
            if run.blocked_hosts:
                raise KeyboardInterrupt('simulated process interruption')
        transport = Transport([response(status=403)])
        with patch.object(d.RunState, 'save', interrupt), self.assertRaises(KeyboardInterrupt):
            d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assert_hard('HTTP_403', 'host')
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            d.acquire_one(self.f.root, OTHER, transport=transport, run_state=d.RunState('after-crash'))
        self.assertEqual(transport.calls, [d.LISTING])

    def receipt_corruption(self, corrupt):
        item = self.f.soft_failure()
        resolution = self.f.resolution(item)
        result = d.acquire_one(self.f.root, SESSION, transport=Transport([]), resolution=resolution)
        event = Path(result['resumed_reference']['path'])
        if corrupt:
            event.write_bytes(b'{}')
        else:
            event.unlink()
        transport = Transport([])
        with self.assertRaisesRegex(ValueError, 'STOP_.*(CONFLICT|MISSING)'):
            d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assertEqual(transport.calls, [])
        self.assertTrue(Path(item['reference']['path']).is_file())

    def test_security_stop_precedes_interruptible_response_logging(self):
        transport = Transport([response(status=403)])
        with patch.object(d.RequestContext, 'capture', side_effect=KeyboardInterrupt('logging interrupted')):
            with self.assertRaises(KeyboardInterrupt):
                d.acquire_one(self.f.root, SESSION, transport=transport)
        hard = self.assert_hard('HTTP_403', 'host')
        self.assertEqual(hard['record']['evidence']['response']['status'], 403)
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE:HTTP_403'):
            d.acquire_one(self.f.root, OTHER, transport=transport, run_state=d.RunState('new-after-log-crash'))
        self.assertEqual(len(transport.calls), 1)

    def test_missing_resume_receipt_fails_closed(self):
        self.receipt_corruption(False)

    def test_corrupt_resume_receipt_fails_closed(self):
        self.receipt_corruption(True)

    def test_resume_index_references_durable_release_graph(self):
        item = self.f.soft_failure()
        resolution = self.f.resolution(item)
        result = d.acquire_one(self.f.root, SESSION, transport=Transport([]), resolution=resolution)
        release = policy.Stops(self.f.store).index['resumed'][0]
        self.assertEqual(release['previous_stop_reference'], item['reference'])
        self.assertEqual(release['resolution_reference'], policy.reference(resolution))
        self.assertEqual(release['resume_event_reference'], result['resumed_reference'])

    def test_resume_missing_sidecar_and_old_unreferenced_release_fail_closed(self):
        item = self.f.soft_failure()
        result = d.acquire_one(self.f.root, SESSION, transport=Transport([]), resolution=self.f.resolution(item))
        event = Path(result['resumed_reference']['path'])
        event.with_suffix('.sha256').unlink()
        with self.assertRaisesRegex(ValueError, 'STOP_RESUME_RECEIPT_CONFLICT'):
            policy.Stops(self.f.store)
        index = json.loads((self.f.store / 'current_stops_v2.json').read_bytes())['index']
        index.pop('resumed')
        event.unlink()
        policy.write_current(self.f.store / 'current_stops_v2.json', index)
        transport = Transport([])
        with self.assertRaisesRegex(ValueError, 'STOP_RESUME_HISTORY_MISSING'):
            d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_stop_write_interrupted_before_sidecar_fails_closed(self):
        seal = policy.seal
        def interrupt(path, record):
            # Model a crash after the complete JSON is published, before sidecar.
            seal(path, record)
            Path(path).with_suffix('.sha256').unlink()
            raise KeyboardInterrupt('sidecar interruption')
        transport = Transport([response(status=403)])
        with patch.object(policy, 'seal', side_effect=lambda path, record:
                interrupt(path, record) if Path(path).name == 'STOP.json' else seal(path, record)):
            with self.assertRaises(KeyboardInterrupt):
                d.acquire_one(self.f.root, SESSION, transport=transport)
        with self.assertRaisesRegex(ValueError, 'STOP_EVIDENCE_HASH_CONFLICT'):
            d.acquire_one(self.f.root, OTHER, transport=transport)
        self.assertEqual(len(transport.calls), 1)

    def test_evidence_seal_is_create_only_with_file_and_directory_fsync(self):
        path = self.f.root / 'evidence.json'
        fsync, sync_dir = policy.os.fsync, policy.frozen._sync_dir
        with patch.object(policy.os, 'fsync', wraps=fsync) as file_sync, \
                patch.object(policy.frozen, '_sync_dir', wraps=sync_dir) as directory_sync:
            policy.seal(path, dict(state='RESUMED'))
        self.assertGreaterEqual(file_sync.call_count, 4)
        self.assertEqual(directory_sync.call_args_list, [call(path.parent)] * 2)
        before = path.read_bytes(), path.with_suffix('.sha256').read_bytes()
        with self.assertRaises(FileExistsError):
            policy.seal(path, dict(state='overwritten'))
        self.assertEqual((path.read_bytes(), path.with_suffix('.sha256').read_bytes()), before)
        self.assertEqual(list(self.f.root.glob('.evidence-*')), [])

    def test_success_resets_429_streak_between_sessions(self):
        run = d.RunState('shared')
        for session in (SESSION, OTHER):
            transport = Transport([response(status=429, **{'Retry-After': '0'}), response(html('2026-10-07'))])
            result = d.acquire_one(self.f.root, session, transport=transport, run_state=run)
            self.assertEqual(result['state'], 'SKIP')
            self.assertEqual(len(transport.calls), 2)
            self.assertEqual(result['retry_count'], 1)
        self.assertFalse(policy.Stops(self.f.store).index['active'])

    def test_host_stop_allows_valid_bundle_offline_skip(self):
        d.acquire_one(self.f.root, SESSION, transport=self.f.acquisition_transport())
        transport = Transport([response(status=403)])
        with self.assertRaisesRegex(ValueError, 'HTTP_403'):
            d.acquire_one(self.f.root, OTHER, transport=transport)
        validator = d.gap.stage_existing_pdf
        with patch.object(d.gap, 'stage_existing_pdf', wraps=validator) as validation:
            result = d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assertEqual(result['status'], 'SKIP_EXISTING_LOCAL')
        self.assertEqual(result['local_validation'], 'PASS')
        self.assertGreater(validation.call_count, 0)
        self.assertEqual(transport.calls, [d.LISTING])

    def test_official_429_wait_survives_interruption_for_other_target(self):
        first = Transport([response(status=429, **{'Retry-After': '7'})])
        with patch.object(d.time, 'sleep', side_effect=KeyboardInterrupt('waiting interrupted')):
            with self.assertRaises(KeyboardInterrupt):
                d.discover_json_one(self.f.root, SESSION, transport=first,
                                    evidence_folder=self.f.root / 'waiting', run_state=d.RunState('waiting'))
        events = []
        class AfterWait(Transport):
            def get(self, url, *, limit):
                events.append('request')
                return super().get(url, limit=limit)
        other = AfterWait([response(html(SESSION))])
        with patch.object(d.time, 'sleep', side_effect=lambda delay: events.append(('wait', delay))):
            result = d.acquire_one(self.f.root, OTHER, transport=other, run_state=d.RunState('other-after-wait'))
        self.assertEqual(result['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(events, [('wait', 7), 'request'])
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(other.calls), 1)

    def test_diagnostic_retry_exhaustion_survives_new_run_ids(self):
        attempts = []
        for number in range(3):
            transport = Transport([TimeoutError('transient')] * 3)
            with self.assertRaisesRegex(ValueError, 'RETRY_EXHAUSTED'):
                d.discover_json_one(self.f.root, SESSION, transport=transport,
                                    evidence_folder=self.f.root / ('diagnostic-' + str(number)),
                                    run_state=d.RunState('run-' + str(number)))
            attempts.append(len(transport.calls))
        self.assertEqual(attempts, [3, 0, 0])
        self.assertFalse(policy.Stops(self.f.store).index['active'])
        # RETRY_EXHAUSTED is not a completed operation; a later day cannot
        # resurrect its attempts. Independent sessions retain fresh budgets.
        with patch.object(d, '_now', return_value=NOW + timedelta(days=1)):
            transport = Transport([TimeoutError('transient')] * 3)
            with self.assertRaisesRegex(ValueError, 'RETRY_EXHAUSTED'):
                d.discover_json_one(self.f.root, SESSION, transport=transport,
                                    evidence_folder=self.f.root / 'later', run_state=d.RunState('later'))
            self.assertEqual(transport.calls, [])

    def test_partial_retry_budget_survives_new_diagnostic_and_acquisition_run_ids(self):
        transport = Transport([TimeoutError('transient'), response(b'not valid JPX listing')])
        # Inspection itself is internal/read-only after download; a simulated
        # process interruption leaves budget1 but no completed discovery result.
        with patch.object(d, 'json_endpoints', side_effect=KeyboardInterrupt('interrupted discovery')):
            with self.assertRaises(KeyboardInterrupt):
                d.discover_json_one(self.f.root, SESSION, transport=transport,
                                    evidence_folder=self.f.root / 'first', run_state=d.RunState('first'))
        self.assertEqual(len(transport.calls), 2)
        exhausted = Transport([TimeoutError('transient')] * 3)
        result = d.acquire_one(self.f.root, SESSION, transport=exhausted, run_state=d.RunState('fresh'))
        self.assertEqual(result['retry_count'], 2)
        self.assertEqual(result['status'], 'SKIP_RETRY_EXHAUSTED')
        self.assertEqual(len(exhausted.calls), 1)  # Same request already started twice; total attempts <= 3.

    def test_unknown_inflight_request_cannot_gain_new_initial_attempt(self):
        class Interrupted(Transport):
            def get(self, url, *, limit):
                self.calls.append(url)
                raise KeyboardInterrupt('in flight')
        transport = Interrupted([])
        with self.assertRaises(KeyboardInterrupt):
            d.discover_json_one(self.f.root, SESSION, transport=transport,
                                evidence_folder=self.f.root / 'inflight', run_state=d.RunState('inflight'))
        second = Transport([])
        with self.assertRaisesRegex(ValueError, 'RETRY_INTERRUPTED_REVIEW_REQUIRED'):
            d.discover_json_one(self.f.root, SESSION, transport=second,
                                evidence_folder=self.f.root / 'next', run_state=d.RunState('next'))
        self.assertEqual(transport.calls, [d.LISTING])
        self.assertEqual(second.calls, [])
        self.assertFalse(policy.Stops(self.f.store).index['active'])

    def test_official_valid_pdf_octet_stream_warns_and_reuses(self):
        transport = Transport([response(), response(self.f.pdf, PDF_URL, kind='binary/octet-stream')])
        result = d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assertEqual(result['state'], 'WARN')
        self.assertEqual(result['warnings'][0]['code'], 'CONTENT_TYPE_MISMATCH')
        self.assertEqual(result['warnings'][0]['target'], PDF_URL)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(d.acquire_one(self.f.root, SESSION, transport=transport)['local_validation'], 'PASS')
        self.assertEqual(len(transport.calls), 2)

    def test_octet_stream_challenge_hard_stops(self):
        transport = Transport([response(), response(b'<html><div class="g-recaptcha"></div></html>',
                                                    PDF_URL, kind='binary/octet-stream')])
        with self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assert_hard('SECURITY_CHALLENGE', 'host')
        self.assertEqual(len(transport.calls), 2)

    def test_octet_stream_corrupt_pdf_cannot_continue(self):
        transport = Transport([response(), response(b'%PDF-1.4\ncorrupt', PDF_URL, kind='binary/octet-stream')])
        with self.assertRaisesRegex(ValueError, 'PDF_CORRUPT'):
            d.acquire_one(self.f.root, SESSION, transport=transport)
        self.assert_hard('PDF_CORRUPT', 'global')
        self.assertEqual(len(transport.calls), 2)

    def parallel(self, mode, resolution=None):
        """Actual processes/actual fcntl lock, shared request counter; no mock lock."""
        ctx = multiprocessing.get_context('fork')
        ready, count, results = ctx.Event(), ctx.Value('i', 0), ctx.Queue()
        def worker(number):
            session = OTHER if mode in ('hard', '429') and number else SESSION
            values = ([response(status=403)] if mode == 'hard' else
                      [response(status=429, **{'Retry-After': '0'})] * 2 if mode == '429' else
                      [] if mode == 'resume' else
                      [response(), response(self.f.pdf, PDF_URL, kind='application/pdf')])
            class Counted(Transport):
                def get(self, url, *, limit):
                    with count.get_lock():
                        count.value += 1
                    return super().get(url, limit=limit)
            ready.wait(10)
            try:
                result = d.acquire_one(self.f.root, session, transport=Counted(values),
                                       resolution=resolution, run_state=d.RunState('worker-' + str(number)))
                results.put(result['status'])
            except Exception as error:
                results.put(type(error).__name__ + ':' + str(error))
        processes = [ctx.Process(target=worker, args=(number,)) for number in range(2)]
        try:
            for process in processes:
                process.start()
            ready.set()
            values = [results.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            return count.value, values
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()

    def test_concurrent_duplicate_target_is_acquired_once(self):
        count, values = self.parallel('duplicate')
        self.assertEqual(count, 2)
        self.assertCountEqual(values, ['VALIDATED_PENDING_EXPLICIT_REGISTRATION', 'SKIP_EXISTING_LOCAL'])

    def test_concurrent_hard_stop_prevents_second_host_request(self):
        count, values = self.parallel('hard')
        self.assertEqual(count, 1)
        self.assertTrue(any(value == 'ValueError:HTTP_403' for value in values))
        self.assertTrue(any('HARD_STOP_ACTIVE:HTTP_403' in value for value in values))
        self.assert_hard('HTTP_403', 'host')

    def test_concurrent_persistent_429_stops_same_host(self):
        count, values = self.parallel('429')
        self.assertEqual(count, 2)
        self.assertTrue(any(value == 'ValueError:HOST_PERSISTENT_429' for value in values))
        self.assertTrue(any('HARD_STOP_ACTIVE:HOST_PERSISTENT_429' in value for value in values))
        self.assert_hard('HOST_PERSISTENT_429', 'host')

    def test_concurrent_resume_has_one_event_and_no_requests(self):
        item = self.f.soft_failure()
        resolution = self.f.resolution(item)
        count, values = self.parallel('resume', resolution)
        self.assertEqual(count, 0)
        self.assertCountEqual(values, ['VALIDATED_PENDING_EXPLICIT_REGISTRATION', 'SKIP_EXISTING_LOCAL'])
        self.assertEqual(len(list((self.f.store / 'stops_v2').glob('resume-*.json'))), 1)
        self.assertEqual(len(policy.Stops(self.f.store).index['resumed']), 1)
