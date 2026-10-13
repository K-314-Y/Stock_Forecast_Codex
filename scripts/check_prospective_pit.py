"""Independent Data Analytics check: stdlib only, no engine imports or acquisition."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path

PRE = set('target_session ticker feature_session feature_set_version features retrieved_at feature_available_at availability_status source_identity source_hashes PIT_validation common_snapshot_hash forecast_bundle_id code_commit'.split())
FEATURES = set('ret_1d ret_5d momentum_20d momentum_60d realized_vol_20d MA20 MA60 MA_state threshold'.split())


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def audit(path, outcome_path=None):
    path=Path(path); value=json.loads(path.read_bytes())
    rows=value['rows']; keys=Counter((r['target_session'],r['ticker']) for r in rows)
    duplicate=sum(n-1 for n in keys.values())
    times=fields=provenance=0
    for r in rows:
        fields += int(set(r)!=PRE or set(r['features'])!=FEATURES)
        retrieved=datetime.fromisoformat(r['retrieved_at']); available=datetime.fromisoformat(r['feature_available_at'])
        start=datetime.fromisoformat(r['target_session']+'T00:00:00+09:00')
        cutoff=datetime.fromisoformat(r['target_session']+'T08:45:00+09:00')
        times += int(retrieved.tzinfo is None or available.tzinfo is None or not start<=retrieved<=available<cutoff
                     or r['availability_status']!='OBSERVED' or r['feature_session']>=r['target_session'])
        provenance += int(r['PIT_validation']!='PASS' or r['source_identity']['provider']!='BIGDATA'
            or r['source_identity']['underlying_provider']!='FMP' or 'ephemeral_response' not in r['source_hashes']
            or any(len(h)!=64 or any(c not in '0123456789abcdef' for c in h) for h in r['source_hashes'].values()))
    hash_ok=value['ledger_sha256']==digest({k:v for k,v in value.items() if k!='ledger_sha256'})
    sidecar_ok=path.with_suffix('.sha256').read_text().strip()==hashlib.sha256(path.read_bytes()).hexdigest()
    join_errors=0
    joined_rows=None
    if outcome_path is not None:
        outcome_path=Path(outcome_path); outcomes=json.loads(outcome_path.read_bytes())
        out_keys=Counter((r['target_session'],r['ticker']) for r in outcomes['rows'])
        join_errors += int(set(out_keys)!=set(keys)) + sum(n-1 for n in out_keys.values())
        for r in outcomes['rows']:
            observed=datetime.fromisoformat(r['outcome_available_at'])
            close=datetime.fromisoformat(r['target_session']+'T15:30:00+09:00')
            join_errors += int(r['availability_status']!='OBSERVED' or observed<close
                or r['outcome_retrieved_at']!=r['outcome_available_at']
                or r['return_oc']!=r['Close']/r['Open']-1)
        join_errors += int(outcomes['ledger_sha256']!=digest({k:v for k,v in outcomes.items() if k!='ledger_sha256'}))
        joined_rows=len(set(keys)&set(out_keys))
    failures=duplicate+times+fields+provenance+int(not hash_ok)+int(not sidecar_ok)+join_errors
    return dict(assessment='Needs revision' if failures else 'Ready within reviewed scope',
        workflow=['validate-data','analyze-data-quality'],grain='target_session × ticker',rows=len(rows),
        duplicate_keys=duplicate,pit_time_errors=times,restricted_content_fields=fields,
        source_provenance_errors=provenance,deterministic_hash=hash_ok,sidecar_integrity=sidecar_ok,
        join_errors=join_errors,joined_rows=joined_rows,
        source=str(path),source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        limitations='Observed clock origin is checked separately by producer tests. No historical availability inferred; no ML source admission granted.')


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('pre_session'); parser.add_argument('--outcomes'); parser.add_argument('--output',required=True)
    args=parser.parse_args(); result=audit(args.pre_session,args.outcomes)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return int(result['assessment']=='Needs revision')


if __name__=='__main__': raise SystemExit(main())
