import copy
import json
import tempfile
import unittest
from pathlib import Path

from engine import fresh_bridge as f
from engine import historical_quality as q
from engine import minimal_trial as e
from tests.test_minimal_trial_engine import fake_rows


TARGET = '2026-09-18'
PREVIOUS = '2026-09-17'
FREEZE = '2026-09-18T08:45:00+09:00'


def source(ticker='6857', day=PREVIOUS):
    return {'price': 112.0, 'volume': 100000,
            'timestamp': day + 'T06:30:00Z',
            'retrieved_at': '2026-09-18T08:30:00+09:00',
            'provider_id': 'FMP', 'exchange': 'JPX', 'currency': 'JPY'}


def setup():
    rows = fake_rows()[:-1]
    training = e.threshold(rows)
    calendar = {'jp_sessions': ['2026-09-16', PREVIOUS, TARGET]}
    obs = {ticker: f.normalize_observation(ticker, source(ticker), PREVIOUS, FREEZE)
           for ticker in e.CORE}
    histories = {ticker: {'records': [{'session_date': r['session_date'],
                                       'values': {key: r[key] for key in ('open', 'high', 'low', 'close', 'volume')},
                                       'issues': set()} for r in rows],
                          'source_hash': 'synthetic'} for ticker in e.CORE}
    bridge = {ticker: {PREVIOUS: dict(obs[ticker], quality_state='VALIDATED_BRIDGE_SEMANTICS')}
              for ticker in e.CORE}
    thresholds = {ticker: training for ticker in e.CORE}
    probs = {'UP': .34, 'FLAT': .33, 'DOWN': .33}
    tables = {ticker: {'base_rate': {'probabilities': probs},
                       'momentum': {state: {'probabilities': probs} for state in ('POSITIVE', 'NEGATIVE', 'FLAT')},
                       'ma_trend': {state: {'probabilities': probs} for state in ('BULL', 'BEAR', 'NEUTRAL')}}
              for ticker in e.CORE}
    return calendar, obs, histories, bridge, thresholds, tables


def snapshot(parts, **kwargs):
    return f.compact_live_snapshot(TARGET, *parts[:2], parts[2], parts[3], parts[4], parts[5],
                                   FREEZE, bridge_semantics_validated=True,
                                   cost_status='UNKNOWN', current_access_available=True,
                                   payment_or_upgrade_required=False, **kwargs)


