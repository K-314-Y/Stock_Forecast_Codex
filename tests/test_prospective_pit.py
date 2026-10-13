"""Prospective grain, observed time and one-to-one outcome infrastructure."""
import copy
import json
import os
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch, Mock

from tests import test_fresh_ephemeral as fixtures
from engine import execution_policy as policy
from engine import forward_trial_inputs as inputs
from engine import fresh_bridge as fresh
from engine import prospective_pit as ledger
from engine.parallel_data import digest
from engine import minimal_trial as core
from scripts.check_prospective_pit import audit as independent_audit

TARGET,NOW=fixtures.TARGET,fixtures.NOW


class ProspectivePITTests(unittest.TestCase):
    setUp=fixtures.EphemeralFreshTests.setUp
    write=fixtures.EphemeralFreshTests.write

    def prepare(self):
        return inputs.prepare_execution_inputs(policy.SCOPE,TARGET,self.fetches,self.root/'ledger/pre_session')

    def pre(self):
        token,path=self.prepare()
        return token,path,core.read_frozen(path/'prospective.json')

    def outcomes(self):
        return {ticker:dict(Open=100,Close=101,corporate_action_status='NOT_APPLICABLE',
            source_identity=dict(provider='SYNTHETIC',source_tool='OFFLINE_FIXTURE',price_basis='SAME_SESSION_OPEN_CLOSE'),
            source_hash='b'*64) for ticker in core.CORE}

    def outcome(self,fetch=None):
        return ledger.record_outcomes(TARGET,fetch or Mock(return_value=self.outcomes()),store=self.root/'ledger')

    def test_observed_retrieval_and_derivation_times_with_no_backdating_parameter(self):
        def fetch():
            self.clock.return_value += timedelta(seconds=2)
            return self.responses['6857']
        self.fetches['6857'].side_effect=fetch
        token,path,pre=self.pre()
        row=pre['rows'][0]
        self.assertEqual(row['retrieved_at'],(NOW+timedelta(seconds=2)).isoformat())
        self.assertEqual(row['feature_available_at'],self.clock.return_value.isoformat())
        self.assertEqual(row['availability_status'],'OBSERVED')
        with self.assertRaises(TypeError):
            inputs.prepare_execution_inputs(policy.SCOPE,TARGET,self.fetches,retrieved_at=NOW.isoformat())

    def test_no_raw_content_and_allowlist_source_provenance(self):
        token,path,pre=self.pre()
        for row in pre['rows']:
            self.assertEqual(set(row),ledger.PRE_FIELDS)
            self.assertEqual(set(row['features']),set(core.REQUIRED_FEATURES))
            self.assertEqual(row['source_identity']['provider'],'BIGDATA')
            self.assertEqual(row['source_hashes']['ephemeral_response'],digest(self.responses[row['ticker']]))
        text=''.join(p.read_text() for p in path.iterdir())
        for forbidden in ('DO_NOT_PERSIST_PROVIDER_BODY','secret_body','structuredContent','"company_overview":','"close":','"price":','"volume":','previous_close'):
            self.assertNotIn(forbidden,text)

    def test_duplicate_target_ticker_prevention_before_second_provider_calls(self):
        self.pre()
        for call in self.fetches.values(): call.reset_mock()
        with self.assertRaises(FileExistsError): self.prepare()
        # No retry is permitted; detect occupied ledger before acquisition.
        for call in self.fetches.values(): call.assert_not_called()

    def test_duplicate_internal_grain_rejected_and_hash_deterministic(self):
        token,path,pre=self.pre()
        reconstructed=ledger.build_pre_session(token._snapshot,token._audit,
            {k:v for k,v in token._source_hashes.items() if k not in
             {str(path/n) for n in ('features.json','audit.json','prospective.json')}},'a'*40)
        self.assertEqual(pre,reconstructed)
        pre['rows'].append(copy.deepcopy(pre['rows'][0])); self.rehash(pre)
        with self.assertRaisesRegex(ValueError,'DUPLICATE_TARGET_TICKER'): ledger.validate_pre_session(pre)

    def rehash(self,value):
        value['ledger_sha256']=digest({k:v for k,v in value.items() if k!='ledger_sha256'})

    def test_backdated_postcutoff_availability_rejected(self):
        _,_,original=self.pre()
        for time in (TARGET+'T08:24:59+09:00',TARGET+'T08:45:00+09:00','2026-10-13T08:25:00+09:00'):
            pre=copy.deepcopy(original); pre['rows'][0]['feature_available_at']=time; self.rehash(pre)
            with self.assertRaisesRegex(ValueError,'PIT_TIME'): ledger.validate_pre_session(pre)

    def test_mtime_never_used(self):
        _,path,pre=self.pre()
        os.utime(path/'prospective.json',(0,0))
        with patch.object(Path,'stat',side_effect=AssertionError('mtime prohibited')):
            ledger.validate_pre_session(pre)
        self.assertEqual(core.read_frozen(path/'prospective.json')['rows'][0]['feature_available_at'],NOW.isoformat())

    def test_outcome_before_close_not_acquired(self):
        self.pre(); fetch=Mock(return_value=self.outcomes())
        with self.assertRaisesRegex(ValueError,'COMPLETED_SESSION'): self.outcome(fetch)
        fetch.assert_not_called()

    def test_outcomes_observed_completion_join_one_to_one_and_duplicate_reject(self):
        _,_,pre=self.pre()
        self.clock.return_value=NOW.replace(hour=16)
        fetch=Mock(return_value=self.outcomes())
        path=self.outcome(fetch)
        outcomes=core.read_frozen(path/'outcomes.json')
        self.assertEqual(len(outcomes['rows']),6)
        self.assertTrue(all(r['outcome_available_at']==self.clock.return_value.isoformat() for r in outcomes['rows']))
        joined=ledger.join_rows(pre,outcomes,(self.clock.return_value+timedelta(seconds=1)).isoformat())
        self.assertEqual(len(joined),6)
        self.assertTrue(all(r['outcome']['return_oc']==101/100-1 for r in joined))
        with self.assertRaises(FileExistsError): self.outcome(fetch)
        self.assertEqual(fetch.call_count,1)
        with self.assertRaisesRegex(ValueError,'NOT_AVAILABLE'): ledger.join_rows(pre,outcomes,self.clock.return_value.isoformat())

    def test_outcome_duplicate_missing_hash_time_ca_and_join_mismatch(self):
        _,_,pre=self.pre(); self.clock.return_value=NOW.replace(hour=16)
        path=self.outcome(); original=core.read_frozen(path/'outcomes.json')
        mutations=[lambda v:v['rows'].append(copy.deepcopy(v['rows'][0])),
                   lambda v:v['rows'].pop(),lambda v:v['rows'][0].update(ticker='9999'),
                   lambda v:v['rows'][0].update(outcome_available_at=TARGET+'T15:29:00+09:00'),
                   lambda v:v['rows'][0].update(source_hash='bad')]
        for mutate in mutations:
            v=copy.deepcopy(original); mutate(v); self.rehash(v)
            with self.assertRaises(ValueError): ledger.validate_outcomes(v,pre)
        v=copy.deepcopy(original); v['rows'][0]['corporate_action_status']='UNKNOWN'; self.rehash(v)
        with self.assertRaisesRegex(ValueError,'CORPORATE_ACTION'): ledger.join_rows(pre,v,TARGET+'T17:00:00+09:00')

    def test_outcome_callback_forbidden_timestamp_or_body_not_persisted(self):
        self.pre(); self.clock.return_value=NOW.replace(hour=16)
        for field in ('body','outcome_available_at'):
            values=self.outcomes(); values['6857'][field]='forbidden'
            with self.assertRaisesRegex(ValueError,'ALLOWLIST'): self.outcome(Mock(return_value=values))
            self.assertFalse((self.root/'ledger/outcomes'/TARGET).exists())

    def test_independent_data_analytics_grain_and_temporal_checks(self):
        _,path,pre=self.pre()
        evidence=independent_audit(path/'prospective.json')
        self.assertEqual(evidence['assessment'],'Ready within reviewed scope')
        self.assertEqual(evidence['rows'],6)
        self.assertEqual(evidence['duplicate_keys'],0)
        self.assertEqual(evidence['pit_time_errors'],0)
        self.assertEqual(evidence['restricted_content_fields'],0)


if __name__=='__main__': unittest.main()
