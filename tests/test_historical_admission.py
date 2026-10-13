"""Synthetic admission proofs and failure cases; no provider or local market fixtures."""
import copy
import csv
import io
import inspect
import math
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from engine import historical_admission as audit
from engine import historical_quality as quality
from engine import minimal_trial as core
from engine import parallel_training as training
from training_fixture import fixture


def prices(n=61):
    return [dict(session_date=(date(2010, 1, 1) + timedelta(days=i)).isoformat(),
                 open=100 + i*.013, close=100 + i*.013 + math.sin(i)*1.3,
                 high=105 + i*.013, low=95 + i*.013, volume=1000) for i in range(n)]


def scaled(rows, factors):
    return [dict(r, **{k: r[k]*f for k in ('open','high','low','close')}) for r,f in zip(rows,factors)]


def features(rows):
    return core.features(rows, dict(status='ENGINE_TEST_ONLY', threshold_version='short-oc-p33-v1',
        training_end_date=rows[-1]['session_date'], T_final=.01))


def outcome(source=audit.YAHOO):
    return dict(target_session='2020-01-06', ticker='6857', source=source,
        source_hash='a'*64, values=dict(open=100., high=112., low=98., close=110., volume=1000),
        quality_issues=[], source_refs=['synthetic-'+source], outcome_source_admitted=True,
        outcome_integrity=True, outcome_evidence={k:'PASS' for k in (
            'source_identity','field_semantics','same_basis','revision_safety','corporate_action')})


class ProofTests(unittest.TestCase):
    def test_one_proof_per_actual_price9(self):
        p=audit.feature_proofs()
        self.assertEqual([x['feature'] for x in p['features']], list(core.REQUIRED_FEATURES))
        self.assertEqual([x['feature'] for x in p['features'] if x['revision_invariant']=='NO'], ['MA20','MA60'])
        self.assertTrue(all(x['counterexample'] and x['invariance_condition'] and x['final_admission_status'] for x in p['features']))

    def test_synthetic_split_after_window_uniform(self):
        r=prices(); a=features(r); b=features(scaled(r,[.25]*61))
        for k in core.REQUIRED_FEATURES:
            if k not in ('MA20','MA60','MA_state'):
                self.assertAlmostEqual(a[k],b[k],places=12)
        self.assertEqual(a['MA_state'],b['MA_state'])
        for k in ('MA20','MA60'):
            self.assertAlmostEqual(a[k]*.25,b[k],places=12)
            self.assertNotEqual(a[k],b[k])

    def test_synthetic_split_before_window(self):
        r=prices(90); s=scaled(r,[.5]*20+[1.]*70)
        self.assertEqual(features(r[-61:]),features(s[-61:]))

    def test_split_inside_window_changes_supported_ratios_and_vol(self):
        r=prices(); a=features(r); b=features(scaled(r,[.5]*51+[1.]*10))
        for k in ('momentum_20d','momentum_60d','realized_vol_20d'):
            self.assertNotAlmostEqual(a[k],b[k],places=6)
        self.assertAlmostEqual(a['ret_1d'],b['ret_1d'])

    def test_nonuniform_ma_order_can_flip(self):
        r=prices(); r=[dict(v,close=100+i) for i,v in enumerate(r)]
        self.assertEqual(features(r)['MA_state'],'BULL')
        self.assertEqual(features(scaled(r,[1.]*41+[.25]*20))['MA_state'],'BEAR')

    def test_threshold_full_algorithm_uniform_factor(self):
        def limit(r):
            return quality.threshold_candidate(dict(ticker='6857',source_hash='a'*64,
                records=[dict(ticker='6857',session_date=v['session_date'],values={k:v[k] for k in ('open','high','low','close','volume')},issues=set()) for v in r]))
        r=prices(1100); a=limit(r); b=limit(scaled(r,[.25]*len(r)))
        for k in ('T_base','T_final','recent_volatility','normal_volatility','volatility_ratio'):
            self.assertAlmostEqual(a[k],b[k],places=12)
        c=limit(scaled(r,[.5]*1090+[1.]*10))
        self.assertNotAlmostEqual(a['T_final'],c['T_final'],places=6)

    def test_open_close_same_factor(self):
        for k in (.01,.5,2,5,100):
            self.assertAlmostEqual((k*110)/(k*100)-1,110/100-1,places=12)
        r=outcome();r['outcome_evidence'].update(open_revision_factor=.5,close_revision_factor=.5)
        self.assertTrue(audit.outcome_decision(r)['admitted'])

    def test_open_close_different_factor_rejected(self):
        self.assertNotAlmostEqual(110/(.5*100)-1,110/100-1)
        r=outcome();r['outcome_evidence'].update(open_revision_factor=.5,close_revision_factor=1)
        self.assertEqual(audit.outcome_decision(r)['status'],'FAIL')