class FreshBridgeTests(unittest.TestCase):
    def test_fresh_and_feature_without_atr(self):
        parts = setup()
        result = snapshot(parts)
        self.assertEqual(result['run_status'], 'COMPLETE')
        self.assertEqual(result['included_count'], 6)
        self.assertNotIn('ATR14_pct', result['stocks']['6857']['features'])
        self.assertNotIn('volume_ratio_20d', result['stocks']['6857']['features'])
        self.assertEqual(result['stocks']['6857']['features']['previous_session'], PREVIOUS)
        self.assertEqual(result['stocks']['6857']['provenance']['bridge_provider'], 'BIGDATA')
        self.assertEqual(result['news'], {ticker: [] for ticker in e.CORE})

    def test_stale_missing_conflict_only_skip_ticker(self):
        parts = list(setup())
        for state in ('STALE', 'MISSING', 'CONFLICT'):
            case = copy.deepcopy(parts)
            case[1]['8306']['freshness_state'] = state
            result = snapshot(case)
            self.assertEqual(result['run_status'], 'PARTIAL_1_SKIPPED')
            self.assertEqual(result['included_count'], 5)
            self.assertNotIn('8306', result['stocks'])
            self.assertEqual(result['ticker_status']['8306']['decision'], 'SKIP_FRESH_FEATURE')

    def test_normalize_fresh_stale_missing_conflict_unknown(self):
        self.assertEqual(f.normalize_observation('6857', source(), PREVIOUS, FREEZE)['freshness_state'], 'FRESH')
        self.assertEqual(f.normalize_observation('6857', source(day='2026-09-16'), PREVIOUS, FREEZE)['freshness_state'], 'STALE')
        self.assertEqual(f.normalize_observation('6857', None, PREVIOUS, FREEZE)['freshness_state'], 'MISSING')
        bad = dict(source(), exchange='NYSE')
        self.assertEqual(f.normalize_observation('6857', bad, PREVIOUS, FREEZE)['freshness_state'], 'CONFLICT')
        no_receipt = dict(source(), retrieved_at=None)
        self.assertEqual(f.normalize_observation('6857', no_receipt, PREVIOUS, FREEZE)['freshness_state'], 'UNKNOWN')

    def test_gap_and_quarantine_block_no_cache_mutation(self):
        parts = list(setup())
        original = copy.deepcopy(parts[2]['6857'])
        parts[3]['6857'].clear()
        result = snapshot(parts)
        self.assertNotIn('6857', result['stocks'])
        self.assertEqual(parts[2]['6857'], original)
        parts = list(setup())
        parts[2]['6857']['records'][-20]['issues'].add('CLOSE_BELOW_LOW')
        result = snapshot(parts)
        self.assertNotIn('6857', result['stocks'])

    def test_cost_and_semantics_gate(self):
        parts = setup()
        with self.assertRaisesRegex(ValueError, 'ADDITIONAL_COST_REQUIRED'):
            f.compact_live_snapshot(TARGET, *parts[:2], parts[2], parts[3], parts[4], parts[5],
                                    FREEZE, cost_status='ADDITIONAL_COST_REQUIRED',
                                    current_access_available=True, payment_or_upgrade_required=True)
        unknown = f.compact_live_snapshot(TARGET, *parts[:2], parts[2], parts[3], parts[4], parts[5],
                                          FREEZE, bridge_semantics_validated=True, cost_status='UNKNOWN',
                                          current_access_available=True, payment_or_upgrade_required=False)
        self.assertEqual(unknown['included_count'], 6)
        self.assertEqual(unknown['cost_status'], 'UNKNOWN')
        unproven = f.compact_live_snapshot(TARGET, *parts[:2], parts[2], parts[3], parts[4], parts[5],
                                           FREEZE, cost_status='UNKNOWN',
                                           current_access_available=True, payment_or_upgrade_required=False)
        self.assertEqual(unproven['included_count'], 0)

    def test_news_budget_and_market_pit(self):
        parts = setup()
        news = [{'event_type': 'EARNINGS', 'available_at': '2026-09-18T08:30:00+09:00'}] * 3
        result = snapshot(parts, news={'6857': news})
        self.assertEqual(len(result['news']['6857']), 3)
        four = snapshot(parts, news={'6857': news + news[:1]})
        self.assertNotIn('6857', four['stocks'])
        with self.assertRaisesRegex(ValueError, 'POST_FREEZE_MARKET_DATA'):
            snapshot(parts, market={'usdjpy_change': {'value': 1,
                     'available_at': '2026-09-18T09:00:00+09:00', 'source': 'test'}})

    def test_dry_run_exclusion_immutable(self):
        result = snapshot(setup())
        record = {'forecast_id': 'SYNTHETIC-6857-v1', 'ticker': '6857',
                  'run_type': 'LIVE_DRY_RUN_ONLY', 'exclude_from_trial_metrics': True,
                  'input_snapshot_hash': result['snapshot_hash']}
        with tempfile.TemporaryDirectory() as temp:
            path = f.write_dry_run_record(record, result, temp)
            self.assertTrue(path.is_file())
            with self.assertRaises(FileExistsError):
                f.write_dry_run_record(record, result, temp)
            with self.assertRaisesRegex(ValueError, 'DRY_RUN_RECORD_GATE'):
                f.write_dry_run_record(dict(record, exclude_from_trial_metrics=False), result, temp)
        parts = list(setup())
        parts[1]['8306']['freshness_state'] = 'STALE'
        skipped = snapshot(parts)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'DRY_RUN_RECORD_GATE'):
                f.write_dry_run_record(dict(record, ticker='8306',
                                                input_snapshot_hash=skipped['snapshot_hash']), skipped, temp)

    def test_candidate_training_frequency_reproduced(self):
        thresholds = json.loads((f.ROOT / 'config/trial_thresholds_candidate.json').read_text())['thresholds']
        candidates = json.loads((f.ROOT / 'config/trial_baselines_candidate.json').read_text())['baselines']
        for ticker in e.CORE:
            audit = q.audit(ticker)
            tables = f.training_state_tables(audit, thresholds[ticker], candidates[ticker])
            self.assertEqual(tables['base_rate']['sample_count'], audit['summary']['valid_rows'])
        bundle = json.loads((f.ROOT / 'config/trial_baseline_state_tables_candidate.json').read_text())
        self.assertFalse(bundle['automatic_reestimation'])
        self.assertEqual(set(bundle['tables']), set(e.CORE))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'candidate.json'
            f.write_state_table_bundle(path, thresholds, candidates)
            with self.assertRaises(FileExistsError):
                f.write_state_table_bundle(path, thresholds, candidates)

    def test_real_sample_not_admitted_and_official_date_guard(self):
        sample = json.loads((f.ROOT / 'data/fresh_bridge/jp/2026-09-25_validation_only.json').read_text())
        self.assertEqual(len(sample['samples']), 6)
        self.assertFalse(sample['forecast_input_eligible'])
        obs = {'session_date': '2026-09-25', 'close_bridge': 100, 'volume_bridge': 10}
        self.assertEqual(f.compare_official(obs, {'session_date': '2026-09-18', 'close': 100, 'volume': 10}), 'UNKNOWN')
        self.assertEqual(f.compare_official(obs, {'session_date': '2026-09-25', 'close': 100, 'volume': 10}), 'EXACT_MATCH')
        self.assertEqual(f.compare_official(obs, {'session_date': '2026-09-25', 'close': 100, 'volume': 99}), 'EXACT_MATCH')
        self.assertEqual(f.compare_official(obs, {'session_date': '2026-09-25', 'close': 101, 'volume': 10}), 'MISMATCH')

    def test_price_only_bridge(self):
        parts = list(setup())
        for ticker in e.CORE:
            parts[1][ticker]['volume_bridge'] = None
            parts[3][ticker][PREVIOUS]['volume_bridge'] = None
        self.assertEqual(snapshot(parts)['included_count'], 6)
        record = source()
        record['volume'] = None
        self.assertEqual(f.normalize_observation('6857', record, PREVIOUS, FREEZE)['freshness_state'], 'FRESH')


if __name__ == '__main__':
    unittest.main()
