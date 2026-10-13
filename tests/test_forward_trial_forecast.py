"""Synthetic real-entry tests in temporary roots, never actual provider data."""
import copy
import json
import multiprocessing
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_fresh_ephemeral as fixtures
from engine import minimal_trial as core
from engine import execution_policy as policy
from engine import forward_trial_inputs as inputs
from engine import forward_trial_forecast as runner
from engine import forward_storage as storage
from engine import prospective_pit as ledger
from engine import primary_readiness as readiness
from engine import fresh_bridge as fresh
from models.linear_pool import forward_predict, decision

TARGET, NOW = fixtures.TARGET, fixtures.NOW


class PrimaryRunnerTests(unittest.TestCase):
    write = fixtures.EphemeralFreshTests.write
    prepare = fixtures.EphemeralFreshTests.prepare

    def setUp(self):
        fixtures.EphemeralFreshTests.setUp(self)
        self.store = self.root / 'forecasts'
        self.runtime = self.root / 'state/runtime'
        self.runtime.mkdir(parents=True)
        for name in runner.RUNTIME_NAMES:
            self.write(self.runtime / name, dict(forecast_generated=False, forecast_count=7,
                formal_forecast_enabled=False, schedules_enabled=False, scoring_run=False, gates={}))
        self.before = self.runtime_bytes()

    def runtime_bytes(self):
        return {n: (self.runtime / n).read_bytes() for n in runner.RUNTIME_NAMES}

    def run_token(self, token):
        return runner.run_forward(token, policy.SCOPE, TARGET, store=self.store, runtime_dir=self.runtime)

    def test_equal_pool_exact_weights_and_residual_ties(self):
        baselines = dict(base_rate={'UP': .9, 'FLAT': .05, 'DOWN': .05},
                         momentum={'UP': .3, 'FLAT': .6, 'DOWN': .1},
                         ma_trend={'UP': .6, 'FLAT': .15, 'DOWN': .25})
        raw, percent = forward_predict(baselines)
        self.assertEqual(raw, {'UP': .6, 'FLAT': 4 / 15, 'DOWN': 2 / 15})
        self.assertEqual(percent, {'UP': 60, 'FLAT': 25, 'DOWN': 15})
        for values, expected in (([1/3]*3, [35,35,30]), ([.475,.475,.05], [45,50,5]),
                                  ([.367,.333,.3], [35,35,30])):
            p = dict(zip(core.CLASSES, values))
            _, out = forward_predict({n:p for n in ('base_rate','momentum','ma_trend')})
            self.assertEqual(list(out.values()), expected)
            self.assertEqual(sum(out.values()), 100)
            self.assertTrue(all(v % 5 == 0 and 5 <= v <= 95 for v in out.values()))
        with self.assertRaisesRegex(ValueError, 'BOUNDS'):
            forward_predict({n: {'UP':1,'FLAT':0,'DOWN':0} for n in ('base_rate','momentum','ma_trend')})

    def test_forecast_class_and_confidence_boundaries(self):
        for p, expected in (([60,35,5],('UP','HIGH')), ([55,40,5],('UP','MEDIUM')),
                             ([45,45,10],('FLAT','MEDIUM')), ([40,40,20],('FLAT','LOW')),
                             ([45,10,45],('UP','MEDIUM')), ([10,45,45],('FLAT','MEDIUM'))):
            self.assertEqual(decision(dict(zip(core.CLASSES,p))), expected)

    def test_confidence_config_conflict_stops_before_provider(self):
        p = self.root / 'config/minimal_forward_trial_config.json'
        value = json.loads(p.read_bytes()); value['forecast']['confidence_rule'] = 'different'
        self.write(p, value)
        with self.assertRaisesRegex(ValueError, 'RULE_CONFLICT'):
            self.prepare()
        for call in self.fetches.values(): call.assert_not_called()

    def test_primary_freeze_receipt_then_runtime(self):
        token, _ = self.prepare()
        real_replace = storage.replace_json
        def checked(path, obj):
            self.assertTrue((self.store / token._bundle_id / 'receipt.json').exists())
            return real_replace(path, obj)
        with patch.object(storage, 'replace_json', side_effect=checked):
            path = self.run_token(token)
        receipt, transaction = runner._read_bundle(path)
        self.assertEqual(len(receipt['forecast_ids']), 6)
        self.assertEqual(receipt['model_id'], 'equal-linear-pool-v1')
        self.assertEqual(receipt['feature_snapshot_hash'], token._snapshot_hash)
        for name in runner.RUNTIME_NAMES:
            state = json.loads((self.runtime / name).read_bytes())
            self.assertEqual(state['forecast_count'], 13)
            self.assertTrue(state['forecast_generated'] and state['forecast_frozen'])
            self.assertFalse(state['formal_forecast_enabled'] or state['schedules_enabled'] or state['scoring_run'])
        for fid in receipt['forecast_ids']:
            r = core.read_frozen(path / (fid + '.json'))
            self.assertEqual(r['percent_probabilities'], {'UP':35,'FLAT':35,'DOWN':30})
            self.assertEqual((r['predicted_class'],r['confidence']), ('FLAT','LOW'))
            self.assertEqual(r['relative_strength_reason'], 'RULE_NOT_FROZEN')
        before = self.runtime_bytes()
        runner.recover_runtime(store=self.store, runtime_dir=self.runtime)
        self.assertEqual(self.runtime_bytes(),before)

    def test_persisted_and_manually_constructed_inputs_rejected(self):
        token, path = self.prepare()
        saved = core.read_frozen(path / 'features.json')
        forged = inputs._ExecutionInputs(token._snapshot,token._audit,token._started,token._source_hashes)
        for value in (saved, path / 'features.json', forged):
            with self.assertRaisesRegex(ValueError, 'NEW_EXECUTION_INPUTS'):
                self.run_token(value)
        self.assertEqual(self.runtime_bytes(),self.before)

    def test_consumed_input_rejected(self):
        token, _ = self.prepare()
        inputs.consume_execution_inputs(token,policy.SCOPE,TARGET)
        with self.assertRaisesRegex(ValueError,'NEW_EXECUTION_INPUTS'):
            self.run_token(token)

    def test_modified_snapshot_audit_binding_clock_or_hash_registry_rejected(self):
        for field in ('snapshot','audit','started','source_hashes','bundle_id'):
            token, _ = inputs.prepare_execution_inputs(policy.SCOPE,TARGET,self.fetches,self.root / field)
            if field == 'snapshot':
                token._snapshot['stocks']['6857']['ret_1d'] = .9
                token._snapshot_hash = inputs._digest(token._snapshot)
            elif field == 'audit':
                token._audit['stocks']['6857']['source_response_sha256'] = '0'*64
                token._audit_hash = inputs._digest(token._audit)
            elif field == 'started': token._started = NOW.replace(minute=20)
            elif field == 'source_hashes': token._source_hashes.clear()
            else: token._bundle_id = ledger.new_bundle_id(TARGET)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,'CHANGED_AFTER_DERIVATION'):
                self.run_token(token)
        self.assertEqual(self.runtime_bytes(),self.before)

    def test_wrong_scope_session_and_late_clock_rejected(self):
        token, _ = self.prepare()
        for scope, target in (('COMMERCIAL',TARGET),(policy.SCOPE,'2026-10-13'),(policy.SCOPE,'2026-10-15')):
            with self.assertRaises(ValueError):
                runner.run_forward(token,scope,target,store=self.store,runtime_dir=self.runtime)
        self.clock.return_value = NOW.replace(hour=8,minute=45)
        with self.assertRaises(ValueError): self.run_token(token)
        self.assertEqual(self.runtime_bytes(),self.before)

    def test_changed_source_and_frozen_identity_rejected(self):
        token, _ = self.prepare()
        self.originals[0].write_text('tampered')
        with self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):
            self.run_token(token)
        self.assertEqual(self.runtime_bytes(),self.before)

    def test_consumed_binding_is_independent_of_mutable_token(self):
        token,_ = self.prepare()
        actual = inputs.consume_execution_inputs
        def consume(*args,**kwargs):
            result = actual(*args,**kwargs)
            token._source_hashes.clear()
            token._snapshot['stocks'].clear()
            token._bundle_id = 'modified'
            return result
        with patch.object(inputs,'consume_execution_inputs',side_effect=consume):
            path = self.run_token(token)
        self.assertEqual(len(runner._read_bundle(path)[0]['forecast_ids']),6)

    def test_source_tamper_during_staging_prevents_final_freeze(self):
        token,_=self.prepare(); actual=storage.seal
        def tamper(path,obj):
            actual(path,obj)
            if path.name=='receipt.json': self.originals[0].write_text('changed during staging')
        with patch.object(storage,'seal',side_effect=tamper), self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):
            self.run_token(token)
        self.assertFalse((self.store/token._bundle_id).exists())
        self.assertEqual(self.runtime_bytes(),self.before)

    def test_all_missing_and_partial_integrity_failure_do_not_create_forecasts(self):
        self.fetches={t:None for t in core.CORE}
        with self.assertRaisesRegex(ValueError,'NO_PROCESSABLE_TICKERS'): self.prepare()
        self.assertFalse(self.store.exists())
        self.fetches={t:fixtures.Mock(return_value=self.responses[t]) for t in core.CORE}
        self.fetches['3197']=None
        self.responses['6857']['structuredContent']['company_overview']['price']=float('nan')
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.store.exists())
        self.assertEqual(self.runtime_bytes(),self.before)

    def test_duplicate_ids_and_target_create_only(self):
        with patch.object(ledger.uuid,'uuid4') as uuid:
            uuid.return_value.hex = '1'*32
            token,_ = self.prepare(); path=self.run_token(token)
            before = {p.name:p.read_bytes() for p in path.iterdir()}
            token,_ = inputs.prepare_execution_inputs(policy.SCOPE,TARGET,self.fetches,self.root / 'second')
            with self.assertRaisesRegex(FileExistsError,'DUPLICATE_FORECAST_ID'):
                self.run_token(token)
            self.assertEqual(before,{p.name:p.read_bytes() for p in path.iterdir()})
        token,_ = inputs.prepare_execution_inputs(policy.SCOPE,TARGET,self.fetches,self.root / 'third')
        with self.assertRaisesRegex(FileExistsError,'TARGET_SESSION_ALREADY_FROZEN'):
            self.run_token(token)

    def test_partial_missing_input_proceeds_errors_never_become_skip(self):
        self.fetches['3197'] = None
        token,_ = self.prepare(); path=self.run_token(token)
        receipt,_=runner._read_bundle(path)
        self.assertEqual(len(receipt['forecast_ids']),5)
        self.assertEqual(receipt['ticker_statuses']['3197']['status'],'SKIPPED_MISSING_INPUT')
        self.assertFalse(any('_3197_' in f for f in receipt['forecast_ids']))

    def test_partial_write_failure_has_no_final_or_runtime_update(self):
        token,_=self.prepare()
        actual=storage.seal
        def failing(path,obj):
            actual(path,obj)
            if path.name == 'prospective.json': raise OSError('injected write failure')
        with patch.object(storage,'seal',side_effect=failing), self.assertRaises(OSError):
            self.run_token(token)
        self.assertEqual(self.runtime_bytes(),self.before)
        self.assertFalse((self.store/token._bundle_id).exists())
        self.assertFalse(list(self.store.glob('.pending-*')))

    def test_crash_before_rename_orphan_is_ignored_runtime_unchanged(self):
        token,_=self.prepare()
        def child():
            with patch.object(storage.os,'rename',side_effect=lambda *a: os._exit(71)):
                self.run_token(token)
        process=multiprocessing.get_context('fork').Process(target=child)
        process.start(); process.join(10)
        self.assertEqual(process.exitcode,71)
        self.assertTrue(list(self.store.glob('.pending-*')))
        self.assertFalse((self.store/token._bundle_id).exists())
        runner.recover_runtime(store=self.store,runtime_dir=self.runtime)
        self.assertEqual(self.runtime_bytes(),self.before)
        self.assertFalse((self.runtime/'primary_projection.json').exists())

    def test_cutoff_during_staging_does_not_commit_or_update_runtime(self):
        token,_=self.prepare(); actual=storage.seal
        def late(path,obj):
            actual(path,obj)
            if path.name=='receipt.json': self.clock.return_value=NOW.replace(minute=45)
        with patch.object(storage,'seal',side_effect=late), self.assertRaises(ValueError):
            self.run_token(token)
        self.assertFalse((self.store/token._bundle_id).exists())
        self.assertEqual(self.runtime_bytes(),self.before)

    def test_post_freeze_runtime_crash_recovers_without_double_count(self):
        token,_=self.prepare(); actual=storage.replace_json
        for fail_at in (0,2):
            # Each scenario starts with the same before-image and fresh temp store.
            store=self.root/('crash'+str(fail_at)); count=[0]
            for n in runner.RUNTIME_NAMES: (self.runtime/n).write_bytes(self.before[n])
            (self.runtime/'primary_projection.json').unlink(missing_ok=True)
            def fail(path,obj):
                if count[0]==fail_at: raise OSError('runtime interrupted')
                count[0]+=1; return actual(path,obj)
            token,_=inputs.prepare_execution_inputs(policy.SCOPE,TARGET,self.fetches,self.root/('input'+str(fail_at)))
            with patch.object(storage,'replace_json',side_effect=fail), self.assertRaisesRegex(OSError,'runtime interrupted'):
                runner.run_forward(token,policy.SCOPE,TARGET,store=store,runtime_dir=self.runtime)
            self.assertTrue((store/token._bundle_id/'receipt.json').exists())
            runner.recover_runtime(store=store,runtime_dir=self.runtime)
            for n in runner.RUNTIME_NAMES: self.assertEqual(json.loads((self.runtime/n).read_bytes())['forecast_count'],13)
            after=self.runtime_bytes(); runner.recover_runtime(store=store,runtime_dir=self.runtime)
            self.assertEqual(after,self.runtime_bytes())

    def test_historical_ml_blocker_is_not_primary_dependency(self):
        with patch('engine.parallel_models.train_models', side_effect=AssertionError('NO_ML')):
            self.assertEqual(readiness.assess_primary()['readiness'],'READY')
            token,_=self.prepare(); self.run_token(token)
        shadow=readiness.shadow_readiness()
        self.assertEqual(shadow['readiness'],'TRAINING_BLOCKED')
        self.assertEqual(set(shadow['models'].values()),{'TRAINING_BLOCKED'})
        self.assertFalse(readiness.assess_primary()['historical_ml_dependency'])

    def test_missing_future_historical_extension_blocks_before_fetch(self):
        with self.assertRaisesRegex(ValueError,'VALIDATED_HISTORICAL_EXTENSION_MISSING:2026-10-13'):
            inputs.load_execution_history(self.calendar,self.thresholds,NOW,'2026-10-14')
        for call in self.fetches.values(): call.assert_not_called()


if __name__=='__main__': unittest.main()