class SourceTests(unittest.TestCase):
    def meta(self):
        return dict(symbol='6857.T',status='RETRIEVED',yfinance_version='1.7.0',
            metadata=dict(symbol='6857.T',currency='JPY',exchangeTimezoneName='Asia/Tokyo'),
            requested=dict(auto_adjust=False,repair=False,keepna=True,actions=True,interval='1d'))

    def test_schema_and_actual_parameter_semantics(self):
        d=audit.detect_yfinance_schema(audit.FIELDS,self.meta())
        self.assertEqual(d['schema'],'PASS');self.assertEqual(d['field_semantics'],'PASS')
        self.assertFalse(d['auto_adjust']);self.assertFalse(d['back_adjust']);self.assertFalse(d['native_unadjusted'])
        self.assertEqual(d['back_adjust_evidence'],'VERIFIED_1.7.0_DEFAULT')
        self.assertEqual(d['fields_used']['Adj Close'],[])
        self.assertEqual(d['fields_used']['Volume'],['QUALITY_ONLY'])

    def test_adjusted_backadjusted_and_unknown_version_fail_closed(self):
        for flag in ('auto_adjust','back_adjust','repair','prepost'):
            m=self.meta();m['requested'][flag]=True
            self.assertEqual(audit.detect_yfinance_schema(audit.FIELDS,m)['field_semantics'],'UNKNOWN')
        m=self.meta();m['yfinance_version']='UNKNOWN'
        self.assertIsNone(audit.detect_yfinance_schema(audit.FIELDS,m)['back_adjust'])
        for fields in (audit.FIELDS[:-1],audit.FIELDS+('Open',),audit.FIELDS+('unknown',)):
            self.assertEqual(audit.detect_yfinance_schema(fields,self.meta())['schema'],'FAIL')

    def test_adjustment_formulas_distinguish_back_adjust(self):
        # The official utility formula: auto adjusts C too; back_adjust does not.
        o,c,adj=100,110,55;factor=adj/c
        self.assertAlmostEqual(adj/(o*factor)-1,c/o-1)
        self.assertNotAlmostEqual(c/(o*factor)-1,c/o-1)

    def test_source_identity_unknown_never_admitted(self):
        r=outcome();r['outcome_evidence']['source_identity']='UNKNOWN'
        self.assertFalse(audit.outcome_decision(r)['admitted'])

    def test_jpx_preference_is_label_only(self):
        y=outcome();j=outcome('JPX_DAILY_REPORT')
        result,checks=audit.select_outcomes([y,j])
        self.assertEqual(result[0]['source'],'JPX_DAILY_REPORT')
        self.assertEqual(checks[0]['status'],'MATCH_WITHIN_TOLERANCE')
        self.assertEqual(y['source'],audit.YAHOO)

    def test_official_candidate_flag_does_not_admit(self):
        y=outcome();j=outcome('JPX_DAILY_REPORT')
        j['outcome_evidence']['corporate_action']='UNKNOWN'
        result,_=audit.select_outcomes([y,j])
        self.assertEqual(result[0]['source'],audit.YAHOO)
        self.assertFalse(audit.outcome_decision(j)['admitted'])

    def test_overlap_conflict_skip_no_average_no_fallback(self):
        y=outcome();j=outcome('JPX_DAILY_REPORT');j['values']['close']=111.
        result,checks=audit.select_outcomes([y,j]);r=result[0]
        self.assertEqual(checks[0]['status'],'OUTCOME_CONFLICT')
        self.assertEqual(r['values']['close'],111.);self.assertFalse(r['outcome_source_admitted'])
        self.assertIn('SKIPPED_OUTCOME_INTEGRITY',r['outcome_admission']['reasons'])

    def test_duplicate_source_stop(self):
        with self.assertRaisesRegex(ValueError,'DUPLICATE_OUTCOME_SOURCE'):
            audit.select_outcomes([outcome(),outcome()])

    def test_unresolved_actions_and_mufg_quarantine_excluded(self):
        r=outcome();r['outcome_evidence']['corporate_action']='UNKNOWN'
        self.assertEqual(audit.outcome_decision(r)['status'],'UNKNOWN')
        r=outcome();r.update(ticker='8306',target_session='2022-05-17')
        self.assertIn('SKIPPED_OUTCOME_INTEGRITY',audit.outcome_decision(r)['reasons'])

    def test_normalized_serialization_tolerance_without_repair(self):
        r=outcome()['values']; n=copy.deepcopy(r); n['close']+=1e-12
        self.assertEqual(len(audit.passthrough_deltas(r,n,1e-6)),1)
        self.assertEqual(r['close'],110.)
        n['close']+=1e-4
        with self.assertRaisesRegex(ValueError,'NORMALIZED_VALUE_CONFLICT'):
            audit.passthrough_deltas(r,n,1e-6)

    def test_provenance_deterministic_no_prices(self):
        g=dict(source='X',provider='X',file_artifact='synthetic.csv',fields=['Close','Open'],
            ticker='6857',sessions=['2020-01-07','2020-01-06'],raw_or_derived='LIBRARY_OUTPUT',
            adjustment_basis='UNKNOWN',source_hash='a'*64,existing_retrieved_at='2020-01-08T00:00:00+09:00',historical_vintage_known=False,role='CANDIDATE')
        a=audit.provenance_csv([g]);h=copy.deepcopy(g);h['sessions'].reverse();h['fields'].reverse()
        self.assertEqual(a,audit.provenance_csv([h]))
        rows=list(csv.DictReader(io.StringIO(a.decode())));self.assertEqual(len(rows),4)
        self.assertNotIn('value',rows[0])


