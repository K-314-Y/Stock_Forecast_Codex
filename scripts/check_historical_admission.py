"""Independent standard-library recomputation. No engine imports or network."""
import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

from scripts.check_parallel_training_dataset import audit as base_audit


def check(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit(destination, root):
    destination, root = Path(destination), Path(root)
    result = base_audit(destination, root)
    d = json.loads((destination/'dataset.json').read_bytes())
    detail = d['admission_audit']
    rules = json.loads((root/'config/historical_pit_admission_v1.json').read_bytes())
    quality = json.loads((root/'config/historical_data_quality_policy.json').read_bytes())
    core = tuple(d['pit_audit']['per_ticker_coverage'])
    check(len(core) == 6, 'CORE6_GRAIN')
    all_rows = d['eligible_rows'] + d['excluded_rows']
    counts = Counter()
    for r in all_rows:
        gates = r['admission_checks']
        check(set(gates) == set(rules['required_gates']), 'GATE_COVERAGE')
        expected = ('UNKNOWN_PIT' if 'UNKNOWN' in gates.values() else 'REJECTED_PIT' if 'FAIL' in gates.values() else 'ADMITTED_PIT')
        check(expected == r['admission_status'], 'ROW_CLASSIFICATION')
        counts[expected] += 1
        if expected != 'ADMITTED_PIT':
            check('target_open' not in r and 'target_close' not in r, 'EXCLUDED_RAW_VALUE_COPY')
        else:
            check(all(v in ('PASS', 'NOT_APPLICABLE') for v in gates.values()), 'ADMITTED_UNKNOWN')
    check(dict(counts) == {k:v for k,v in d['pit_audit']['admission_counts'].items() if v}, 'ADMISSION_COUNTS')
    check(counts['ADMITTED_PIT'] == len(d['eligible_rows']), 'ELIGIBLE_COUNT')
    for ticker in core:
        check(d['pit_audit']['per_ticker_coverage'][ticker] == {s:sum(r['ticker']==ticker and r['admission_status']==s for r in all_rows)
            for s in ('ADMITTED_PIT','UNKNOWN_PIT','REJECTED_PIT')}, 'TICKER_COVERAGE')
    matrix = destination/'source_provenance.csv'
    check(sha(matrix) == detail['source_provenance_csv_sha256'], 'MATRIX_HASH')
    matrix_keys, matrix_rows = set(), 0
    with matrix.open(newline='') as f:
        for r in csv.DictReader(f):
            key = tuple(r[k] for k in ('file_artifact','ticker','session','field'))
            check(key not in matrix_keys, 'DUPLICATE_PROVENANCE_GRAIN')
            matrix_keys.add(key); matrix_rows += 1
            check(d['source_hashes'][r['file_artifact']] == r['source_hash'], 'MATRIX_SOURCE_HASH')
            check(r['historical_vintage_known'] == 'False', 'UNSUPPORTED_HISTORICAL_VINTAGE')
    check(matrix_rows == sum(len(g['sessions'])*len(g['fields']) for g in detail['source_provenance_groups']), 'MATRIX_ROW_COUNT')
    raw = {}; adjustment_checks=[]; actions=[]; invalid=[]; nonvalidated_basis=0
    for ticker in core:
        cache = root/'analysis/provider_test_results/yfinance_cache_2026-09-13'
        with (cache/(ticker+'_auto_adjust_false.csv')).open(newline='') as f: a=list(csv.DictReader(f))
        with (cache/(ticker+'_auto_adjust_true_local.csv')).open(newline='') as f: b=list(csv.DictReader(f))
        check(len(a)==len(b), 'AUTO_ADJUST_GRAIN')
        meta=json.loads((cache/(ticker+'_probe.json')).read_bytes())
        check(meta['requested']['auto_adjust'] is False and meta['requested']['repair'] is False, 'ACQUISITION_OPTIONS')
        norm=json.loads((cache/(ticker+'_split_normalized_candidate.json')).read_bytes())
        nonvalidated_basis += sum(v['source_reference']['price_basis_status']!='VALIDATED' for v in norm)
        max_price_delta=0;max_return_delta=0
        for x,y in zip(a,b):
            check(x['Date']==y['Date'], 'AUTO_ADJUST_EXACT_SESSION_JOIN')
            day=x['Date'][:10];key=(day,ticker);check(key not in raw,'DUPLICATE_YAHOO')
            raw[key]=x
            values=[float(x[k]) for k in ('Open','High','Low','Close','Volume')]
            check(all(math.isfinite(v) for v in values), 'NONFINITE_RAW')
            o,h,l,c,v=values
            if not (o>0 and c>0 and v>0 and l<=min(o,c)+quality['numeric_tolerance'] and h+quality['numeric_tolerance']>=max(o,c) and h>=l):
                invalid.append(key)
            factor=float(x['Adj Close'])/c
            for field in ('Open','High','Low','Close'):
                delta=abs(float(y[field])-float(x[field])*factor)
                max_price_delta=max(max_price_delta,delta)
                check(delta<=quality['numeric_tolerance'],'AUTO_ADJUST_VALUE_MISMATCH')
            check(float(y['Volume'])==v,'AUTO_ADJUST_VOLUME')
            max_return_delta=max(max_return_delta,abs(float(y['Close'])/float(y['Open'])-c/o))
            if float(x['Stock Splits']):
                actions.append(dict(ticker=ticker,session=day,ratio=float(x['Stock Splits'])))
        adjustment_checks.append(dict(ticker=ticker,rows=len(a),same_factor_checks='PASS',max_price_delta=max_price_delta,
            max_return_delta=max_return_delta,scope='Same cached input local transformation only; not an independent vintage guarantee'))
    manifest=json.loads((root/'data/outcome_input/manual_jpx/retention_manifest.json').read_bytes())
    jpx={};jpx_admitted=0;jpx_unknown_action=0
    for e in manifest['entries']:
        if e['session_date']>=d['target_session']:continue
        p=json.loads((root/e['normalized_output_path']).read_bytes())
        check(sha(root/e['normalized_output_path'])==e['normalized_output_sha256'],'JPX_MANIFEST_HASH')
        for r in p['records']:
            key=(r['session_date'],r['ticker']);check(key not in jpx,'DUPLICATE_JPX');jpx[key]=r
            jpx_admitted+=bool(p['OUTCOME_SCORING_SOURCE_VALIDATED'])
            jpx_unknown_action+=r['corporate_action_status']=='UNKNOWN'
            check(r['open']==r['morning']['open'] and r['close']==r['afternoon']['close'],'JPX_AM_PM_JOIN')
    overlap=sorted(set(raw)&set(jpx))
    comparisons={(r['target_session'],r['ticker']):r for r in detail['overlap']}
    check(set(comparisons)==set(overlap),'OVERLAP_GRAIN')
    for key in overlap:
        a,b=raw[key],jpx[key]; saved=comparisons[key]
        delta={k:float(a[k.title()])-b[k] for k in ('open','high','low','close')}
        if saved['sources'][0]!='YAHOO_FINANCE_YFINANCE':delta={k:-v for k,v in delta.items()}
        check(delta==saved['price_differences'],'OVERLAP_DELTAS')
        check(saved['status']==('MATCH_WITHIN_TOLERANCE' if all(abs(v)<=rules['overlap_price_absolute_tolerance'] for v in delta.values()) else 'OUTCOME_CONFLICT'),'OVERLAP_DECISION')
    check(detail['outcome_candidates']==len(raw)+len(jpx),'OUTCOME_CANDIDATE_COUNT')
    check(detail['selected_outcomes']==len(set(raw)|set(jpx)),'OUTCOME_JOIN_COUNT')
    # This current inventory has explicit unresolved source flags; zero admission
    # must be explained by actual evidence rather than a hard-coded adapter STOP.
    check(nonvalidated_basis==len(raw),'CURRENT_CACHE_BASIS_AUDIT_CHANGED')
    check(jpx_admitted==0 and jpx_unknown_action==len(jpx),'CURRENT_JPX_ADMISSION_CHANGED')
    check(not d['eligible_rows'] and not d['fold_definitions'],'UNSAFE_CURRENT_HISTORY_ADMITTED')
    known=next(r for r in all_rows if r['ticker']=='8306' and r['target_session']=='2022-05-17')
    check('SKIPPED_OUTCOME_INTEGRITY' in known['reasons'],'MUFG_QUARANTINE_REMOVED')
    check(invalid==[('2022-05-17','8306')],'QUALITY_QUARANTINE_CHANGED')
    result.update(independent_validation='PASS_WITH_HISTORY_BLOCKED',
        admission_counts={s:counts[s] for s in ('ADMITTED_PIT','UNKNOWN_PIT','REJECTED_PIT')},
        matrix_rows=matrix_rows,matrix_sha256=sha(matrix),matrix_duplicates=0,
        original_yahoo_rows=len(raw),original_jpx_rows=len(jpx),jpx_nonoverlap_rows=len(set(jpx)-set(raw)),
        overlap_rows=len(overlap),overlap_all_ohlc_exact=all(all(v==0 for v in comparisons[k]['price_differences'].values()) for k in overlap),
        jpx_legacy_source_admitted_rows=jpx_admitted,jpx_unknown_action_rows=jpx_unknown_action,
        yfinance_unvalidated_basis_rows=nonvalidated_basis,adjustment_checks=adjustment_checks,
        local_split_events=actions,quality_quarantine_keys=invalid,
        proof_verification='Algebra plus actual implementation unit tests; provider revision restrictions remain UNKNOWN',
        fold_validation='PASS_EMPTY_BLOCKED; no synthetic folds or real model fit',
        limitations=['No original historical publication/vintage evidence',
                    'Corporate-action list is not a complete merger/listing/reverse-split audit',
                    'No claim of terms resolution or next-session Fresh availability'])
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset',type=Path)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=audit(args.dataset,args.root)
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({k:result[k] for k in ('independent_validation','candidate_rows','admission_counts','matrix_rows','original_yahoo_rows','original_jpx_rows','overlap_rows','fold_count')},ensure_ascii=False))


if __name__=='__main__':main()
