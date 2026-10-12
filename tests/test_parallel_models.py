import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from engine import minimal_trial as core
from engine import parallel_data as data
from engine import parallel_models as models
from engine import parallel_runner as runner
from models.linear_pool import equal_pool, round_primary, predict
from models.lightgbm import search_space
from parallel_fixture import samples, snapshot


class PrimaryTests(unittest.TestCase):
    def test_exact_equal_weights(self):
        baselines = dict(base_rate={'UP': .9, 'FLAT': .05, 'DOWN': .05},
                         momentum={'UP': .3, 'FLAT': .6, 'DOWN': .1},
                         ma_trend={'UP': .6, 'FLAT': .15, 'DOWN': .25})
        self.assertEqual(equal_pool(baselines), {'UP': .6, 'FLAT': 4 / 15, 'DOWN': 2 / 15})

    def test_tie_order_flat_up_down(self):
        p = {'UP': 1 / 3, 'FLAT': 1 / 3, 'DOWN': 1 / 3}
        self.assertEqual(round_primary(p), {'UP': 35, 'FLAT': 35, 'DOWN': 30})
        self.assertEqual(round_primary({'UP': .475, 'FLAT': .475, 'DOWN': .05}),
                         {'UP': 45, 'FLAT': 50, 'DOWN': 5})

    def test_remainders_and_bounds(self):
        self.assertEqual(round_primary({'UP': .367, 'FLAT': .333, 'DOWN': .3}),
                         {'UP': 35, 'FLAT': 35, 'DOWN': 30})
        self.assertEqual(round_primary({'UP': 1, 'FLAT': 0, 'DOWN': 0}),
                         {'UP': 90, 'FLAT': 5, 'DOWN': 5})

    def test_sum100_5pp_bounds_deterministic_exhaustive(self):
        for u in range(101):
            for f in range(101 - u):
                p = {'UP': u / 100, 'FLAT': f / 100, 'DOWN': (100 - u - f) / 100}
                result = round_primary(p)
                self.assertEqual(sum(result.values()), 100)
                self.assertTrue(all(5 <= v <= 95 and v % 5 == 0 for v in result.values()))
                self.assertEqual(result, round_primary(dict(reversed(list(p.items())))))

    def test_news_or_shadow_output_cannot_enter_primary(self):
        baselines = snapshot('2025-07-01')['stocks']['6857']['baselines']
        with self.assertRaisesRegex(ValueError, 'THREE_FROZEN'):
            equal_pool(dict(baselines, news={'UP': 1, 'FLAT': 0, 'DOWN': 0}))

    def test_brier_independent_hand_calculation(self):
        m = models.offline_metrics(['UP'], [{'UP': .5, 'FLAT': .25, 'DOWN': .25}])
        self.assertEqual(m['brier'], .375)
        self.assertAlmostEqual(m['log_loss'], np.log(2))

    def test_log_loss_zero_no_hidden_clip(self):
        m = models.offline_metrics(['UP'], [{'UP': 0, 'FLAT': 1, 'DOWN': 0}])
        self.assertEqual(m['brier'], 2)
        self.assertIsNone(m['log_loss'])


class ShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source, cls.target = samples()
        cls.created_at = cls.target + 'T08:05:00+09:00'
        cls.packs, cls.summary = models.train_models(cls.source, cls.target, cls.created_at,
                                                    'a' * 40, synthetic=True)
        cls.rows = data.training_rows(cls.source, cls.target, cls.created_at, synthetic=True)

    def test_three_models_only_narrow_input_versions(self):
        self.assertEqual(set(data.MODEL_ROLES), {data.PRIMARY, data.LOGISTIC, data.LIGHTGBM})
        self.assertEqual(set(self.packs), {data.LOGISTIC, data.LIGHTGBM})
        for p in self.packs.values():
            self.assertEqual(p['metadata']['features'], list(data.PRICE9))
            self.assertEqual(p['metadata']['training_dataset_hash'], self.summary['dataset_hash'])

    def test_fold_scaler_train_only(self):
        train, valid = data.expanding_folds(self.rows)[0]
        estimator = models._fit(data.LOGISTIC, train, data.rules()['logistic'])
        scaler = estimator.named_steps['scaler']
        np.testing.assert_allclose(scaler.mean_, models.matrix(train).mean(axis=0))
        self.assertFalse(np.array_equal(scaler.mean_, models.matrix(train + valid).mean(axis=0)))
        self.assertEqual(scaler.n_samples_seen_, len(train))

    def test_final_scaler_only_training_period(self):
        scaler = self.packs[data.LOGISTIC]['estimator'].named_steps['scaler']
        np.testing.assert_allclose(scaler.mean_, models.matrix(self.rows).mean(axis=0))
        self.assertEqual(scaler.n_samples_seen_, 744)

    def test_logistic_multinomial_probabilities_sum1(self):
        model = self.packs[data.LOGISTIC]['estimator']
        self.assertEqual(model.named_steps['classifier'].solver, 'lbfgs')
        self.assertEqual(list(model.classes_), [0, 1, 2])
        probabilities = models.predict_probabilities(data.LOGISTIC, model, self.rows[:12])
        self.assertTrue(all(abs(sum(p.values()) - 1) < 1e-12 and min(p.values()) >= 0 for p in probabilities))

    def test_lightgbm_small_frozen_search(self):
        self.assertEqual(len(search_space()), 24)
        self.assertTrue(all(p['num_leaves'] <= 2 ** p['max_depth'] for p in search_space()))
        from models.lightgbm import fit
        with self.assertRaisesRegex(ValueError, 'NOT_FROZEN'):
            fit(models.matrix(self.rows), models.labels(self.rows), dict(num_leaves=100))

    def test_lightgbm_probabilities_sum1(self):
        probabilities = models.predict_probabilities(data.LIGHTGBM, self.packs[data.LIGHTGBM]['estimator'], self.rows[:12])
        self.assertTrue(all(abs(sum(p.values()) - 1) < 1e-12 for p in probabilities))

    def test_deterministic_seed_and_reproducibility(self):
        for m, pack in self.packs.items():
            meta = pack['metadata']
            self.assertEqual(meta['seed'], 314)
            repeated = models._fit(m, self.rows, meta['hyperparameters'])
            self.assertEqual(models.predict_probabilities(m, pack['estimator'], self.rows[:12]),
                             models.predict_probabilities(m, repeated, self.rows[:12]))

    def test_walkforward_brier_selection_first(self):
        meta = self.packs[data.LIGHTGBM]['metadata']
        s = meta['walk_forward_summary']
        scores = [d['metrics']['brier'] for d in s['candidates']]
        self.assertEqual(s['selected_candidate'], min(range(len(scores)), key=lambda i: scores[i]))
        for pack in self.packs.values():
            for c in pack['metadata']['walk_forward_summary']['candidates']:
                self.assertEqual([f['validation_sessions'] for f in c['folds']], [20, 20, 20])
                self.assertTrue(all(f['training_end'] < f['validation_start'] for f in c['folds']))

    def test_no_future_data_all_training_end_before_target(self):
        for p in self.packs.values():
            self.assertLess(p['metadata']['training_end'], self.target)
            models.validate_model_metadata(p['metadata'])

    def test_real_model_freeze_requires_pit_training_provenance(self):
        meta = copy.deepcopy(self.packs[data.LOGISTIC]['metadata'])
        meta['synthetic'] = False
        with self.assertRaisesRegex(ValueError, 'PIT_TRAINING_PROVENANCE_REQUIRED'):
            models.validate_model_metadata(meta)

    def test_missing_training_class_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ALL_THREE'):
            models._fit(data.LOGISTIC, [dict(r, label='UP') for r in self.rows], data.rules()['logistic'])

    def test_model_freeze_immutable_and_hash_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            for m, pack in self.packs.items():
                path = models.freeze_model(pack, Path(directory) / m)
                loaded = models.load_model_freeze(path)
                self.assertEqual(loaded['metadata'], pack['metadata'])
                self.assertEqual(models.predict_probabilities(m, loaded['estimator'], self.rows[:6]),
                                 models.predict_probabilities(m, pack['estimator'], self.rows[:6]))
                with self.assertRaises(FileExistsError):
                    models.freeze_model(pack, path)
                artifact = path / ('estimator.joblib' if m == data.LOGISTIC else 'estimator.txt')
                with artifact.open('ab') as f:
                    f.write(b'TAMPERED')
                with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
                    models.load_model_freeze(path)

    def test_model_estimator_cannot_disagree_with_freeze_metadata(self):
        pack = copy.deepcopy(self.packs[data.LOGISTIC])
        pack['estimator'].named_steps['classifier'].set_params(C=100)
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'ESTIMATOR_METADATA'):
            models.freeze_model(pack, Path(directory) / 'mismatch')

    def bundle(self, directory):
        refs = {m: models.freeze_model(pack, Path(directory) / m) for m, pack in self.packs.items()}
        return runner.build_probe_bundle(snapshot(self.target, 124), refs,
                                         self.target + 'T08:10:00+09:00', self.target + 'T08:20:00+09:00')

    def test_all_models_same_snapshot_roles_and_same_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self.bundle(directory)
            self.assertEqual({r['common_snapshot_hash'] for r in bundle['records']},
                             {bundle['snapshot']['common_snapshot_hash']})
            self.assertEqual(len(bundle['records']), 18)
            for r in bundle['records']:
                self.assertEqual(r['model_role'], data.MODEL_ROLES[r['model_id']])
                self.assertEqual(r['freeze_time'], bundle['manifest']['freeze_time'])
                self.assertEqual(r['relative_strength'], {'status': 'missing_optional', 'reason': 'RULE_NOT_FROZEN'})

    def test_forecast_probe_immutable_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self.bundle(directory)
            path = runner.freeze_probe_bundle(bundle, Path(directory) / 'bundle')
            self.assertEqual(runner.read_probe_bundle(path), bundle)
            with self.assertRaises(FileExistsError):
                runner.freeze_probe_bundle(bundle, path)

    def test_role_or_snapshot_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self.bundle(directory)
            bundle['records'][6]['model_role'] = 'PRIMARY'
            with self.assertRaisesRegex(ValueError, 'ROLE_OR_FREEZE'):
                runner.validate_bundle(bundle)

    def test_comparison_same_outcome_six_series_no_scoring(self):
        with tempfile.TemporaryDirectory() as directory:
            records = runner.comparison_rows(self.bundle(directory))
            self.assertEqual(len(records), 36)
            for ticker in core.CORE:
                r = [r for r in records if r['ticker'] == ticker]
                self.assertEqual(len({tuple(a['outcome_join_key']) for a in r}), 1)
                self.assertEqual({a['series'] for a in r}, set(data.MODEL_ROLES) | {'base_rate', 'momentum', 'ma_trend'})
                self.assertTrue(all('score' not in a and a['outcome_status'] == 'PENDING_DATA' for a in r))

    def test_freeze_schema_rejects_raw_extra_fields(self):
        from jsonschema import ValidationError
        with tempfile.TemporaryDirectory() as directory:
            bundle = self.bundle(directory)
            bundle['manifest']['provider_response'] = 'MUST_NOT_PERSIST'
            with self.assertRaises(ValidationError):
                runner.validate_bundle(bundle)

    def test_live_forecast_always_disabled_before_acquisition(self):
        with patch('engine.parallel_runner.common_from_execution') as consume, self.assertRaisesRegex(ValueError, 'LIVE_FORECAST_DISABLED'):
            runner.run_forward(target_session='2026-10-13', provider='must_not_call')
        consume.assert_not_called()

    def test_execution_activation_does_not_change_frozen_specification(self):
        current = data.rules()
        expected = data.specification_hash()
        future = dict(current, task_mode='FORWARD_EXECUTION', live_generation_enabled=True)
        with patch('engine.parallel_data.rules', return_value=future):
            self.assertEqual(data.specification_hash(), expected)

    def test_one_execution_flag_cannot_enable_live_runner(self):
        config = dict(data.rules(), live_generation_enabled=True)
        with patch('engine.parallel_runner.rules', return_value=config), self.assertRaisesRegex(ValueError, 'LIVE_FORECAST_DISABLED'):
            runner.run_forward()

    def test_no_synthetic_10_13_forecast(self):
        with self.assertRaisesRegex(ValueError, 'NON_TARGET_PROBE'):
            runner.build_probe_bundle(snapshot('2026-10-13'), {}, '2026-10-13T08:10:00+09:00',
                                      '2026-10-13T08:20:00+09:00')
