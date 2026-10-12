import copy
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from engine import historical_quality as quality
from engine import minimal_trial as core
from engine import parallel_training as training
from engine import parallel_training_local as local
from engine import parallel_models as models
from engine import parallel_data as data
from models.lightgbm import search_space
from training_fixture import fixture


class AvailabilityTests(unittest.TestCase):
    def test_unknown_has_no_timestamp(self):
        self.assertEqual(training.availability(dict(status='UNKNOWN', available_at='2020-01-01T00:00:00+09:00'),
                         'X', '2020-01-01')['available_at'], None)

    def test_mtime_git_path_not_evidence(self):
        for kind in ('MTIME', 'GIT_COMMIT', 'FILE_PATH', 'SESSION_CLOSE'):
            with self.assertRaisesRegex(ValueError, 'TIME_KIND'):
                training.availability(dict(status='OBSERVED', source='X', evidence_sha256='a' * 64,
                    time_kind=kind, available_at='2020-01-01T17:00:00+09:00'), 'X', '2020-01-01')
        self.assertNotIn('.stat(', inspect.getsource(training))

    def test_source_bound_and_aware_timestamp_required(self):
        evidence = dict(status='OBSERVED', source='X', evidence_sha256='a' * 64,
                        time_kind='RETRIEVAL', available_at='2020-01-01T17:00:00')
        with self.assertRaises(ValueError): training.availability(evidence, 'X', '2020-01-01')
        with self.assertRaises(ValueError): training.availability(evidence, 'Y', '2020-01-01')

    def test_schedule_reproducible_versioned_bound(self):
        rule = dict(source='X', evidence_sha256='a' * 64, guaranteed_upper_bound=True,
            effective_start='2020-01-01', effective_end='2020-12-31',
            kind='CALENDAR_DAYS_AFTER_SESSION', days_after_session=1, latest_time_jst='23:59:59')
        evidence = dict(status='SOURCE_SCHEDULE_DERIVED', source='X', evidence_sha256='a' * 64, rule_version='test-v1')
        with patch.object(training, 'policy', return_value={'source_schedule_rules': {'test-v1': rule}}):
            a = training.availability(evidence, 'X', '2020-03-02')
            self.assertEqual(a, training.availability(evidence, 'X', '2020-03-02'))
            self.assertEqual(a['available_at'], '2020-03-03T23:59:59+09:00')
            self.assertEqual(a['rule_version'], 'test-v1')
            with self.assertRaises(ValueError): training.availability(evidence, 'X', '2021-01-01')

    def test_expected_schedule_not_a_guaranteed_bound(self):
        self.assertEqual(training.policy()['source_schedule_rules'], {})
        with self.assertRaisesRegex(ValueError, 'NOT_PROVEN'):
            training.availability(dict(status='SOURCE_SCHEDULE_DERIVED', source='JPX',
                rule_version='expected-next-business-day', evidence_sha256='a' * 64), 'JPX', '2026-10-08')


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.history, cls.features, cls.dataset = fixture(85)

    def build(self, features=None, outcomes=None):
        d = self.dataset
        return training.build_dataset(self.features if features is None else features,
            self.history if outcomes is None else outcomes, d['target_session'], d['training_as_of'],
            d['source_hashes'], synthetic=True)

    def test_grain_and_one_to_one(self):
        audit = self.dataset['pit_audit']
        self.assertEqual(audit['matched_rows'], 85 * 6)
        self.assertEqual(audit['eligible_rows'], (85 - 61) * 6)
        self.assertEqual(audit['duplicates'], 0)
        self.assertEqual(audit['ticker_mismatches'], 0)
        self.assertEqual(audit['session_mismatches'], 0)
        self.assertTrue(all(len(r['features']) == 9 for r in self.dataset['eligible_rows']))

    def test_feature_uses_previous_61_sessions_only(self):
        r = self.dataset['eligible_rows'][0]
        self.assertEqual(len(r['input_sessions']), 61)
        self.assertLess(max(r['input_sessions']), r['target_session'])
        self.assertEqual(max(r['input_sessions']), r['feature_session'])
        self.assertEqual(r['features']['threshold'], .004)
        self.assertLess(r['feature_threshold_metadata']['training_end'], r['target_session'])

    def test_target_close_cannot_change_own_price_features(self):
        h = copy.deepcopy(self.history)
        day = self.dataset['eligible_rows'][0]['target_session']
        next(r for r in h if r['target_session'] == day and r['ticker'] == '6857')['values']['close'] *= 10
        features = training.replay_price_features(h, [r['target_session'] for r in h], self.dataset['target_session'], synthetic=True)
        before = next(f for f in self.features if f['target_session'] == day and f['ticker'] == '6857')
        after = next(f for f in features if f['target_session'] == day and f['ticker'] == '6857')
        self.assertEqual(before['features'], after['features'])

    def test_fold_label_threshold_cannot_overwrite_pre_target_input(self):
        row = self.dataset['eligible_rows'][0]
        thresholds = {t: dict(threshold_value=.02) for t in core.CORE}
        materialized = training._materialize([row], thresholds, 'CV_LABEL_THRESHOLD')[0]
        self.assertEqual(materialized['features']['threshold'], .004)
        self.assertEqual(materialized['threshold_used_for_label'], .02)
        self.assertEqual(materialized['feature_threshold_metadata'], row['feature_threshold_metadata'])

    def test_historical_threshold_input_cannot_use_target_day(self):
        h = copy.deepcopy(self.history)
        for r in h: r['synthetic_feature_threshold']['training_end'] = self.dataset['target_session']
        with self.assertRaisesRegex(ValueError, 'FEATURE_THRESHOLD_LOOKAHEAD'):
            training.replay_price_features(h, sorted({r['target_session'] for r in h}),
                                          self.dataset['target_session'], synthetic=True)

    def test_unknown_feature_excluded(self):
        h = copy.deepcopy(self.history)
        for r in h: r['availability'] = dict(status='UNKNOWN')
        f = training.replay_price_features(h, [r['target_session'] for r in h], self.dataset['target_session'], synthetic=True)
        d = self.build(features=f)
        self.assertEqual(d['pit_audit']['eligible_rows'], 0)
        self.assertEqual(d['pit_audit']['excluded_by_reason']['FEATURE_AVAILABILITY_UNKNOWN'], 85 * 6)

    def test_observed_current_vintage_is_not_backdated(self):
        h = copy.deepcopy(self.history)
        for r in h: r['availability']['available_at'] = self.dataset['training_as_of']
        f = training.replay_price_features(h, [r['target_session'] for r in h], self.dataset['target_session'], synthetic=True)
        d = self.build(features=f)
        self.assertEqual(d['pit_audit']['eligible_rows'], 0)
        self.assertGreater(d['pit_audit']['excluded_by_reason']['FEATURE_AFTER_CUTOFF'], 0)

    def test_duplicates_on_either_side_stop(self):
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_FEATURE'): self.build(features=self.features + self.features[:1])
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_OUTCOME'): self.build(outcomes=self.history + self.history[:1])

    def test_missing_outcome_skip_without_join_expansion(self):
        key = (self.dataset['eligible_rows'][0]['target_session'], '6857')
        h = [r for r in self.history if (r['target_session'], r['ticker']) != key]
        d = self.build(outcomes=h)
        self.assertEqual(d['pit_audit']['unmatched_feature_rows'], 1)
        self.assertEqual(d['pit_audit']['excluded_by_reason']['SKIPPED_MISSING_OUTCOME'], 1)

    def test_unknown_and_future_outcome_excluded(self):
        for evidence in (dict(status='UNKNOWN'), dict(status='OBSERVED', source='SYNTHETIC_NUMERICAL_FIXTURE',
                time_kind='RETRIEVAL', evidence_sha256='a' * 64, available_at='2099-01-01T00:00:00+09:00')):
            h = copy.deepcopy(self.history)
            for r in h: r['availability'] = evidence
            self.assertEqual(self.build(outcomes=h)['pit_audit']['eligible_rows'], 0)

    def test_outcome_not_before_close(self):
        h = copy.deepcopy(self.history)
        for r in h: r['availability']['available_at'] = r['target_session'] + 'T09:00:00+09:00'
        d = self.build(outcomes=h)
        self.assertEqual(d['pit_audit']['excluded_by_reason']['OUTCOME_AVAILABILITY_BEFORE_COMPLETED_SESSION'], 85 * 6)

    def test_corporate_action_unresolved_skip(self):
        h = copy.deepcopy(self.history)
        for r in h: r['outcome_integrity'] = False
        d = self.build(outcomes=h)
        self.assertEqual(d['pit_audit']['excluded_by_reason']['SKIPPED_OUTCOME_INTEGRITY'], 85 * 6)
        self.assertNotIn('target_close', d['excluded_rows'][-1])

    def test_future_row_excluded_before_reading_values(self):
        bad = dict(target_session=self.dataset['target_session'], ticker='6857')
        self.assertEqual(self.build(outcomes=self.history + [bad]), self.dataset)

    def test_deterministic_dataset_hash_and_no_overwrite(self):
        self.assertEqual(training.dataset_sha256(self.build()), training.dataset_sha256(self.dataset))
        with tempfile.TemporaryDirectory() as root:
            dest = training.write_dataset(self.dataset, root)
            self.assertEqual(training.read_dataset(dest)['dataset_sha256'], training.dataset_sha256(self.dataset))
            with self.assertRaises(FileExistsError): training.write_dataset(self.dataset, root)

    def test_synthetic_not_real_training_evidence(self):
        with self.assertRaisesRegex(ValueError, 'NOT_READY'): training.require_training_ready(self.dataset)
        with self.assertRaisesRegex(ValueError, 'NOT_READY'): training.final_training_rows(self.dataset)

    def test_no_legacy_real_training_bypass(self):
        with self.assertRaisesRegex(ValueError, 'PIT_TRAINING_DATASET_REQUIRED'):
            models.train_models([], '2026-10-13', '2026-10-13T07:00:00+09:00', 'a' * 40)

    def test_blocked_dataset_cannot_start_fit_or_model_freeze(self):
        with tempfile.TemporaryDirectory() as root:
            path = training.write_dataset(self.dataset, root)
            with patch.object(models, '_fit', side_effect=AssertionError('FIT_FORBIDDEN')):
                with self.assertRaisesRegex(ValueError, 'NOT_READY'):
                    models.train_pit_dataset(path, self.history, '2026-10-13T07:00:00+09:00', 'a' * 40)
            self.assertEqual(sorted(p.name for p in path.iterdir()), ['dataset.json', 'dataset.sha256'])

    def test_known_missing_session_is_not_bridged(self):
        h = copy.deepcopy(self.history)
        eligible = self.dataset['eligible_rows'][0]
        missing = eligible['input_sessions'][20]
        h = [r for r in h if r['target_session'] != missing]
        # Sessions are supplied independently of the price table.
        f = training.replay_price_features(h, sorted({r['target_session'] for r in self.history}),
                                          self.dataset['target_session'], synthetic=True)
        r = next(r for r in f if r['target_session'] == eligible['target_session'] and r['ticker'] == '6857')
        self.assertIn('SKIPPED_MISSING_FEATURE', r['feature_reasons'])
        self.assertIn(missing, r['input_sessions'])


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history, _, source = fixture()
        cls.dataset = copy.deepcopy(source)
        cls.folds = training.plan_folds(cls.dataset, cls.history)

    def test_two_meaningful_date_group_folds(self):
        self.assertEqual(len(self.folds), 2)
        for f in self.folds:
            train, valid = f['train'], f['validation']
            self.assertGreaterEqual(f['definition']['training_dates'], 60)
            self.assertEqual(f['definition']['validation_dates'], 20)
            self.assertLess(max(r['session'] for r in train), min(r['session'] for r in valid))
            self.assertFalse({r['session'] for r in train} & {r['session'] for r in valid})
            self.assertTrue(all(r['threshold_source'] == 'CV_LABEL_THRESHOLD' for r in train + valid))

    def test_validation_outcomes_do_not_enter_threshold(self):
        f = self.folds[0]['definition']
        h = copy.deepcopy(self.history)
        for r in h:
            if r['target_session'] >= f['validation_start']:
                r['values']['open'] = r['values']['close'] / 2
        replay = training.cv_threshold_replay(h, '6857', f['training_end'], f['threshold_fit_as_of'], f['fold_id'], synthetic=True)
        original = next(r for r in f['cv_threshold_replays'] if r['ticker'] == '6857')
        self.assertEqual(replay, original)
        self.assertEqual(replay['purpose'], 'CROSS_VALIDATION_ONLY')
        self.assertFalse(replay['production_candidate'])

    def test_data_hash_excludes_calculation_clock_but_binds_replay_boundary(self):
        original = training.dataset_sha256(self.dataset)
        d = copy.deepcopy(self.dataset)
        for r in d['cv_threshold_replays']: r['calculated_at'] = '2026-10-13T07:00:00+09:00'
        self.assertEqual(training.dataset_sha256(d), original)
        training.plan_folds(d, self.history)
        self.assertEqual(training.dataset_sha256(d), original)
        self.assertEqual(d['cv_threshold_replays'][0]['calculated_at'], '2026-10-13T07:00:00+09:00')
        d['cv_threshold_replays'][0]['replay_as_of'] = '2099-01-01T00:00:00+09:00'
        self.assertNotEqual(training.dataset_sha256(d), original)

    def test_same_existing_threshold_algorithm(self):
        f = self.folds[0]['definition']
        h = [r for r in self.history if r['ticker'] == '6857' and r['target_session'] <= f['training_end']]
        result = dict(ticker='6857', source_hash=data.digest(h), records=[dict(ticker='6857',
            session_date=r['target_session'], values=r['values'], issues=set()) for r in h])
        limit, _, _ = quality.training_candidates(result)
        replay = next(r for r in f['cv_threshold_replays'] if r['ticker'] == '6857')
        self.assertEqual(limit['T_final'], replay['threshold_value'])

    def test_production_threshold_and_spec_hash_unchanged(self):
        paths = [core.ROOT / 'config/threshold_spec.json', quality.POLICY_PATH,
                 core.ROOT / 'config/trial_thresholds_candidate.json']
        before = {str(p): core.sha256(p) for p in paths if p.exists()}
        f = self.folds[0]['definition']
        training.cv_threshold_replay(self.history, '6857', f['training_end'],
                                    f['threshold_fit_as_of'], f['fold_id'], synthetic=True)
        self.assertEqual(before, {name: core.sha256(name) for name in before})

    def test_embargo_removes_same_date_all_tickers(self):
        d = copy.deepcopy(self.dataset)
        cutoff = self.folds[0]['validation'][0]['cutoff']
        late = self.folds[0]['train'][0]['session']
        next(r for r in d['eligible_rows'] if r['target_session'] == late)['outcome_available_at'] = cutoff
        # Make this population candidate again; fold reconstruction must embargo.
        d['fold_definitions'] = []; d['cv_threshold_replays'] = []
        folds = training.plan_folds(d, self.history)
        self.assertTrue(all(r['session'] != late for r in folds[0]['train']))

    def test_bad_threshold_input_rejected_without_registration(self):
        f = self.folds[0]['definition']
        h = copy.deepcopy(self.history)
        next(r for r in h if r['ticker'] == '6857')['outcome_integrity'] = False
        with self.assertRaisesRegex(ValueError, 'NOT_PIT_SAFE'):
            training.cv_threshold_replay(h, '6857', f['training_end'], f['threshold_fit_as_of'], f['fold_id'], synthetic=True)

    def test_threshold_unknown_availability_stops(self):
        f = self.folds[0]['definition']
        h = copy.deepcopy(self.history); h[0]['availability'] = dict(status='UNKNOWN')
        with self.assertRaisesRegex(ValueError, 'NOT_PIT_SAFE'):
            training.cv_threshold_replay(h, '6857', f['training_end'], f['threshold_fit_as_of'], f['fold_id'], synthetic=True)

    def test_insufficient_pit_history_stop_without_fake_folds(self):
        h, _, d = fixture(180); d = copy.deepcopy(d)
        self.assertEqual(training.plan_folds(d, h), [])
        self.assertIn('BLOCKER_INSUFFICIENT_PIT_VALIDATION_HISTORY', d['blockers'])
        self.assertEqual(d['readiness'], 'BLOCKED')

    def test_120_not_training_prerequisite(self):
        from parallel_fixture import samples
        s, target = samples(100)
        rows = data.training_rows(s, target, target + 'T08:00:00+09:00', synthetic=True)
        self.assertEqual(len(data.expanding_folds(rows)), 2)
        self.assertEqual(training.policy()['strong_evaluation_target'], 120)

    def test_train_only_scaler_and_multiclass(self):
        train, valid = self.folds[0]['train'], self.folds[0]['validation']
        fitted = models._fit(data.LOGISTIC, train, data.rules()['logistic'])
        np.testing.assert_allclose(fitted.named_steps['scaler'].mean_, models.matrix(train).mean(axis=0))
        self.assertEqual(fitted.named_steps['scaler'].n_samples_seen_, len(train))
        for p in models.predict_probabilities(data.LOGISTIC, fitted, valid): data.probability_valid(p)

    def test_lightgbm_fold_only_fixed_space_and_reproducible(self):
        train, valid = self.folds[0]['train'], self.folds[0]['validation']
        self.assertEqual(len(search_space()), 24)
        from models import lightgbm
        with patch.object(lightgbm.lgb, 'Dataset', wraps=lightgbm.lgb.Dataset) as capture:
            a = models._fit(data.LIGHTGBM, train, search_space()[0])
            np.testing.assert_array_equal(capture.call_args.args[0], models.matrix(train))
        b = models._fit(data.LIGHTGBM, train, search_space()[0])
        self.assertEqual(models.predict_probabilities(data.LIGHTGBM, a, valid),
                         models.predict_probabilities(data.LIGHTGBM, b, valid))

    def test_synthetic_no_provider_jev_forecast_or_scoring(self):
        self.assertNotIn('jev', inspect.getsource(training))
        with patch('socket.socket', side_effect=AssertionError('NETWORK_FORBIDDEN')):
            h, f, d = fixture(70)
            result = training.build_dataset(f, h, d['target_session'], d['training_as_of'], d['source_hashes'], synthetic=True)
        self.assertEqual(result['pit_audit']['eligible_rows'], 54)


if __name__ == '__main__': unittest.main()
