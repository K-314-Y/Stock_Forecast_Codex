import copy
import unittest

from engine import minimal_trial as core
from engine import parallel_data as data
from parallel_fixture import snapshot, samples, SOURCE_REGISTRY


class DataTests(unittest.TestCase):
    def setUp(self):
        self.snap = snapshot('2025-07-01')
        self.item = copy.deepcopy(self.snap['stocks']['6857']['feature_metadata']['ret_1d'])

    def validate(self, **changes):
        self.item.update(changes)
        return data.validate_numeric(self.item, 'ret_1d', self.snap['feature_end'], self.snap['cutoff'],
                                     self.snap['created_at'], SOURCE_REGISTRY)

    def test_numeric_pass_and_missing(self):
        self.assertEqual(self.validate(), 'PASS')
        self.assertEqual(self.validate(value=None), 'MISSING')

    def test_source_validation(self):
        with self.assertRaisesRegex(ValueError, 'SOURCE_NOT_VALIDATED'):
            self.validate(source='UNKNOWN_PROVIDER')

    def test_hash_required(self):
        with self.assertRaisesRegex(ValueError, 'SOURCE_HASH'):
            self.validate(source_hash='fake')

    def test_finite_anomaly(self):
        for v in (float('nan'), float('inf'), -2, True):
            with self.subTest(v=v), self.assertRaises(ValueError):
                self.validate(value=v)

    def test_currency_and_session(self):
        for change in ({'currency': 'USD'}, {'session': '2025-06-27'}):
            with self.assertRaises(ValueError):
                self.item = copy.deepcopy(self.snap['stocks']['6857']['feature_metadata']['ret_1d'])
                self.validate(**change)

    def test_no_future_available_at(self):
        with self.assertRaisesRegex(ValueError, 'PIT'):
            self.validate(available_at='2025-07-01T08:45:00+09:00')

    def test_no_naive_time(self):
        with self.assertRaisesRegex(ValueError, 'TIMEZONE'):
            self.validate(available_at='2025-06-30T16:00:00')

    def test_numeric_session_timestamp_consistency(self):
        with self.assertRaisesRegex(ValueError, 'SESSION_TIMESTAMP'):
            self.validate(timestamp='2025-06-29T15:30:00+09:00')

    def test_no_raw_provider_fields(self):
        with self.assertRaisesRegex(ValueError, 'ALLOWLIST'):
            self.validate(body='DO_NOT_STORE')

    def test_snapshot_valid_and_mutation_detected(self):
        data.validate_snapshot(self.snap)
        self.snap['stocks']['6857']['features']['MA20'] += 1
        with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
            data.validate_snapshot(self.snap)

    def test_rehashed_mutation_still_detected(self):
        self.snap['stocks']['6857']['features']['MA20'] += 1
        self.snap['common_snapshot_hash'] = data.digest({k: v for k, v in self.snap.items() if k != 'common_snapshot_hash'})
        with self.assertRaisesRegex(ValueError, 'DERIVED_VALUES'):
            data.validate_snapshot(self.snap)

    def test_candidate_not_automatic_model_input(self):
        for name in ('material_news_count', 'sox_overnight_return', 'unknown_embedding'):
            m = data.candidate_metadata(name, acquired=True, validated=True)
            self.assertFalse(m['model_input'])
            self.assertEqual(m['model_versions_allowed'], [])
        self.assertEqual(data.candidate_metadata('ret_1d', acquired=True, validated=True)['model_versions_allowed'],
                         [data.LOGISTIC, data.LIGHTGBM])

    def test_duplicate_numeric_candidates_before_dict_conversion(self):
        record = dict(ticker='6857', feature='ret_1d', data=self.item)
        result = data.validate_numeric_candidates([record], self.snap['feature_end'], self.snap['cutoff'],
                                                  self.snap['created_at'], SOURCE_REGISTRY)
        self.assertTrue(result[0]['model_input'])
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_NUMERIC'):
            data.validate_numeric_candidates([record, record], self.snap['feature_end'], self.snap['cutoff'],
                                              self.snap['created_at'], SOURCE_REGISTRY)

    def test_same_date_core6_grouped_expanding(self):
        source, target = samples(124)
        rows = data.training_rows(source, target, target + 'T08:05:00+09:00', synthetic=True)
        folds = data.expanding_folds(rows)
        self.assertEqual([len(t) // 6 for t, _ in folds], [64, 84, 104])
        self.assertEqual([len(v) // 6 for _, v in folds], [20, 20, 20])
        for train, valid in folds:
            train_days, valid_days = ({r['session'] for r in part} for part in (train, valid))
            self.assertFalse(train_days & valid_days)
            self.assertLess(max(train_days), min(valid_days))
            for day in valid_days:
                self.assertEqual({r['ticker'] for r in valid if r['session'] == day}, set(core.CORE))

    def test_target_future_rows_excluded_before_use(self):
        source, target = samples(5)
        source += [dict(snapshot=dict(target_session=target), outcomes='malformed_future')]
        rows = data.training_rows(source, target, target + 'T08:05:00+09:00', synthetic=True)
        self.assertEqual(len(rows), 30)
        self.assertTrue(all(r['session'] < target for r in rows))

    def test_duplicate_training_session(self):
        source, target = samples(5)
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            data.training_rows(source + [source[0]], target, target + 'T08:05:00+09:00', synthetic=True)

    def test_duplicate_ticker_grain_rejected(self):
        source, target = samples(5)
        rows = data.training_rows(source, target, target + 'T08:05:00+09:00', synthetic=True)
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            data.expanding_folds(rows + [rows[0]], minimum_train_sessions=1, validation_sessions=1, folds=1)

    def test_incomplete_date_group_rejected(self):
        source, target = samples(5)
        rows = data.training_rows(source, target, target + 'T08:05:00+09:00', synthetic=True)
        with self.assertRaisesRegex(ValueError, 'DATE_GROUP'):
            data.expanding_folds(rows[1:], minimum_train_sessions=1, validation_sessions=1, folds=1)

    def test_outcomes_not_available_rejected(self):
        source, target = samples(5)
        source[0]['outcomes']['6857']['available_at'] = target + 'T09:00:00+09:00'
        with self.assertRaisesRegex(ValueError, 'OUTCOME_NOT_AVAILABLE'):
            data.training_rows(source, target, target + 'T08:05:00+09:00', synthetic=True)

    def test_label_embargo_removes_entire_date(self):
        source, target = samples(9)
        rows = data.training_rows(source, target, target + 'T08:05:00+09:00', synthetic=True)
        rows[0]['outcome_available_at'] = rows[-1]['cutoff']
        train, _ = data.expanding_folds(rows, minimum_train_sessions=2, validation_sessions=2, folds=2)[0]
        self.assertNotIn(rows[0]['session'], {r['session'] for r in train})

    def test_insufficient_sessions_fail_closed(self):
        source, target = samples(99)
        rows = data.training_rows(source, target, target + 'T08:05:00+09:00', synthetic=True)
        with self.assertRaisesRegex(ValueError, 'INSUFFICIENT'):
            data.expanding_folds(rows)

    def test_synthetic_cannot_be_training_for_real_model(self):
        source, target = samples(3)
        with self.assertRaisesRegex(ValueError, 'DATASET_MODE'):
            data.training_rows(source, target, target + 'T08:05:00+09:00')

    def test_required_missing_and_threshold_lookahead_rejected(self):
        for field in ('missing', 'lookahead'):
            snap = copy.deepcopy(self.snap)
            if field == 'missing':
                snap['stocks']['6857']['feature_metadata']['ret_1d']['value'] = None
            else:
                snap['stocks']['6857']['threshold']['training_end_date'] = '2025-07-01'
            snap['common_snapshot_hash'] = data.digest({k: v for k, v in snap.items() if k != 'common_snapshot_hash'})
            with self.assertRaisesRegex(ValueError, 'REQUIRED_FEATURE_MISSING|THRESHOLD_TRAINING_LOOKAHEAD'):
                data.validate_snapshot(snap)

    def test_existing_task10b_one_use_input_to_common_snapshot(self):
        from tests.test_fresh_ephemeral import EphemeralFreshTests, PROJECT, TARGET
        from engine import execution_policy
        fixture = EphemeralFreshTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for name in ('parallel_forward_trial_v1.json', 'parallel_forecast_freeze_schema.json'):
            (fixture.root / 'config' / name).write_bytes((PROJECT / 'config' / name).read_bytes())
        for threshold in fixture.thresholds.values():
            threshold['status'] = 'TRIAL_CANDIDATE_NOT_ACTIVATED'
        path = fixture.root / 'config/trial_thresholds_candidate.json'
        import json
        bundle = json.loads(path.read_bytes())
        bundle['thresholds'] = fixture.thresholds
        fixture.write(path, bundle)
        token, _ = fixture.prepare()
        common = data.common_from_execution(token, execution_policy.SCOPE, TARGET)
        data.validate_snapshot(common)
        self.assertEqual(set(common['stocks']), set(core.CORE))
        self.assertEqual(common['feature_end'], '2026-10-09')
        self.assertTrue(set(fixture.hashes) <= set(common['source_hashes']))
        self.assertIn('ephemeral-derived-core6', common['source_hashes'])
        self.assertNotIn('company_overview', json.dumps(common))
        self.assertNotIn('DO_NOT_PERSIST_PROVIDER_BODY', json.dumps(common))
        with self.assertRaisesRegex(ValueError, 'NEW_EXECUTION_INPUTS_REQUIRED'):
            data.common_from_execution(token, execution_policy.SCOPE, TARGET)
