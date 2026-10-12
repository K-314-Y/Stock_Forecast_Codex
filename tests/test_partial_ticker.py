"""Self-contained synthetic offline coverage; never acquires market data."""
import copy
import hashlib
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from engine import minimal_trial as core
from engine import partial_ticker as p

TARGET = '2026-09-18'
PREVIOUS = '2026-09-17'
FREEZE = TARGET + 'T08:45:00+09:00'


def fixture():
    start = date.fromisoformat(PREVIOUS) - timedelta(days=99)
    rows = []
    for index in range(100):
        opening = 100 + index * .12
        closing = opening * (1 + .005 * (index % 5 - 2))
        rows.append(dict(session_date=(start + timedelta(days=index)).isoformat(),
                         open=opening, high=max(opening, closing) + 1,
                         low=min(opening, closing) - 1, close=closing, volume=1000 + index))
    threshold = core.threshold(rows)
    probabilities = dict(UP=.34, FLAT=.33, DOWN=.33)
    distribution = dict(probabilities=probabilities, sample_count=100, fallback=False)
    tables = dict(base_rate=distribution,
                  momentum={state: distribution for state in ('POSITIVE', 'NEGATIVE', 'FLAT')},
                  ma_trend={state: distribution for state in ('BULL', 'BEAR', 'NEUTRAL')})
    metadata = dict(training_end_date=PREVIOUS, threshold_version=threshold['threshold_version'],
                    baseline_version='minimal-baselines-v1')
    inputs = {ticker: copy.deepcopy(dict(rows=rows, threshold=threshold, baseline_tables=tables,
                                        baseline_metadata=metadata, provenance={'fixture': 'SYNTHETIC'}))
              for ticker in core.CORE}
    return inputs, [row['session_date'] for row in rows]


def prepare(inputs, sessions, **kwargs):
    return p.prepare_session(TARGET, PREVIOUS, FREEZE, inputs, sessions, **kwargs)


def prepared_stocks(inputs):
    stocks = {}
    for ticker, item in inputs.items():
        feature = core.features(item['rows'], item['threshold'])
        feature['momentum_state'] = core.momentum_state(feature['momentum_20d'])
        baseline = dict(p.select_baselines(feature, item['baseline_tables']), **item['baseline_metadata'])
        stocks[ticker] = dict(features=feature, threshold=item['threshold'], baselines=baseline,
                              provenance=item['provenance'])
    return copy.deepcopy(stocks)


