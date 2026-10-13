"""Synthetic public-tree regressions for ephemeral Fresh execution inputs."""
import copy
import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from engine import execution_policy as policy
from engine import forward_trial_inputs as inputs
from engine import fresh_bridge as fresh
from engine import minimal_trial as core
from tests.test_minimal_trial_engine import fake_rows

PROJECT = core.ROOT
TARGET, PREVIOUS = '2026-10-14', '2026-10-13'
NOW = datetime.fromisoformat(TARGET + 'T08:25:00+09:00')


class EphemeralFreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        (self.root / 'analysis').mkdir()
        for name in ('threshold_spec.json', 'baseline_spec.json', 'historical_data_quality_policy.json',
                     'fresh_feature_bridge.json', 'forward_trial_execution_policy.json',
                     'minimal_forward_trial_config.json', 'primary_forward_trial_v1.json', 'official_gap_policy.json'):
            (self.root / 'config' / name).write_bytes((PROJECT / 'config' / name).read_bytes())
        root_patch = patch.object(core, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = patch.object(fresh, '_receipt_now', return_value=NOW)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        rows = fake_rows('2026-09-11')
        self.calendar = {'jp_sessions': [row['session_date'] for row in fake_rows(TARGET)
                                         if row['session_date'] not in ('2026-09-21', '2026-09-22',
                                                                       '2026-09-23', '2026-10-12')]}
        self.write(self.root / 'analysis/calendar_validation_2026-09-12.json', self.calendar)
        threshold = core.threshold(rows)
        self.thresholds = {ticker: dict(threshold, ticker=ticker) for ticker in core.CORE}
        common = dict(status='CANDIDATE_FROZEN_PRETRIAL_NOT_ACTIVATED', automatic_reestimation=False,
                      quality_policy_sha256=core.sha256(self.root / 'config/historical_data_quality_policy.json'),
                      training_end_date='2026-09-11')
        self.write(self.root / 'config/trial_thresholds_candidate.json',
                   dict(common, threshold_version=threshold['threshold_version'], thresholds=self.thresholds))
        rate = dict(probabilities={'UP': .34, 'FLAT': .33, 'DOWN': .33})
        self.write(self.root / 'config/trial_baselines_candidate.json',
                   dict(common, baseline_version='minimal-baselines-v1',
                        baselines={ticker: dict(base_rate=rate) for ticker in core.CORE}))
        self.write(self.root / 'config/trial_baseline_state_tables_candidate.json',
                   dict(common, baseline_version='minimal-baselines-v1', threshold_version=threshold['threshold_version'],
                        tables={ticker: dict(base_rate=rate,
                            momentum={state: rate for state in ('POSITIVE', 'NEGATIVE', 'FLAT')},
                            ma_trend={state: rate for state in ('BULL', 'BEAR', 'NEUTRAL')})
                            for ticker in core.CORE}))
        historical = [dict(session_date=r['session_date'], close=r['close']) for r in rows[-61:]]
        historical += [dict(session_date=day, close=120 + index / 10)
                       for index, day in enumerate(self.calendar['jp_sessions'])
                       if '2026-09-11' < day <= '2026-10-09']
        self.history = {ticker: copy.deepcopy(historical) for ticker in core.CORE}
        source = self.root / 'history-preparation.json'
        self.write(source, dict(stocks={ticker: historical + [dict(session_date=PREVIOUS, close=88888)]
                                       for ticker in core.CORE}))
        self.originals = []
        for day in ('2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08'):
            path = self.root / 'data/outcome_parsed' / (day + '.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            self.write(path, {'synthetic_original': day})
            self.originals.append(path)
        self.hashes = {str(path): core.sha256(path) for path in self.originals}
        refs = self.root / 'source-hashes.json'
        self.write(refs, self.hashes)
        preparation = self.root / 'preparation.json'
        self.write(preparation, dict(history_through='2026-10-09', point_in_time_validation='PASS',
                                    available_tickers=list(core.CORE),
                                    validated_close_history_reference=dict(path=str(source), sha256=core.sha256(source)),
                                    source_hash_reference=dict(path=str(refs), sha256=core.sha256(refs))))
        rules_path = self.root / 'config/forward_trial_execution_policy.json'
        rules = json.loads(rules_path.read_bytes())
        rules['approved_preparation_sha256'] = core.sha256(preparation)
        rules['historical_through'] = '2026-10-09'
        rules['calendar_sha256'] = core.sha256(self.root / rules['calendar'])
        self.write(rules_path, rules)
        from engine import forward_storage as storage
        code = patch.object(storage, 'code_commit', return_value='a' * 40)
        code.start()
        self.addCleanup(code.stop)
        primary_path = self.root / 'config/primary_forward_trial_v1.json'
        primary = json.loads(primary_path.read_bytes())
        review_path = self.root / primary['execution_review']['path']
        review_path.parent.mkdir(parents=True, exist_ok=True)
        self.write(review_path, dict(schema_version='forecast-execution-review-v1', scope=policy.SCOPE,
            terms_status=policy.TERMS, fresh_strategy='EPHEMERAL_RETRIEVAL', raw_provider_persistence='DISABLED',
            ready_for_task10c=True, remaining_blockers=[], remaining_issue_counts={'BLOCKER':0,'HIGH':0,'MEDIUM':1},
            formal_forecast_enabled=False, schedules_enabled=False))
        primary['execution_review']['sha256'] = core.sha256(review_path)
        primary['frozen_contract_hashes'] = {p: core.sha256(self.root / p) for p in primary['frozen_contract_hashes']}
        self.write(primary_path, primary)
        self.registered = inputs.register_validated_history(preparation)
        self.responses = {}
        self.fetches = {}
        for index, ticker in enumerate(core.CORE):
            response = dict(structuredContent=dict(company_overview=dict(
                isin=fresh.CORE_ISINS[ticker], price=123.456789 + index,
                timestamp=PREVIOUS + 'T15:30:00+09:00', currency='JPY', exchange='JPX',
                source=dict(provider_id='FMP'), volume=999999, secret_body='DO_NOT_PERSIST_PROVIDER_BODY')))
            self.responses[ticker] = response
            self.fetches[ticker] = Mock(return_value=response)

    def write(self, path, value):
        path.write_bytes(core.canonical_bytes(value))

    def prepare(self, **kwargs):
        return inputs.prepare_execution_inputs(policy.SCOPE, TARGET, self.fetches, self.root / 'derived', **kwargs)

    def test_A_B_capture_and_derivation_never_persist_raw_response_or_close(self):
        before = {str(p) for p in self.root.rglob('*') if p.is_file()}
        ticket = fresh.capture_fresh_close('6857', PREVIOUS, self.fetches['6857'])
        self.assertEqual(before, {str(p) for p in self.root.rglob('*') if p.is_file()})
        self.assertNotIn('123.456789', repr(ticket))
        _, destination = self.prepare()
        created = [p for p in self.root.rglob('*') if p.is_file() and str(p) not in before]
        self.assertEqual({p.name for p in created}, {'features.json', 'features.sha256', 'audit.json', 'audit.sha256', 'prospective.json', 'prospective.sha256', '.lock'})
        for path in created:
            text = path.read_text()
            for forbidden in ('"company_overview":', 'structuredContent', 'previous_close', 'secret_body',
                              'DO_NOT_PERSIST_PROVIDER_BODY', '"volume":999999', '123.456789'):
                self.assertNotIn(forbidden, text)
        self.assertFalse(fresh._receipt_root().exists())

    def test_C_D_E_derived_features_and_non_content_audit(self):
        token, destination = self.prepare()
        snapshot = core.read_frozen(destination / 'features.json')
        audit = core.read_frozen(destination / 'audit.json')
        for ticker in core.CORE:
            self.fetches[ticker].assert_called_once_with()
            close = self.responses[ticker]['structuredContent']['company_overview']['price']
            expected = core.features(self.history[ticker] + [dict(session_date=PREVIOUS, close=close)], self.thresholds[ticker])
            self.assertEqual(snapshot['stocks'][ticker], {name: expected[name] for name in core.REQUIRED_FEATURES})
            datum = audit['stocks'][ticker]
            self.assertEqual(set(datum), {'ticker', 'target_session', 'previous_session', 'provider', 'underlying_provider',
                                         'retrieved_at', 'provider_timestamp_present', 'session_validation',
                                         'currency_validation', 'PIT_validation', 'feature_snapshot_sha256', 'source_response_sha256', 'feature_available_at'})
            self.assertEqual(datum['retrieved_at'], NOW.isoformat())
            self.assertEqual(datum['feature_snapshot_sha256'], core.sha256(destination / 'features.json'))
        self.assertEqual(inputs.consume_execution_inputs(token, policy.SCOPE, TARGET), (snapshot, audit))

    def test_F_historical_inputs_unchanged_and_10_09_excluded(self):
        loaded, _ = inputs.load_validated_history(self.calendar, self.thresholds, NOW)
        self.assertEqual(loaded, self.history)
        self.assertEqual({str(path): core.sha256(path) for path in self.originals}, self.hashes)
        self.assertNotIn('88888', self.registered.read_text())
        self.prepare()
        self.assertEqual({str(path): core.sha256(path) for path in self.originals}, self.hashes)

    def test_G_old_raw_and_stored_snapshots_cannot_be_execution_inputs(self):
        with self.assertRaisesRegex(ValueError, 'NOT_ELIGIBLE_FOR_FORECAST_REUSE'):
            fresh.consume_fresh_close({'path': 'legacy/source.json', 'receipt': 'legacy/receipt.json'},
                                      '6857', PREVIOUS, NOW, NOW)
        _, destination = self.prepare()
        with self.assertRaisesRegex(ValueError, 'NEW_EXECUTION_INPUTS_REQUIRED'):
            inputs.consume_execution_inputs(core.read_frozen(destination / 'features.json'), policy.SCOPE, TARGET)

    def test_H_new_capabilities_are_one_use_and_execution_bound(self):
        ticket = fresh.capture_fresh_close('6857', PREVIOUS, self.fetches['6857'])
        later = datetime.fromisoformat(TARGET + 'T08:26:00+09:00')
        with self.assertRaisesRegex(ValueError, 'REUSED_FRESH_INPUT'):
            fresh.consume_fresh_close(ticket, '6857', PREVIOUS, later, later)
        token, _ = self.prepare()
        inputs.consume_execution_inputs(token, policy.SCOPE, TARGET)
        with self.assertRaisesRegex(ValueError, 'NEW_EXECUTION_INPUTS_REQUIRED'):
            inputs.consume_execution_inputs(token, policy.SCOPE, TARGET)

    def test_I_J_K_prohibited_scope_schedule_formal_make_zero_calls(self):
        for scope, kwargs in [('COMMERCIAL', {}), (policy.SCOPE, {'scheduled': True}),
                              (policy.SCOPE, {'formal': True})]:
            with self.subTest(scope=scope, kwargs=kwargs), self.assertRaisesRegex(ValueError, 'GATE_CLOSED'):
                inputs.prepare_execution_inputs(scope, TARGET, self.fetches, self.root / 'derived', **kwargs)
        for call in self.fetches.values():
            call.assert_not_called()

    def test_off_target_day_and_cutoff_fail_before_retrieval(self):
        for stamp in ('2026-10-12T08:25:00+09:00', TARGET + 'T08:45:00+09:00'):
            self.clock.return_value = datetime.fromisoformat(stamp)
            with self.assertRaisesRegex(ValueError, 'ACTUAL_TARGET_DAY_PRE_CUTOFF_REQUIRED|GATE_CLOSED'):
                self.prepare()
        for call in self.fetches.values():
            call.assert_not_called()

    def test_invalid_identity_session_currency_timestamp_and_payload_rejected_without_writes(self):
        cases = [('isin', fresh.CORE_ISINS['7203']), ('currency', 'USD'),
                 ('timestamp', '2026-10-08T15:30:00+09:00'), ('timestamp', PREVIOUS + 'T09:00:00+09:00'),
                 ('timestamp', None), ('price', float('nan')), ('price', True), ('exchange', 'NYSE')]
        for field, value in cases:
            response = copy.deepcopy(self.responses['6857'])
            response['structuredContent']['company_overview'][field] = value
            self.fetches['6857'].return_value = response
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                self.prepare()
            self.assertFalse((self.root / 'derived' / TARGET).exists())

    def test_provider_failure_does_not_publish_or_serialize_error_body(self):
        self.fetches['8306'].side_effect = RuntimeError('connector failure')
        with self.assertRaisesRegex(RuntimeError, 'connector failure'):
            self.prepare()
        self.assertFalse((self.root / 'derived' / TARGET).exists())

    def test_mutated_history_or_source_is_rejected_before_provider_call(self):
        self.originals[0].write_text('changed')
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED'):
            self.prepare()
        for call in self.fetches.values():
            call.assert_not_called()

    def test_input_tampering_before_consumption_and_clock_rollback_rejected(self):
        token, _ = self.prepare()
        token._snapshot['stocks']['6857']['ret_1d'] = 1
        with self.assertRaisesRegex(ValueError, 'EXECUTION_INPUT_CHANGED'):
            inputs.consume_execution_inputs(token, policy.SCOPE, TARGET)
        with patch.object(fresh, '_receipt_now', side_effect=[NOW,
                datetime.fromisoformat(TARGET + 'T08:24:00+09:00')]):
            with self.assertRaisesRegex(ValueError, 'ACQUISITION_CLOCK_INVALID'):
                fresh.capture_fresh_close('6857', PREVIOUS, self.fetches['6857'])

    def test_create_only_and_cutoff_after_last_call(self):
        self.prepare()
        with self.assertRaises(FileExistsError):
            self.prepare()
        # Independent fresh store; last provider response arrives too late.
        self.fetches['3197'].side_effect = lambda: setattr(self.clock, 'return_value',
            datetime.fromisoformat(TARGET + 'T08:45:00+09:00')) or self.responses['3197']
        with self.assertRaisesRegex(ValueError, 'POST_GENERATION'):
            inputs.prepare_execution_inputs(policy.SCOPE, TARGET, self.fetches, self.root / 'late')
        self.assertFalse((self.root / 'late' / TARGET).exists())

    def test_non_callable_stored_response_and_incomplete_core6_rejected(self):
        for fetches in ({'6857': self.fetches['6857']}, self.responses):
            with self.assertRaisesRegex(ValueError, 'NEW_FRESH_RETRIEVAL_REQUIRED'):
                inputs.prepare_execution_inputs(policy.SCOPE, TARGET, fetches, self.root / 'derived')


if __name__ == '__main__':
    unittest.main()
