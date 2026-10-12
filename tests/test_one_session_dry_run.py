"""Synthetic one-session probe tests. All writes stay in temporary stores."""
import copy
import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from jsonschema import ValidationError

from tests.fresh_receipt_fixture import synthetic_legacy_receipt

from engine import minimal_trial as core
from engine import fresh_bridge as bridge
from engine import one_session_dry_run as probe
from tests.test_minimal_trial_engine import fake_rows

PROJECT = core.ROOT
TARGET = '2026-10-08'
PREVIOUS = '2026-10-07'
BEFORE = datetime.fromisoformat(TARGET + 'T08:25:00+09:00')
AFTER = datetime.fromisoformat(TARGET + 'T16:00:00+09:00')


class OneSessionProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        (self.root / 'analysis').mkdir()
        for name in ('threshold_spec.json', 'baseline_spec.json', 'historical_data_quality_policy.json',
                     'normalized_market_schema.json', 'fresh_feature_bridge.json'):
            (self.root / 'config' / name).write_bytes((PROJECT / 'config' / name).read_bytes())
        self.calendar = json.loads((PROJECT / 'analysis/calendar_validation_2026-09-12.json').read_text())
        self.write(self.root / 'analysis/calendar_validation_2026-09-12.json', self.calendar)
        root_patch = patch.object(core, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.clock = patch.object(probe, '_now', return_value=BEFORE).start()
        self.addCleanup(patch.stopall)
        self.store = self.root / 'probe_store'
        self.input_path = self.root / 'prepared.json'
        policy_hash = core.sha256(self.root / 'config/historical_data_quality_policy.json')
        common = dict(status='CANDIDATE_FROZEN_PRETRIAL_NOT_ACTIVATED',
                      automatic_reestimation=False, quality_policy_sha256=policy_hash,
                      training_end_date='2026-09-11')
        training = core.threshold(fake_rows('2026-09-11'))
        self.thresholds = dict(common, threshold_version=training['threshold_version'], thresholds={})
        self.baselines = dict(common, baseline_version='minimal-baselines-v1', baselines={})
        self.tables = dict(common, baseline_version='minimal-baselines-v1',
                           threshold_version=training['threshold_version'], tables={})
        self.prepared = dict(target_session=TARGET, feature_cutoff=PREVIOUS + 'T15:30:00+09:00', stocks={})
        cache = self.root / 'analysis/provider_test_results/yfinance_cache_2026-09-13'
        cache.mkdir(parents=True)
        cache_hashes = {}
        for symbol in core.CORE:
            threshold = dict(training, ticker=symbol, status='TRIAL_CANDIDATE_NOT_ACTIVATED')
            self.thresholds['thresholds'][symbol] = threshold
            probabilities = {'UP': 0.37123, 'FLAT': 0.28877, 'DOWN': 0.34}
            rate = dict(probabilities=probabilities, sample_count=100, fallback=False)
            self.baselines['baselines'][symbol] = dict(base_rate=rate)
            self.tables['tables'][symbol] = dict(base_rate=rate,
                momentum={state: copy.deepcopy(rate) for state in ('POSITIVE', 'NEGATIVE', 'FLAT')},
                ma_trend={state: copy.deepcopy(rate) for state in ('BULL', 'BEAR', 'NEUTRAL')})
            historical = fake_rows('2026-09-11')
            source = cache / (symbol + '_auto_adjust_false.csv')
            with source.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
                writer.writeheader()
                for row in historical:
                    writer.writerow(dict(Date=row['session_date'] + 'T00:00:00+09:00',
                        **{k.capitalize(): row[k] for k in ('open', 'high', 'low', 'close', 'volume')}))
            meta = cache / (symbol + '_probe.json')
            self.write(meta, dict(retrieved_at='2026-09-13T09:00:00+09:00', metadata=dict(
                symbol=symbol + '.T', currency='JPY', exchangeTimezoneName='Asia/Tokyo')))
            cache_hashes[symbol] = core.sha256(meta)
            threshold['source_file_sha256'] = core.sha256(source)
            evidence = [dict(source_role='FORECAST_FEATURE', source_kind='HISTORICAL_CACHE',
                path=str(source), sha256=core.sha256(source), session_end='2026-09-11')]
            bridge_rows = [r for r in fake_rows(PREVIOUS) if '2026-09-11' < r['session_date']
                           and r['session_date'] not in self.calendar['jp_holidays']]
            for row in bridge_rows:
                response = dict(structuredContent=dict(company_overview=dict(
                    timestamp=row['session_date'] + 'T15:30:00+09:00', price=row['close'],
                    isin=bridge.CORE_ISINS[symbol], exchange='JPX', currency='JPY', source=dict(provider_id='FMP'))))
                with patch.object(bridge, '_receipt_now', return_value=datetime.fromisoformat(TARGET + 'T08:20:00+09:00')):
                    evidence.append(synthetic_legacy_receipt(symbol, row['session_date'], lambda: response))
            rows = historical + bridge_rows
            self.prepared['stocks'][symbol] = dict(features=core.features(rows, threshold),
                                                   provenance={'evidence': evidence})
        patch.object(bridge, 'CACHE_RECEIPT_HASHES', cache_hashes).start()
        self.save_inputs()
        self.save_candidates()
        inbox = self.root / 'data/outcome_input/manual_jpx'
        inbox.mkdir(parents=True)
        self.raw = inbox / 'retained_original.pdf'
        self.raw.write_bytes(b'%PDF synthetic original, parsing already validated in fixture')
        self.parsed_path = self.root / 'parsed_outcome.json'
        sample = PROJECT / 'data/outcome_parsed/jpx/2026-09-18_core6_browser_v02_engine_test.json'
        self.parsed = json.loads(sample.read_text())
        self.parsed.update(source_file=r'C:\old\manual_jpx\retained_original.pdf',
                           source_file_hash=core.sha256(self.raw), source_file_hash_after=core.sha256(self.raw),
                           session_date=TARGET, retrieved_at=TARGET + 'T15:45:00+09:00')
        for row in self.parsed['records']:
            row.update(session_date=TARGET, source_file_hash=core.sha256(self.raw))
            row['normalized_observation'].update(session_date=TARGET,
                                                  retrieved_at=TARGET + 'T15:45:00+09:00')
            row['normalized_observation']['normalization']['basis_session'] = TARGET
            row['normalized_observation']['source_reference']['reference'] = str(self.raw) + ';sha256=' + core.sha256(self.raw)
        self.manifest_path = inbox / 'retention_manifest.json'
        self.save_outcome()

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def save_inputs(self):
        self.write(self.input_path, self.prepared)

    def save_candidates(self):
        for name, data in (('trial_thresholds_candidate.json', self.thresholds),
                           ('trial_baselines_candidate.json', self.baselines),
                           ('trial_baseline_state_tables_candidate.json', self.tables)):
            self.write(self.root / 'config' / name, data)

    def save_outcome(self):
        self.write(self.parsed_path, self.parsed)
        self.manifest = dict(entries=[dict(filename=self.raw.name, session_date=TARGET,
            sha256=core.sha256(self.raw), normalized_output_sha256=core.sha256(self.parsed_path),
            parser_status='PASS', schema_validation='PASS', core6_count=6,
            parsed_at=TARGET + 'T15:45:00+09:00', source_metadata={'local_file': self.raw.name})])
        self.write(self.manifest_path, self.manifest)

    def forecast(self, **kwargs):
        return probe.forecast_phase(self.store, TARGET, '6857', self.input_path, **kwargs)

    def reject_forecast(self, message):
        self.save_inputs()
        with self.assertRaisesRegex(ValueError, message):
            self.forecast()
        self.assertFalse(self.store.exists())

    def test_base_rate_exact_probabilities_metadata_and_isolation(self):
        key = self.forecast()
        directory, path, forecast, snapshot = probe._read_forecast(self.store, key)
        self.assertEqual(forecast['p_up'], .37123)
        self.assertAlmostEqual(sum(forecast['p_' + k.lower()] for k in core.CLASSES), 1)
        self.assertEqual(forecast['status'], 'PIPELINE_PROBE')
        self.assertEqual(forecast['eligibility'], 'NON_FORMAL')
        self.assertTrue(forecast['exclude_from_trial_metrics'])
        for field in ('target_session', 'ticker', 'created_at', 'forecast_cutoff', 'feature_cutoff',
                      'model_id', 'model_version', 'threshold_version', 'baseline_version',
                      'input_hashes', 'input_snapshot_hash', 'schema_version'):
            self.assertIn(field, forecast)
        self.assertNotIn('outcome', forecast)
        self.assertEqual(probe.lifecycle(self.store, key), 'WAITING_OUTCOME')

    def test_existing_feature_functions_generate_from_evidence_without_supplied_features(self):
        for stock in self.prepared['stocks'].values():
            del stock['features']
        self.save_inputs()
        key = self.forecast()
        self.assertEqual(probe.lifecycle(self.store, key), 'WAITING_OUTCOME')

    def test_duplicate_with_different_id_is_rejected(self):
        key = self.forecast(forecast_id='FIRST-ID')
        _, path, _, _ = probe._read_forecast(self.store, key)
        before = path.read_bytes()
        with self.assertRaisesRegex(FileExistsError, 'DUPLICATE'):
            self.forecast(forecast_id='SECOND-ID')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(self.store.rglob('SECOND-ID.json')))

    def test_other_ticker_same_session_allowed(self):
        first = self.forecast()
        second = probe.forecast_phase(self.store, TARGET, '7203', self.input_path)
        self.assertNotEqual(first, second)

    def test_concurrent_different_ids_commit_one_forecast_only(self):
        def attempt(name):
            try:
                return self.forecast(forecast_id=name)
            except FileExistsError:
                return 'DUPLICATE'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ['CONCURRENT-A', 'CONCURRENT-B']))
        self.assertEqual(results.count('DUPLICATE'), 1)
        self.assertEqual(len(list(self.store.rglob('CONCURRENT-*.json'))), 1)

    def test_store_cannot_bind_second_session(self):
        self.forecast()
        next_day = '2026-10-09'
        self.clock.return_value = datetime.fromisoformat(next_day + 'T08:25:00+09:00')
        # Store binding is checked before any attempt can commit a second session.
        session = core.read_frozen(self.store / 'session/record.json')
        self.assertEqual(session['target_session'], TARGET)
        with patch.object(probe.bridge, 'expected_previous', return_value=PREVIOUS):
            self.prepared['target_session'] = next_day
            self.save_inputs()
            with self.assertRaisesRegex(ValueError, 'ONE_SESSION_STORE_ONLY'):
                probe.forecast_phase(self.store, next_day, '6857', self.input_path)

    def test_future_feature_end_rejected(self):
        self.prepared['stocks']['6857']['features']['previous_session'] = TARGET
        self.reject_forecast('PREPARED_FEATURE_SOURCE_MISMATCH')

    def test_feature_values_cannot_be_forged_with_past_dates(self):
        self.prepared['stocks']['6857']['features']['ret_1d'] = .8
        self.reject_forecast('PREPARED_FEATURE_SOURCE_MISMATCH')

    def test_missing_price_session_is_rejected_without_interpolation(self):
        provenance = self.prepared['stocks']['6857']['provenance']
        provenance['evidence'] = [e for e in provenance['evidence'] if e['session_end'] != '2026-10-05']
        self.reject_forecast('PROBE_CONTINUOUS_HISTORY_REQUIRED')

    def test_future_feature_cutoff_rejected(self):
        self.prepared['feature_cutoff'] = TARGET + 'T15:30:00+09:00'
        self.reject_forecast('FEATURE_CUTOFF')

    def test_future_threshold_training_rejected(self):
        self.thresholds['thresholds']['6857']['training_end_date'] = TARGET
        self.save_candidates()
        self.reject_forecast('THRESHOLD_TRAINING_LOOKAHEAD|INPUT_SESSION_LOOKAHEAD')

    def test_future_baseline_training_rejected(self):
        self.baselines['training_end_date'] = TARGET
        self.save_candidates()
        self.reject_forecast('FROZEN_CANDIDATE_INVALID')

    def test_evidence_after_generation_before_deadline_is_rejected(self):
        self.prepared['stocks']['6857']['provenance']['evidence'][0]['retrieved_at'] = TARGET + 'T08:30:00+09:00'
        self.reject_forecast('CALLER_RECEIPT_TIME_MISMATCH')

    def test_actual_source_future_session_rejected_even_when_metadata_claims_past(self):
        evidence = self.prepared['stocks']['6857']['provenance']['evidence'][-1]
        source = json.loads(Path(evidence['path']).read_text())
        source['structuredContent']['company_overview']['timestamp'] = TARGET + 'T15:30:00+09:00'
        self.write(Path(evidence['path']), source)
        evidence['sha256'] = core.sha256(evidence['path'])
        self.reject_forecast('BRIDGE_SOURCE_BINDING_MISMATCH')

    def test_input_hash_mismatch_rejected(self):
        self.prepared['stocks']['6857']['provenance']['evidence'][0]['sha256'] = '0' * 64
        self.reject_forecast('INPUT_HASH_MISMATCH')

    def test_source_internal_future_receipt_cannot_be_backdated_by_evidence(self):
        evidence = self.prepared['stocks']['6857']['provenance']['evidence'][-1]
        receipt = json.loads(Path(evidence['receipt']).read_text())
        receipt['receipt']['retrieved_at'] = TARGET + 'T08:40:00+09:00'
        self.write(Path(evidence['receipt']), receipt)
        self.reject_forecast('BRIDGE_RECEIPT_AUTHENTICATION_FAILED')

    def test_outcome_cannot_enter_forecast_input(self):
        self.prepared['outcome'] = {'actual_class': 'UP'}
        self.reject_forecast('INVALID_PROBE_PREPARED_INPUT')

    def test_outcome_source_cannot_be_disguised_as_price_evidence(self):
        evidence = self.prepared['stocks']['6857']['provenance']['evidence'][0]
        evidence['path'] = str(self.parsed_path)
        evidence['source_kind'] = 'JPX_PARSED'
        self.reject_forecast('UNSUPPORTED_PROBE_PRICE_EVIDENCE')

    def test_no_backdated_or_future_day_forecast(self):
        for timestamp in ('2026-10-07T08:25:00+09:00', '2026-10-08T08:45:01+09:00',
                          '2026-10-09T08:25:00+09:00'):
            with self.subTest(timestamp=timestamp):
                self.clock.return_value = datetime.fromisoformat(timestamp)
                with self.assertRaisesRegex(ValueError, 'ACTUAL_PRE_CUTOFF'):
                    self.forecast()

    def test_slow_freeze_crossing_deadline_does_not_commit(self):
        self.clock.side_effect = [BEFORE, BEFORE, datetime.fromisoformat(TARGET + 'T08:45:01+09:00')]
        with self.assertRaisesRegex(ValueError, 'FREEZE_MISSED_CUTOFF'):
            self.forecast()
        self.assertEqual([p.name for p in self.store.iterdir() if p.is_dir()], ['session'])
        self.assertFalse(list(self.store.glob('.pending-*')))

    def test_market_close_guard_runs_before_outcome_reader(self):
        key = self.forecast()
        self.clock.return_value = datetime.fromisoformat(TARGET + 'T15:29:59+09:00')
        with patch.object(core, 'load_parsed_jpx') as reader:
            with self.assertRaisesRegex(ValueError, 'OUTCOME_BEFORE_MARKET_CLOSE'):
                probe.settle_phase(self.store, key, self.parsed_path)
            reader.assert_not_called()
        self.assertEqual(probe.lifecycle(self.store, key), 'WAITING_OUTCOME')

    def test_missing_outcome_remains_waiting(self):
        key = self.forecast()
        self.clock.return_value = AFTER
        self.assertEqual(probe.settle_phase(self.store, key), 'WAITING_OUTCOME')

    def test_raw_hash_mismatch_rejected(self):
        key = self.forecast()
        self.clock.return_value = AFTER
        self.raw.write_bytes(b'tampered original')
        with self.assertRaisesRegex(ValueError, 'RAW_SOURCE_HASH_MISMATCH'):
            probe.settle_phase(self.store, key, self.parsed_path)
        self.assertEqual(probe.lifecycle(self.store, key), 'WAITING_OUTCOME')

    def test_parsed_manifest_hash_mismatch_rejected(self):
        key = self.forecast()
        self.clock.return_value = AFTER
        self.parsed['records'][0]['close'] += 1
        self.write(self.parsed_path, self.parsed)
        with self.assertRaisesRegex(ValueError, 'JPX_MANIFEST_OR_VALIDATION_MISMATCH'):
            probe.settle_phase(self.store, key, self.parsed_path)

    def test_jpx_validation_and_normalized_schema_required(self):
        key = self.forecast()
        self.clock.return_value = AFTER
        self.parsed['normalized_schema_validation'] = 'FAIL'
        self.save_outcome()
        with self.assertRaisesRegex(ValueError, 'JPX_MANIFEST_OR_VALIDATION_MISMATCH'):
            probe.settle_phase(self.store, key, self.parsed_path)
        self.parsed['normalized_schema_validation'] = 'PASS'
        del self.parsed['records'][0]['normalized_observation']['currency']
        self.save_outcome()
        with self.assertRaises(ValidationError):
            probe.settle_phase(self.store, key, self.parsed_path)

    def test_future_outcome_import_time_rejected(self):
        key = self.forecast()
        self.clock.return_value = AFTER
        self.parsed['retrieved_at'] = TARGET + 'T17:00:00+09:00'
        self.save_outcome()
        with self.assertRaisesRegex(ValueError, 'JPX_OUTCOME_AVAILABILITY_INVALID'):
            probe.settle_phase(self.store, key, self.parsed_path)

    def test_resume_after_outcome_frozen_without_reopening_sources(self):
        key = self.forecast()
        directory, path, forecast, snapshot = probe._read_forecast(self.store, key)
        forecast_hash = core.sha256(path)
        self.clock.return_value = AFTER
        with patch.object(core, 'score_frozen', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                probe.settle_phase(self.store, key, self.parsed_path)
        self.assertEqual(probe.lifecycle(self.store, key), 'OUTCOME_FROZEN')
        outcome_hash = core.sha256(directory / 'outcome/record.json')
        # Changing current candidates/sources must not change the sealed evaluation vintage.
        self.thresholds['thresholds']['6857']['T_final'] = .99
        self.save_candidates()
        self.raw.unlink()
        self.parsed_path.unlink()
        with patch.object(core, 'load_parsed_jpx', side_effect=AssertionError('must not reread')):
            self.assertEqual(probe.settle_phase(self.store, key), 'SCORED')
        self.assertEqual(core.sha256(path), forecast_hash)
        self.assertEqual(core.sha256(directory / 'outcome/record.json'), outcome_hash)
        result = core.read_frozen(directory / 'score/record.json')
        self.assertEqual(result['score']['brier'], result['score']['baseline_brier']['base_rate'])
        self.assertEqual(result['score']['brier_skill_vs_base_rate'], 0)
        self.assertTrue(result['exclude_from_trial_metrics'])
        previous_bytes = {p: p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        self.assertEqual(probe.settle_phase(self.store, key), 'SCORED')
        self.assertEqual({p: p.read_bytes() for p in previous_bytes}, previous_bytes)

    def test_outcome_uses_forecast_threshold_after_candidates_change(self):
        key = self.forecast()
        _, _, _, snapshot = probe._read_forecast(self.store, key)
        old = snapshot['stocks']['6857']['threshold']['T_final']
        self.thresholds['thresholds']['6857']['T_final'] = .99
        self.save_candidates()
        self.clock.return_value = AFTER
        self.assertEqual(probe.settle_phase(self.store, key, self.parsed_path), 'SCORED')
        outcome = core.read_frozen(self.store / key / 'outcome/record.json')['outcome']
        self.assertEqual(outcome['actual_class'], core.classify(outcome['actual_return'], old))

    def test_forecast_tamper_blocks_settlement(self):
        key = self.forecast()
        _, path, _, _ = probe._read_forecast(self.store, key)
        path.write_bytes(b'{}')
        self.clock.return_value = AFTER
        with self.assertRaisesRegex(ValueError, 'TAMPER_DETECTED'):
            probe.settle_phase(self.store, key, self.parsed_path)

    def test_outcome_and_score_packages_are_create_only(self):
        key = self.forecast()
        self.clock.return_value = AFTER
        probe.settle_phase(self.store, key, self.parsed_path)
        for name in ('outcome', 'score'):
            package = self.store / key / name
            before = (package / 'record.json').read_bytes()
            with self.assertRaises(FileExistsError):
                probe._commit_record(package, {'replacement': True})
            self.assertEqual((package / 'record.json').read_bytes(), before)

    def test_score_interruption_leaves_no_partial_commit_and_can_resume(self):
        key = self.forecast()
        self.clock.return_value = AFTER
        original = probe._seal
        def interrupted(path, record):
            original(path, record)
            if 'scored_at' in record:
                raise RuntimeError('score write interrupted')
        with patch.object(probe, '_seal', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'score write interrupted'):
                probe.settle_phase(self.store, key, self.parsed_path)
        self.assertEqual(probe.lifecycle(self.store, key), 'OUTCOME_FROZEN')
        self.assertFalse(list((self.store / key).glob('.pending-*')))
        self.assertEqual(probe.settle_phase(self.store, key), 'SCORED')

    def test_uncommitted_orphan_is_ignored(self):
        key = self.forecast()
        orphan = self.store / key / '.pending-crash'
        orphan.mkdir()
        (orphan / 'record.json').write_bytes(b'partial')
        self.assertEqual(probe.lifecycle(self.store, key), 'WAITING_OUTCOME')
        self.clock.return_value = AFTER
        self.assertEqual(probe.settle_phase(self.store, key, self.parsed_path), 'SCORED')

    def test_late_research_cannot_score_or_freeze(self):
        key = self.forecast()
        _, _, forecast, snapshot = probe._read_forecast(self.store, key)
        record = dict(forecast, status='LATE_RESEARCH_DIAGNOSTIC')
        with self.assertRaisesRegex(ValueError, 'TEST_ONLY_CORE_FORECAST'):
            core.validate_forecast(record, snapshot)
        with self.assertRaisesRegex(ValueError, 'FORECAST_OUTCOME_LINK_MISMATCH'):
            core.score(record, {'status': 'LATE_RESEARCH_DIAGNOSTIC'}, {})

    def test_zero_base_rate_probability_log_loss_is_explicit_null(self):
        for symbol in core.CORE:
            probabilities = {'UP': 0, 'FLAT': .5, 'DOWN': .5}
            self.baselines['baselines'][symbol]['base_rate']['probabilities'] = probabilities
            self.tables['tables'][symbol]['base_rate']['probabilities'] = probabilities
        self.save_candidates()
        key = self.forecast()
        row = next(r for r in self.parsed['records'] if r['ticker'] == '6857')
        row.update(open=100, low=99, high=103, close=102)
        row['normalized_observation'].update(open=100, low=99, high=103, close=102)
        self.save_outcome()
        self.clock.return_value = AFTER
        self.assertEqual(probe.settle_phase(self.store, key, self.parsed_path), 'SCORED')
        result = core.read_frozen(self.store / key / 'score/record.json')['score']
        self.assertEqual(result['actual_class'], 'UP')
        self.assertIsNone(result['log_loss'])
        self.assertEqual(result['log_loss_reason'], 'ZERO_PROBABILITY_NO_CLIPPING')

    def test_source_and_candidate_files_unchanged(self):
        before = {p: core.sha256(p) for p in self.root.rglob('*') if p.is_file()}
        key = self.forecast()
        self.clock.return_value = AFTER
        probe.settle_phase(self.store, key, self.parsed_path)
        self.assertEqual({p: core.sha256(p) for p in before}, before)


if __name__ == '__main__':
    unittest.main()