class PartialTickerTests(unittest.TestCase):
    def setUp(self):
        self.inputs, self.sessions = fixture()

    def assert_coverage(self, result, missing):
        skipped = list(core.CORE[:missing])
        available = list(core.CORE[missing:])
        self.assertEqual(result['expected_tickers'], list(core.CORE))
        self.assertEqual(result['processed_tickers'], available)
        self.assertEqual(result['available_tickers'], available)
        self.assertEqual(result['skipped_tickers'], skipped)
        self.assertEqual(list(result['snapshot']['stocks']), available)
        self.assertEqual(list(result['snapshot']['news']), available)
        for ticker in skipped:
            record = result['ticker_results'][ticker]
            self.assertEqual(record['ticker'], ticker)
            self.assertEqual(record['session'], TARGET)
            self.assertEqual(record['status'], 'SKIPPED_MISSING_INPUT')
            self.assertTrue(record['missing_inputs'])
            self.assertEqual(record['reason'], 'REQUIRED_INPUT_ABSENT')
            self.assertTrue(record['evidence_refs'])
        for stage in p.STAGES:
            view = p.inputs_for_stage(result, stage)
            self.assertEqual(view['expected_tickers'], list(core.CORE))
            self.assertEqual(view['available_tickers'], available)
            self.assertEqual(view['skipped_tickers'], skipped)
            self.assertEqual(list(view['stocks']), available)
            self.assertEqual(view['processed_tickers'], available if stage in p.STAGES[:2] else [])

    def test_one_missing_five_processed_no_session_stop(self):
        del self.inputs['6857']
        with patch.object(core, 'features', wraps=core.features) as features, \
                patch.object(p, 'select_baselines', wraps=p.select_baselines) as baselines:
            result = prepare(self.inputs, self.sessions)
        self.assert_coverage(result, 1)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(features.call_count, 5)
        self.assertEqual(baselines.call_count, 5)

    def test_multiple_missing_two_through_five(self):
        for count in range(2, 6):
            with self.subTest(count=count):
                inputs = {ticker: value for ticker, value in self.inputs.items() if ticker not in core.CORE[:count]}
                result = prepare(inputs, self.sessions)
                self.assert_coverage(result, count)
                self.assertEqual(result['status'], 'PARTIAL')

    def test_all_valid_legacy_snapshot_and_hash_exactly_compatible(self):
        stocks = prepared_stocks(self.inputs)
        expected = core.compact_snapshot(TARGET, PREVIOUS, FREEZE, stocks)
        for result in (prepare(self.inputs, self.sessions),
                       p.prepare_snapshot(TARGET, PREVIOUS, FREEZE, stocks)):
            self.assert_coverage(result, 0)
            self.assertEqual(result['status'], 'COMPLETE')
            self.assertEqual(core.canonical_bytes(result['snapshot']), core.canonical_bytes(expected))
            self.assertEqual(hashlib.sha256(core.canonical_bytes(result['snapshot'])).hexdigest(),
                             hashlib.sha256(core.canonical_bytes(expected)).hexdigest())

    def test_all_missing_zero_outputs_and_no_downstream_calls(self):
        with patch.object(core, 'features', side_effect=AssertionError('features called')), \
                patch.object(p, 'select_baselines', side_effect=AssertionError('baseline called')):
            for result in (prepare({}, self.sessions), p.prepare_snapshot(TARGET, PREVIOUS, FREEZE, {})):
                self.assert_coverage(result, 6)
                self.assertEqual(result['status'], 'NO_PROCESSABLE_TICKERS')
                self.assertEqual(result['snapshot']['stocks'], {})
                self.assertEqual(result['snapshot']['metrics']['llm_calls'], 0)

    def test_required_ohlc_fields_missing(self):
        for field in ('open', 'high', 'low', 'close'):
            for value in ('ABSENT', None):
                with self.subTest(field=field, value=value):
                    inputs = copy.deepcopy(self.inputs)
                    if value == 'ABSENT':
                        del inputs['6857']['rows'][-1][field]
                    else:
                        inputs['6857']['rows'][-1][field] = value
                    result = prepare(inputs, self.sessions, required_price_fields=('open', 'high', 'low', 'close'))
                    self.assert_coverage(result, 1)
                    self.assertIn('rows[99].' + field, result['ticker_results']['6857']['missing_inputs'])

    def test_required_feature_missing(self):
        for field in core.REQUIRED_FEATURES:
            for absent in (False, True):
                with self.subTest(field=field, absent=absent):
                    stocks = prepared_stocks(self.inputs)
                    if absent:
                        del stocks['6857']['features'][field]
                    else:
                        stocks['6857']['features'][field] = None
                    result = p.prepare_snapshot(TARGET, PREVIOUS, FREEZE, stocks)
                    self.assert_coverage(result, 1)
                    self.assertEqual(result['ticker_results']['6857']['missing_inputs'], ['features.' + field])

    def test_missing_issue_requires_actual_gap_and_never_hides_integrity_issue(self):
        row = self.inputs['6857']['rows'][-1]
        row['close'] = None
        row['issues'] = ['MISSING_OHLC']
        self.assert_coverage(prepare(self.inputs, self.sessions), 1)
        row['issues'].append('CLOSE_BELOW_LOW')
        with self.assertRaisesRegex(ValueError, 'HISTORY_INTEGRITY_CONFLICT'):
            prepare(self.inputs, self.sessions)
        row['close'] = self.inputs['7203']['rows'][-1]['close']
        row['issues'] = ['MISSING_OHLC']
        with self.assertRaisesRegex(ValueError, 'HISTORY_INTEGRITY_CONFLICT'):
            prepare(self.inputs, self.sessions)

    def test_entirely_absent_ticker_is_distinct(self):
        del self.inputs['6857']
        record = prepare(self.inputs, self.sessions)['ticker_results']['6857']
        self.assertEqual(record['missing_inputs'], ['ticker'])

    def test_history_gap_and_insufficient_history_skip_without_repair(self):
        for rows in (self.inputs['6857']['rows'][:-1], self.inputs['6857']['rows'][-60:]):
            with self.subTest(length=len(rows)):
                inputs = copy.deepcopy(self.inputs)
                inputs['6857']['rows'] = rows
                before = copy.deepcopy(inputs)
                result = prepare(inputs, self.sessions)
                self.assert_coverage(result, 1)
                self.assertEqual(inputs, before)

    def test_close_only_contract_remains_compatible(self):
        for item in self.inputs.values():
            item['rows'] = [{key: row[key] for key in ('session_date', 'close')} for row in item['rows']]
        self.assert_coverage(prepare(self.inputs, self.sessions), 0)

    def test_missing_components_and_frozen_baseline_input(self):
        for key in ('rows', 'threshold', 'baseline_tables', 'baseline_metadata', 'provenance'):
            with self.subTest(key=key):
                inputs = copy.deepcopy(self.inputs)
                del inputs['6857'][key]
                self.assert_coverage(prepare(inputs, self.sessions), 1)
        with patch.object(core, 'threshold', side_effect=AssertionError('reestimate')), \
                patch.object(core, 'baselines', side_effect=AssertionError('retrain')):
            self.assert_coverage(prepare(self.inputs, self.sessions), 0)

    def test_missing_plus_ohlc_conflict_other_ticker_stops_before_features(self):
        del self.inputs['6857']
        self.inputs['3197']['rows'][-1]['high'] = 1
        with patch.object(core, 'features', side_effect=AssertionError('features called')), \
                self.assertRaisesRegex(ValueError, 'OHLC_INCONSISTENCY'):
            prepare(self.inputs, self.sessions)

    def test_same_ticker_missing_and_ohlc_conflict_stops(self):
        row = self.inputs['6857']['rows'][-1]
        del row['close']
        row['high'] = 1
        with self.assertRaisesRegex(ValueError, 'OHLC_INCONSISTENCY'):
            prepare(self.inputs, self.sessions)

    def test_missing_feature_does_not_hide_other_integrity_errors(self):
        stocks = prepared_stocks(self.inputs)
        del stocks['6857']['features']['MA20']
        changes = (
            ('features', 'previous_session', TARGET, 'LOOKAHEAD_STALE'),
            ('features', 'threshold', .9, 'FEATURE_THRESHOLD_VALUE_MISMATCH'),
            ('baselines', 'threshold_version', 'wrong', 'BASELINE_THRESHOLD_VERSION_MISMATCH'),
            ('provenance', 'ticker', '7203', 'TICKER_MISMATCH'),
            ('provenance', 'conflict', True, 'PROVENANCE_CONFLICT'),
            ('provenance', 'security_challenge', True, 'SECURITY_CHALLENGE'))
        for kind, field, value, error in changes:
            with self.subTest(field=field):
                case = copy.deepcopy(stocks)
                case['6857'][kind][field] = value
                with self.assertRaisesRegex(ValueError, error):
                    p.prepare_snapshot(TARGET, PREVIOUS, FREEZE, case)

    def test_missing_plus_hash_conflict_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source.txt'
            source.write_text('synthetic evidence', encoding='utf-8')
            ref = dict(path=str(source), sha256=core.sha256(source))
            self.inputs['6857']['provenance'] = {'evidence': [ref]}
            del self.inputs['6857']['rows'][-1]['close']
            result = prepare(self.inputs, self.sessions)
            self.assertIn(ref, result['ticker_results']['6857']['evidence_refs'])
            source.write_text('changed synthetic evidence', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'HASH_CONFLICT'):
                prepare(self.inputs, self.sessions)

    def test_hash_change_during_processing_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source.txt'
            source.write_text('synthetic evidence', encoding='utf-8')
            self.inputs['6857']['provenance'] = {'evidence': [dict(path=str(source), sha256=core.sha256(source))]}
            original = core.features
            def changing(rows, limit):
                source.write_text('changed synthetic evidence', encoding='utf-8')
                return original(rows, limit)
            with patch.object(core, 'features', side_effect=changing), self.assertRaisesRegex(ValueError, 'HASH_CONFLICT'):
                prepare(self.inputs, self.sessions)

    def test_raw_session_identity_and_security_errors_are_not_missing(self):
        changes = (('ticker', '7203', 'TICKER_MISMATCH'),
                   ('session_date', TARGET, 'SESSION_MISMATCH'),
                   ('issues', ['CLOSE_BELOW_LOW'], 'HISTORY_INTEGRITY_CONFLICT'),
                   ('security_challenge', True, 'SECURITY_CHALLENGE'))
        for field, value, error in changes:
            with self.subTest(field=field):
                inputs = copy.deepcopy(self.inputs)
                inputs['6857']['rows'][-1]['close'] = None
                inputs['6857']['rows'][-1][field] = value
                with self.assertRaisesRegex(ValueError, error):
                    prepare(inputs, self.sessions)

    def test_invalid_numeric_values_are_never_missing_or_zero_filled(self):
        for value in (0, -1, float('nan'), float('inf'), '100', True):
            with self.subTest(value=value):
                inputs = copy.deepcopy(self.inputs)
                inputs['6857']['rows'][-1]['close'] = value
                with self.assertRaisesRegex(ValueError, 'INVALID_PARTIAL_INPUT_NUMERIC'):
                    prepare(inputs, self.sessions)

    def test_post_freeze_news_for_absent_ticker_still_stops(self):
        news = {'6857': [dict(event_type='EARNINGS', event_time=FREEZE, available_at=TARGET + 'T09:00:00+09:00',
                              summary='Synthetic test news', source='fixture', materiality='MATERIAL')]}
        with self.assertRaisesRegex(ValueError, 'POST_FREEZE_NEWS'):
            prepare({}, self.sessions, news=news)

    def test_no_mutation_or_forecast_scoring_calls(self):
        before = copy.deepcopy(self.inputs)
        with patch.object(core, 'score', side_effect=AssertionError('score')), \
                patch.object(core, 'write_frozen', side_effect=AssertionError('forecast')), \
                patch.object(core, 'write_test_snapshot', side_effect=AssertionError('snapshot write')):
            result = prepare(self.inputs, self.sessions)
        self.assertEqual(self.inputs, before)
        view = p.inputs_for_stage(result, 'forecast_input')
        view['stocks'].clear()
        self.assertEqual(len(result['snapshot']['stocks']), 6)
        self.assertEqual(result['stages']['scoring']['status'], 'NOT_RUN')
        self.assertEqual(result['stages']['forecast_input']['status'], 'NOT_RUN')

    def test_legacy_strict_api_stays_strict(self):
        with self.assertRaisesRegex(ValueError, 'CORE6_REQUIRED'):
            core.compact_snapshot(TARGET, PREVIOUS, FREEZE, {})


if __name__ == '__main__':
    unittest.main()
