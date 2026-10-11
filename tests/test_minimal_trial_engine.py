import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from engine import minimal_trial as e


def fake_rows(last_session='2026-09-17'):
    days = []
    day = date.fromisoformat(last_session)
    while len(days) < 100:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    rows = []
    for i, current in enumerate(reversed(days)):
        opening = 100 + i * .12
        closing = opening * (1 + .005 * (i % 5 - 2))
        rows.append({'session_date': current.isoformat(), 'open': opening,
                     'high': max(opening, closing)+1, 'low': min(opening, closing)-1,
                     'close': closing, 'volume': 100000 + i * 100})
    return rows


def fixture():
    rows = fake_rows()
    threshold = e.threshold(rows)
    feature = e.features(rows, threshold)
    baseline = e.baselines(rows, threshold)
    stocks = {ticker: {'features': feature, 'threshold': threshold,
                       'baselines': baseline, 'provenance': {'fixture': 'SYNTHETIC'}}
              for ticker in e.CORE}
    snapshot = e.compact_snapshot('2026-09-18', '2026-09-17', '2026-09-18T08:45:00+09:00', stocks)
    forecast = {'status': 'ENGINE_TEST_ONLY', 'forecast_id': 'ENGINE-TEST-6857-v1',
                'forecast_version': 'v1', 'ticker': '6857', 'target_session': '2026-09-18',
                'created_at': '2026-09-18T08:40:00+09:00',
                'freeze_time': '2026-09-18T08:45:00+09:00',
                'input_snapshot_hash': hashlib.sha256(e.canonical_bytes(snapshot)).hexdigest(),
                'UP': 65, 'FLAT': 20, 'DOWN': 15,
                'most_likely_class': 'UP', 'confidence': 'HIGH',
                'threshold_version': threshold['threshold_version'], 'top_factors': []}
    return snapshot, forecast


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.forecast = fixture()

    def test_current_live_feature_spec_and_close_only_inputs(self):
        spec = json.loads((e.ROOT / 'config/minimal_live_feature_spec.json').read_text())
        runtime = json.loads((e.ROOT / 'state/runtime/runtime_snapshot.json').read_text())
        self.assertEqual(set(e.REQUIRED_FEATURES), set(spec['features']))
        self.assertEqual(set(e.REQUIRED_FEATURES), set(runtime['features']))
        rows = [{'session_date': row['session_date'], 'close': row['close']} for row in fake_rows()]
        limit = self.snapshot['stocks']['6857']['threshold']
        with patch.object(e, 'atr14_pct', side_effect=AssertionError('live ATR calculation')):
            feature = e.features(rows, limit)
        for removed in ('ATR14_pct', 'volume_ratio_20d'):
            self.assertNotIn(removed, e.REQUIRED_FEATURES)
            self.assertNotIn(removed, feature)
        stocks = copy.deepcopy(self.snapshot['stocks'])
        for item in stocks.values():
            item['features'] = {key: feature[key] for key in
                                (*spec['features'], 'threshold_version', 'previous_session')}
        snapshot = e.compact_snapshot('2026-09-18', '2026-09-17',
                                      '2026-09-18T08:45:00+09:00', stocks)
        self.assertEqual(len(snapshot['stocks']), 6)

    def test_formal_and_trial_execution_gates_remain_off(self):
        config = json.loads((e.ROOT / 'config/minimal_forward_trial_config.json').read_text())
        runtime = json.loads((e.ROOT / 'state/runtime/runtime_snapshot.json').read_text())
        for name in ('formal_forecast_enabled', 'forward_trial_enabled', 'forecast_execution_enabled'):
            self.assertIs(config['gates'][name], False)
        self.assertIs(runtime['forecast_allowed'], False)
        self.assertIs(runtime['gates']['trial_enabled'], False)
        for name in ('formal_forecast', 'forward_trial', 'score', 'prediction_log', 'backdating'):
            self.assertIs(runtime['late_research'][name], False)

    def test_old_frozen_threshold_and_baseline_are_not_stale(self):
        rows = fake_rows('2026-10-02')
        training = [row for row in rows if row['session_date'] <= '2026-09-11']
        limit = e.threshold(training)
        original = copy.deepcopy(limit)
        baseline = e.baselines(training, limit)
        with patch.object(e, 'threshold', side_effect=AssertionError('threshold reestimation')):
            feature = e.features(rows, limit)
            # The offline helper also accepts a fixed threshold from an earlier session.
            self.assertEqual(e.baselines(rows, limit)['training_end_date'], '2026-10-02')
        stocks = {ticker: {'features': feature, 'threshold': limit, 'baselines': baseline,
                           'provenance': {'fixture': 'SYNTHETIC'}} for ticker in e.CORE}
        with patch.object(e, 'threshold', side_effect=AssertionError('threshold reestimation')), \
                patch.object(e, 'baselines', side_effect=AssertionError('baseline reestimation')):
            snapshot = e.compact_snapshot('2026-10-05', '2026-10-02',
                                          '2026-10-05T08:45:00+09:00', stocks)
        self.assertEqual(snapshot['stocks']['6857']['threshold']['training_end_date'], '2026-09-11')
        self.assertEqual(snapshot['stocks']['6857']['baselines'], baseline)
        self.assertEqual(limit, original)
        limit['T_final'] *= 2
        self.assertEqual(snapshot['stocks']['6857']['threshold'], original)

    def test_future_threshold_and_baseline_training_rejected(self):
        rows = fake_rows('2026-10-02')
        for day in ('2026-10-05', '2026-10-06'):
            limit = dict(self.snapshot['stocks']['6857']['threshold'], training_end_date=day)
            for function in (e.features, e.baselines):
                with self.subTest(day=day, function=function.__name__), \
                        self.assertRaisesRegex(ValueError, 'THRESHOLD_TRAINING_LOOKAHEAD'):
                    function(rows, limit)
            stocks = copy.deepcopy(self.snapshot['stocks'])
            stocks['6857']['threshold'] = limit
            with self.assertRaisesRegex(ValueError, 'THRESHOLD_TRAINING_LOOKAHEAD'):
                e.compact_snapshot('2026-10-05', '2026-10-02', '2026-10-05T08:45:00+09:00', stocks)
        stocks = copy.deepcopy(self.snapshot['stocks'])
        stocks['6857']['baselines']['training_end_date'] = '2026-09-18'
        with self.assertRaisesRegex(ValueError, 'LOOKAHEAD_STALE_OR_BASELINE_MISMATCH'):
            e.compact_snapshot('2026-09-18', '2026-09-17', '2026-09-18T08:45:00+09:00', stocks)

    def test_threshold_value_and_versions_must_match(self):
        for field, value, error in (
                ('threshold', .5, 'FEATURE_THRESHOLD_VALUE_MISMATCH'),
                ('threshold_version', 'unknown', 'FEATURE_THRESHOLD_VERSION_MISMATCH')):
            stocks = copy.deepcopy(self.snapshot['stocks'])
            stocks['6857']['features'][field] = value
            with self.assertRaisesRegex(ValueError, error):
                e.compact_snapshot('2026-09-18', '2026-09-17', '2026-09-18T08:45:00+09:00', stocks)
        stocks = copy.deepcopy(self.snapshot['stocks'])
        stocks['6857']['baselines']['threshold_version'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'BASELINE_THRESHOLD_VERSION_MISMATCH'):
            e.compact_snapshot('2026-09-18', '2026-09-17', '2026-09-18T08:45:00+09:00', stocks)
        limit = dict(stocks['6857']['threshold'], threshold_version='unregistered')
        with self.assertRaisesRegex(ValueError, 'THRESHOLD_VERSION_MISSING_OR_UNREGISTERED'):
            e.features(fake_rows(), limit)
        for status in (None, 'UNFROZEN'):
            limit = dict(stocks['6857']['threshold'], status=status)
            with self.assertRaisesRegex(ValueError, 'THRESHOLD_NOT_FROZEN_RESEARCH_OR_TEST'):
                e.features(fake_rows(), limit)

    def test_frozen_candidate_bundle_reused_without_activation_or_refit(self):
        limits = json.loads((e.ROOT / 'config/trial_thresholds_candidate.json').read_text())
        baselines = json.loads((e.ROOT / 'config/trial_baselines_candidate.json').read_text())
        tables = json.loads((e.ROOT / 'config/trial_baseline_state_tables_candidate.json').read_text())
        from engine.fresh_bridge import select_baselines
        rows = fake_rows('2026-10-02')
        stocks = {}
        for ticker in e.CORE:
            limit = limits['thresholds'][ticker]
            feature = e.features(rows, limit)
            feature['momentum_state'] = e.momentum_state(feature['momentum_20d'])
            baseline = dict(baselines['baselines'][ticker],
                            **select_baselines(feature, tables['tables'][ticker]))
            stocks[ticker] = {'features': feature, 'threshold': limit,
                              'baselines': baseline, 'provenance': {'fixture': 'SYNTHETIC'}}
        before = e.canonical_bytes(stocks)
        with patch.object(e, 'threshold', side_effect=AssertionError('threshold reestimation')), \
                patch.object(e, 'baselines', side_effect=AssertionError('baseline reestimation')):
            snapshot = e.compact_snapshot('2026-10-05', '2026-10-02',
                                          '2026-10-05T08:45:00+09:00', stocks)
        self.assertEqual(before, e.canonical_bytes(stocks))
        self.assertEqual(snapshot['stocks'], stocks)
        self.assertFalse(limits['human_start_approval'])
        stocks['6857']['threshold']['T_final'] *= 2
        stocks['6857']['features']['threshold'] = stocks['6857']['threshold']['T_final']
        with self.assertRaisesRegex(ValueError, 'FROZEN_THRESHOLD_BUNDLE_MISMATCH'):
            e.compact_snapshot('2026-10-05', '2026-10-02', '2026-10-05T08:45:00+09:00', stocks)

    def test_feature_threshold_baselines(self):
        item = self.snapshot['stocks']['6857']
        self.assertEqual(item['features']['previous_session'], '2026-09-17')
        self.assertEqual(item['features']['MA_state'], 'BULL')
        self.assertGreater(item['threshold']['T_final'], 0)
        for name in ('base_rate', 'momentum', 'ma_trend'):
            self.assertAlmostEqual(sum(item['baselines'][name]['probabilities'].values()), 1)
        self.assertEqual(self.snapshot['market_common']['sp500_overnight'], None)
        self.assertEqual(len(self.snapshot['stocks']), 6)
        self.assertEqual(sum(len(v) for v in self.snapshot['news'].values()), 0)

    def test_boundary_and_atr_trigger(self):
        self.assertEqual(e.classify(.02, .02), 'UP')
        self.assertEqual(e.classify(-.02, .02), 'DOWN')
        self.assertEqual(e.classify(0, .02), 'FLAT')
        rows = fake_rows()
        for i in range(80, 100):
            rows[i]['close'] *= 1 + (i % 2) * .25
            rows[i]['high'] = max(rows[i]['high'], rows[i]['close']+1)
            rows[i]['low'] = min(rows[i]['low'], rows[i]['close']-1)
        self.assertTrue(e.threshold(rows)['volatility_adjustment_used'])

    def test_real_cache_and_known_conflict(self):
        for ticker in ('6857', '7203', '1605', '9433', '3197'):
            rows, provenance = e.load_history(ticker)
            self.assertGreater(len(rows), 1000)
            self.assertEqual(provenance['eligibility'], 'RESEARCH_ONLY')
            self.assertGreater(e.threshold(rows)['T_final'], 0)
        with self.assertRaisesRegex(ValueError, 'CACHE_OHLC_CONFLICT_8306_2022-05-17'):
            e.load_history('8306')

    def test_conditional_fallback(self):
        rows = fake_rows()
        baseline = e.baselines(rows, e.threshold(rows), min_conditional=1000)
        self.assertTrue(baseline['momentum']['fallback'])
        self.assertTrue(baseline['ma_trend']['fallback'])
        self.assertEqual(baseline['base_rate']['probabilities'], baseline['momentum']['probabilities'])

    def test_threshold_bundle_create_only(self):
        thresholds = {ticker: self.snapshot['stocks'][ticker]['threshold'] for ticker in e.CORE}
        with tempfile.TemporaryDirectory() as tmp:
            path = e.write_test_threshold_bundle(thresholds, tmp)
            self.assertTrue(path.is_file())
            with self.assertRaises(FileExistsError):
                e.write_test_threshold_bundle(thresholds, tmp)
            self.assertEqual(len(__import__('json').loads(path.read_text())['thresholds']), 6)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'CORE6_THRESHOLD_FREEZE_REQUIRED'):
                e.write_test_threshold_bundle({'6857': thresholds['6857']}, tmp)

    def test_full_engine_real_jpx_outcome_test_only(self):
        parsed = e.load_parsed_jpx(e.ROOT / 'data/outcome_parsed/jpx/2026-09-18_core6_browser_v02_engine_test.json')
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = e.write_test_snapshot(self.snapshot, tmp)
            for ticker in e.CORE:
                forecast = dict(self.forecast, ticker=ticker, forecast_id=f'ENGINE-TEST-{ticker}-v1')
                path = e.write_frozen(forecast, self.snapshot, tmp)
                frozen = e.read_frozen(path)
                outcome = e.outcome_from_jpx(parsed, ticker, frozen,
                                             self.snapshot['stocks'][ticker]['threshold']['T_final'])
                scored = e.score_frozen(path, snapshot_path, outcome)
                review = e.failure_trigger(frozen, scored, self.snapshot['stocks'][ticker]['baselines'])
                self.assertEqual(outcome['status'], 'ENGINE_TEST_ONLY')
                self.assertEqual(outcome['actual_return'], outcome['actual_close']/outcome['actual_open']-1)
                self.assertEqual(scored['status'], 'ENGINE_TEST_ONLY')
                self.assertEqual(len(scored['baseline_brier']), 3)
                self.assertFalse(review['llm_review_executed'])
                self.assertEqual(e.read_frozen(path), frozen)
            path = Path(tmp) / 'ENGINE-TEST-6857-v1.json'
            frozen = e.read_frozen(path)
            with self.assertRaises(FileExistsError):
                e.write_frozen(dict(self.forecast, UP=70, FLAT=15), self.snapshot, tmp)
            self.assertEqual(e.read_frozen(path), frozen)

    def test_hash_tamper_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = e.write_frozen(self.forecast, self.snapshot, tmp)
            path.write_text('tampered', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'FORECAST_TAMPER_DETECTED'):
                e.read_frozen(path)

    def test_missing_feature_and_stale_or_future_input(self):
        stocks = copy.deepcopy(self.snapshot['stocks'])
        del stocks['6857']['features']['MA20']
        with self.assertRaisesRegex(ValueError, 'MISSING_REQUIRED_FEATURE'):
            e.compact_snapshot('2026-09-18', '2026-09-17', '2026-09-18T08:45:00+09:00', stocks)
        stocks = copy.deepcopy(self.snapshot['stocks'])
        stocks['6857']['features']['raw_history'] = fake_rows()
        with self.assertRaisesRegex(ValueError, 'RAW_ROWS_OR_UNKNOWN_FEATURE'):
            e.compact_snapshot('2026-09-18', '2026-09-17', '2026-09-18T08:45:00+09:00', stocks)
        stocks = copy.deepcopy(self.snapshot['stocks'])
        stocks['6857']['features']['previous_session'] = '2026-09-18'
        with self.assertRaisesRegex(ValueError, 'LOOKAHEAD_STALE'):
            e.compact_snapshot('2026-09-18', '2026-09-17', '2026-09-18T08:45:00+09:00', stocks)

    def test_missing_news_and_market_are_explicit_null_or_empty(self):
        self.assertIsNone(self.snapshot['market_common']['usdjpy_change'])
        self.assertEqual(self.snapshot['news']['6857'], [])

    def test_missing_outcome_parser_failure_and_core5(self):
        with self.assertRaises(FileNotFoundError):
            e.load_parsed_jpx('nonexistent.pdf')
        parsed = {'normalized_schema_validation': 'FAIL', 'core6_extraction': '6/6'}
        with self.assertRaisesRegex(ValueError, 'PARSER_OR_CORE6_FAILURE'):
            e.outcome_from_jpx(parsed, '6857', self.forecast, .01)
        parsed['normalized_schema_validation'] = 'PASS'
        parsed['core6_extraction'] = '5/6'
        with self.assertRaisesRegex(ValueError, 'PARSER_OR_CORE6_FAILURE'):
            e.outcome_from_jpx(parsed, '6857', self.forecast, .01)

    def test_threshold_missing_and_bad_probabilities(self):
        forecast = dict(self.forecast, threshold_version=None)
        with self.assertRaisesRegex(ValueError, 'THRESHOLD_VERSION'):
            e.validate_forecast(forecast, self.snapshot)
        for probabilities in ((60, 20, 15), (61, 19, 20)):
            forecast = dict(self.forecast, UP=probabilities[0], FLAT=probabilities[1], DOWN=probabilities[2])
            with self.assertRaisesRegex(ValueError, 'INVALID_PROBABILITIES'):
                e.validate_forecast(forecast, self.snapshot)

    def test_session_mismatch(self):
        parsed = {'normalized_schema_validation': 'PASS', 'core6_extraction': '6/6',
                  'session_date': '2026-09-17'}
        with self.assertRaisesRegex(ValueError, 'OUTCOME_SESSION_MISMATCH'):
            e.outcome_from_jpx(parsed, '6857', self.forecast, .01)

    def test_failure_triggers(self):
        baseline = self.snapshot['stocks']['6857']['baselines']
        scored = {'accuracy': 0, 'brier': 1.2, 'actual_class': 'DOWN',
                  'baseline_brier': {k: 0.3 for k in ('base_rate', 'momentum', 'ma_trend')}}
        review = e.failure_trigger(self.forecast, scored, baseline, data_quality=True)
        self.assertIn('HIGH_CONFIDENCE_MISS', review['candidate_reasons'])
        self.assertIn('LARGE_BRIER', review['candidate_reasons'])
        self.assertIn('DATA_QUALITY_SUSPECTED', review['candidate_reasons'])
        self.assertEqual(review['failure_type'], 'UNKNOWN')


class RawSourceResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        root_patch = patch.object(e, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.inbox = self.root / 'data/outcome_input/manual_jpx'
        self.inbox.mkdir(parents=True)
        self.raw = self.inbox / 'retained.pdf'
        self.raw.write_bytes(b'%PDF-1.4 retained original fixture')
        self.parsed = {
            'source_file': r'C:\old\project\data\outcome_input\manual_jpx\stq_20260918.pdf',
            'source_file_hash': e.sha256(self.raw), 'session_date': '2026-09-18'}
        self.entry = {'filename': self.raw.name, 'sha256': self.parsed['source_file_hash'],
                      'session_date': self.parsed['session_date'],
                      'source_metadata': {'local_file': self.raw.name}}
        self.manifest = self.inbox / 'retention_manifest.json'
        self.save_manifest([self.entry])
        self.parsed_path = self.root / 'parsed.json'
        self.parsed_path.write_text(json.dumps(self.parsed), encoding='utf-8')

    def save_manifest(self, entries):
        self.manifest.write_text(json.dumps({'entries': entries}), encoding='utf-8')

    def assert_rejected(self, message='RAW_SOURCE_HASH_MISMATCH'):
        with self.assertRaisesRegex(ValueError, message):
            e._resolve_jpx_raw_source(self.parsed)

    def test_valid_saved_path_without_manifest(self):
        self.parsed['source_file'] = str(self.raw)
        self.manifest.unlink()
        self.assertEqual(e._resolve_jpx_raw_source(self.parsed), self.raw)

    def test_invalid_windows_path_resolves_manifest_hash_even_after_rename(self):
        self.assertEqual(e._resolve_jpx_raw_source(self.parsed), self.raw)

    def test_metadata_local_file_resolves_raw(self):
        self.entry['filename'] = 'missing.pdf'
        self.save_manifest([self.entry])
        self.assertEqual(e._resolve_jpx_raw_source(self.parsed), self.raw)

    def test_same_filename_with_mismatched_hash_is_rejected(self):
        self.raw.write_bytes(b'%PDF-1.4 different original')
        self.assert_rejected()

    def test_valid_saved_path_mismatch_does_not_use_matching_fallback(self):
        wrong = self.root / 'wrong.pdf'
        wrong.write_bytes(b'different')
        self.parsed['source_file'] = str(wrong)
        self.assert_rejected()

    def test_missing_raw_is_rejected(self):
        self.raw.unlink()
        self.assert_rejected()

    def test_missing_manifest_is_rejected(self):
        self.manifest.unlink()
        self.assert_rejected()

    def test_manifest_hash_or_session_mismatch_is_rejected(self):
        for field, value in (('sha256', '0' * 64), ('session_date', '2026-09-17')):
            with self.subTest(field=field):
                entry = dict(self.entry, **{field: value})
                self.save_manifest([entry])
                self.assert_rejected()

    def test_distinct_matching_raw_candidates_are_ambiguous(self):
        other = self.inbox / 'duplicate.pdf'
        other.write_bytes(self.raw.read_bytes())
        self.save_manifest([self.entry, dict(self.entry, filename=other.name,
                                           source_metadata={'local_file': other.name})])
        self.assert_rejected('RAW_SOURCE_AMBIGUOUS')

    def test_duplicate_manifest_references_to_same_path_are_not_ambiguous(self):
        self.save_manifest([self.entry, self.entry])
        self.assertEqual(e._resolve_jpx_raw_source(self.parsed), self.raw)

    def test_missing_non_windows_path_does_not_use_fallback(self):
        self.parsed['source_file'] = str(self.root / 'missing.pdf')
        self.assert_rejected()

    def test_manifest_path_outside_raw_inbox_is_rejected(self):
        outside = self.root / 'outside.pdf'
        outside.write_bytes(self.raw.read_bytes())
        for reference in ('../../../outside.pdf', str(outside)):
            with self.subTest(reference=reference):
                self.save_manifest([dict(self.entry, filename=reference,
                                         source_metadata={'local_file': reference})])
                self.assert_rejected()

    def test_loader_preserves_parsed_raw_and_outcome_history(self):
        outcome = self.root / 'outcome_history.json'
        outcome.write_bytes(b'{"existing_outcome": true}')
        paths = (self.parsed_path, self.raw, self.manifest, outcome)
        before = {p: p.read_bytes() for p in paths}
        self.assertEqual(e.load_parsed_jpx(self.parsed_path), self.parsed)
        self.assertEqual({p: p.read_bytes() for p in paths}, before)


if __name__ == '__main__':
    unittest.main()
