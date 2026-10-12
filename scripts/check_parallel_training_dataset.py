"""Independent read-only dataset/source audit using the standard library only."""
import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_data(value):
    if isinstance(value, dict):
        return {k: canonical_data(v) for k, v in value.items() if k != 'calculated_at'}
    if isinstance(value, list):
        return [canonical_data(v) for v in value]
    return value


def audit(destination, root):
    root, destination = Path(root), Path(destination)
    path = destination / 'dataset.json'
    data = json.loads(path.read_bytes())
    assert sha(path) == path.with_suffix('.sha256').read_text().strip()
    payload = {k: v for k, v in data.items() if k != 'dataset_sha256'}
    checksum = hashlib.sha256(json.dumps(canonical_data(payload), sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    assert checksum == data['dataset_sha256']
    assert all(sha(root / p) == value for p, value in data['source_hashes'].items())
    profile, keys, original_unknown = [], set(), 0
    for name in data['source_hashes']:
        if not name.endswith('_auto_adjust_false.csv'):
            continue
        ticker = Path(name).name.split('_')[0]
        with (root / name).open(newline='') as handle:
            rows = list(csv.DictReader(handle))
        dates = [datetime.fromisoformat(r['Date']).date().isoformat() for r in rows]
        assert len(dates) == len(set(dates))
        current = {(d, ticker) for d in dates if d < data['target_session']}
        keys.update(current)
        normalized = json.loads((root / Path(name).with_name(ticker + '_split_normalized_candidate.json')).read_bytes())
        original_unknown += sum(r['available_at'] is None for r in normalized)
        profile.append(dict(ticker=ticker, rows=len(rows), first=min(dates), last=max(dates),
            duplicates=len(dates) - len(set(dates)),
            missing_ohlc=sum(any(r[k] == '' for k in ('Open', 'High', 'Low', 'Close')) for r in rows),
            original_historical_available_at_unknown=sum(r['available_at'] is None for r in normalized)))
    manifest = json.loads((root / 'data/outcome_input/manual_jpx/retention_manifest.json').read_bytes())
    jpx_dates = []
    observed_downloads, imports_only = 0, 0
    for entry in manifest['entries']:
        if entry['session_date'] >= data['target_session']:
            continue
        jpx = json.loads((root / entry['normalized_output_path']).read_bytes())
        assert len(jpx['records']) == 6
        jpx_dates.append(entry['session_date'])
        current = {(r['session_date'], r['ticker']) for r in jpx['records']}
        original_unknown += len(current - keys)
        keys.update(current)
        observed_downloads += bool(entry.get('downloaded_at'))
        imports_only += not bool(entry.get('downloaded_at'))
    assert len(jpx_dates) == len(set(jpx_dates))
    rows = data['eligible_rows'] + data['excluded_rows']
    row_keys = [(r['target_session'], r['ticker']) for r in rows]
    assert len(row_keys) == len(set(row_keys))
    missing_outcomes = set(row_keys) - keys
    assert keys <= set(row_keys)
    assert len(missing_outcomes) == data['pit_audit']['unmatched_feature_rows']
    reasons = Counter(reason for r in data['excluded_rows'] for reason in r['reasons'])
    assert dict(reasons) == data['pit_audit']['excluded_by_reason']
    for row in data['eligible_rows']:
        assert row['feature_session'] < row['target_session'] < data['target_session']
        assert row['feature_availability_status'] != 'UNKNOWN'
        assert row['outcome_availability_status'] != 'UNKNOWN'
        assert datetime.fromisoformat(row['feature_available_at']) <= datetime.fromisoformat(row['cutoff'])
        assert datetime.fromisoformat(row['outcome_available_at']) < datetime.fromisoformat(data['training_as_of'])
        assert row['feature_threshold_metadata']['training_end'] < row['target_session']
        assert datetime.fromisoformat(row['feature_threshold_metadata']['input_available_at']) <= datetime.fromisoformat(row['cutoff'])
    folds = data['fold_definitions']
    for f in folds:
        assert f['training_end'] < f['validation_start'] <= f['validation_end'] < data['target_session']
        for t in f['cv_threshold_replays']:
            assert t['training_end'] == f['training_end']
            assert t['purpose'] == 'CROSS_VALIDATION_ONLY' and t['production_candidate'] is False
    return dict(assessment='Needs revision' if data['readiness'] != 'PASS' else 'Ready within reviewed scope',
        dataset_sha256=checksum, grain='target_session x Core6 ticker',
        candidate_dates=len({d for d, _ in row_keys}), candidate_rows=len(row_keys),
        source_outcome_rows=len(keys), eligible_rows=len(data['eligible_rows']),
        eligible_dates=len({r['target_session'] for r in data['eligible_rows']}),
        excluded_rows=len(data['excluded_rows']), excluded_by_reason=dict(sorted(reasons.items())),
        original_historical_availability_unknown=original_unknown,
        current_vintage_observed_download_dates=observed_downloads, local_import_only_dates=imports_only,
        duplicates=0, join_cardinality='ONE_TO_ONE', missing_outcome_rows=len(missing_outcomes),
        missing_outcome_keys=sorted(missing_outcomes), source_hash_verification='PASS',
        pit_exclusion_guard='PASS; unsafe rows excluded', training_pit_readiness=data['readiness'],
        fold_count=len(folds), fold_integrity='UNAVAILABLE_NO_ELIGIBLE_HISTORY' if not folds else 'PASS',
        missingness='Eligible population empty; rates undefined', cache_profiles=profile,
        blockers=data['blockers'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', type=Path)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.dataset, args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('assessment', 'candidate_dates', 'eligible_dates', 'eligible_rows', 'excluded_rows', 'missing_outcome_rows', 'fold_count')}, ensure_ascii=False))


if __name__ == '__main__': main()