class AdmissionTests(unittest.TestCase):
    def test_all_required_gates_and_unknown_precedence(self):
        g={k:'PASS' for k in audit.policy()['required_gates']}
        self.assertEqual(audit.classify(g),'ADMITTED_PIT')
        g['corporate_action']='NOT_APPLICABLE';self.assertEqual(audit.classify(g),'ADMITTED_PIT')
        g['revision_safety']='FAIL';self.assertEqual(audit.classify(g),'REJECTED_PIT')
        g['historical_availability']='UNKNOWN';self.assertEqual(audit.classify(g),'UNKNOWN_PIT')

    def test_real_dataset_requires_explicit_admission_even_with_fixture_times(self):
        h,f,d=copy.deepcopy(fixture(85))
        for r in h+f:r['synthetic']=False
        with patch.object(core,'sha256',return_value='a'*64):
            result=training.build_dataset(f,h,d['target_session'],d['training_as_of'],d['source_hashes'])
        self.assertEqual(len(result['eligible_rows']),0)
        self.assertEqual({r['admission_status'] for r in result['excluded_rows']},{'UNKNOWN_PIT'})

    def test_schedule_bound_supported_but_real_registry_empty(self):
        self.assertEqual(audit.policy()['source_schedule_rules'],{})
        rule=dict(source='TEST',evidence_sha256='a'*64,guaranteed_upper_bound=True,
            effective_start='2020-01-01',effective_end='2020-12-31',kind='CALENDAR_DAYS_AFTER_SESSION',days_after_session=1,latest_time_jst='23:59:59')
        evidence=dict(status='SOURCE_SCHEDULE_DERIVED',source='TEST',evidence_sha256='a'*64,rule_version='synthetic-v1')
        with patch.object(training,'policy',return_value=dict(source_schedule_rules={'synthetic-v1':rule})):
            self.assertEqual(training.availability(evidence,'TEST','2020-01-06')['available_at'],'2020-01-07T23:59:59+09:00')
        with self.assertRaisesRegex(ValueError,'NOT_PROVEN'):
            training.availability(evidence,'TEST','2020-01-06')

    def test_no_mtime_or_backdating(self):
        for kind in ('MTIME','GIT_COMMIT','CACHE_CREATION','BACKDATED_RETRIEVAL'):
            with self.assertRaisesRegex(ValueError,'TIME_KIND'):
                training.availability(dict(status='OBSERVED',source='X',evidence_sha256='a'*64,
                    time_kind=kind,available_at='2020-01-06T17:00:00+09:00'),'X','2020-01-06')
        for module in (audit,training):self.assertNotIn('.stat(',inspect.getsource(module))
        r=outcome();r.update(source_identity='PASS',feature_corporate_action='PASS',split_sessions=[])
        b=dict(available_at='2026-10-13T08:00:00+09:00')
        checks=audit.feature_checks([r]*61,list(range(61)),'2020-01-07',[b]*61)
        self.assertEqual(checks['historical_availability'],'UNKNOWN')

    def test_schedule_does_not_prove_absolute_ma_vintage(self):
        r=outcome();r.update(source_identity='PASS',feature_corporate_action='PASS',feature_integrity=True,split_sessions=[])
        b=dict(status='SOURCE_SCHEDULE_DERIVED',available_at='2020-01-06T17:00:00+09:00')
        checks=audit.feature_checks([r]*61,list(range(61)),'2020-01-07',[b]*61)
        self.assertEqual(checks['historical_availability'],'PASS')
        self.assertEqual(checks['revision_safety'],'UNKNOWN')
        b['status']='OBSERVED'
        self.assertEqual(audit.feature_checks([r]*61,list(range(61)),'2020-01-07',[b]*61)['revision_safety'],'PASS')

    def test_unvalidated_feature_integrity_not_reported_pass(self):
        r=outcome();r.update(source_identity='PASS',feature_corporate_action='UNKNOWN',feature_integrity=False,split_sessions=[])
        b=dict(status='OBSERVED',available_at='2020-01-06T17:00:00+09:00')
        checks=audit.feature_checks([r]*61,list(range(61)),'2020-01-07',[b]*61)
        self.assertEqual(checks['feature_integrity'],'UNKNOWN')

    def test_future_split_absolute_basis_fail(self):
        r=outcome();r.update(source_identity='PASS',feature_corporate_action='UNKNOWN',split_sessions=['2021-01-01'])
        checks=audit.feature_checks([r]*61,list(range(61)),'2020-01-07',[dict(available_at='2026-10-13T08:00:00+09:00')]*61)
        self.assertEqual(checks['revision_safety'],'FAIL')

    def test_artifact_hash_deterministic_and_provenance_sealed(self):
        _,_,d=copy.deepcopy(fixture(65))
        detail=dict(version=audit.VERSION,source_provenance_groups=[],feature_proofs=audit.feature_proofs(),
            source_provenance_csv_sha256=__import__('hashlib').sha256(audit.provenance_csv([])).hexdigest())
        d['admission_audit']=detail
        self.assertEqual(training.dataset_sha256(d),training.dataset_sha256(copy.deepcopy(d)))
        with tempfile.TemporaryDirectory() as root:
            dest=training.write_dataset(d,root)
            self.assertEqual(training.read_dataset(dest)['dataset_sha256'],training.dataset_sha256(d))
            (dest/'source_provenance.csv').write_text('tampered')
            with self.assertRaisesRegex(ValueError,'PROVENANCE_MATRIX_HASH'):
                training.read_dataset(dest)


if __name__=='__main__':unittest.main()
