import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta

from engine import late_research as r
from engine import minimal_trial as e


def fake_rows():
    days = []
    day = date(2026, 10, 2)
    while len(days) < 100:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    rows = []
    for i, current in enumerate(reversed(days)):
        opening = 100 + i * .1
        closing = opening * (1 + .002 * (i % 5 - 2))
        rows.append({'session_date': current.isoformat(), 'open': opening,
                     'high': max(opening, closing)+1, 'low': min(opening, closing)-1,
                     'close': closing, 'volume': 100000 + i * 100})
    return rows


def stocks():
    rows = fake_rows()
    training = [row for row in rows if row['session_date'] <= '2026-09-11']
    threshold = e.threshold(training)
    feature = e.features(rows, threshold)
    baseline = e.baselines(training, threshold)
    return {ticker: {'features': copy.deepcopy(feature), 'threshold': copy.deepcopy(threshold),
                     'baselines': copy.deepcopy(baseline),
                     'provenance': {'fixture': 'SYNTHETIC', 'evidence': [
                         {'source': 'SYNTHETIC_TEST_ONLY_ALL_INPUTS',
                          'available_at': '2026-10-05T08:30:00+09:00',
                          'retrieved_at': '2026-10-05T08:30:00+09:00'}]}}
            for ticker in e.CORE}


