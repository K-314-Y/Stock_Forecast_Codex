"""Receipt authentication and Dry Run admission; synthetic acquisition only."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import json
import os
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.fresh_receipt_fixture import synthetic_legacy_receipt

from engine import fresh_bridge as bridge
from engine import minimal_trial as core
from engine import one_session_dry_run as probe

PROJECT = core.ROOT
PREVIOUS = '2026-10-08'
TARGET = '2026-10-09'
RETRIEVED = datetime.fromisoformat(TARGET + 'T08:20:00+09:00')
CREATED = datetime.fromisoformat(TARGET + 'T08:25:00+09:00')
CUTOFF = datetime.fromisoformat(TARGET + 'T08:45:00+09:00')


class FreshBridgeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/fresh_feature_bridge.json').write_bytes(
            (PROJECT / 'config/fresh_feature_bridge.json').read_bytes())
        root_patch = patch.object(core, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.response = dict(structuredContent=dict(company_overview=dict(
            timestamp=PREVIOUS + 'T15:30:00+09:00', price=123.5, volume=100,
            isin=bridge.CORE_ISINS['6857'], currency='JPY', exchange='JPX', source=dict(provider_id='FMP'))))
        self.fetch = Mock(return_value=self.response)

    def capture(self, when=RETRIEVED, response=None, ticker='6857'):
        with patch.object(bridge, '_receipt_now', return_value=when):
            return synthetic_legacy_receipt(ticker, PREVIOUS,
                self.fetch if response is None else lambda: response)

    def verify(self, evidence, created=CREATED, cutoff=CUTOFF):
        return probe._evidence({'6857': dict(provenance={'evidence': [evidence]})},
                               PREVIOUS, created, cutoff)

    def test_correct_bridge_receipt_binds_source_close_times_and_snapshot_hashes(self):
        evidence = self.capture()
        hashes, prices = self.verify(evidence)
        self.fetch.assert_called_once_with()
        self.assertEqual(prices['6857'], [dict(session_date=PREVIOUS, close=123.5)])
        receipt = json.loads(Path(evidence['receipt']).read_bytes())['receipt']
        for field in ('source', 'session', 'ticker', 'close', 'provider_timestamp',
                      'retrieval_completed_at', 'source_data_sha256'):
            self.assertIn(field, receipt)
        self.assertEqual(receipt['source_role'], 'FORECAST_FEATURE')
        self.assertEqual(receipt['retrieved_at'], RETRIEVED.isoformat())
        self.assertEqual(receipt['source_data_sha256'], core.sha256(evidence['path']))
        self.assertIn(evidence['path'], hashes)
        self.assertIn(evidence['receipt'], hashes)
        self.assertNotIn(str(bridge._receipt_root() / '.producer-key'), hashes)

    def test_caller_time_assertion_cannot_replace_recorded_retrieval(self):
        evidence = self.capture()
        for field in ('available_at', 'retrieved_at'):
            changed = dict(evidence, **{field: TARGET + 'T08:00:00+09:00'})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'CALLER_RECEIPT_TIME_MISMATCH'):
                self.verify(changed)
        self.verify(dict(evidence, retrieved_at=RETRIEVED.isoformat(), available_at=RETRIEVED.isoformat()))

    def test_signed_receipt_retrieved_at_tampering_rejected(self):
        evidence = self.capture()
        path = Path(evidence['receipt'])
        envelope = json.loads(path.read_bytes())
        envelope['receipt']['retrieved_at'] = TARGET + 'T08:00:00+09:00'
        path.write_bytes(core.canonical_bytes(envelope))
        with self.assertRaisesRegex(ValueError, 'BRIDGE_RECEIPT_AUTHENTICATION_FAILED'):
            self.verify(evidence)

    def test_rewriting_all_times_and_recomputing_public_hashes_cannot_backdate(self):
        evidence = self.capture(CUTOFF + timedelta(seconds=1))
        package = Path(evidence['receipt']).parent
        envelope = json.loads(Path(evidence['receipt']).read_bytes())
        for field in ('retrieval_started_at', 'retrieval_completed_at', 'retrieved_at'):
            envelope['receipt'][field] = RETRIEVED.isoformat()
        # Attacker can update every unkeyed hash and rename the package, but not
        # authenticate the fabricated time using the private producer key.
        identifier = hashlib.sha256(core.canonical_bytes(envelope)).hexdigest()
        replacement = package.parent / identifier
        package.rename(replacement)
        (replacement / 'receipt.json').write_bytes(core.canonical_bytes(envelope))
        evidence.update(path=str(replacement / 'source.json'), receipt=str(replacement / 'receipt.json'))
        with self.assertRaisesRegex(ValueError, 'BRIDGE_RECEIPT_AUTHENTICATION_FAILED'):
            self.verify(evidence, CUTOFF + timedelta(minutes=1))

    def test_source_data_modification_rejected(self):
        evidence = self.capture()
        Path(evidence['path']).write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'INPUT_HASH_MISMATCH'):
            self.verify(evidence)

    def test_source_data_modification_rejected_even_with_recomputed_caller_hash(self):
        evidence = self.capture()
        changed = copy.deepcopy(self.response)
        changed['structuredContent']['company_overview']['price'] += 1
        Path(evidence['path']).write_bytes(core.canonical_bytes(changed))
        evidence['sha256'] = core.sha256(evidence['path'])
        with self.assertRaisesRegex(ValueError, 'BRIDGE_SOURCE_BINDING_MISMATCH'):
            self.verify(evidence)

    def test_cutoff_and_after_cutoff_retrieval_rejected_without_caller_times(self):
        for when in (CUTOFF, CUTOFF + timedelta(seconds=1)):
            with self.subTest(when=when):
                evidence = self.capture(when)
                with self.assertRaisesRegex(ValueError, 'POST_GENERATION_INPUT_EVIDENCE'):
                    self.verify(evidence, CUTOFF + timedelta(minutes=1))

    def test_post_generation_before_cutoff_retrieval_rejected(self):
        evidence = self.capture(CREATED + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, 'POST_GENERATION_INPUT_EVIDENCE'):
            self.verify(evidence)

    def test_ticker_mismatch_rejected(self):
        response = copy.deepcopy(self.response)
        response['structuredContent']['company_overview']['isin'] = bridge.CORE_ISINS['7203']
        evidence = self.capture(ticker='7203', response=response)
        with self.assertRaisesRegex(ValueError, 'BRIDGE_TICKER_SESSION_MISMATCH'):
            self.verify(evidence)

    def test_session_mismatch_rejected(self):
        evidence = self.capture()
        evidence['session_end'] = '2026-10-07'
        with self.assertRaisesRegex(ValueError, 'BRIDGE_TICKER_SESSION_MISMATCH'):
            self.verify(evidence)

    def test_outcome_only_role_rejected_before_reader(self):
        evidence = self.capture()
        evidence['source_role'] = 'OUTCOME_ONLY'
        with patch.object(bridge, 'verify_fresh_evidence') as reader:
            with self.assertRaisesRegex(ValueError, 'OUTCOME_ONLY_SOURCE_NOT_FORECAST_FEATURE'):
                self.verify(evidence)
            reader.assert_not_called()

    def test_jpx_raw_and_parsed_cannot_be_relabelled_forecast_features(self):
        evidence = self.capture()
        for relative in ('data/outcome_input/manual_jpx/file.pdf', 'data/outcome_parsed/jpx/file.json'):
            with self.subTest(relative=relative):
                evidence['path'] = str(self.root / relative)
                with self.assertRaisesRegex(ValueError, 'OUTCOME_ONLY_SOURCE_NOT_FORECAST_FEATURE'):
                    self.verify(evidence)

    def test_provider_timestamp_alone_and_unsigned_prices_are_rejected(self):
        source = self.root / 'caller_prices.json'
        source.write_bytes(core.canonical_bytes(self.response))
        evidence = dict(source_role='FORECAST_FEATURE', source_kind='FRESH_CLOSE_BRIDGE',
            path=str(source), sha256=core.sha256(source), session_end=PREVIOUS,
            retrieved_at=RETRIEVED.isoformat(), available_at=RETRIEVED.isoformat())
        with self.assertRaisesRegex(ValueError, 'UNTRUSTED_BRIDGE_RECEIPT'):
            self.verify(evidence)
        # The old arbitrary-file format is no longer a bypass.
        del evidence['source_role']
        with self.assertRaisesRegex(ValueError, 'PROBE_INPUT_EVIDENCE_REQUIRED'):
            self.verify(evidence)

    def test_mtime_cannot_rescue_post_cutoff_receipt(self):
        evidence = self.capture(CUTOFF + timedelta(seconds=1))
        for path in (evidence['path'], evidence['receipt']):
            os.utime(path, (0, 0))
        with self.assertRaisesRegex(ValueError, 'POST_GENERATION_INPUT_EVIDENCE'):
            self.verify(evidence, CUTOFF + timedelta(minutes=1))

    def test_receipt_cannot_be_loaded_from_caller_selected_store(self):
        evidence = self.capture()
        evidence['receipt'] = str(self.root / 'caller_receipt.json')
        with self.assertRaisesRegex(ValueError, 'UNTRUSTED_BRIDGE_RECEIPT'):
            self.verify(evidence)

    def test_changed_semantics_config_is_fail_closed(self):
        evidence = self.capture()
        path = self.root / 'config/fresh_feature_bridge.json'
        config = json.loads(path.read_bytes())
        config['BIGDATA_FRESH_BRIDGE_TECHNICALLY_VALIDATED'] = False
        path.write_bytes(core.canonical_bytes(config))
        with self.assertRaisesRegex(ValueError, 'BRIDGE_UNVALIDATED_OR_STALE'):
            self.verify(evidence)

    def test_receipt_is_create_only_and_failed_fetch_cannot_publish_evidence(self):
        evidence = self.capture()
        original = Path(evidence['receipt']).read_bytes()
        with self.assertRaises(FileExistsError):
            self.capture()
        self.assertEqual(Path(evidence['receipt']).read_bytes(), original)
        with patch.object(bridge, '_receipt_now', return_value=RETRIEVED):
            with self.assertRaisesRegex(RuntimeError, 'connector failed'):
                synthetic_legacy_receipt('6857', PREVIOUS, Mock(side_effect=RuntimeError('connector failed')))
        self.assertEqual(len(list(bridge._receipt_root().glob('*/receipt.json'))), 1)

    def test_acquisition_clock_rollback_rejected(self):
        with patch.object(bridge, '_receipt_now', side_effect=[CREATED, RETRIEVED]):
            with self.assertRaisesRegex(ValueError, 'BRIDGE_ACQUISITION_CLOCK_INVALID'):
                synthetic_legacy_receipt('6857', PREVIOUS, self.fetch)
        self.assertFalse(list(bridge._receipt_root().glob('*/receipt.json')))

    def test_provider_session_currency_and_identity_use_existing_normalizer(self):
        for field, value in [('currency', 'USD'), ('timestamp', '2026-10-07T15:30:00+09:00')]:
            changed = copy.deepcopy(self.response)
            changed['structuredContent']['company_overview'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'BRIDGE_UNVALIDATED_OR_STALE'):
                self.capture(response=changed)

    def test_already_audited_bridge_records_are_readable_without_rewriting_or_signing(self):
        created = datetime.fromisoformat('2026-10-09T08:25:00+09:00')
        with patch.object(core, 'ROOT', PROJECT):
            before = {}
            for name in bridge.LEGACY_RECEIPTS:
                path = PROJECT / 'data/fresh_bridge/jp/live_observations' / name
                record = json.loads(path.read_bytes())
                for ticker in core.CORE:
                    evidence = dict(source_role='FORECAST_FEATURE', source_kind='FRESH_CLOSE_BRIDGE',
                        path=str(path), sha256=core.sha256(path), session_end=record['expected_previous_session'])
                    rows, hashes = bridge.verify_fresh_evidence(evidence, ticker, created, CUTOFF)
                    self.assertEqual(rows[0][0], evidence['session_end'])
                    before.update(hashes)
            for path, expected in before.items():
                self.assertEqual(core.sha256(path), expected)

    def test_existing_registered_historical_cache_uses_pinned_receipt_and_candidate_hash(self):
        with patch.object(core, 'ROOT', PROJECT):
            for ticker in core.CORE:
                path = PROJECT / 'analysis/provider_test_results/yfinance_cache_2026-09-13' / (ticker + '_auto_adjust_false.csv')
                evidence = dict(source_role='FORECAST_FEATURE', source_kind='HISTORICAL_CACHE',
                    path=str(path), sha256=core.sha256(path), session_end='2026-09-11')
                rows, hashes = bridge.verify_cached_evidence(evidence, ticker, CREATED, CUTOFF)
                self.assertEqual(rows[-1][0], '2026-09-11')
                self.assertEqual(len(hashes), 4)

    def test_provider_response_ticker_cannot_be_mislabelled_by_capture_caller(self):
        with self.assertRaisesRegex(ValueError, 'BRIDGE_PROVIDER_TICKER_MISMATCH'):
            self.capture(ticker='7203')

    def test_first_parallel_acquisitions_use_one_complete_private_key(self):
        barrier = threading.Barrier(len(core.CORE))
        def acquire(ticker):
            response = copy.deepcopy(self.response)
            response['structuredContent']['company_overview']['isin'] = bridge.CORE_ISINS[ticker]
            barrier.wait()
            return ticker, synthetic_legacy_receipt(ticker, PREVIOUS, lambda: response)
        with patch.object(bridge, '_receipt_now', return_value=RETRIEVED):
            with ThreadPoolExecutor(max_workers=len(core.CORE)) as pool:
                records = list(pool.map(acquire, core.CORE))
        self.assertEqual(len(bridge._receipt_key()), 32)
        for ticker, evidence in records:
            bridge.verify_fresh_evidence(evidence, ticker, CREATED, CUTOFF)

    def test_failed_provider_response_and_intraday_stamp_cannot_be_sealed_as_close(self):
        for change in ('provider_error', 'intraday'):
            response = copy.deepcopy(self.response)
            if change == 'provider_error':
                response['isError'] = True
                reason = 'BRIDGE_PROVIDER_RESPONSE_ERROR'
            else:
                response['structuredContent']['company_overview']['timestamp'] = PREVIOUS + 'T09:00:00+09:00'
                reason = 'BRIDGE_UNVALIDATED_OR_STALE'
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, reason):
                self.capture(response=response)
        self.assertFalse(list(bridge._receipt_root().glob('*/receipt.json')))

    def test_legacy_raw_time_and_public_self_hash_rewriting_cannot_pass_checkpoint(self):
        name = '2026-09-29_0820_0835.json'
        report_name, _, hash_field = bridge.LEGACY_RECEIPTS[name]
        path = self.root / 'data/fresh_bridge/jp/live_observations' / name
        path.parent.mkdir(parents=True)
        report_path = self.root / 'analysis/provider_test_results' / report_name
        report_path.parent.mkdir(parents=True)
        report_path.write_bytes((PROJECT / 'analysis/provider_test_results' / report_name).read_bytes())
        raw = json.loads((PROJECT / 'data/fresh_bridge/jp/live_observations' / name).read_bytes())
        raw['observations'][0]['retrieval_completed_at'] = '2026-09-28 23:00:00 UTC'
        canonical = dict(raw)
        canonical.pop('record_hash')
        raw['record_hash'] = hashlib.sha256(core.canonical_bytes(canonical)).hexdigest()
        path.write_bytes(core.canonical_bytes(raw))
        evidence = dict(source_role='FORECAST_FEATURE', source_kind='FRESH_CLOSE_BRIDGE',
            path=str(path), sha256=core.sha256(path), session_end='2026-09-28')
        with self.assertRaisesRegex(ValueError, 'BRIDGE_SOURCE_BINDING_MISMATCH'):
            bridge.verify_fresh_evidence(evidence, '6857', CREATED, CUTOFF)
        report = json.loads(report_path.read_bytes())
        report[hash_field] = raw['record_hash']
        report_path.write_bytes(core.canonical_bytes(report))
        with self.assertRaisesRegex(ValueError, 'BRIDGE_RECEIPT_AUTHENTICATION_FAILED'):
            bridge.verify_fresh_evidence(evidence, '6857', CREATED, CUTOFF)

    def test_historical_cache_receipt_cannot_be_replaced_or_backdated(self):
        ticker = '6857'
        cache = self.root / 'analysis/provider_test_results/yfinance_cache_2026-09-13'
        cache.mkdir(parents=True)
        source = cache / (ticker + '_auto_adjust_false.csv')
        metadata = cache / (ticker + '_probe.json')
        for path in (source, metadata):
            path.write_bytes((PROJECT / 'analysis/provider_test_results/yfinance_cache_2026-09-13' / path.name).read_bytes())
        (self.root / 'config/trial_thresholds_candidate.json').write_bytes(
            (PROJECT / 'config/trial_thresholds_candidate.json').read_bytes())
        meta = json.loads(metadata.read_bytes())
        meta['retrieved_at'] = '2026-09-12T00:00:00+09:00'
        metadata.write_bytes(core.canonical_bytes(meta))
        evidence = dict(source_role='FORECAST_FEATURE', source_kind='HISTORICAL_CACHE',
            path=str(source), sha256=core.sha256(source), session_end='2026-09-11')
        with self.assertRaisesRegex(ValueError, 'CACHE_RECEIPT_HASH_MISMATCH'):
            bridge.verify_cached_evidence(evidence, ticker, CREATED, CUTOFF)


if __name__ == '__main__':
    unittest.main()