class LateResearchTests(unittest.TestCase):
    def test_late_snapshot_keeps_nominal_cutoff(self):
        snap = r.build_snapshot('2026-10-05', '2026-10-02',
                                '2026-10-05T14:29:27+09:00', stocks())
        self.assertEqual(snap['status'], r.STATUS)
        self.assertEqual(snap['nominal_freeze_time'], '2026-10-05T08:45:00+09:00')
        self.assertEqual(snap['actual_run_time'], '2026-10-05T14:29:27+09:00')
        self.assertEqual(snap['stocks']['6857']['threshold']['training_end_date'], '2026-09-11')
        self.assertNotIn('ATR14_pct', snap['stocks']['6857']['features'])
        self.assertNotIn('volume_ratio_20d', snap['stocks']['6857']['features'])
        self.assertFalse(snap['eligibility']['formal_forecast'])
        self.assertFalse(snap['eligibility']['forward_trial'])
        self.assertFalse(snap['eligibility']['score'])
        self.assertFalse(snap['eligibility']['prediction_log'])

    def test_post_cutoff_market_data_rejected(self):
        market = {'usdjpy_change': {'value': 1.0,
                                    'available_at': '2026-10-05T09:00:00+09:00',
                                    'source': 'fixture'}}
        with self.assertRaisesRegex(ValueError, 'INVALID_OR_POST_FREEZE_MARKET_VALUE'):
            r.build_snapshot('2026-10-05', '2026-10-02',
                             '2026-10-05T14:29:27+09:00', stocks(), market=market)

    def test_normal_path_required_before_cutoff(self):
        with self.assertRaisesRegex(ValueError, 'USE_NORMAL_PRE_0845_PATH'):
            r.build_snapshot('2026-10-05', '2026-10-02',
                             '2026-10-05T08:40:00+09:00', stocks())
        snap = e.compact_snapshot('2026-10-05', '2026-10-02',
                                  '2026-10-05T08:40:00+09:00', stocks())
        self.assertEqual(snap['status'], 'ENGINE_TEST_ONLY')

    def test_missing_or_unknown_stock_evidence_rejected(self):
        for evidence in (None, [], [{'source': 'fixture', 'available_at': None}]):
            inputs = stocks()
            inputs['6857']['provenance']['evidence'] = evidence
            with self.subTest(evidence=evidence), \
                    self.assertRaisesRegex(ValueError, 'MISSING_PRE_0845_STOCK_EVIDENCE'):
                r.build_snapshot('2026-10-05', '2026-10-02',
                                 '2026-10-05T14:29:27+09:00', inputs)

    def test_post_cutoff_stock_availability_and_receipt_rejected(self):
        for field in ('available_at', 'retrieved_at'):
            inputs = stocks()
            # One admissible dependency does not excuse a later dependency.
            late = dict(inputs['6857']['provenance']['evidence'][0])
            late[field] = '2026-10-05T09:00:00+09:00'
            inputs['6857']['provenance']['evidence'].append(late)
            with self.subTest(field=field), \
                    self.assertRaisesRegex(ValueError, 'POST_0845_STOCK_EVIDENCE_FORBIDDEN'):
                r.build_snapshot('2026-10-05', '2026-10-02',
                                 '2026-10-05T14:29:27+09:00', inputs)

    def test_oct5_existing_inputs_have_no_pre_cutoff_proof(self):
        # Read availability metadata only; no real price/probability generation.
        audit = json.loads((e.ROOT / 'analysis/automation/core6/core6_20261005T222824+0900/preflight.json').read_text())
        inputs = stocks()
        for ticker in e.CORE:
            evidence = []
            for check in audit['source_checks']:
                record = next(row for row in check['records'] if row['ticker'] == ticker)
                evidence.append({'source': check['normalized_path'],
                                 'available_at': record['available_at'],
                                 'retrieved_at': record['retrieved_at']})
            inputs[ticker]['provenance'] = {'evidence': evidence}
        with self.assertRaisesRegex(ValueError, 'MISSING_PRE_0845_STOCK_EVIDENCE'):
            r.build_snapshot('2026-10-05', '2026-10-02',
                             audit['actual_run_time'], inputs)

    def test_post_cutoff_news_rejected(self):
        event = {'event_type': 'EARNINGS', 'event_time': '2026-10-05T09:00:00+09:00',
                 'available_at': '2026-10-05T09:00:00+09:00', 'summary': 'synthetic',
                 'source': 'fixture', 'materiality': 'MATERIAL'}
        with self.assertRaisesRegex(ValueError, 'POST_FREEZE_NEWS'):
            r.build_snapshot('2026-10-05', '2026-10-02',
                             '2026-10-05T14:29:27+09:00', stocks(), news={'6857': [event]})

    def test_naive_stock_evidence_time_rejected(self):
        inputs = stocks()
        inputs['6857']['provenance']['evidence'][0]['available_at'] = '2026-10-05T08:30:00'
        with self.assertRaisesRegex(ValueError, 'NAIVE_AVAILABILITY_TIME'):
            r.build_snapshot('2026-10-05', '2026-10-02',
                             '2026-10-05T14:29:27+09:00', inputs)

    def test_record_is_non_scoreable_and_uses_actual_time(self):
        snap = r.build_snapshot('2026-10-05', '2026-10-02',
                                '2026-10-05T14:29:27+09:00', stocks())
        record = {
            'status': r.STATUS,
            'ticker': '6857',
            'target_session': '2026-10-05',
            'created_at': '2026-10-05T14:29:00+09:00',
            'nominal_freeze_time': snap['nominal_freeze_time'],
            'input_snapshot_hash': hashlib.sha256(e.canonical_bytes(snap)).hexdigest(),
            'UP': 60, 'FLAT': 25, 'DOWN': 15,
            'most_likely_class': 'UP', 'confidence': 'HIGH',
            'top_factors': [],
            'eligibility': snap['eligibility'],
        }
        self.assertTrue(r.validate_record(record, snap))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(r.write_record(record, snap, tmp).is_file())
            with self.assertRaisesRegex(ValueError, 'TEST_ONLY_CORE_FORECAST'):
                e.write_frozen(record, snap, tmp)
        with self.assertRaisesRegex(ValueError, 'FORECAST_OUTCOME_LINK_MISMATCH'):
            e.score(record, {'status': 'ENGINE_TEST_ONLY'}, {})
        bad = copy.deepcopy(record)
        bad['eligibility']['score'] = True
        with self.assertRaisesRegex(ValueError, 'RESEARCH_ELIGIBILITY_VIOLATION'):
            r.validate_record(bad, snap)


if __name__ == '__main__':
    unittest.main()
